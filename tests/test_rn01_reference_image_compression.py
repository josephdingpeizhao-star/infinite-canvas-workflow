from __future__ import annotations

import base64
import hashlib
import json
import random
import sys
import tempfile
import unittest
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
import reference_image_compression as compression_module  # noqa: E402
from openai_image_executor import HttpResponse, OpenAIImageExecutor  # noqa: E402
from reference_image_compression import (  # noqa: E402
    CompressedReference,
    compress_reference_image,
)


REFERENCE_LIMIT_ENV = "OPENAI_IMAGE_REFERENCE_MAX_BYTES"
DEFAULT_REFERENCE_LIMIT = 2_000_000
BOUNDARY = "rn01-boundary"


@lru_cache(maxsize=1)
def _large_jpeg_bytes() -> bytes:
    size = (2400, 2400)
    pixels = random.Random(20260730).randbytes(size[0] * size[1] * 3)
    image = Image.frombytes("RGB", size, pixels)
    output = BytesIO()
    image.save(output, format="JPEG", quality=100, subsampling=0)
    return output.getvalue()


def _transparent_png_bytes() -> bytes:
    size = (1024, 1024)
    pixels = random.Random(524).randbytes(size[0] * size[1] * 3)
    image = Image.frombytes("RGB", size, pixels).convert("RGBA")
    alpha = Image.new("L", size, 0)
    ImageDraw.Draw(alpha).rectangle((256, 256, 767, 767), fill=255)
    image.putalpha(alpha)
    output = BytesIO()
    image.save(output, format="PNG", compress_level=0)
    return output.getvalue()


@lru_cache(maxsize=1)
def _response_png_bytes() -> bytes:
    image = Image.new("RGB", (1, 1), "white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _success_response() -> HttpResponse:
    payload = {
        "data": [{"b64_json": base64.b64encode(_response_png_bytes()).decode("ascii")}],
        "output_format": "png",
        "usage": {},
    }
    return HttpResponse(
        status=200,
        headers={"x-request-id": "rn01-request"},
        body=json.dumps(payload).encode("utf-8"),
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            }
        )
        return _success_response()


def _executor(
    transport: RecordingTransport,
    *,
    environment: dict[str, str] | None = None,
) -> OpenAIImageExecutor:
    values = {"OPENAI_API_KEY": "fake-unit-test-key"}
    if environment is not None:
        values.update(environment)
    return OpenAIImageExecutor(
        ExecutorContext(manifest={}, environment=values),
        transport=transport,
        boundary_factory=lambda: BOUNDARY,
    )


def _image_parts(body: bytes) -> list[tuple[bytes, bytes]]:
    marker = f"--{BOUNDARY}".encode("ascii")
    image_parts: list[tuple[bytes, bytes]] = []
    for part in body.split(marker):
        if b'name="image[]"' not in part:
            continue
        headers, payload = part.split(b"\r\n\r\n", 1)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        image_parts.append((headers, payload))
    return image_parts


class ReferenceImageCompressionTest(unittest.TestCase):
    def test_large_jpeg_compresses_without_mutating_source_or_file(self) -> None:
        source = _large_jpeg_bytes()
        source_snapshot = bytes(source)
        self.assertGreater(len(source), DEFAULT_REFERENCE_LIMIT)

        with tempfile.TemporaryDirectory() as tmp:
            original_path = Path(tmp) / "large.JPEG"
            original_path.write_bytes(source)
            original_sha = hashlib.sha256(original_path.read_bytes()).hexdigest()

            compressed = compress_reference_image(
                source,
                max_bytes=DEFAULT_REFERENCE_LIMIT,
                filename=original_path.name,
            )
            repeated = compress_reference_image(
                source,
                max_bytes=DEFAULT_REFERENCE_LIMIT,
                filename=original_path.name,
            )

            self.assertEqual(original_sha, hashlib.sha256(original_path.read_bytes()).hexdigest())

        self.assertEqual(source_snapshot, source)
        self.assertEqual(compressed, repeated)
        self.assertTrue(compressed.compressed)
        self.assertLessEqual(compressed.sent_bytes, DEFAULT_REFERENCE_LIMIT)
        self.assertEqual(compressed.sent_bytes, len(compressed.data))
        self.assertEqual("large.jpg", compressed.filename)
        self.assertEqual("image/jpeg", compressed.content_type)
        self.assertIn(compressed.quality, (85, 80, 75, 70))
        self.assertIn(compressed.long_edge, (2048, 1600, 1280))

    def test_small_unparseable_data_bypasses_unavailable_pillow(self) -> None:
        source = b"not-a-real-image"
        with mock.patch(
            "reference_image_compression._load_pillow",
            side_effect=ModuleNotFoundError("Pillow unavailable"),
        ) as load_pillow:
            result = compress_reference_image(
                source,
                max_bytes=len(source),
                filename="small.png",
            )

        load_pillow.assert_not_called()
        self.assertIs(source, result.data)
        self.assertEqual(len(source), result.sent_bytes)
        self.assertFalse(result.compressed)
        self.assertIsNone(result.quality)
        self.assertIsNone(result.long_edge)

    def test_transparency_is_flattened_and_exif_orientation_is_applied(self) -> None:
        source = _transparent_png_bytes()
        max_bytes = 500_000
        self.assertGreater(len(source), max_bytes)

        result = compress_reference_image(
            source,
            max_bytes=max_bytes,
            filename="alpha.png",
        )

        self.assertTrue(result.compressed)
        self.assertEqual("alpha.jpg", result.filename)
        self.assertEqual("image/jpeg", result.content_type)
        self.assertLessEqual(result.sent_bytes, max_bytes)
        with Image.open(BytesIO(result.data)) as decoded:
            rgb = decoded.convert("RGB")
            corners = (
                (0, 0),
                (rgb.width - 1, 0),
                (0, rgb.height - 1),
                (rgb.width - 1, rgb.height - 1),
            )
            for corner in corners:
                self.assertTrue(all(channel >= 250 for channel in rgb.getpixel(corner)))

        color_key_image = Image.new("RGB", (32, 32), (255, 0, 0))
        color_key_png = BytesIO()
        color_key_image.save(
            color_key_png,
            format="PNG",
            transparency=(255, 0, 0),
            compress_level=0,
        )
        color_key_result = compress_reference_image(
            color_key_png.getvalue(),
            max_bytes=1_000,
            filename="color-key.png",
        )
        with Image.open(BytesIO(color_key_result.data)) as decoded:
            self.assertTrue(all(channel >= 250 for channel in decoded.convert("RGB").getpixel((0, 0))))

        premultiplied = Image.new("RGBA", (1, 1), (255, 0, 0, 0)).convert("RGBa")
        flattened = compression_module._rgb_on_white(premultiplied, Image)
        self.assertEqual((255, 255, 255), flattened.getpixel((0, 0)))

        oriented_image = Image.new("RGB", (40, 20), "blue")
        exif = Image.Exif()
        exif[274] = 6
        exif[37510] = b"x" * 10_000
        oriented_jpeg = BytesIO()
        oriented_image.save(oriented_jpeg, format="JPEG", quality=95, exif=exif)
        oriented_result = compress_reference_image(
            oriented_jpeg.getvalue(),
            max_bytes=2_000,
            filename="oriented.jpg",
        )
        with Image.open(BytesIO(oriented_result.data)) as decoded:
            self.assertEqual((20, 40), decoded.size)

    def test_oversized_unparseable_data_fails_closed(self) -> None:
        with self.assertRaises(ExecutorExecutionError) as ctx:
            compress_reference_image(
                b"not-image" * 60_000,
                max_bytes=500_000,
                filename="broken.jpg",
            )

        self.assertEqual("参考图无法解析为图像，已停止", str(ctx.exception))

    def test_image_that_cannot_fit_after_all_attempts_fails_closed(self) -> None:
        encoded_qualities: list[int] = []

        def oversized_encoding(_image: object, quality: int) -> bytes:
            encoded_qualities.append(quality)
            return b"xx"

        with mock.patch(
            "reference_image_compression._resize_long_edge",
            wraps=compression_module._resize_long_edge,
        ) as resize_long_edge, mock.patch(
            "reference_image_compression._encode_jpeg",
            side_effect=oversized_encoding,
        ):
            with self.assertRaises(ExecutorExecutionError) as ctx:
                compress_reference_image(
                    _large_jpeg_bytes(),
                    max_bytes=1,
                    filename="large.jpg",
                )

        self.assertEqual("参考图压缩后仍超过上限，已停止", str(ctx.exception))
        self.assertEqual(
            [2048, 1600, 1280],
            [call.args[2] for call in resize_long_edge.call_args_list],
        )
        self.assertEqual([85, 80, 75, 70] * 3, encoded_qualities)

    def test_executor_compresses_large_reference_and_reports_metadata(self) -> None:
        source = _large_jpeg_bytes()
        transport = RecordingTransport()
        executor = _executor(transport)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "reference.JPEG"
            output = root / "render.png"
            reference.write_bytes(source)
            original_sha = hashlib.sha256(reference.read_bytes()).hexdigest()

            result = executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="preserve product",
                        output_path=output,
                        reference_images=(reference,),
                    ),
                )
            )

            self.assertEqual(original_sha, hashlib.sha256(reference.read_bytes()).hexdigest())
            self.assertTrue(output.is_file())
            self.assertNotIn(str(root), repr(result.metadata["reference_images"]))

        self.assertEqual(1, len(transport.calls))
        call = transport.calls[0]
        self.assertEqual("https://api.openai.com/v1/images/edits", call["url"])
        parts = _image_parts(call["body"])
        self.assertEqual(1, len(parts))
        headers, image_data = parts[0]
        self.assertLessEqual(len(image_data), DEFAULT_REFERENCE_LIMIT)
        self.assertIn(b'filename="reference.jpg"', headers)
        self.assertIn(b"Content-Type: image/jpeg", headers)

        records = result.metadata["reference_images"]
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(
            {
                "name",
                "original_bytes",
                "sent_bytes",
                "compressed",
                "quality",
                "long_edge",
            },
            set(record),
        )
        self.assertEqual("reference.jpg", record["name"])
        self.assertEqual(len(source), record["original_bytes"])
        self.assertEqual(len(image_data), record["sent_bytes"])
        self.assertTrue(record["compressed"])
        self.assertIn(record["quality"], (85, 80, 75, 70))
        self.assertIn(record["long_edge"], (2048, 1600, 1280))

    def test_executor_passes_small_reference_through_unchanged(self) -> None:
        source = b"small-not-an-image"
        transport = RecordingTransport()
        executor = _executor(transport)

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "reference_image_compression._load_pillow",
            side_effect=ModuleNotFoundError("Pillow unavailable"),
        ) as load_pillow:
            root = Path(tmp)
            reference = root / "small.png"
            reference.write_bytes(source)
            result = executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="preserve product",
                        output_path=root / "render.png",
                        reference_images=(reference,),
                    ),
                )
            )

        load_pillow.assert_not_called()
        parts = _image_parts(transport.calls[0]["body"])
        self.assertEqual(1, len(parts))
        headers, image_data = parts[0]
        self.assertIn(b'filename="small.png"', headers)
        self.assertIn(b"Content-Type: image/png", headers)
        self.assertEqual(source, image_data)
        self.assertEqual(
            [
                {
                    "name": "small.png",
                    "original_bytes": len(source),
                    "sent_bytes": len(source),
                    "compressed": False,
                    "quality": None,
                    "long_edge": None,
                }
            ],
            result.metadata["reference_images"],
        )

    def test_default_reference_limit_and_text_only_metadata(self) -> None:
        transport = RecordingTransport()
        executor = _executor(transport)
        self.assertEqual(DEFAULT_REFERENCE_LIMIT, executor.reference_image_max_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            result = executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="product",
                        output_path=Path(tmp) / "render.png",
                    ),
                )
            )

        self.assertEqual([], result.metadata["reference_images"])
        self.assertEqual("https://api.openai.com/v1/images/generations", transport.calls[0]["url"])

    def test_reference_limit_environment_validation(self) -> None:
        for value in ("", "abc", "499999", "20000001", "500000.0", "0500000"):
            with self.subTest(invalid=value):
                transport = RecordingTransport()
                with self.assertRaises(ExecutorExecutionError) as ctx:
                    _executor(transport, environment={REFERENCE_LIMIT_ENV: value})
                self.assertIn(REFERENCE_LIMIT_ENV, str(ctx.exception))
                self.assertEqual([], transport.calls)

        for value in ("500000", "20000000"):
            with self.subTest(valid=value):
                executor = _executor(
                    RecordingTransport(),
                    environment={REFERENCE_LIMIT_ENV: value},
                )
                self.assertEqual(int(value), executor.reference_image_max_bytes)

    def test_executor_invariant_rejects_oversized_part_when_compression_is_bypassed(self) -> None:
        source = _large_jpeg_bytes()
        transport = RecordingTransport()
        executor = _executor(transport)
        bypassed = CompressedReference(
            data=source,
            filename="reference.jpg",
            content_type="image/jpeg",
            original_bytes=len(source),
            sent_bytes=1,
            compressed=True,
            quality=85,
            long_edge=2048,
        )

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "openai_image_executor.compress_reference_image",
            return_value=bypassed,
        ):
            root = Path(tmp)
            reference = root / "reference.jpg"
            output = root / "render.png"
            reference.write_bytes(source)
            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(
                            prompt="preserve product",
                            output_path=output,
                            reference_images=(reference,),
                        ),
                    )
                )
            self.assertFalse(output.exists())

        self.assertEqual("参考图发送字节超过上限，已停止", str(ctx.exception))
        self.assertEqual([], transport.calls)


if __name__ == "__main__":
    unittest.main()
