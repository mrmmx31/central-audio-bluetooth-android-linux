# SPDX-License-Identifier: GPL-2.0-only
"""Exercise audio-route.sh against an isolated, strict fake audio server."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "audio-route.sh"
FAKE = Path(__file__).with_name("fake_pactl.py")
INTERNAL = "internal_speakers"
MONITOR = INTERNAL + ".monitor"
ALTERNATE = "usb_headphones"
LOCAL = "local_mic"
TABLET = "tablet_mic"
ROUTE_HEADER = "AUDIO_CENTER_ROUTES_V1"
LEVEL_HEADER = "AUDIO_CENTER_LEVELS_V1"


def initial_state():
    return {
        "default_source": MONITOR,
        "default_sink": INTERNAL,
        "sources": {
            TABLET: {"index": 2, "description": "Microfone_Tablet", "volumes": [27654], "mute": 0},
            LOCAL: {"index": 3, "description": "Local microphone", "volumes": [28765], "mute": 1},
            MONITOR: {"index": 4, "description": "Monitor of internal speakers", "volumes": [65536, 65536], "mute": 0},
        },
        "sinks": {
            INTERNAL: {"index": 5, "description": "Internal speakers", "volumes": [32111, 54321], "mute": 1, "channels": ["front-left", "front-right"]},
            ALTERNATE: {"index": 6, "description": "USB headphones", "volumes": [45678, 56789], "mute": 0},
        },
        "source_outputs": [],
        "failures": [],
        "delays": [],
    }


class AudioRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="audio-route-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # A closed PATH ensures a broken fake cannot fall through to real pactl,
        # scrcpy, systemctl, or any other unlisted program.
        for utility in ("bash", "timeout", "cat", "awk", "mkdir", "flock", "tail", "od", "mktemp", "rm", "mv"):
            executable = shutil.which(utility)
            if executable is None:
                self.skipTest(f"required local utility is missing: {utility}")
            (self.bin / utility).symlink_to(executable)
        self.bash = str(self.bin / "bash")
        pactl = self.bin / "pactl"
        pactl.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
        pactl.chmod(0o700)
        for utility, body in {
            "pgrep": "exit 1\n",
            "systemctl": "printf 'inactive\\n'\nexit 3\n",
            "obexctl": "printf 'fake-obexctl 1.0\\n'\n",
        }.items():
            shim = self.bin / utility
            shim.write_text(f"#!{self.bash}\n{body}")
            shim.chmod(0o700)
        self.server_file = self.root / "server.json"
        self.log_file = self.root / "pactl.jsonl"
        self.xdg = self.root / "xdg-state"
        self.state_directory = self.xdg / "central-gtk2-audio"
        self.routes_file = self.state_directory / "routes.tsv"
        self.levels_file = self.state_directory / "levels.tsv"
        self.env = dict(os.environ, PATH=str(self.bin), XDG_STATE_HOME=str(self.xdg),
                        FAKE_PACTL_DIR=str(self.root), FAKE_PACTL_STATE=str(self.server_file),
                        FAKE_PACTL_LOG=str(self.log_file))
        self.env.pop("BASH_ENV", None)
        self.env.pop("ENV", None)
        self.allow_protocol_rejections = False
        self.write_server(initial_state())
        self.assertEqual(shutil.which("pactl", path=self.env["PATH"]), str(pactl))

    def tearDown(self):
        if not self.allow_protocol_rejections:
            errors = [entry for entry in self.log() if entry.get("protocol_error")]
            self.assertEqual(errors, [], "helper violated the strict pactl contract")

    def write_server(self, state):
        self.server_file.write_text(json.dumps(state))

    def server(self):
        return json.loads(self.server_file.read_text())

    def log(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines() if line.strip()]

    def calls(self):
        return [entry["argv"] for entry in self.log() if entry["phase"] == "begin"]

    def mutations(self):
        return [argv for argv in self.calls() if argv[0].startswith("set-")]

    def audio_state(self):
        state = self.server()
        return {key: state[key] for key in ("default_source", "default_sink", "sources", "sinks", "source_outputs")}

    def fail_once(self, *argv):
        state = self.server()
        state["failures"].append({"argv": list(argv), "remaining": 1})
        self.write_server(state)

    def run_helper(self, *argv, success=True):
        result = subprocess.run([self.bash, str(HELPER), *argv], env=self.env,
                                cwd=self.root, text=True, capture_output=True, timeout=20)
        message = f"command={argv!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        if success:
            self.assertEqual(result.returncode, 0, message)
        else:
            self.assertNotEqual(result.returncode, 0, message)
        return result

    def write_snapshot(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode())

    def route_snapshot(self):
        return f"{ROUTE_HEADER}\nsource\t{MONITOR}\nsink\t{INTERNAL}\n"

    def level_row(self, kind="sink", name=INTERNAL):
        original = initial_state()[kind + "s"][name]
        return f"{kind}\t{name}\t{','.join(map(str, original['volumes']))}\t{original['mute']}"

    def assert_original_device(self, kind, name):
        original = initial_state()[kind + "s"][name]
        actual = self.server()[kind + "s"][name]
        self.assertEqual(actual["volumes"], original["volumes"])
        self.assertEqual(actual["mute"], original["mute"])

    def test_fake_rejects_csv_unknown_commands_and_wrong_argument_counts(self):
        self.allow_protocol_rejections = True
        original = self.audio_state()
        for argv in (["set-sink-volume", INTERNAL, "32111,54321"],
                     ["move-sink-input", "7", INTERNAL], ["info", "extra"],
                     ["set-sink-mute", INTERNAL, "toggle"], ["list", "short", "unknown"]):
            with self.subTest(argv=argv):
                result = subprocess.run([str(self.bin / "pactl"), *argv], env=self.env,
                                        text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(self.audio_state(), original)

    def test_stereo_balance_and_mute_restore_exactly_and_snapshot_only_once(self):
        self.run_helper("set-sink-levels", INTERNAL, "87", "0")
        original_snapshot = f"{LEVEL_HEADER}\n{self.level_row()}\n".encode()
        self.assertEqual(self.levels_file.read_bytes(), original_snapshot)
        self.assertEqual(self.server()["sinks"][INTERNAL]["mute"], 0)
        self.run_helper("set-sink-levels", INTERNAL, "10", "1")
        self.assertEqual(self.levels_file.read_bytes(), original_snapshot)
        self.run_helper("restore-volumes")
        self.assert_original_device("sink", INTERNAL)
        self.assertIn(["set-sink-volume", INTERNAL, "32111", "54321"], self.calls())
        self.assertIn(["set-sink-mute", INTERNAL, "1"], self.calls())
        self.assertFalse(self.levels_file.exists())

    def test_source_volume_restore_keeps_original_mute(self):
        self.run_helper("set-source-volume", LOCAL, "70")
        self.run_helper("restore-volumes")
        self.assert_original_device("source", LOCAL)
        self.assertIn(["set-source-volume", LOCAL, "28765"], self.calls())
        self.assertIn(["set-source-mute", LOCAL, "1"], self.calls())

    def test_snapshot_query_failure_prevents_all_audio_mutations(self):
        original = self.audio_state()
        for query in ("get-sink-volume", "get-sink-mute"):
            with self.subTest(query=query):
                self.fail_once(query, INTERNAL)
                self.run_helper("set-sink-levels", INTERNAL, "70", "0", success=False)
                self.assertEqual(self.audio_state(), original)
                self.assertEqual(self.mutations(), [])
                self.assertFalse(self.levels_file.exists())

    def test_pair_failure_rolls_back_and_preserves_original_snapshot(self):
        self.fail_once("set-default-sink", ALTERNATE)
        result = self.run_helper("set-route", LOCAL, ALTERNATE, success=False)
        self.assertEqual(self.server()["default_source"], MONITOR)
        self.assertEqual(self.server()["default_sink"], INTERNAL)
        self.assertEqual(self.routes_file.read_text(), self.route_snapshot())
        self.assertIn("voltou", result.stderr)

    def test_pair_rollback_failure_is_reported_and_can_be_retried(self):
        self.fail_once("set-default-sink", ALTERNATE)
        self.fail_once("set-default-source", MONITOR)
        result = self.run_helper("set-route", LOCAL, ALTERNATE, success=False)
        self.assertEqual(self.server()["default_source"], LOCAL)
        self.assertEqual(self.routes_file.read_text(), self.route_snapshot())
        self.assertIn("reversão", result.stderr)
        self.assertIn("falharam", result.stderr)
        self.run_helper("restore-routes")
        self.assertEqual(self.server()["default_source"], MONITOR)
        self.assertFalse(self.routes_file.exists())

    def test_each_partial_route_restore_failure_preserves_snapshot(self):
        for kind, target in (("source", MONITOR), ("sink", INTERNAL)):
            with self.subTest(kind=kind):
                self.run_helper("set-route", LOCAL, ALTERNATE)
                snapshot = self.routes_file.read_bytes()
                self.fail_once(f"set-default-{kind}", target)
                result = self.run_helper("restore-routes", success=False)
                self.assertEqual(self.routes_file.read_bytes(), snapshot)
                self.assertNotIn("padrão restauradas", result.stdout)
                self.run_helper("restore-routes")
                self.assertFalse(self.routes_file.exists())

    def test_restore_all_attempts_levels_even_when_route_restore_fails(self):
        self.run_helper("set-route", LOCAL, ALTERNATE)
        self.run_helper("set-sink-levels", INTERNAL, "25", "0")
        self.fail_once("set-default-source", MONITOR)
        self.run_helper("restore-all", success=False)
        self.assertTrue(self.routes_file.exists())
        self.assertFalse(self.levels_file.exists())
        self.assert_original_device("sink", INTERNAL)
        self.assertIn(["set-sink-volume", INTERNAL, "32111", "54321"], self.calls())

    def test_invalid_route_snapshots_are_preserved_before_restore_or_new_mutation(self):
        snapshots = ["", "old-format\na\nb\n", self.route_snapshot().rstrip("\n"),
                     f"{ROUTE_HEADER}\nsource\t{MONITOR}\n",
                     self.route_snapshot() + "extra\n",
                     f"{ROUTE_HEADER}\nsource\t\nsink\t{INTERNAL}\n"]
        for contents in snapshots:
            for command in (("restore-routes",), ("set-source", LOCAL)):
                with self.subTest(contents=contents, command=command):
                    self.write_snapshot(self.routes_file, contents)
                    self.run_helper(*command, success=False)
                    self.assertEqual(self.routes_file.read_bytes(), contents.encode())
                    self.assertEqual(self.mutations(), [])

    def test_invalid_level_snapshots_are_preserved_before_restore_or_new_mutation(self):
        row = self.level_row()
        snapshots = ["", "wrong-header\n" + row + "\n", f"{LEVEL_HEADER}\n",
                     f"{LEVEL_HEADER}\n{row}", f"{LEVEL_HEADER}\n{row}\n{row}\n",
                     f"{LEVEL_HEADER}\nsink\t{INTERNAL}\t32111,\t1\n",
                     f"{LEVEL_HEADER}\nsink\t{INTERNAL}\t32111,54321\n",
                     f"{LEVEL_HEADER}\nsink\t{INTERNAL}\t32111,54321\t2\n",
                     f"{LEVEL_HEADER}\n{row}\textra\n"]
        for contents in snapshots:
            for command in (("restore-volumes",), ("set-sink-volume", INTERNAL, "40")):
                with self.subTest(contents=contents, command=command):
                    self.write_snapshot(self.levels_file, contents)
                    self.run_helper(*command, success=False)
                    self.assertEqual(self.levels_file.read_bytes(), contents.encode())
                    self.assertEqual(self.mutations(), [])

    def test_restore_all_continues_after_corrupt_route_snapshot(self):
        self.run_helper("set-sink-levels", INTERNAL, "35", "0")
        self.write_snapshot(self.routes_file, "broken\n")
        self.run_helper("restore-all", success=False)
        self.assertEqual(self.routes_file.read_text(), "broken\n")
        self.assertFalse(self.levels_file.exists())
        self.assert_original_device("sink", INTERNAL)

    def test_missing_device_retains_only_its_pending_level_row(self):
        self.run_helper("set-source-volume", LOCAL, "40")
        self.run_helper("set-sink-levels", INTERNAL, "35", "0")
        state = self.server()
        unplugged = state["sources"].pop(LOCAL)
        self.write_server(state)
        self.run_helper("restore-volumes", success=False)
        self.assertEqual(self.levels_file.read_text(), f"{LEVEL_HEADER}\n{self.level_row('source', LOCAL)}\n")
        self.assert_original_device("sink", INTERNAL)
        state = self.server()
        state["sources"][LOCAL] = unplugged
        self.write_server(state)
        self.run_helper("restore-volumes")
        self.assert_original_device("source", LOCAL)
        self.assertFalse(self.levels_file.exists())

    def test_volume_or_mute_restore_failure_preserves_the_whole_device_row(self):
        for field in ("volume", "mute"):
            with self.subTest(field=field):
                self.run_helper("set-sink-levels", INTERNAL, "35", "0")
                snapshot = self.levels_file.read_bytes()
                values = ("32111", "54321") if field == "volume" else ("1",)
                self.fail_once(f"set-sink-{field}", INTERNAL, *values)
                self.run_helper("restore-volumes", success=False)
                self.assertEqual(self.levels_file.read_bytes(), snapshot)
                self.run_helper("restore-volumes")
                self.assert_original_device("sink", INTERNAL)

    def test_client_capture_blocks_route_commands_before_snapshot_or_mutation(self):
        state = self.server()
        state["source_outputs"] = [[25, 2, 47, "fake-driver", "float32le 1ch 48000Hz"]]
        self.write_server(state)
        original = self.audio_state()
        for command in (("set-source", LOCAL), ("set-sink", ALTERNATE),
                        ("set-route", LOCAL, ALTERNATE), ("tablet-profile",)):
            with self.subTest(command=command):
                self.run_helper(*command, success=False)
                self.assertEqual(self.audio_state(), original)
                self.assertEqual(self.mutations(), [])
                self.assertFalse(self.routes_file.exists())
        self.write_snapshot(self.routes_file, self.route_snapshot())
        self.run_helper("restore-routes", success=False)
        self.assertEqual(self.routes_file.read_text(), self.route_snapshot())
        self.assertEqual(self.mutations(), [])

    def test_module_source_output_with_no_client_does_not_block_routes(self):
        state = self.server()
        state["source_outputs"] = [[25, 4, "-", "module-remap-source", "float32le 1ch 48000Hz"]]
        self.write_server(state)
        self.run_helper("set-source", TABLET)
        self.assertEqual(self.server()["default_source"], TABLET)

    def test_failed_capture_query_blocks_routes_without_guessing(self):
        self.fail_once("list", "short", "source-outputs")
        self.run_helper("set-source", TABLET, success=False)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.routes_file.exists())

    def test_readonly_commands_use_only_queries_and_create_no_xdg_state(self):
        original = self.audio_state()
        for command in ("help", "--help", "status", "inventory", "tablet-source", "scrcpy-status", "obex-status"):
            with self.subTest(command=command):
                self.run_helper(command)
                self.assertFalse(self.xdg.exists())
                self.assertEqual(self.audio_state(), original)
        self.assertEqual(self.mutations(), [])
        allowed = {"info", "get-default-source", "get-default-sink", "list"}
        self.assertTrue(all(argv[0] in allowed for argv in self.calls()))

    def test_real_tablet_name_description_and_monitor_default_are_distinguished(self):
        result = self.run_helper("tablet-source")
        self.assertEqual(result.stdout.strip(), TABLET)
        inventory = self.run_helper("inventory").stdout.splitlines()
        self.assertIn("source\ttablet_mic\tMicrofone_Tablet\t0", inventory)
        self.assertTrue(any(line.startswith(f"source\t{MONITOR}\t") and line.endswith("\t1") for line in inventory))
        self.assertIn("tablet_mic", self.run_helper("scrcpy-status").stdout)
        self.assertEqual(self.server()["default_source"], MONITOR)
        self.run_helper("tablet-profile")
        self.assertEqual(self.server()["default_source"], TABLET)
        self.assertEqual(self.server()["default_sink"], INTERNAL)
        self.assertEqual(self.mutations(), [["set-default-source", TABLET]])

    def test_unique_description_fallback_excludes_monitor_and_ambiguous_sources(self):
        state = self.server()
        tablet = state["sources"].pop(TABLET)
        state["sources"]["tablet_custom"] = tablet
        state["sources"][MONITOR]["description"] = "Microfone_Tablet"
        self.write_server(state)
        self.assertEqual(self.run_helper("tablet-source").stdout.strip(), "tablet_custom")
        state = self.server()
        state["sources"]["tablet_other"] = copy.deepcopy(tablet)
        self.write_server(state)
        self.run_helper("tablet-source", success=False)
        state = self.server()
        del state["sources"]["tablet_custom"]
        del state["sources"]["tablet_other"]
        self.write_server(state)
        self.run_helper("tablet-profile", success=False)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.xdg.exists())

    def test_legacy_tablet_name_is_supported_but_real_name_has_priority(self):
        state = self.server()
        state["sources"]["Microfone_Tablet"] = copy.deepcopy(state["sources"][TABLET])
        self.write_server(state)
        self.assertEqual(self.run_helper("tablet-source").stdout.strip(), TABLET)
        state = self.server()
        del state["sources"][TABLET]
        self.write_server(state)
        self.assertEqual(self.run_helper("tablet-source").stdout.strip(), "Microfone_Tablet")

    def test_percent_validation_and_leading_zero_are_decimal(self):
        for percent in ("151", "-1", "1.5", "none"):
            with self.subTest(percent=percent):
                self.run_helper("set-sink-volume", INTERNAL, percent, success=False)
                self.assertEqual(self.mutations(), [])
                self.assertFalse(self.levels_file.exists())
        self.run_helper("set-sink-volume", INTERNAL, "08")
        self.assertIn(["set-sink-volume", INTERNAL, "8%"], self.calls())

    def concurrent_helpers(self, first, second, delayed_query):
        state = self.server()
        state["delays"].append({"argv": list(delayed_query), "remaining": 1, "seconds": 0.7})
        self.write_server(state)
        processes = []
        try:
            processes.append(subprocess.Popen([self.bash, str(HELPER), *first], env=self.env,
                                              cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 10
            while list(delayed_query) not in self.calls():
                if processes[0].poll() is not None:
                    stdout, stderr = processes[0].communicate()
                    self.fail(f"first helper stopped before delay: {stdout}\n{stderr}")
                if time.monotonic() >= deadline:
                    self.fail("fake pactl did not reach the expected concurrency barrier")
                time.sleep(0.01)
            processes.append(subprocess.Popen([self.bash, str(HELPER), *second], env=self.env,
                                              cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_concurrent_distinct_level_mutations_keep_both_original_rows(self):
        self.concurrent_helpers(("set-sink-levels", INTERNAL, "20", "0"),
                                ("set-source-volume", LOCAL, "50"),
                                ("get-sink-volume", INTERNAL))
        self.assertEqual(set(self.levels_file.read_text().splitlines()[1:]),
                         {self.level_row(), self.level_row("source", LOCAL)})
        self.run_helper("restore-volumes")
        self.assert_original_device("sink", INTERNAL)
        self.assert_original_device("source", LOCAL)

    def test_concurrent_same_device_keeps_first_state_once(self):
        self.concurrent_helpers(("set-sink-levels", INTERNAL, "20", "0"),
                                ("set-sink-levels", INTERNAL, "60", "1"),
                                ("get-sink-volume", INTERNAL))
        self.assertEqual(self.levels_file.read_text(), f"{LEVEL_HEADER}\n{self.level_row()}\n")
        self.run_helper("restore-volumes")
        self.assert_original_device("sink", INTERNAL)

    def test_concurrent_route_mutations_keep_initial_pair(self):
        self.concurrent_helpers(("set-source", LOCAL), ("set-sink", ALTERNATE), ("get-default-source",))
        self.assertEqual(self.routes_file.read_text(), self.route_snapshot())
        self.run_helper("restore-routes")
        self.assertEqual(self.server()["default_source"], MONITOR)
        self.assertEqual(self.server()["default_sink"], INTERNAL)


if __name__ == "__main__":
    unittest.main()
