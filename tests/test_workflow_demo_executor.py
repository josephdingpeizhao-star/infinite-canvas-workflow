from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionRequest, ExecutorContext, ExecutorExecutionError  # noqa: E402
from workflow_demo_executor import WorkflowDemoExecutor, read_png_dimensions  # noqa: E402


def manifest_for(root: Path, renders: Path | None = None) -> dict:
    return {
        "product_id": "demo_live",
        "workspace": {"root": str(root), "outputs_root": str(root / "outputs")},
        "outputs": {"renders": [str(renders or root / "outputs" / "renders")]},
    }


class WorkflowDemoExecutorTests(unittest.TestCase):
    def marked_root(self, parent: Path) -> Path:
        root = parent / "demo"
        root.mkdir()
        (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        return root

    def executor(self, root: Path, *, renders: Path | None = None) -> WorkflowDemoExecutor:
        return WorkflowDemoExecutor(
            ExecutorContext(manifest=manifest_for(root, renders), manifest_path=root / "manifests" / "batch_manifest.json"),
            sleep=lambda _seconds: None,
        )

    def test_refuses_every_write_without_canvas_demo_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "unmarked"
            root.mkdir()
            with self.assertRaisesRegex(ExecutorExecutionError, "canvas_demo"):
                self.executor(root).execute(ExecutionRequest(step="renders", metadata={"run_id": "run-safe-001"}))
            self.assertFalse((root / "outputs").exists())

    def test_refuses_output_path_outside_marked_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = self.marked_root(parent)
            outside = parent / "outside"
            with self.assertRaisesRegex(ExecutorExecutionError, "越界"):
                self.executor(root, renders=outside).execute(ExecutionRequest(step="renders", metadata={"run_id": "run-safe-002"}))
            self.assertFalse(outside.exists())

    def test_only_accepts_registered_demo_render_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.marked_root(Path(tmp))
            with self.assertRaisesRegex(ExecutorExecutionError, "只接受 renders"):
                self.executor(root).execute(ExecutionRequest(step="identity", metadata={"run_id": "run-safe-003"}))

    def test_writes_six_main_and_eight_detail_pngs_before_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.marked_root(Path(tmp))
            seen: list[tuple[int, str, str]] = []

            def on_output(artifact) -> None:
                self.assertTrue(artifact.path.is_file())
                self.assertGreater(artifact.path.stat().st_size, 0)
                seen.append((artifact.index, artifact.kind, artifact.path.name))

            result = self.executor(root).execute(
                ExecutionRequest(step="renders", metadata={"run_id": "run-complete-001", "on_output": on_output})
            )

            self.assertEqual(14, len(result.outputs))
            self.assertEqual(list(range(1, 15)), [item[0] for item in seen])
            self.assertEqual([f"main_{index:02d}.png" for index in range(1, 7)], [path.name for path in result.outputs[:6]])
            self.assertEqual([f"detail_{index:02d}.png" for index in range(1, 9)], [path.name for path in result.outputs[6:]])
            self.assertEqual({(720, 720)}, {read_png_dimensions(path) for path in result.outputs[:6]})
            self.assertEqual({(720, 960)}, {read_png_dimensions(path) for path in result.outputs[6:]})
            self.assertFalse(list((root / "outputs").rglob("*.tmp")))

    def test_rerun_uses_new_directory_and_preserves_old_fourteen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.marked_root(Path(tmp))
            executor = self.executor(root)
            first = executor.execute(ExecutionRequest(step="renders", metadata={"run_id": "run-first-001"}))
            first_bytes = {path: path.read_bytes() for path in first.outputs}
            second = executor.execute(ExecutionRequest(step="renders", metadata={"run_id": "run-second-001"}))
            self.assertEqual(28, len(list((root / "outputs" / "renders").rglob("*.png"))))
            self.assertEqual(first_bytes, {path: path.read_bytes() for path in first.outputs})
            self.assertTrue(all("run-second-001" in str(path) for path in second.outputs))

    def test_cancellation_before_start_creates_no_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.marked_root(Path(tmp))
            with self.assertRaisesRegex(ExecutorExecutionError, "中断"):
                self.executor(root).execute(
                    ExecutionRequest(step="renders", metadata={"run_id": "run-cancel-001", "should_cancel": lambda: True})
                )
            self.assertFalse((root / "outputs" / "renders" / "run-cancel-001").exists())

    def test_interruption_preserves_complete_files_and_removes_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.marked_root(Path(tmp))
            produced = 0

            def on_output(_artifact) -> None:
                nonlocal produced
                produced += 1

            with self.assertRaisesRegex(ExecutorExecutionError, "中断"):
                self.executor(root).execute(
                    ExecutionRequest(
                        step="renders",
                        metadata={
                            "run_id": "run-interrupt-001",
                            "on_output": on_output,
                            "should_cancel": lambda: produced >= 3,
                        },
                    )
                )
            run_dir = root / "outputs" / "renders" / "run-interrupt-001"
            self.assertEqual(3, len(list(run_dir.glob("*.png"))))
            self.assertFalse(list(run_dir.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
