from __future__ import annotations

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
        self.calls = 0

    def execute(self, _request):
        self.calls += 1
        return ExecutionResult(detail="ok", outputs=(self.output,), provider=self.name)


class ProductionRenderObserverTest(unittest.TestCase):
    @staticmethod
    def _write_detail(path: Path, *, width: int = 128, height: int = 192, ordinal: int = 2) -> None:
        write_placeholder_png(path, width=width, height=height, kind="detail", ordinal=ordinal)

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
                    on_output=seen.append,
                )
                wrapped.execute(ExecutionRequest(step="renders"))
            self.assertEqual(["main_01", "detail_01"], [item.config_id for item in seen])

    def test_three_by_four_detail_is_byte_identical_and_emits_no_padding_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_01.png"
            self._write_detail(path, width=120, height=160, ordinal=1)
            before = path.read_bytes()
            seen = []
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                on_output=seen.append,
            )

            wrapped.execute(ExecutionRequest(step="renders"))

            self.assertEqual(before, path.read_bytes())
            self.assertEqual(["detail_01"], [item.config_id for item in seen])

    def test_unusual_detail_and_non_square_main_are_streamed_byte_identically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            seen = []
            for name, kind in (("main_01.png", "main"), ("detail_01.png", "detail")):
                path = root / name
                write_placeholder_png(path, width=43, height=64, kind=kind, ordinal=1)
                before = path.read_bytes()
                wrapped = observer.ProductionRenderObserverExecutor(
                    FakeImageExecutor(path),
                    batch_id="cup",
                    on_output=seen.append,
                    expected_ids=(name.removesuffix(".png"),),
                )

                wrapped.execute(ExecutionRequest(step="renders"))

                self.assertEqual(before, path.read_bytes())
            self.assertEqual(["main_01", "detail_01"], [item.config_id for item in seen])
            self.assertEqual([(43, 64), (43, 64)], [(item.width, item.height) for item in seen])

    def test_bad_png_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "detail_01.png"
            path.write_bytes(b"not-a-png")
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                on_output=lambda _item: self.fail("bad PNG must not be streamed"),
                expected_ids=("detail_01",),
            )

            with self.assertRaisesRegex(ValueError, "正式图片不是有效 PNG"):
                wrapped.execute(ExecutionRequest(step="renders"))

    def test_output_outside_registered_ids_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_02.png"
            write_placeholder_png(path, width=43, height=64, kind="main", ordinal=2)
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                on_output=lambda _item: self.fail("unregistered output must not be streamed"),
                expected_ids=("main_01",),
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "渲染结果不在当前批次登记图位中。",
            ):
                wrapped.execute(ExecutionRequest(step="renders"))


if __name__ == "__main__":
    unittest.main()
