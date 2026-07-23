"""Observe each real render without changing image-production internals."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from executor_contract import ExecutionRequest, ExecutionResult, Executor, ExecutorExecutionError
from workflow_production_projection import (
    WorkflowProductionArtifact,
    artifact_from_path,
    read_png_dimensions,
)


_TWO_BY_THREE_STOP_MESSAGE = "详情图返回 2:3，供应端原图已审计保留，等待人工扩边批准。"
_AUTO_PAD_STRIP_WIDTH = 24
_AUTO_PAD_BLUR_RADIUS = 18


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_original(source: Path, audit_root: Path) -> Path:
    target = audit_root / "render_originals" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    try:
        with target.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if _sha256(target) != hashlib.sha256(data).hexdigest():
            raise ExecutorExecutionError("尺寸审计目录已有同名但不同内容的原图，已停止。") from None
    return target


def _load_pillow() -> tuple[Any, Any]:
    from PIL import Image, ImageFilter

    return Image, ImageFilter


def _auto_pad_detail(
    artifact: WorkflowProductionArtifact,
    audit_root: Path,
) -> tuple[WorkflowProductionArtifact, dict[str, str | int]]:
    _audit_original(artifact.path, audit_root)
    try:
        Image, ImageFilter = _load_pillow()
    except ImportError:
        raise ExecutorExecutionError(_TWO_BY_THREE_STOP_MESSAGE) from None

    target_width_numerator = artifact.height * 3
    if target_width_numerator % 4:
        raise ExecutorExecutionError(_TWO_BY_THREE_STOP_MESSAGE)
    target_width = target_width_numerator // 4
    total_padding = target_width - artifact.width
    if total_padding <= 0 or total_padding % 2:
        raise ExecutorExecutionError(_TWO_BY_THREE_STOP_MESSAGE)
    side_padding = total_padding // 2

    temporary = artifact.path.with_name(
        f".{artifact.path.name}.{uuid.uuid4().hex}.auto-pad.tmp"
    )
    try:
        with Image.open(artifact.path) as original:
            original.load()
            strip_width = min(_AUTO_PAD_STRIP_WIDTH, artifact.width)
            left = (
                original.crop((0, 0, strip_width, artifact.height))
                .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                .resize((side_padding, artifact.height), Image.Resampling.LANCZOS)
                .filter(ImageFilter.GaussianBlur(radius=_AUTO_PAD_BLUR_RADIUS))
            )
            right = (
                original.crop(
                    (
                        artifact.width - strip_width,
                        0,
                        artifact.width,
                        artifact.height,
                    )
                )
                .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                .resize((side_padding, artifact.height), Image.Resampling.LANCZOS)
                .filter(ImageFilter.GaussianBlur(radius=_AUTO_PAD_BLUR_RADIUS))
            )
            padded = Image.new(original.mode, (target_width, artifact.height))
            padded.paste(left, (0, 0))
            padded.paste(original, (side_padding, 0))
            padded.paste(right, (side_padding + artifact.width, 0))
            padded.save(temporary, format="PNG", optimize=False, compress_level=6)

        if read_png_dimensions(temporary) != (target_width, artifact.height):
            raise ExecutorExecutionError("详情图自动扩边后不是精确 3:4，已停止。")
        os.replace(temporary, artifact.path)
    finally:
        if temporary.exists():
            temporary.unlink()

    padded_artifact = artifact_from_path(artifact.batch_id, artifact.path)
    if padded_artifact.width * 4 != padded_artifact.height * 3:
        raise ExecutorExecutionError("详情图自动扩边后不是精确 3:4，已停止。")
    return padded_artifact, {
        "config_id": artifact.config_id,
        "original_sha256": artifact.sha256,
        "original_width": artifact.width,
        "original_height": artifact.height,
        "padded_width": padded_artifact.width,
        "padded_height": padded_artifact.height,
    }


def _validate_ratio(
    artifact: WorkflowProductionArtifact,
    audit_root: Path,
) -> tuple[WorkflowProductionArtifact, dict[str, str | int] | None]:
    width, height = artifact.width, artifact.height
    if artifact.kind == "main":
        if width != height:
            raise ExecutorExecutionError("主图不是正方形，已停止且不会自动重试。")
        return artifact, None
    if width * 4 == height * 3:
        return artifact, None
    if width * 3 == height * 2:
        return _auto_pad_detail(artifact, audit_root)
    raise ExecutorExecutionError("详情图比例既不是 3:4 也不是受控的 2:3，已停止。")


class ProductionRenderObserverExecutor:
    """Wrap one provider executor and notify after each accepted disk file."""

    name = "workflow-production-observer"

    def __init__(
        self,
        delegate: Executor,
        *,
        batch_id: str,
        audit_root: Path,
        on_output: Callable[[WorkflowProductionArtifact], None],
        renders_root: Path | None = None,
        on_auto_padded: Callable[[Mapping[str, str | int]], None] | None = None,
    ) -> None:
        self.delegate = delegate
        self.batch_id = batch_id
        self.audit_root = audit_root
        self.renders_root = renders_root
        self.on_output = on_output
        self.on_auto_padded = on_auto_padded or (lambda _record: None)
        self._sweep_complete = False

    def _accept_padded(
        self,
        artifact: WorkflowProductionArtifact,
    ) -> WorkflowProductionArtifact:
        accepted, record = _validate_ratio(artifact, self.audit_root)
        if record is not None:
            self.on_auto_padded(record)
        return accepted

    def _sweep_existing_details(self) -> None:
        if self.renders_root is None:
            return
        for index in range(1, 9):
            path = self.renders_root / f"detail_{index:02d}.png"
            if not path.is_file():
                continue
            artifact = artifact_from_path(self.batch_id, path)
            accepted, record = _validate_ratio(artifact, self.audit_root)
            if record is None:
                continue
            self.on_auto_padded(record)
            self.on_output(accepted)

    def prepare(self) -> None:
        if not self._sweep_complete:
            self._sweep_existing_details()
            self._sweep_complete = True

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.prepare()
        result = self.delegate.execute(request)
        for path in result.outputs:
            artifact = artifact_from_path(self.batch_id, path)
            self.on_output(self._accept_padded(artifact))
        return result
