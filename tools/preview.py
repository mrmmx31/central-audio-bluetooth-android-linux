#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Capture only the application's private Xvfb window with simulated audio."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import select
import secrets
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tests"))
from test_audio_route import AudioRouteTests, initial_state  # noqa: E402


def checked(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10, **kwargs)


def xauth(path, display, cookie):
    path.touch(mode=0o600)
    checked(["xauth", "-f", str(path), "source", "-"],
            input=f"add {display} MIT-MAGIC-COOKIE-1 {cookie}\n",
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})


def main():
    for tool in ("Xvfb", "xauth", "xdpyinfo", "xwininfo", "xwd", "magick"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing preview tool: {tool}; nothing installed.")
    fixture = AudioRouteTests()
    fixture.setUp()
    server = app = None
    try:
        root = fixture.root
        home = root / "home"
        runtime = root / "runtime"
        home.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        (fixture.bin / "python3").symlink_to(sys.executable)
        state = initial_state()
        for device in state["sources"].values():
            device["description"] += " (simulado)"
        for device in state["sinks"].values():
            device["description"] = "Fones / alto-falantes do notebook (simulado)"
        fixture.write_server(state)
        server_auth = root / "server.auth"
        client_auth = root / "client.auth"
        cookie = secrets.token_hex(16)
        # The server consumes protocol/data from this private seed record.
        # No client connects to this placeholder; -displayfd allocates the server.
        xauth(server_auth, f":{os.getpid() + 1000}", cookie)
        read_fd, write_fd = os.pipe()
        log = (root / "xvfb.log").open("wb")
        server = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1100x1100x24",
             "-nolisten", "tcp", "-auth", str(server_auth)],
            pass_fds=(write_fd,), stdout=log, stderr=log, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        os.close(write_fd)
        if not select.select([read_fd], [], [], 10)[0]:
            raise RuntimeError("Private X server did not report readiness.")
        number = os.read(read_fd, 128).decode().strip()
        os.close(read_fd)
        if not number.isdigit() or server.poll() is not None:
            raise RuntimeError("Private X server failed; no fallback transport was attempted.")
        display = ":" + number
        xauth(client_auth, display, cookie)
        xenv = {"PATH": "/usr/bin:/bin", "DISPLAY": display,
                "XAUTHORITY": str(client_auth), "LC_ALL": "C.UTF-8"}
        checked(["xdpyinfo"], env=xenv)
        empty_auth = root / "empty.auth"
        empty_auth.touch(mode=0o600)
        denied = subprocess.run(["xdpyinfo"], env={**xenv, "XAUTHORITY": str(empty_auth)},
                                capture_output=True, timeout=5)
        if denied.returncode == 0:
            raise RuntimeError("Private server accepted a client without its cookie; preview stopped.")
        env = {**fixture.env, "HOME": str(home), "XDG_CONFIG_HOME": str(root / "config"),
               "XDG_RUNTIME_DIR": str(runtime), "DISPLAY": display,
               "XAUTHORITY": str(client_auth), "PULSE_SERVER": "unix:/nonexistent-central-audio-preview",
               "NO_AT_BRIDGE": "1", "GTK_MODULES": "", "LC_ALL": "C.UTF-8"}
        gtkrc = root / "preview.gtkrc"
        gtkrc.write_text('gtk-theme-name="Raleigh"\ngtk-font-name="Sans 10"\n')
        env["GTK2_RC_FILES"] = str(gtkrc)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        env.pop("SESSION_MANAGER", None)
        app_log = (root / "application.log").open("wb")
        app = subprocess.Popen([str(PROJECT / "audio-center")], cwd=PROJECT, env=env,
                               stdout=app_log, stderr=app_log, start_new_session=True)
        window = None
        deadline = time.monotonic() + 30
        tree = ""
        while time.monotonic() < deadline:
            if app.poll() is not None:
                raise RuntimeError("Application exited: " + (root / "application.log").read_text(errors="replace"))
            tree = checked(["xwininfo", "-root", "-tree"], env=xenv).stdout
            match = re.search(r'(0x[0-9a-f]+) "Central de', tree)
            if match:
                window = match.group(1)
                break
            time.sleep(0.1)
        if window is None:
            raise RuntimeError("Application window not observed on the private display.\n" + tree +
                               "\nApplication log:\n" + (root / "application.log").read_text(errors="replace"))
        time.sleep(0.5)
        destination = PROJECT / "build" / "evidence"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        xwd = destination / f"central-audio-{stamp}.xwd"
        png = destination / f"central-audio-{stamp}.png"
        checked(["xwd", "-silent", "-id", window, "-out", str(xwd)], env=xenv)
        checked(["magick", str(xwd), str(png)])
        dimensions = checked(["magick", "identify", "-format", "%wx%h", str(png)]).stdout
        digest = hashlib.sha256(png.read_bytes()).hexdigest()
        fixture.tearDown()
        print(json.dumps({"image": str(png), "dimensions": dimensions, "sha256": digest,
                          "kind": "Xvfb-only; simulated audio; private-cookie rejection verified"}))
    finally:
        for process in (app, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        fixture.temporary.cleanup()


if __name__ == "__main__":
    main()
