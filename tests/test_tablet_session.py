# SPDX-License-Identifier: GPL-2.0-only
"""Isolated tablet lifecycle integration and ownership boundary tests.

Only generated WAV bytes, a private FIFO and fake child processes are used.
No pactl, ADB, scrcpy, FFmpeg or capture device from the host is accessed.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "tablet-session.py"
FIXTURE = Path(__file__).with_name("fake_tablet_tools.py")
BUS = "tablet_mic_bus"
SOURCE = "tablet_mic"
SERIAL = "FAKE-TABLET-ONLY"


def server_state():
    return {"default_source": "internal.monitor", "default_sink": "internal",
            "sinks": {"internal": {"index": 1}}, "sources": {"internal.monitor": {"index": 2}},
            "modules": [], "streams": [], "source_outputs": [], "next_module": 40,
            "failures": [], "config": {"scrcpy_version": "3.3.1", "pulse": True}}


class TabletSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tablet-session-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("scrcpy", "ffmpeg", "pactl"):
            executable = self.bin / tool
            executable.write_text(f"#!{sys.executable}\nimport os, runpy\nos.environ['FAKE_TABLET_TOOL']={tool!r}\nrunpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n")
            executable.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.bin), "XDG_STATE_HOME": str(self.root / "xdg"),
                    "FAKE_TABLET_ROOT": str(self.root)}
        self.server_file = self.root / "server.json"
        self.log_file = self.root / "tools.jsonl"
        self.write_server(server_state())
        spec = importlib.util.spec_from_file_location("tablet_session_under_test", HELPER)
        self.helper = importlib.util.module_from_spec(spec)
        # Import executes definitions only; constants are then confined to temp.
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(self.helper)
        self.state_directory = self.root / "xdg" / "central-gtk2-audio"
        self.state_file = self.state_directory / "session.json"
        self.helper.STATE_DIR = self.state_directory
        self.helper.STATE = self.state_file
        self.children = []
        self.real_popen = subprocess.Popen
        self.addCleanup(self.cleanup_children)

    def tearDown(self):
        errors = [entry for entry in self.events() if entry["phase"] == "protocol-error"]
        self.assertEqual(errors, [], "helper violated the strict external-tool boundary")
        state = self.server()
        self.assertEqual(state["default_source"], "internal.monitor")
        self.assertEqual(state["default_sink"], "internal")

    def track_popen(self, *args, **kwargs):
        process = self.real_popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            self.children.append(process)
        return process

    def cleanup_children(self):
        for process in reversed(self.children):
            if process.poll() is None:
                # Every entry is a Popen created by this test, never a PID read
                # from untrusted session state or an existing host application.
                try:
                    if os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)

    def write_server(self, data):
        self.server_file.write_text(json.dumps(data))

    def server(self):
        # Writes can overlap the short-lived fake stream shutdown. Reuse the
        # fixture's lock rather than observing partially written JSON.
        import fcntl
        with (self.root / "server.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return json.loads(self.server_file.read_text())

    def events(self):
        import fcntl
        with (self.root / "server.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not self.log_file.exists():
                return []
            return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def pa_calls(self):
        return [entry["argv"] for entry in self.events() if entry["tool"] == "pactl" and entry["phase"] == "begin"]

    def pa_mutations(self):
        return [argv for argv in self.pa_calls() if argv[0] in {"load-module", "unload-module"}]

    def session(self):
        return json.loads(self.state_file.read_text())

    def configure(self, **values):
        state = self.server()
        state["config"].update(values)
        self.write_server(state)

    def invoke(self, *argv, success=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, "argv", [str(HELPER), *argv]), \
             mock.patch.object(self.helper.subprocess, "Popen", side_effect=self.track_popen), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.helper.main()
        for child in self.children:
            child.poll()
        message = f"argv={argv!r}\nstdout={stdout.getvalue()}\nstderr={stderr.getvalue()}"
        self.assertEqual(result, 0 if success else 1, message)
        return stdout.getvalue(), stderr.getvalue()

    def start(self):
        self.invoke("start", "--serial", SERIAL)
        data = self.session()
        self.assertEqual(len(data["processes"]), 2)
        self.assertTrue(all(self.helper.identity(process["pid"]) == process["start"] for process in data["processes"]))
        return data

    def assert_no_session(self):
        self.assertFalse(self.state_file.exists())
        self.assertEqual(list(self.state_directory.glob("session-*")), [])

    def add_external_resources(self):
        state = self.server()
        state["modules"] = [
            {"id": 10, "kind": "module-null-sink", "args": [f"sink_name={BUS}", "sink_properties=device.description=External"]},
            {"id": 11, "kind": "module-remap-source", "args": [f"master={BUS}.monitor", f"source_name={SOURCE}", "source_properties=device.description=Microfone_Tablet"]},
        ]
        state["sinks"][BUS] = {"index": 30, "owner_module": 10}
        state["sources"][BUS + ".monitor"] = {"index": 31, "owner_module": 10}
        state["sources"][SOURCE] = {"index": 32, "owner_module": 11}
        self.write_server(state)
        return copy.deepcopy(state)

    def test_start_uses_real_private_fifo_and_exact_explicit_audio_targets(self):
        data = self.start()
        fifo = Path(data["runtime"]) / "microphone.wav"
        self.assertTrue(fifo.is_fifo())
        self.assertEqual(fifo.stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(data["runtime"]) / "owner").read_text().strip(), data["token"])
        streams = self.server()["streams"]
        consumer = next(process for process in data["processes"] if process["role"] == "ffmpeg")
        self.assertEqual([row["reported_pid"] for row in streams], [consumer["pid"]])
        producer = [entry for entry in self.events() if entry["tool"] == "scrcpy" and f"--serial={SERIAL}" in entry["argv"]]
        self.assertTrue(producer)
        self.assertIn("--no-audio-playback", producer[0]["argv"])
        self.invoke("stop")
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], [])
        self.assertEqual(self.server()["streams"], [])
        self.assertTrue(all(process.poll() is not None for process in self.children))

    def test_reconnect_replaces_only_its_own_session_and_retains_serial(self):
        first = self.start()
        first_pids = {row["pid"] for row in first["processes"]}
        self.invoke("reconnect")
        second = self.session()
        self.assertEqual(second["serial"], SERIAL)
        self.assertNotEqual(second["token"], first["token"])
        self.assertTrue(first_pids.isdisjoint({row["pid"] for row in second["processes"]}))
        self.assertFalse(Path(first["runtime"]).exists())
        self.invoke("stop")
        self.assert_no_session()

    def test_capture_guard_refuses_start_before_audio_mutation_or_process_creation(self):
        state = self.server()
        state["source_outputs"] = [[5, 2, 77, "fake-client"]]
        self.write_server(state)
        _, stderr = self.invoke("start", "--serial", SERIAL, success=False)
        self.assertIn("captura", stderr)
        self.assertEqual(self.pa_mutations(), [])
        self.assertEqual(self.children, [])
        self.assert_no_session()

    def test_capture_guard_preserves_running_session_on_stop_and_reconnect(self):
        data = self.start()
        state = self.server()
        state["source_outputs"] = [[5, 32, 77, "fake-client"]]
        self.write_server(state)
        mutation_count = len(self.pa_mutations())
        for command in ("stop", "reconnect"):
            with self.subTest(command=command):
                self.invoke(command, success=False)
                self.assertEqual(self.session(), data)
                self.assertEqual(len(self.pa_mutations()), mutation_count)
                self.assertTrue(all(process.poll() is None for process in self.children))

    def test_module_capture_without_client_does_not_block_start(self):
        state = self.server()
        state["source_outputs"] = [[5, 2, "-", "module-remap-source"]]
        self.write_server(state)
        self.start()
        self.invoke("stop")

    def test_old_scrcpy_and_missing_required_options_fail_before_audio_mutation(self):
        for config in ({"scrcpy_version": "2.2"}, {"scrcpy_version": "not-a-version"},
                       {"scrcpy_version": "2.3", "missing_options": ["--no-audio-playback"]}):
            with self.subTest(config=config):
                self.configure(**config)
                self.invoke("start", "--serial", SERIAL, success=False)
                self.assertEqual(self.pa_mutations(), [])
                self.assertEqual(self.children, [])
                self.assert_no_session()

    def test_ffmpeg_without_pulse_fails_before_audio_mutation(self):
        self.configure(pulse=False)
        _, stderr = self.invoke("start", "--serial", SERIAL, success=False)
        self.assertIn("PulseAudio", stderr)
        self.assertEqual(self.pa_mutations(), [])
        self.assertEqual(self.children, [])
        self.assert_no_session()

    def test_serial_is_required_before_running_any_external_tool(self):
        for argv in (("start",), ("start", "--serial", ""), ("start", "--serial", "invalid\nserial"),
                     ("start", "--serial", "x" * 257)):
            with self.subTest(argv=argv):
                self.invoke(*argv, success=False)
                self.assertEqual(self.events(), [])
                self.assertEqual(self.children, [])
                self.assert_no_session()

    def test_preexisting_bus_and_source_are_reused_without_ownership_or_unload(self):
        initial = self.add_external_resources()
        data = self.start()
        self.assertEqual(data["modules"], [])
        self.assertEqual(self.pa_mutations(), [])
        self.invoke("stop")
        self.assertEqual(self.server()["modules"], initial["modules"])
        self.assertEqual(self.server()["sources"], initial["sources"])
        self.assertEqual(self.server()["sinks"], initial["sinks"])
        self.assertEqual(self.pa_mutations(), [])

    def test_existing_bus_with_foreign_stream_is_preserved(self):
        self.add_external_resources()
        state = self.server()
        state["streams"] = [{"index": 91, "sink": 30, "owner_pid": -1, "reported_pid": -1}]
        self.write_server(state)
        self.invoke("start", "--serial", SERIAL, success=False)
        self.assertEqual(self.pa_mutations(), [])
        self.assertEqual(self.children, [])
        self.assertEqual(len(self.server()["modules"]), 2)
        # A foreign stream can intentionally leave a cleanup record pending;
        # neither that record nor the foreign modules may be discarded.
        if self.state_file.exists():
            self.assertEqual(self.session()["modules"], [])

    def test_preexisting_source_with_wrong_master_is_never_assumed(self):
        self.add_external_resources()
        state = self.server()
        state["modules"][1]["args"][0] = f"master={BUS}.monitor_other"
        self.write_server(state)
        self.invoke("start", "--serial", SERIAL, success=False)
        self.assertEqual(self.pa_mutations(), [])
        self.assertEqual(self.children, [])

    def test_partial_module_creation_failure_removes_only_owned_bus(self):
        state = self.server()
        # The exact ownership property contains a generated token; inject at
        # the strict pa boundary while retaining real fake execution otherwise.
        original_pa = self.helper.pa
        def refuse_source(*argv):
            if argv[:2] == ("load-module", "module-remap-source"):
                raise self.helper.SessionError("injected remap creation failure")
            return original_pa(*argv)
        with mock.patch.object(self.helper, "pa", side_effect=refuse_source):
            self.invoke("start", "--serial", SERIAL, success=False)
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], state["modules"])
        self.assertEqual(self.server()["sinks"], state["sinks"])
        self.assertEqual(self.children, [])
        self.assertEqual([call[0] for call in self.pa_mutations()], ["load-module", "unload-module"])

    def test_producer_failure_cleans_up_own_children_modules_and_fifo(self):
        self.configure(producer_exit=True)
        self.invoke("start", "--serial", SERIAL, success=False)
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], [])
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_ambiguous_module_creation_is_cleaned_or_retains_a_recoverable_record(self):
        self.configure(bad_module_id_for="module-null-sink")
        self.invoke("start", "--serial", SERIAL, success=False)
        remaining = self.server()["modules"]
        if remaining:
            self.assertTrue(self.state_file.exists(),
                            "created module survived but its session ownership record was deleted")
            recorded = {module["id"] for module in self.session()["modules"]}
            self.assertTrue({module["id"] for module in remaining}.issubset(recorded),
                            "created modules must remain identifiable for a later cleanup")
        else:
            self.assert_no_session()

    def test_uncertain_creation_with_failed_introspection_keeps_intent_for_retry(self):
        self.configure(bad_module_id_for="module-null-sink")
        state = self.server()
        state["failures"].append({"argv": ["list", "short", "modules"], "skip": 1, "remaining": 4})
        self.write_server(state)
        self.invoke("start", "--serial", SERIAL, success=False)
        self.assertEqual(len(self.server()["modules"]), 1)
        self.assertTrue(self.state_file.exists())
        self.assertIn("module-null-sink", self.session().get("pending_modules", []))
        state = self.server()
        state["failures"] = []
        self.write_server(state)
        self.invoke("stop")
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], [])

    def test_startup_timeout_cleans_up_owned_resources(self):
        self.configure(no_stream=True)
        real_monotonic = time.monotonic
        ticks = iter((0.0, 0.0, 20.0))
        def clock():
            return next(ticks, real_monotonic())
        with mock.patch.object(self.helper.time, "monotonic", side_effect=clock), \
             mock.patch.object(self.helper, "own_stream_ready", return_value=False):
            _, stderr = self.invoke("start", "--serial", SERIAL, success=False)
        self.assertIn("Tempo esgotado", stderr)
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], [])
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_external_command_timeout_is_reported_as_session_error(self):
        with mock.patch.object(self.helper.subprocess, "run", side_effect=subprocess.TimeoutExpired(["fake-command"], 8)):
            with self.assertRaises(self.helper.SessionError):
                self.helper.run(["fake-command"])
        self.assertEqual(self.events(), [])

    def test_stream_readiness_requires_both_bus_and_the_consumers_pid(self):
        def response(*argv):
            if argv == ("list", "short", "sinks"):
                return f"30\t{BUS}\tfake"
            if argv == ("-f", "json", "list", "sink-inputs"):
                return json.dumps([{"sink": 30, "properties": {"application.process.id": "123"}},
                                   {"sink": 99, "properties": {"application.process.id": "456"}}])
            self.fail(f"unexpected pactl query {argv!r}")
        with mock.patch.object(self.helper, "pa", side_effect=response):
            self.assertFalse(self.helper.own_stream_ready(456))
            self.assertTrue(self.helper.own_stream_ready(123))

    def test_module_unload_failure_keeps_child_and_parent_pending_for_retry(self):
        data = self.start()
        source_id = data["modules"][1]["id"]
        state = self.server()
        state["failures"].append({"argv": ["unload-module", str(source_id)], "remaining": 1})
        self.write_server(state)
        self.invoke("stop", success=False)
        remaining = self.session()
        self.assertEqual(remaining["processes"], [])
        self.assertEqual(remaining["modules"], data["modules"])
        self.assertEqual(len(self.server()["modules"]), 2)
        self.invoke("stop")
        self.assert_no_session()
        self.assertEqual(self.server()["modules"], [])

    def test_changed_module_owner_preserves_it_and_its_parent(self):
        data = self.start()
        state = self.server()
        state["modules"][1]["args"][-1] = state["modules"][1]["args"][-1].replace(data["token"], "0" * 32)
        self.write_server(state)
        mutation_count = len(self.pa_mutations())
        self.invoke("stop", success=False)
        self.assertEqual(len(self.pa_mutations()), mutation_count)
        self.assertEqual(self.session()["modules"], data["modules"])

    def test_token_substring_in_another_owner_value_does_not_prove_ownership(self):
        data = self.start()
        state = self.server()
        state["modules"][1]["args"][-1] = state["modules"][1]["args"][-1].replace(data["token"], "other-" + data["token"])
        self.write_server(state)
        mutation_count = len(self.pa_mutations())
        self.invoke("stop", success=False)
        self.assertEqual(len(self.pa_mutations()), mutation_count)
        self.assertTrue(self.state_file.exists())

    def owned_sleeper(self, new_session=True):
        process = self.real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  start_new_session=new_session)
        self.children.append(process)
        stamp = self.helper.identity(process.pid)
        self.assertIsNotNone(stamp)
        return process, stamp

    def test_wrong_process_starttime_is_preserved_without_signals(self):
        process, stamp = self.owned_sleeper()
        with self.assertRaises(self.helper.SessionError):
            self.helper.stop_process({"pid": process.pid, "start": str(int(stamp) + 1)})
        self.assertIsNone(process.poll())

    def test_process_that_is_not_its_group_leader_is_preserved(self):
        process, stamp = self.owned_sleeper(new_session=False)
        with self.assertRaises(self.helper.SessionError):
            self.helper.stop_process({"pid": process.pid, "start": stamp})
        self.assertIsNone(process.poll())

    def test_cleanup_preserves_unverified_pid_and_does_not_unload_modules(self):
        data = self.start()
        process, stamp = self.owned_sleeper()
        unverified = {"pid": process.pid, "start": str(int(stamp) + 1), "role": "unverified-test-child"}
        data["processes"].append(unverified)
        self.state_file.write_text(json.dumps(data))
        mutation_count = len(self.pa_mutations())
        self.invoke("stop", success=False)
        self.assertIsNone(process.poll())
        self.assertEqual(self.session()["processes"], [unverified])
        self.assertEqual(len(self.pa_mutations()), mutation_count)

    def test_corrupt_state_and_owner_marker_are_preserved(self):
        self.state_directory.mkdir(parents=True)
        for contents in ("{truncated", json.dumps({"version": 1, "token": "invalid"})):
            with self.subTest(contents=contents):
                self.state_file.write_text(contents)
                self.invoke("stop", success=False)
                self.assertEqual(self.state_file.read_text(), contents)
                self.assertEqual(self.pa_mutations(), [])
        self.state_file.unlink()
        data = self.start()
        owner = Path(data["runtime"]) / "owner"
        owner.write_text("wrong-owner\n")
        self.invoke("stop", success=False)
        self.assertEqual(self.session(), data)
        self.assertTrue(all(child.poll() is None for child in self.children))

    def test_check_and_status_are_readonly_and_stop_without_session_is_idempotent(self):
        self.invoke("check")
        self.invoke("status")
        self.assertFalse(self.state_directory.exists())
        self.assertEqual(self.pa_mutations(), [])
        self.invoke("stop")
        self.invoke("stop")
        self.assert_no_session()
        self.assertEqual(self.pa_mutations(), [])

    def supervision_state(self, processes):
        """Private valid state backed by owned sleepers, with no audio input."""
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime = self.state_directory / "session-observer-fixture"
        runtime.mkdir(mode=0o700)
        token = "a" * 32
        (runtime / "owner").write_text(token + "\n")
        data = {"version": 1, "token": token, "runtime": str(runtime), "serial": SERIAL,
                "scrcpy": str(self.bin / "scrcpy"), "processes": processes,
                "modules": [], "pending_modules": []}
        self.helper.save_state(data)
        return data

    @contextlib.contextmanager
    def forbid_capture_boundaries(self):
        """Fail immediately if a privacy/observer action probes or starts audio."""
        with contextlib.ExitStack() as stack:
            for boundary in ("pa", "run", "requirements", "start", "guard"):
                stack.enter_context(mock.patch.object(
                    self.helper, boundary, side_effect=AssertionError(f"forbidden boundary: {boundary}")))
            stack.enter_context(mock.patch.object(
                self, "track_popen", side_effect=AssertionError("privacy/observer action attempted Popen")))
            yield

    def test_observe_without_session_is_readonly_and_never_starts_or_probes_audio(self):
        with self.forbid_capture_boundaries(), mock.patch.object(
                self.helper, "stop_process", side_effect=AssertionError("observer attempted to stop a process")):
            self.invoke("observe")
            self.invoke("observe")
        self.assertFalse(self.state_directory.exists())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.children, [])

    def test_observe_logs_only_process_changes_and_never_restarts_failed_capture(self):
        consumer, consumer_stamp = self.owned_sleeper()
        producer, producer_stamp = self.owned_sleeper()
        records = [{"pid": consumer.pid, "start": consumer_stamp, "role": "ffmpeg"},
                   {"pid": producer.pid, "start": producer_stamp, "role": "scrcpy"}]
        data = self.supervision_state(records)
        log = Path(data["runtime"]) / "events.jsonl"
        with self.forbid_capture_boundaries(), mock.patch.object(
                self.helper, "stop_process", side_effect=AssertionError("observer attempted to stop a process")):
            self.invoke("observe")
            self.invoke("observe")
            first = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(first), 1, "unchanged observations must not append duplicate events")
            # Terminate only our inert test children. Observation must report
            # their disappearance without recovering or unmuting anything.
            consumer.terminate()
            producer.terminate()
            consumer.wait(timeout=5)
            producer.wait(timeout=5)
            stdout, _ = self.invoke("observe")
            self.invoke("observe")
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["processes"], {"ffmpeg": True, "scrcpy": True, "privacy_latched": False})
        self.assertEqual(entries[1]["processes"], {"ffmpeg": False, "scrcpy": False, "privacy_latched": False})
        for event in entries:
            self.assertEqual(set(event), {"time_utc", "processes", "automatic_restart", "call_mute"})
            self.assertIs(event["automatic_restart"], False)
            self.assertEqual(event["call_mute"], "unknown")
            self.assertRegex(event["time_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn(SERIAL, log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(data["runtime"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.session()["processes"], records)
        self.assertIn("Reinício automático bloqueado", stdout)
        self.assertIn("mute da chamada desconhecido", stdout)
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.children), 2)

    def test_status_reports_unknown_call_mute_and_automatic_restart_block(self):
        self.supervision_state([])
        with self.forbid_capture_boundaries():
            stdout, _ = self.invoke("status")
        self.assertIn("Reinício automático bloqueado", stdout)
        self.assertIn("mute da chamada desconhecido", stdout)
        self.assertEqual(self.events(), [])

    def test_privacy_pause_stops_only_owned_processes_during_active_capture(self):
        data = self.start()
        state = self.server()
        state["source_outputs"] = [[5, 32, 77, "fake-call-client"]]
        self.write_server(state)
        expected_modules = copy.deepcopy(state["modules"])
        expected_sinks = copy.deepcopy(state["sinks"])
        expected_sources = copy.deepcopy(state["sources"])
        route_snapshot = self.state_directory / "routes.tsv"
        level_snapshot = self.state_directory / "levels.tsv"
        route_snapshot.write_text("route-state-sentinel\n")
        level_snapshot.write_text("level-state-sentinel\n")
        before_calls = self.pa_calls()
        with self.forbid_capture_boundaries():
            self.invoke("privacy-pause")
            self.invoke("privacy-pause")
        paused = self.session()
        self.assertIs(paused["privacy_latched"], True)
        self.assertEqual(paused["processes"], [])
        self.assertEqual(paused["modules"], data["modules"])
        self.assertEqual(paused["token"], data["token"])
        self.assertTrue(Path(data["runtime"]).exists())
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertEqual(self.pa_calls(), before_calls, "privacy pause must not query or change pactl state")
        remaining = self.server()
        self.assertEqual(remaining["modules"], expected_modules)
        self.assertEqual(remaining["sinks"], expected_sinks)
        self.assertEqual(remaining["sources"], expected_sources)
        self.assertEqual(remaining["source_outputs"], state["source_outputs"])
        self.assertEqual(remaining["streams"], [])
        self.assertEqual(route_snapshot.read_text(), "route-state-sentinel\n")
        self.assertEqual(level_snapshot.read_text(), "level-state-sentinel\n")

    def test_privacy_latch_blocks_reconnect_before_requirements_or_capture_actions(self):
        self.supervision_state([])
        with self.forbid_capture_boundaries():
            self.invoke("privacy-pause")
            snapshot = self.state_file.read_bytes()
            _, stderr = self.invoke("reconnect", "--serial", "DO-NOT-START", "--scrcpy", "/forbidden/executable", success=False)
        self.assertIn("privacidade", stderr.lower())
        self.assertEqual(self.state_file.read_bytes(), snapshot)
        self.assertIs(self.session()["privacy_latched"], True)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.children, [])

    def test_privacy_pause_keeps_mismatched_pid_identity_and_latches_failure(self):
        process, stamp = self.owned_sleeper()
        record = {"pid": process.pid, "start": str(int(stamp) + 1), "role": "scrcpy"}
        self.supervision_state([record])
        with self.forbid_capture_boundaries():
            _, stderr = self.invoke("privacy-pause", success=False)
            snapshot = self.state_file.read_bytes()
            self.invoke("reconnect", success=False)
        self.assertIn("incompleta", stderr.lower())
        self.assertIsNone(process.poll())
        self.assertEqual(self.session()["processes"], [record])
        self.assertIs(self.session()["privacy_latched"], True)
        self.assertEqual(self.state_file.read_bytes(), snapshot)
        self.assertEqual(self.events(), [])

    def test_observe_records_privacy_latch_without_restarting_or_unmuting(self):
        data = self.supervision_state([])
        with self.forbid_capture_boundaries():
            self.invoke("observe")
            self.invoke("privacy-pause")
            stdout, _ = self.invoke("observe")
            self.invoke("observe")
        entries = [json.loads(line) for line in (Path(data["runtime"]) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["processes"], {"privacy_latched": False})
        self.assertEqual(entries[1]["processes"], {"privacy_latched": True})
        self.assertTrue(all(event["automatic_restart"] is False and event["call_mute"] == "unknown" for event in entries))
        self.assertIn("captura pausada", stdout)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
