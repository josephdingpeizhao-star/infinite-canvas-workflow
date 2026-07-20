from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionRequest, ExecutionResult, ExecutorExecutionError  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
import workflow_production_render_observer as observer  # noqa: E402


class FakeImageExecutor:
    name = "fake-image"

    def __init__(self, output: Path):
        self.output = output

    def execute(self, _request):
        return ExecutionResult(detail="ok", outputs=(self.output,), provider=self.name)


class ProductionRenderObserverTest(unittest.TestCase):
    def test_square_main_and_three_by_four_detail_are_streamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            seen = []
            for name, size in (("main_01.png", (1254, 1254)), ("detail_01.png", (1086, 1448))):
                path = root / name
                write_placeholder_png(path, width=size[0], height=size[1], kind="main", ordinal=1)
                wrapped = observer.ProductionRenderObserverExecutor(
                    FakeImageExecutor(path),
                    batch_id="cup",
                    audit_root=root / "audit",
                    on_output=seen.append,
                )
                wrapped.execute(ExecutionRequest(step="renders"))
            self.assertEqual(["main_01", "detail_01"], [item.config_id for item in seen])

    def test_two_by_three_detail_is_audited_byte_for_byte_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_02.png"
            write_placeholder_png(path, width=1024, height=1536, kind="detail", ordinal=2)
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                audit_root=root / "audit",
                on_output=lambda _item: self.fail("2:3 must not be projected"),
            )
            with self.assertRaises(ExecutorExecutionError):
                wrapped.execute(ExecutionRequest(step="renders"))
            audit = root / "audit" / "render_originals" / "detail_02.png"
            self.assertTrue(audit.is_file())
            self.assertEqual(original_hash, hashlib.sha256(audit.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
