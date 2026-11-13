#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 central-audio-bluetooth-android-linux contributors
"""Own one scrcpy -> WAV FIFO -> FFmpeg -> explicit PulseAudio sink session."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

BUS = "tablet_mic_bus"
SOURCE = "tablet_mic"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "central-gtk2-audio"
STATE = STATE_DIR / "session.json"


class SessionError(RuntimeError):
    pass


def run(args, timeout=8):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, errors="replace", timeout=timeout,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionError(f"Não foi possível executar {Path(args[0]).name}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise SessionError(f"{Path(args[0]).name}: {detail or 'comando recusado'}")
    return result.stdout.strip()


def pa(*args):
    return run(["pactl", *args])


def short(kind):
    text = pa("list", "short", kind)
    return [line.split("\t") for line in text.splitlines() if line]


def guard():
    pa("info")
    if any(len(row) >= 3 and row[2].isdigit() for row in short("source-outputs")):
        raise SessionError("Há captura ou medidor ativo. A conexão foi preservada; encerre a captura antes desta ação.")


def requirements(scrcpy=None):
    path = shutil.which(scrcpy or "scrcpy")
    if not path:
        raise SessionError("scrcpy externo não encontrado. Instale uma versão oficial >= 2.3 ou informe o executável.")
    version_text = run([path, "--version"])
    match = re.search(r"\bscrcpy\s+v?(\d+)\.(\d+)(?:\.(\d+))?", version_text)
    if not match or tuple(int(x or 0) for x in match.groups()) < (2, 3, 0):
        raise SessionError("É necessário scrcpy >= 2.3 para microfone com gravação PCM/WAV.")
    help_text = run([path, "--help"])
    for option in ("--audio-source", "--no-video", "--no-control", "--no-audio-playback",
                   "--require-audio", "--audio-codec", "--record-format"):
        if option not in help_text:
            raise SessionError(f"O scrcpy informado não anuncia a opção necessária {option}.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SessionError("FFmpeg externo não encontrado. Esta função precisa da saída PulseAudio do FFmpeg.")
    pulse = run([ffmpeg, "-hide_banner", "-h", "muxer=pulse"])
    if "pulse" not in pulse.lower() or "not found" in pulse.lower() or "unknown" in pulse.lower():
        raise SessionError("Este FFmpeg não oferece o muxer PulseAudio necessário.")
    return path, ffmpeg, "--no-window" in help_text, match.group(0)


def browser_status():
    rows = []
    for name in ("chromium", "chromium-browser", "google-chrome", "firefox-esr", "firefox"):
        path = shutil.which(name)
        if path:
            try:
                rows.append(f"{name}: {run([path, '--version'], 5).splitlines()[0]}")
            except SessionError:
                rows.append(f"{name}: encontrado; versão indisponível")
    return rows or ["Navegador externo não detectado. A central não instala nem duplica navegadores."]


@contextlib.contextmanager
def locked():
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE_DIR / "lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def load_state():
    if not STATE.exists():
        return None
    try:
        data = json.loads(STATE.read_text())
        if data["version"] != 1 or not re.fullmatch(r"[0-9a-f]{32}", data["token"]):
            raise ValueError("versão ou identidade")
        runtime = Path(data["runtime"])
        if runtime.parent != STATE_DIR or not runtime.name.startswith("session-") or runtime.is_symlink():
            raise ValueError("diretório")
        if (runtime / "owner").read_text().strip() != data["token"]:
            raise ValueError("propriedade")
        for process in data["processes"]:
            if not isinstance(process["pid"], int) or process["pid"] <= 1 or not str(process["start"]).isdigit():
                raise ValueError("processo")
        for module in data["modules"]:
            if not isinstance(module["id"], int) or module["kind"] not in ("module-null-sink", "module-remap-source"):
                raise ValueError("módulo")
        if any(kind not in ("module-null-sink", "module-remap-source") for kind in data.get("pending_modules", [])):
            raise ValueError("intenção de módulo")
        return data
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise SessionError("Estado de sessão inválido; arquivo e recursos foram preservados.") from exc


def save_state(data):
    handle, name = tempfile.mkstemp(prefix="session-state-", dir=STATE_DIR)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def identity(pid):
    try:
        path = Path("/proc") / str(pid)
        if path.stat().st_uid != os.getuid():
            return None
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def record_process(process, role):
    stamp = identity(process.pid)
    if stamp is None:
        raise SessionError(f"{role} encerrou antes da confirmação de identidade.")
    return {"pid": process.pid, "start": stamp, "role": role}


def stop_process(record):
    pid = record["pid"]
    stamp = identity(pid)
    if stamp is None:
        return
    if stamp != record["start"] or os.getpgid(pid) != pid:
        raise SessionError("A identidade de um processo mudou; ele não foi encerrado.")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while identity(pid) == stamp and time.monotonic() < deadline:
        time.sleep(0.05)
    if identity(pid) == stamp:
        os.killpg(pid, signal.SIGKILL)


def modules():
    return {int(row[0]): row for row in short("modules") if len(row) >= 3 and row[0].isdigit()}


def module_owner(arguments):
    owners = []
    for argument in shlex.split(arguments):
        if argument.startswith(("sink_properties=", "source_properties=")):
            for prop in shlex.split(argument.split("=", 1)[1]):
                if prop.startswith("central.audio.owner="):
                    owners.append(prop.split("=", 1)[1])
    return owners[0] if len(owners) == 1 else None


def bus_index():
    matches = [row[0] for row in short("sinks") if len(row) >= 2 and row[1] == BUS]
    return matches[0] if len(matches) == 1 else None


def bus_has_stream():
    index = bus_index()
    return index is not None and any(len(row) >= 2 and row[1] == index for row in short("sink-inputs"))


def reconcile_modules(data):
    pending = data.get("pending_modules", [])
    if not pending:
        return
    known = {module["id"] for module in data["modules"]}
    for module_id, row in modules().items():
        if row[1] in pending and module_owner(row[2]) == data["token"] and module_id not in known:
            data["modules"].append({"id": module_id, "kind": row[1]})
    data["pending_modules"] = []
    save_state(data)


def load_owned_module(data, kind, *arguments):
    data.setdefault("pending_modules", []).append(kind)
    save_state(data)
    try:
        module_id = pa("load-module", kind, *arguments)
    except SessionError:
        reconcile_modules(data)
        raise
    if not module_id.isdigit():
        reconcile_modules(data)
        raise SessionError("Resposta de criação sem ID válido; recursos com a identidade própria foram rastreados para limpeza.")
    data["modules"].append({"id": int(module_id), "kind": kind})
    data["pending_modules"].remove(kind)
    save_state(data)


def ensure_modules(data):
    sink_exists = bus_index() is not None
    source_exists = any(len(row) >= 2 and row[1] == SOURCE for row in short("sources"))
    current = modules()
    if source_exists and not sink_exists:
        raise SessionError("Há uma fonte tablet_mic sem o barramento esperado; nada foi assumido.")
    if source_exists:
        matching = [row for row in current.values() if row[1] == "module-remap-source"
                    and f"master={BUS}.monitor" in shlex.split(row[2])
                    and f"source_name={SOURCE}" in shlex.split(row[2])]
        if len(matching) != 1:
            raise SessionError("A fonte existente não tem a ligação esperada; recursos externos preservados.")
    if sink_exists and bus_has_stream():
        raise SessionError("O barramento já recebe outro fluxo. A central não assume processos externos.")
    if not sink_exists:
        properties = f'sink_properties="device.description=Tablet_Mic_Bus central.audio.owner={data["token"]}"'
        load_owned_module(data, "module-null-sink", f"sink_name={BUS}", properties)
    if not source_exists:
        properties = f'source_properties="device.description=Microfone_Tablet central.audio.owner={data["token"]}"'
        load_owned_module(data, "module-remap-source", f"master={BUS}.monitor", f"source_name={SOURCE}", properties)


def cleanup(data):
    errors = []
    remaining = []
    for process in reversed(data["processes"]):
        try:
            stop_process(process)
        except (SessionError, OSError) as exc:
            errors.append(str(exc))
            remaining.append(process)
    data["processes"] = list(reversed(remaining))
    save_state(data)
    if data["processes"]:
        raise SessionError("; ".join(errors))
    reconcile_modules(data)
    if any(module["kind"] == "module-null-sink" for module in data["modules"]):
        deadline = time.monotonic() + 1
        while bus_has_stream() and time.monotonic() < deadline:
            time.sleep(0.05)
        if bus_has_stream():
            raise SessionError("Outro fluxo ainda usa o barramento; módulos e estado preservados.")
    current = modules()
    remaining_modules = []
    parent_blocked = False
    for module in reversed(data["modules"]):
        if parent_blocked:
            remaining_modules.append(module)
            continue
        row = current.get(module["id"])
        if row is None:
            continue
        if row[1] != module["kind"] or module_owner(row[2]) != data["token"]:
            errors.append("Identidade de módulo diferente; recurso preservado.")
            remaining_modules.append(module)
            parent_blocked = True
            continue
        try:
            pa("unload-module", str(module["id"]))
        except SessionError as exc:
            errors.append(str(exc))
            remaining_modules.append(module)
            parent_blocked = True
    data["modules"] = list(reversed(remaining_modules))
    save_state(data)
    if errors or data["modules"]:
        raise SessionError("; ".join(errors) or "Limpeza de módulos pendente.")
    runtime = Path(data["runtime"])
    if (runtime / "owner").read_text().strip() != data["token"]:
        raise SessionError("Diretório de sessão sem identidade válida; preservado.")
    STATE.unlink()
    shutil.rmtree(runtime)


def own_stream_ready(pid):
    index = bus_index()
    entries = json.loads(pa("-f", "json", "list", "sink-inputs"))
    return any(str(entry.get("sink")) == index
               and str(entry.get("properties", {}).get("application.process.id")) == str(pid)
               for entry in entries)


def start(serial, scrcpy=None):
    if not serial or len(serial) > 256 or any(char in serial for char in ("\n", "\r", "\x00")):
        raise SessionError("Informe o serial ADB autorizado ou endereço de conexão já configurado.")
    path, ffmpeg, no_window, _ = requirements(scrcpy)
    guard()
    previous = load_state()
    if previous:
        raise SessionError("Já existe uma sessão própria ou limpeza pendente. Use Reconectar/Desconectar.")
    runtime = Path(tempfile.mkdtemp(prefix="session-", dir=STATE_DIR))
    token = uuid.uuid4().hex
    (runtime / "owner").write_text(token + "\n")
    fifo = runtime / "microphone.wav"
    os.mkfifo(fifo, 0o600)
    data = {"version": 1, "token": token, "runtime": str(runtime), "serial": serial,
            "scrcpy": path, "processes": [], "modules": [], "pending_modules": []}
    save_state(data)
    processes = []
    try:
        ensure_modules(data)
        log = (runtime / "session.log").open("ab", buffering=0)
        env = {**os.environ, "LC_ALL": "C"}
        consumer = subprocess.Popen([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-probesize", "32", "-analyzeduration", "0", "-i", str(fifo),
            "-map", "0:a:0", "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            "-f", "pulse", "-device", BUS, "-buffer_duration", "100", "CentralAudioMicrophone"
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
           env=env, start_new_session=True)
        processes.append(consumer)
        data["processes"].append(record_process(consumer, "ffmpeg"))
        save_state(data)
        args = [path, f"--serial={serial}", "--audio-source=mic", "--no-video", "--no-control",
                "--no-audio-playback", "--require-audio", "--audio-codec=raw",
                "--record-format=wav", f"--record={fifo}"]
        if no_window:
            args.append("--no-window")
        producer = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    env=env, start_new_session=True)
        processes.append(producer)
        data["processes"].append(record_process(producer, "scrcpy"))
        save_state(data)
        log.close()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                detail = (runtime / "session.log").read_text(errors="replace")[-2000:]
                raise SessionError("Captura não iniciou. " + detail)
            if own_stream_ready(consumer.pid):
                print("Fluxo próprio do FFmpeg confirmado no barramento do tablet.")
                print("Selecione Microfone_Tablet no aplicativo de chamada. Voz e latência precisam de teste humano.")
                return
            time.sleep(0.15)
        raise SessionError("Tempo esgotado sem confirmar o fluxo próprio no barramento.")
    except (SessionError, OSError, ValueError) as exc:
        try:
            guard()
            cleanup(data)
        except (SessionError, OSError) as cleanup_error:
            raise SessionError(f"{exc}\nLimpeza pendente: {cleanup_error}") from exc
        finally:
            for process in processes:
                if process.poll() is not None:
                    process.wait()
        raise SessionError(str(exc)) from exc


def stop():
    data = load_state()
    if not data:
        print("Nenhuma sessão própria para encerrar.")
        return
    guard()
    cleanup(data)
    print("Processos próprios encerrados; apenas módulos criados por esta sessão foram removidos.")


def privacy_pause():
    data = load_state()
    if not data:
        print("Nenhuma captura própria para pausar.")
        return
    data["privacy_latched"] = True
    save_state(data)
    remaining = []
    errors = []
    for process in reversed(data["processes"]):
        try:
            stop_process(process)
        except (SessionError, OSError) as exc:
            remaining.append(process)
            errors.append(str(exc))
    data["processes"] = list(reversed(remaining))
    save_state(data)
    if errors:
        raise SessionError("Pausa de privacidade incompleta: " + "; ".join(errors))
    print("Captura própria pausada. Fontes e rotas preservadas; nenhum mute foi removido.")


def observe_route(pid):
    try:
        sinks = run(["pactl", "list", "short", "sinks"], timeout=1)
        index = next((row.split("\t")[0] for row in sinks.splitlines()
                      if len(row.split("\t")) >= 2 and row.split("\t")[1] == BUS), None)
        entries = json.loads(run(["pactl", "-f", "json", "list", "sink-inputs"], timeout=1))
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or
                not isinstance(entry.get("properties", {}), dict) for entry in entries):
            return "unknown"
        sources = run(["pactl", "list", "short", "sources"], timeout=1)
        source_exists = any(len(row.split("\t")) >= 2 and row.split("\t")[1] == SOURCE
                            for row in sources.splitlines())
        ready = source_exists and any(str(entry.get("sink")) == index and
                str(entry.get("properties", {}).get("application.process.id")) == str(pid)
                for entry in entries)
        return "ready" if ready else "missing"
    except (SessionError, ValueError, TypeError, AttributeError):
        return "unknown"


def observe():
    data = load_state()
    if not data:
        status()
        return
    observation = {process["role"]: identity(process["pid"]) == process["start"]
                   for process in data["processes"]}
    observation["privacy_latched"] = bool(data.get("privacy_latched"))
    ffmpeg = next((process for process in data["processes"] if process["role"] == "ffmpeg"), None)
    if observation["privacy_latched"]:
        observation["route"] = "paused"
    elif ffmpeg and observation.get("ffmpeg"):
        observation["route"] = observe_route(ffmpeg["pid"])
    else:
        observation["route"] = "not_running"
    if observation != data.get("last_observed"):
        event = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "processes": observation, "automatic_restart": False,
                 "call_mute": "unknown"}
        with (Path(data["runtime"]) / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        data["last_observed"] = observation
        save_state(data)
    status()


def status():
    data = load_state()
    if not data:
        print("Nenhuma sessão de captura gerenciada pela central.")
        return
    live = sum(identity(process["pid"]) == process["start"] for process in data["processes"])
    print(f"Processos próprios ativos: {live}/{len(data['processes'])}.")
    if data.get("privacy_latched"):
        print("Privacidade: pausa solicitada." if live else "Privacidade: captura pausada.")
    else:
        print("Estado: " + ("em execução; sinal de voz ainda requer teste." if live == 2 else "captura interrompida; causa ainda precisa de diagnóstico."))
    route = data.get("last_observed", {}).get("route")
    if route:
        labels = {"ready": "ligação própria observada", "missing": "ligação própria não encontrada",
                  "unknown": "consulta indisponível", "paused": "pausada por privacidade",
                  "not_running": "produtor interrompido"}
        print("Rota: " + labels.get(route, "estado desconhecido") + ".")
    print("Reinício automático bloqueado: mute da chamada desconhecido.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "status", "observe", "start", "stop", "reconnect", "privacy-pause"))
    parser.add_argument("--serial", help="Serial ADB já autorizado; não executa pareamento.")
    parser.add_argument("--scrcpy", help="Executável externo do scrcpy; padrão: PATH.")
    args = parser.parse_args()
    try:
        if args.action == "check":
            try:
                _, _, _, version = requirements(args.scrcpy)
                print(f"{version}: requisitos de captura detectados (FFmpeg com PulseAudio).")
            except SessionError as exc:
                print(str(exc))
            print("\n".join(browser_status()))
        elif args.action == "status":
            status()
        elif args.action == "observe" and not STATE.exists():
            status()
        else:
            with locked():
                if args.action == "start":
                    start(args.serial, args.scrcpy)
                elif args.action == "stop":
                    stop()
                elif args.action == "privacy-pause":
                    privacy_pause()
                elif args.action == "observe":
                    observe()
                else:
                    data = load_state()
                    if data and data.get("privacy_latched"):
                        raise SessionError("Pausa de privacidade ativa. Encerre a sessão fora da captura e use Conectar explicitamente; não haverá retomada automática.")
                    serial = args.serial or (data["serial"] if data else None)
                    path = args.scrcpy or (data["scrcpy"] if data else None)
                    requirements(path)
                    guard()
                    stop()
                    start(serial, path)
    except (SessionError, OSError, ValueError, KeyError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
