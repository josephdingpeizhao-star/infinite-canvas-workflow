import json
import tempfile
import unittest
from pathlib import Path

from launcher.process_control import ProcessRecord, matches_identity
from launcher.state_store import StateFileError, StateStore


class LauncherStateTests(unittest.TestCase):
    def test_state_round_trip_uses_the_requested_path(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher" / "launcher_state.json"
            store = StateStore(path)
            state = {
                "version": 1,
                "started_at": "2026-07-27T00:00:00Z",
                "services": {
                    "agent": {
                        "pid": 123,
                        "ports": [17371],
                        "command_summary": "bun.exe run canvas-agent dev",
                        "command_digest": "abc",
                        "started_at": "2026-07-27T00:00:00Z",
                    }
                },
            }

            store.write(state)

            self.assertEqual(store.read(), state)
            self.assertTrue(path.is_file())

    def test_corrupt_state_is_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher_state.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(StateFileError, "状态文件已损坏"):
                StateStore(path).read()

    def test_state_with_invalid_pid_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher_state.json"
            path.write_text(
                json.dumps({"version": 1, "started_at": "now", "services": {"agent": {"pid": 0}}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StateFileError, "PID"):
                StateStore(path).read()

    def test_pid_reuse_for_an_unrelated_process_never_matches(self):
        expected = [["infinite-canvas/canvas-agent", "src/index.ts"]]
        reused = ProcessRecord(
            pid=123,
            executable_path=r"C:\Windows\System32\notepad.exe",
            command_line="notepad.exe notes.txt",
        )
        owned = ProcessRecord(
            pid=124,
            executable_path=r"C:\Program Files\nodejs\node.exe",
            command_line=r"node D:\dev\infinite-canvas\canvas-agent\src\index.ts",
        )

        self.assertFalse(matches_identity(reused, expected))
        self.assertTrue(matches_identity(owned, expected))


if __name__ == "__main__":
    unittest.main()
