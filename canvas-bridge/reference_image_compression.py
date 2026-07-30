"""Deterministic in-memory compression for oversized reference images."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from executor_contract import ExecutorExecutionError


@dataclass(frozen=True)
class CompressedReference:
    data: bytes
    filename: str
    content_type: str
    original_bytes: int
    sent_bytes: int
    compressed: bool
    quality: int | None
    long_edge: int | None


def _load_pillow() -> tuple[Any, Any]:
    from PIL import Image, ImageOps

    return Image, ImageOps


def _rgb_on_white(image: Any, image_module: Any) -> Any:
    bands = image.getbands()
    if "A" in bands or "a" in bands or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = image_module.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _resize_long_edge(image: Any, image_module: Any, long_edge: int) -> Any:
    width, height = image.size
    current_long_edge = max(width, height)
    if current_long_edge <= long_edge:
        return image.copy()
    scale = long_edge / current_long_edge
    resized_size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    return image.resize(resized_size, image_module.Resampling.LANCZOS)


def _encode_jpeg(image: Any, quality: int) -> bytes:
    output = BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def compress_reference_image(
    data: bytes,
    *,
    max_bytes: int,
    filename: str,
) -> CompressedReference:
    original_bytes = len(data)
    if original_bytes <= max_bytes:
        return CompressedReference(
            data=data,
            filename=filename,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            original_bytes=original_bytes,
            sent_bytes=original_bytes,
            compressed=False,
            quality=None,
            long_edge=None,
        )

    try:
        Image, ImageOps = _load_pillow()
        with Image.open(BytesIO(data)) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            rgb_image = _rgb_on_white(oriented, Image)
    except Exception as exc:
        raise ExecutorExecutionError("参考图无法解析为图像，已停止") from exc

    previous_size: tuple[int, int] | None = None
    try:
        for target_long_edge in (2048, 1600, 1280):
            candidate = _resize_long_edge(rgb_image, Image, target_long_edge)
            if candidate.size == previous_size:
                continue
            previous_size = candidate.size
            for quality in (85, 80, 75, 70):
                encoded = _encode_jpeg(candidate, quality)
                if len(encoded) <= max_bytes:
                    return CompressedReference(
                        data=encoded,
                        filename=f"{Path(filename).stem}.jpg",
                        content_type="image/jpeg",
                        original_bytes=original_bytes,
                        sent_bytes=len(encoded),
                        compressed=True,
                        quality=quality,
                        long_edge=max(candidate.size),
                    )
    except Exception as exc:
        raise ExecutorExecutionError("参考图无法解析为图像，已停止") from exc

    raise ExecutorExecutionError("参考图压缩后仍超过上限，已停止")
