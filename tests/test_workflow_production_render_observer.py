from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class GeneratingImageExecutor(FakeImageExecutor):
    def __init__(self, output: Path, *, width: int, height: int):
        super().__init__(output)
        self.width = width
        self.height = height
        self.original_bytes = b""

    def execute(self, request):
        write_placeholder_png(
            self.output,
            width=self.width,
            height=self.height,
            kind="detail",
            ordinal=2,
        )
        self.original_bytes = self.output.read_bytes()
        return super().execute(request)


class EmptyImageExecutor:
    name = "empty-image"

    def __init__(self):
        self.calls = 0

    def execute(self, _request):
        self.calls += 1
        return ExecutionResult(detail="ok", provider=self.name)


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
                    audit_root=root / "audit",
                    on_output=seen.append,
                )
                wrapped.execute(ExecutionRequest(step="renders"))
            self.assertEqual(["main_01", "detail_01"], [item.config_id for item in seen])

    def test_two_by_three_detail_is_audited_auto_padded_and_streamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_02.png"
            delegate = GeneratingImageExecutor(path, width=1024, height=1536)
            seen = []
            events = []
            wrapped = observer.ProductionRenderObserverExecutor(
                delegate,
                batch_id="cup",
                audit_root=root / "audit",
                renders_root=root,
                on_output=seen.append,
                on_auto_padded=events.append,
            )
            wrapped.execute(ExecutionRequest(step="renders"))

            original_hash = hashlib.sha256(delegate.original_bytes).hexdigest()
            audit = root / "audit" / "render_originals" / "detail_02.png"
            self.assertTrue(audit.is_file())
            self.assertEqual(original_hash, hashlib.sha256(audit.read_bytes()).hexdigest())
            padded_artifact = observer.artifact_from_path("cup", path)
            self.assertEqual((1152, 1536), (padded_artifact.width, padded_artifact.height))
            self.assertEqual(["detail_02"], [item.config_id for item in seen])
            self.assertEqual(
                [
                    {
                        "config_id": "detail_02",
                        "original_sha256": original_hash,
                        "original_width": 1024,
                        "original_height": 1536,
                        "padded_width": 1152,
                        "padded_height": 1536,
                    }
                ],
                events,
            )

            Image, ImageFilter = observer._load_pillow()
            with Image.open(io.BytesIO(delegate.original_bytes)) as original:
                original.load()
                with Image.open(path) as padded:
                    padded.load()
                    self.assertEqual(
                        original.tobytes(),
                        padded.crop((64, 0, 1088, 1536)).tobytes(),
                    )
                    expected_left = (
                        original.crop((0, 0, 24, 1536))
                        .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                        .resize((64, 1536), Image.Resampling.LANCZOS)
                        .filter(ImageFilter.GaussianBlur(radius=18))
                    )
                    expected_right = (
                        original.crop((1000, 0, 1024, 1536))
                        .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                        .resize((64, 1536), Image.Resampling.LANCZOS)
                        .filter(ImageFilter.GaussianBlur(radius=18))
                    )
                    self.assertEqual(expected_left.tobytes(), padded.crop((0, 0, 64, 1536)).tobytes())
                    self.assertEqual(expected_right.tobytes(), padded.crop((1088, 0, 1152, 1536)).tobytes())

    def test_three_by_four_detail_is_byte_identical_and_emits_no_padding_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_01.png"
            self._write_detail(path, width=120, height=160, ordinal=1)
            before = path.read_bytes()
            seen = []
            events = []
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                audit_root=root / "audit",
                on_output=seen.append,
                on_auto_padded=events.append,
            )

            wrapped.execute(ExecutionRequest(step="renders"))

            self.assertEqual(before, path.read_bytes())
            self.assertEqual(["detail_01"], [item.config_id for item in seen])
            self.assertEqual([], events)

    def test_square_detail_still_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_02.png"
            self._write_detail(path, width=128, height=128)
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                audit_root=root / "audit",
                on_output=lambda _item: self.fail("invalid detail must not be projected"),
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "详情图比例既不是 3:4 也不是受控的 2:3，已停止。",
            ):
                wrapped.execute(ExecutionRequest(step="renders"))

    def test_non_square_main_still_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_01.png"
            write_placeholder_png(path, width=128, height=192, kind="main", ordinal=1)
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                audit_root=root / "audit",
                on_output=lambda _item: self.fail("invalid main must not be projected"),
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "主图不是正方形，已停止且不会自动重试。",
            ):
                wrapped.execute(ExecutionRequest(step="renders"))

    def test_sweep_reuses_matching_audit_without_recopying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            renders = root / "outputs" / "renders"
            path = renders / "detail_02.png"
            self._write_detail(path)
            audit = root / "audit" / "render_originals" / path.name
            audit.parent.mkdir(parents=True)
            audit.write_bytes(path.read_bytes())
            audit_stat = audit.stat()
            delegate = EmptyImageExecutor()
            seen = []
            events = []
            wrapped = observer.ProductionRenderObserverExecutor(
                delegate,
                batch_id="cup",
                audit_root=root / "audit",
                renders_root=renders,
                on_output=seen.append,
                on_auto_padded=events.append,
            )

            wrapped.execute(ExecutionRequest(step="renders"))

            self.assertEqual(1, delegate.calls)
            self.assertEqual((144, 192), observer.read_png_dimensions(path))
            self.assertEqual(["detail_02"], [item.config_id for item in seen])
            self.assertEqual(1, len(events))
            self.assertEqual(audit_stat.st_mtime_ns, audit.stat().st_mtime_ns)

    def test_sweep_stops_before_delegate_when_audit_has_different_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            renders = root / "outputs" / "renders"
            path = renders / "detail_02.png"
            self._write_detail(path)
            original = path.read_bytes()
            audit = root / "audit" / "render_originals" / path.name
            self._write_detail(audit, ordinal=3)
            delegate = EmptyImageExecutor()
            events = []
            wrapped = observer.ProductionRenderObserverExecutor(
                delegate,
                batch_id="cup",
                audit_root=root / "audit",
                renders_root=renders,
                on_output=lambda _item: self.fail("conflicting audit must stop"),
                on_auto_padded=events.append,
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "尺寸审计目录已有同名但不同内容的原图，已停止。",
            ):
                wrapped.execute(ExecutionRequest(step="renders"))

            self.assertEqual(0, delegate.calls)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], events)

    def test_sweep_creates_missing_audit_before_padding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            renders = root / "outputs" / "renders"
            path = renders / "detail_02.png"
            self._write_detail(path)
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            delegate = EmptyImageExecutor()
            wrapped = observer.ProductionRenderObserverExecutor(
                delegate,
                batch_id="cup",
                audit_root=root / "audit",
                renders_root=renders,
                on_output=lambda _item: None,
                on_auto_padded=lambda _event: None,
            )

            wrapped.execute(ExecutionRequest(step="renders"))

            audit = root / "audit" / "render_originals" / path.name
            self.assertEqual(original_hash, hashlib.sha256(audit.read_bytes()).hexdigest())
            self.assertEqual((144, 192), observer.read_png_dimensions(path))

    def test_missing_pillow_preserves_old_stop_behavior_after_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_02.png"
            self._write_detail(path)
            original = path.read_bytes()
            wrapped = observer.ProductionRenderObserverExecutor(
                FakeImageExecutor(path),
                batch_id="cup",
                audit_root=root / "audit",
                on_output=lambda _item: self.fail("missing Pillow must stop"),
            )

            with mock.patch.object(observer, "_load_pillow", side_effect=ImportError):
                with self.assertRaisesRegex(
                    ExecutorExecutionError,
                    "详情图返回 2:3，供应端原图已审计保留，等待人工扩边批准。",
                ):
                    wrapped.execute(ExecutionRequest(step="renders"))

            audit = root / "audit" / "render_originals" / path.name
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(original, audit.read_bytes())

    def test_auto_padding_is_byte_deterministic(self) -> None:
        outputs = []
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            for tmp in (first_tmp, second_tmp):
                root = Path(tmp)
                (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
                path = root / "detail_02.png"
                self._write_detail(path)
                wrapped = observer.ProductionRenderObserverExecutor(
                    FakeImageExecutor(path),
                    batch_id="cup",
                    audit_root=root / "audit",
                    on_output=lambda _item: None,
                    on_auto_padded=lambda _event: None,
                )
                wrapped.execute(ExecutionRequest(step="renders"))
                outputs.append(path.read_bytes())

        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
