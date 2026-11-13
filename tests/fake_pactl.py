#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Strict, isolated pactl double used by test_audio_route.py.

The real audio server is never contacted. Every invocation is validated and
recorded; planned failures and delays live beside the fake server JSON state.
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time


class ProtocolError(Exception):
    """An unsupported command or invalid argument vector."""


class ServerError(Exception):
    """A syntactically valid command that the fake server cannot apply."""


def validate_cli(argv):
    if not argv:
        raise ProtocolError("missing command")
    command = argv[0]
    if command in {"info", "get-default-source", "get-default-sink"}:
        valid = len(argv) == 1
    elif command == "list":
        valid = (len(argv) == 2 and argv[1] in {"sources", "sinks"}) or (
            len(argv) == 3
            and argv[1] == "short"
            and argv[2] in {"sources", "sinks", "source-outputs"}
        )
    elif command in {"set-default-source", "set-default-sink"}:
        valid = len(argv) == 2 and bool(argv[1])
    elif re.fullmatch(r"get-(source|sink)-(volume|mute)", command):
        valid = len(argv) == 2 and bool(argv[1])
    elif re.fullmatch(r"set-(source|sink)-mute", command):
        valid = len(argv) == 3 and argv[2] in {"0", "1"}
    elif re.fullmatch(r"set-(source|sink)-volume", command):
        valid = len(argv) >= 3
        if valid and any("," in value for value in argv[2:]):
            raise ProtocolError("CSV is not a VOLUME argument; use one argv per channel")
        valid = valid and all(re.fullmatch(r"[0-9]+%?", value) for value in argv[2:])
    else:
        raise ProtocolError(f"unsupported command: {command}")
    if not valid:
        raise ProtocolError(f"invalid argument vector: {argv!r}")


def device(state, kind, name):
    try:
        return state[f"{kind}s"][name]
    except KeyError as exc:
        raise ServerError(f"No such {kind}: {name}") from exc


def execute(state, argv):
    command = argv[0]
    if command == "info":
        return "Server Name: Strict fake PulseAudio\n"
    if command.startswith("get-default-"):
        return state[command.removeprefix("get-").replace("-", "_")] + "\n"
    if command.startswith("set-default-"):
        kind = command.removeprefix("set-default-")
        device(state, kind, argv[1])
        state[f"default_{kind}"] = argv[1]
        return ""
    if command == "list":
        short = argv[1] == "short"
        collection = argv[-1]
        if collection == "source-outputs":
            return "".join("\t".join(map(str, row)) + "\n" for row in state["source_outputs"])
        lines = []
        for name, item in state[collection].items():
            if short:
                lines.append(
                    f"{item['index']}\t{name}\tfake-driver\ts16le {len(item['volumes'])}ch 48000Hz\tRUNNING\n"
                )
            else:
                kind = "Source" if collection == "sources" else "Sink"
                lines.append(
                    f"{kind} #{item['index']}\n\tName: {name}\n\tDescription: {item['description']}\n"
                )
        return "".join(lines)
    operation, kind, field = command.split("-")
    item = device(state, kind, argv[1])
    if operation == "get":
        if field == "mute":
            return "Mute: " + ("yes" if item["mute"] else "no") + "\n"
        channel_names = item.get("channels", [f"channel-{i}" for i in range(len(item["volumes"]))])
        fields = [
            f"{name}: {value} / {round(value * 100 / 65536)}% / 0.00 dB"
            for name, value in zip(channel_names, item["volumes"])
        ]
        return "Volume: " + ", ".join(fields) + "\n        balance 0.00\n"
    if field == "mute":
        item["mute"] = int(argv[2])
        return ""
    volumes = argv[2:]
    if len(volumes) not in {1, len(item["volumes"])}:
        raise ProtocolError("VOLUME argument count does not match the channel count")
    parsed = [round(int(value[:-1]) * 65536 / 100) if value.endswith("%") else int(value) for value in volumes]
    item["volumes"] = parsed * len(item["volumes"]) if len(parsed) == 1 else parsed
    return ""


def main():
    root = Path(os.environ["FAKE_PACTL_DIR"]).resolve()
    state_path = Path(os.environ["FAKE_PACTL_STATE"]).resolve()
    log_path = Path(os.environ["FAKE_PACTL_LOG"]).resolve()
    if state_path.parent != root or log_path.parent != root:
        raise RuntimeError("fake state and log must remain in the test directory")

    @contextlib.contextmanager
    def locked():
        with (root / "fake-server.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def load_state():
        return json.loads(state_path.read_text())

    def save_state(state):
        state_path.write_text(json.dumps(state))

    def log(**event):
        with log_path.open("a") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:], **event}) + "\n")

    argv = sys.argv[1:]
    with locked():
        log(phase="begin")
        try:
            validate_cli(argv)
        except ProtocolError as exc:
            log(phase="end", returncode=64, protocol_error=str(exc))
            print(f"fake pactl protocol error: {exc}", file=sys.stderr)
            return 64
        state = load_state()
        failure = None
        delay = 0
        for rule in state.get("failures", []):
            if rule["argv"] == argv and rule.get("remaining", 1) > 0:
                rule["remaining"] = rule.get("remaining", 1) - 1
                failure = dict(rule)
                break
        for rule in state.get("delays", []):
            if rule["argv"] == argv and rule.get("remaining", 1) > 0:
                rule["remaining"] = rule.get("remaining", 1) - 1
                delay = rule["seconds"]
                break
        save_state(state)

    # Do not hold the fake server lock while delayed: concurrent helper calls
    # must expose missing helper locks rather than being serialized by the fake.
    if delay:
        time.sleep(delay)
    with locked():
        state = load_state()
        returncode = 0
        output = ""
        stderr = ""
        protocol_error = None
        if failure is not None:
            returncode = failure.get("returncode", 1)
            stderr = failure.get("stderr", "Injected fake-server failure")
        else:
            try:
                output = execute(state, argv)
            except ProtocolError as exc:
                returncode, stderr, protocol_error = 64, str(exc), str(exc)
            except ServerError as exc:
                returncode, stderr = 1, str(exc)
        save_state(state)
        log(phase="end", returncode=returncode, protocol_error=protocol_error)
    sys.stdout.write(output)
    if stderr:
        print(stderr, file=sys.stderr)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
