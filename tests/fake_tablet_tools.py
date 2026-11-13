#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Strict fake scrcpy/FFmpeg/pactl; no real device or audio API is used."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import struct
import sys
import time


BUS = "tablet_mic_bus"
SOURCE = "tablet_mic"
OPTIONS = ("--audio-source", "--no-video", "--no-control", "--no-audio-playback",
           "--require-audio", "--audio-codec", "--record-format", "--no-window")
ROOT = Path(os.environ["FAKE_TABLET_ROOT"]).resolve()
STATE = ROOT / "server.json"
LOG = ROOT / "tools.jsonl"
TOOL = os.environ["FAKE_TABLET_TOOL"]


class ProtocolError(Exception):
    pass


@contextlib.contextmanager
def locked():
    with (ROOT / "server.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def update(callback):
    with locked():
        state = json.loads(STATE.read_text())
        result = callback(state)
        STATE.write_text(json.dumps(state))
        return result


def event(phase, **fields):
    with locked():
        with LOG.open("a") as handle:
            handle.write(json.dumps({"tool": TOOL, "argv": sys.argv[1:], "pid": os.getpid(),
                                     "phase": phase, **fields}) + "\n")


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def pactl(argv):
    def command(state):
        for rule in state.get("failures", []):
            if rule["argv"] == argv and rule.get("remaining", 1):
                if rule.get("skip", 0):
                    rule["skip"] -= 1
                    continue
                rule["remaining"] = rule.get("remaining", 1) - 1
                return 1, "", "Injected fake pactl failure"
        if argv == ["info"]:
            return 0, "Server Name: fake tablet server\n", ""
        if len(argv) == 3 and argv[:2] == ["list", "short"]:
            kind = argv[2]
            require(kind in {"sources", "sinks", "modules", "source-outputs", "sink-inputs"}, "unsupported list type")
            if kind == "modules":
                rows = [[m["id"], m["kind"], " ".join(m["args"]), "0"] for m in state["modules"]]
            elif kind in {"sinks", "sources"}:
                rows = [[item["index"], name, "fake-driver", "s16le 2ch 48000Hz", "RUNNING"]
                        for name, item in state[kind].items()]
            elif kind == "source-outputs":
                rows = state["source_outputs"]
            else:
                rows = [[entry["index"], entry["sink"], "99", "fake-driver", "s16le 2ch 48000Hz"]
                        for entry in state["streams"]]
            return 0, "".join("\t".join(map(str, row)) + "\n" for row in rows), ""
        if argv == ["-f", "json", "list", "sink-inputs"]:
            entries = [{"index": row["index"], "sink": row["sink"],
                        "properties": {"application.process.id": str(row["reported_pid"])}}
                       for row in state["streams"]]
            return 0, json.dumps(entries), ""
        if len(argv) >= 2 and argv[0] == "load-module":
            kind = argv[1]
            if kind == "module-null-sink":
                require(len(argv) == 4 and argv[2] == f"sink_name={BUS}", "unexpected null sink args")
                require(re.fullmatch(r'sink_properties="device.description=Tablet_Mic_Bus central.audio.owner=[0-9a-f]{32}"', argv[3]) is not None,
                        "null sink missing exact owner token")
                require(BUS not in state["sinks"], "attempt to replace existing bus")
            elif kind == "module-remap-source":
                require(len(argv) == 5 and argv[2:4] == [f"master={BUS}.monitor", f"source_name={SOURCE}"],
                        "unexpected remap source args")
                require(re.fullmatch(r'source_properties="device.description=Microfone_Tablet central.audio.owner=[0-9a-f]{32}"', argv[4]) is not None,
                        "remap source missing exact owner token")
                require(SOURCE not in state["sources"], "attempt to replace existing tablet source")
            else:
                raise ProtocolError("unsupported load-module")
            module_id = state["next_module"]
            state["next_module"] += 1
            state["modules"].append({"id": module_id, "kind": kind, "args": argv[2:]})
            if kind == "module-null-sink":
                state["sinks"][BUS] = {"index": 30, "owner_module": module_id}
                state["sources"][BUS + ".monitor"] = {"index": 31, "owner_module": module_id}
            else:
                state["sources"][SOURCE] = {"index": 32, "owner_module": module_id}
            result = "invalid-module-id" if state["config"].get("bad_module_id_for") == kind else str(module_id)
            return 0, result + "\n", ""
        if len(argv) == 2 and argv[0] == "unload-module" and argv[1].isdigit():
            module_id = int(argv[1])
            require(any(module["id"] == module_id for module in state["modules"]), "unload of unknown module")
            state["modules"] = [module for module in state["modules"] if module["id"] != module_id]
            for kind in ("sinks", "sources"):
                state[kind] = {name: item for name, item in state[kind].items() if item.get("owner_module") != module_id}
            return 0, "", ""
        raise ProtocolError(f"forbidden or unsupported pactl argv: {argv!r}")

    return update(command)


def stop_signal(_number, _frame):
    raise SystemExit(0)


def scrcpy(argv):
    config = update(lambda state: state["config"].copy())
    if argv == ["--version"]:
        return 0, f"scrcpy {config.get('scrcpy_version', '3.3.1')}\n", ""
    if argv == ["--help"]:
        return 0, "\n".join(option for option in OPTIONS if option not in config.get("missing_options", [])), ""
    required = {"--audio-source=mic", "--no-video", "--no-control", "--no-audio-playback",
                "--require-audio", "--audio-codec=raw", "--record-format=wav"}
    require(required.issubset(argv), "producer requires microphone, PCM/WAV and no local audio playback")
    serials = [value for value in argv if value.startswith("--serial=")]
    records = [value for value in argv if value.startswith("--record=")]
    require(len(serials) == len(records) == 1 and bool(serials[0].split("=", 1)[1]), "producer requires exactly one serial and FIFO")
    require(set(argv) == required | set(serials) | set(records) | ({"--no-window"} if "--no-window" in argv else set()),
            "unexpected scrcpy option")
    fifo = Path(records[0].split("=", 1)[1])
    require(fifo.is_fifo(), "producer output must be a FIFO")
    signal.signal(signal.SIGTERM, stop_signal)
    if config.get("producer_exit"):
        return 5, "", "Injected producer failure"
    # Streaming WAV header and silent PCM data: generated bytes, never capture.
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ",
                         16, 1, 2, 48000, 192000, 4, 16, b"data", 0xFFFFFFFF)
    try:
        with fifo.open("wb", buffering=0) as stream:
            stream.write(header)
            while True:
                stream.write(b"\x00" * 1920)
                time.sleep(0.01)
    except BrokenPipeError:
        return 0, "", ""


def ffmpeg(argv):
    config = update(lambda state: state["config"].copy())
    if argv == ["-hide_banner", "-h", "muxer=pulse"]:
        return 0, ("Muxer pulse [PulseAudio output]:\n" if config.get("pulse", True) else "Unknown format 'pulse'.\n"), ""
    require("-i" in argv and len(argv) > argv.index("-i") + 1, "consumer missing FIFO input")
    fifo = argv[argv.index("-i") + 1]
    expected = ["-nostdin", "-hide_banner", "-loglevel", "error", "-f", "wav", "-probesize", "32",
                "-analyzeduration", "0", "-i", fifo, "-map", "0:a:0", "-vn", "-ar", "48000",
                "-ac", "2", "-c:a", "pcm_s16le", "-f", "pulse", "-device", BUS,
                "-buffer_duration", "100", "CentralAudioMicrophone"]
    require(argv == expected, "consumer must target only explicit tablet_mic_bus Pulse sink")
    require(Path(fifo).is_fifo(), "consumer input must be a FIFO")
    signal.signal(signal.SIGTERM, stop_signal)
    if config.get("consumer_exit"):
        return 6, "", "Injected consumer failure"
    pid = os.getpid()
    try:
        with open(fifo, "rb", buffering=0) as stream:
            header = stream.read(44)
            require(len(header) == 44 and header[:4] == b"RIFF" and header[8:12] == b"WAVE", "consumer did not receive WAV bytes")
            if not config.get("no_stream"):
                def register(state):
                    state["streams"].append({"index": 70, "sink": state["sinks"][BUS]["index"],
                                             "owner_pid": pid, "reported_pid": pid})
                update(register)
                event("stream-ready")
            while stream.read(4096):
                pass
    finally:
        def remove(state):
            state["streams"] = [row for row in state["streams"] if row.get("owner_pid") != pid]
        update(remove)
    return 0, "", ""


def main():
    event("begin")
    try:
        if TOOL == "pactl":
            result = pactl(sys.argv[1:])
        elif TOOL == "scrcpy":
            result = scrcpy(sys.argv[1:])
        elif TOOL == "ffmpeg":
            result = ffmpeg(sys.argv[1:])
        else:
            raise ProtocolError("unexpected executable")
        if result is None:
            result = (0, "", "")
        returncode, stdout, stderr = result
        event("end", returncode=returncode)
        sys.stdout.write(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        return returncode
    except ProtocolError as exc:
        event("protocol-error", message=str(exc))
        print(f"fake tablet protocol error: {exc}", file=sys.stderr)
        return 64
    finally:
        event("exit")


if __name__ == "__main__":
    sys.exit(main())
