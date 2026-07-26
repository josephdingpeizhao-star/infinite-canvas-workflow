from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_recycle_state import (  # noqa: E402
    BatchLifecycleReadError,
    read_batch_lifecycle,
)


class BatchRecycleStateTests(unittest.TestCase):
    def _journal(self, root: Path, events: list[dict]) -> Path:
        path = root / "batch.events.jsonl"
        if events:
            path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
                encoding="utf-8",
            )
        return path

    def test_missing_journal_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = read_batch_lifecycle(Path(tmp) / "missing.events.jsonl")
            self.assertEqual("open", state.status)

    def test_recycled_event_freezes_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._journal(Path(tmp), [{"event": "batch_recycled", "request_id": "r1"}])
            state = read_batch_lifecycle(path)
            self.assertTrue(state.recycled)
            self.assertEqual("r1", state.active_recycled_event["request_id"])

    def test_restore_after_recycle_thaws_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._journal(
                Path(tmp),
                [{"event": "batch_recycled"}, {"event": "batch_restored"}],
            )
            self.assertEqual("open", read_batch_lifecycle(path).status)

    def test_second_recycle_after_restore_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._journal(
                Path(tmp),
                [
                    {"event": "batch_recycled", "request_id": "old"},
                    {"event": "batch_restored"},
                    {"event": "batch_recycled", "request_id": "new"},
                ],
            )
            state = read_batch_lifecycle(path)
            self.assertEqual("new", state.active_recycled_event["request_id"])

    def test_closed_event_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._journal(Path(tmp), [{"event": "batch_acceptance_closed"}])
            state = read_batch_lifecycle(path)
            self.assertTrue(state.closed)
            self.assertEqual("closed", state.status)

    def test_recycled_status_has_priority_over_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._journal(
                Path(tmp),
                [
                    {"event": "batch_acceptance_closed"},
                    {"event": "batch_recycled"},
                ],
            )
            self.assertEqual("recycled", read_batch_lifecycle(path).status)

    def test_malformed_unrelated_line_does_not_erase_valid_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.events.jsonl"
            path.write_text(
                '{"event":"batch_recycled"}\nnot-json\n',
                encoding="utf-8",
            )
            self.assertTrue(read_batch_lifecycle(path).recycled)

    def test_unreadable_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.events.jsonl"
            path.mkdir()
            with self.assertRaises(BatchLifecycleReadError):
                read_batch_lifecycle(path)


if __name__ == "__main__":
    unittest.main()
