"""Zero-cost workflow demo executor that writes real placeholder PNG files.

This adapter is deliberately limited to workspaces carrying ``.canvas_demo``.
It never calls a model or network service and it never overwrites an existing
run.  Each completed file is reported only after an atomic local rename.
"""

from __future__ import annotations

import binascii
import os
import re
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from executor_contract import ExecutionRequest, ExecutionResult, ExecutorContext, ExecutorExecutionError


DEMO_MARKER = ".canvas_demo"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
MAIN_COUNT = 6
DETAIL_COUNT = 8
TOTAL_COUNT = MAIN_COUNT + DETAIL_COUNT
DEFAULT_FRAME_DELAY_SECONDS = 2.5


@dataclass(frozen=True)
class WorkflowDemoArtifact:
    path: Path
    index: int
    total: int
    kind: str
    ordinal: int
    width: int
    height: int


def require_demo_workspace(root: Path) -> Path:
    resolved = root.resolve()
    if not (resolved / DEMO_MARKER).is_file():
        raise ExecutorExecutionError(f"拒绝演示写盘：缺少 {DEMO_MARKER} 安全标记")
    return resolved


def require_demo_write_path(root: Path, target: Path) -> Path:
    resolved_root = require_demo_workspace(root)
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ExecutorExecutionError(f"拒绝演示写盘：目标路径越界：{resolved_target}")
    return resolved_target


def _find_marked_workspace(target: Path) -> Path:
    resolved = target.resolve()
    for candidate in (resolved.parent, *resolved.parents):
        if (candidate / DEMO_MARKER).is_file():
            return candidate
    raise ExecutorExecutionError(f"拒绝演示写盘：缺少 {DEMO_MARKER} 安全标记")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _placeholder_png_bytes(*, width: int, height: int, kind: str, ordinal: int) -> bytes:
    if width <= 0 or height <= 0:
        raise ExecutorExecutionError("演示 PNG 尺寸无效")
    if kind not in {"main", "detail"}:
        raise ExecutorExecutionError(f"演示 PNG 类别无效：{kind}")

    if kind == "main":
        background = (220, 235, 255)
        accent = (37, 99, 235)
    else:
        background = (239, 231, 255)
        accent = (124, 58, 237)
    border = (51, 65, 85)
    light = (248, 250, 252)
    raw = bytearray()
    border_size = max(8, width // 45)
    band_top = height * 42 // 100
    band_bottom = height * 58 // 100
    marker_width = max(12, width // 28)
    marker_gap = max(8, width // 60)
    marker_total = ordinal * marker_width + max(0, ordinal - 1) * marker_gap
    marker_left = max(border_size * 2, (width - marker_total) // 2)
    marker_top = height * 68 // 100
    marker_bottom = height * 78 // 100

    for y in range(height):
        row = bytearray(bytes(background) * width)
        if y < border_size or y >= height - border_size:
            row[:] = bytes(border) * width
        else:
            row[: border_size * 3] = bytes(border) * border_size
            row[(width - border_size) * 3 :] = bytes(border) * border_size
            if band_top <= y < band_bottom:
                row[border_size * 3 : (width - border_size) * 3] = bytes(light) * (width - border_size * 2)
            if marker_top <= y < marker_bottom:
                for marker in range(ordinal):
                    left = marker_left + marker * (marker_width + marker_gap)
                    right = min(width - border_size, left + marker_width)
                    row[left * 3 : right * 3] = bytes(accent) * max(0, right - left)
        raw.append(0)
        raw.extend(row)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    label = f"Canvas demo {kind} {ordinal:02d}".encode("latin-1")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", b"Title\x00" + label)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + _png_chunk(b"IEND", b"")
    )


def write_placeholder_png(path: Path, *, width: int, height: int, kind: str, ordinal: int) -> None:
    workspace_root = _find_marked_workspace(path)
    target = require_demo_write_path(workspace_root, path)
    if target.exists():
        raise ExecutorExecutionError(f"拒绝覆盖已有演示图片：{target.name}")
    require_demo_write_path(workspace_root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    require_demo_write_path(workspace_root, temporary)
    if temporary.exists():
        temporary.unlink()
    try:
        temporary.write_bytes(_placeholder_png_bytes(width=width, height=height, kind=kind, ordinal=ordinal))
        if read_png_dimensions(temporary) != (width, height):
            raise ExecutorExecutionError(f"演示 PNG 校验失败：{target.name}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise ExecutorExecutionError(f"不是有效 PNG：{path.name}")
    return struct.unpack(">II", header[16:24])


class WorkflowDemoExecutor:
    """Create one isolated 6-main + 8-detail placeholder run."""

    name = "workflow-demo"

    def __init__(self, context: ExecutorContext, *, sleep: Callable[[float], None] = time.sleep):
        workspace = context.manifest.get("workspace") or {}
        root = workspace.get("root")
        if not root:
            raise ValueError("manifest.workspace.root 缺失，workflow-demo 执行器无法定位工作区")
        self.workspace_root = Path(str(root))
        self.context = context
        self.sleep = sleep

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.step != "renders":
            raise ExecutorExecutionError("workflow-demo 只接受 renders")

        metadata = dict(request.metadata or {})
        run_id = str(metadata.get("run_id") or "")
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ExecutorExecutionError("演示请求编号无效")
        on_output = metadata.get("on_output")
        should_cancel = metadata.get("should_cancel")
        if on_output is not None and not callable(on_output):
            raise ExecutorExecutionError("演示输出回调无效")
        if should_cancel is not None and not callable(should_cancel):
            raise ExecutorExecutionError("演示中断检查无效")

        root = require_demo_workspace(self.workspace_root)
        render_values = ((self.context.manifest.get("outputs") or {}).get("renders") or [])
        if not isinstance(render_values, list) or len(render_values) != 1:
            raise ExecutorExecutionError("demo manifest 必须声明唯一 renders 目录")
        renders_root = require_demo_write_path(root, Path(str(render_values[0])))
        run_dir = require_demo_write_path(root, renders_root / run_id)
        if run_dir.exists():
            raise ExecutorExecutionError("演示请求编号已存在，不会重复执行")
        if should_cancel and should_cancel():
            raise ExecutorExecutionError("演示已中断，未创建新文件")
        run_dir.mkdir(parents=True, exist_ok=False)

        outputs: list[Path] = []
        specs = [
            *(('main', ordinal, 720, 720) for ordinal in range(1, MAIN_COUNT + 1)),
            *(('detail', ordinal, 720, 960) for ordinal in range(1, DETAIL_COUNT + 1)),
        ]
        try:
            for index, (kind, ordinal, width, height) in enumerate(specs, start=1):
                if should_cancel and should_cancel():
                    raise ExecutorExecutionError("演示已中断，已经完成的图片仍然保留")
                filename = f"{kind}_{ordinal:02d}.png"
                target = require_demo_write_path(root, run_dir / filename)
                write_placeholder_png(target, width=width, height=height, kind=kind, ordinal=ordinal)
                artifact = WorkflowDemoArtifact(
                    path=target,
                    index=index,
                    total=TOTAL_COUNT,
                    kind=kind,
                    ordinal=ordinal,
                    width=width,
                    height=height,
                )
                outputs.append(target)
                if on_output:
                    on_output(artifact)
                if index < TOTAL_COUNT:
                    self.sleep(DEFAULT_FRAME_DELAY_SECONDS)
        except ExecutorExecutionError:
            raise
        except KeyboardInterrupt as exc:
            raise ExecutorExecutionError("演示已中断，已经完成的图片仍然保留") from exc
        except Exception as exc:
            raise ExecutorExecutionError("演示执行失败，已经完成的图片仍然保留") from exc

        return ExecutionResult(
            detail=f"已生成 {len(outputs)} 张演示占位图",
            outputs=tuple(outputs),
            provider=self.name,
            metadata={"run_id": run_id, "main_count": MAIN_COUNT, "detail_count": DETAIL_COUNT},
        )
