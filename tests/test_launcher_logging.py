import tempfile
import unittest
from pathlib import Path

from launcher.logging_utils import rotate_child_log


class LauncherLoggingTests(unittest.TestCase):
    def test_oversized_child_log_rotates_with_bounded_backups(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "agent.log"
            log.write_bytes(b"current-log")
            log.with_name("agent.log.1").write_bytes(b"previous-one")
            log.with_name("agent.log.2").write_bytes(b"previous-two")

            rotate_child_log(log, max_bytes=4, backups=2)

            self.assertFalse(log.exists())
            self.assertEqual(log.with_name("agent.log.1").read_bytes(), b"current-log")
            self.assertEqual(log.with_name("agent.log.2").read_bytes(), b"previous-one")
            self.assertFalse(log.with_name("agent.log.3").exists())

    def test_small_child_log_is_kept_in_place(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "web.log"
            log.write_bytes(b"ok")

            rotate_child_log(log, max_bytes=4, backups=2)

            self.assertEqual(log.read_bytes(), b"ok")
            self.assertFalse(log.with_name("web.log.1").exists())


if __name__ == "__main__":
    unittest.main()
