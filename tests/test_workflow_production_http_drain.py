from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_production_http_server import (  # noqa: E402
    DRAIN_CAP,
    WorkflowProductionHttpServer,
    _drain_unread_request_body,
)


class _UnusedAssistant:
    def submit(self, _question: str, _history: list[dict[str, str]]) -> dict[str, object]:
        raise AssertionError("超限请求不得进入助手")


class _ShortReadStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


class WorkflowProductionHttpDrainTest(unittest.TestCase):
    def test_oversized_assistant_body_returns_413_five_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository_root = Path(temp) / "repo"
            (repository_root / "manifests").mkdir(parents=True)
            server = WorkflowProductionHttpServer(
                repository_root=repository_root,
                token="canvas-token",
                host="127.0.0.1",
                port=0,
                assistant_service=_UnusedAssistant(),
            )
            server.start()
            try:
                endpoint = (
                    f"http://127.0.0.1:{server.bound_port}"
                    "/readonly-assistant/questions"
                )
                body = json.dumps(
                    {"question": "问" * 20_000, "history": []},
                    ensure_ascii=False,
                ).encode("utf-8")
                for _ in range(5):
                    request = urllib.request.Request(
                        endpoint,
                        data=body,
                        method="POST",
                        headers={
                            "Origin": "http://localhost:3000",
                            "x-canvas-agent-token": "canvas-token",
                            "content-type": "application/json",
                        },
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(caught.exception.code, 413)
                    caught.exception.read()
            finally:
                server.stop()

    def test_style_upload_rejection_returns_400_after_bounded_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository_root = Path(temp) / "repo"
            (repository_root / "manifests").mkdir(parents=True)
            server = WorkflowProductionHttpServer(
                repository_root=repository_root,
                token="canvas-token",
                host="127.0.0.1",
                port=0,
            )
            server.start()
            try:
                request = urllib.request.Request(
                    (
                        f"http://127.0.0.1:{server.bound_port}"
                        "/style-reference-intake/batch/card/files/bad%2Fname"
                    ),
                    data=b"x" * 60_000,
                    method="POST",
                    headers={
                        "Origin": "http://localhost:3000",
                        "x-canvas-agent-token": "canvas-token",
                        "content-type": "application/octet-stream",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.read()
            finally:
                server.stop()

    def test_drain_is_bounded_and_skips_declarations_above_cap(self) -> None:
        stream = io.BytesIO(b"x" * (DRAIN_CAP + 1))
        self.assertEqual(
            _drain_unread_request_body(
                stream,
                declared_length=DRAIN_CAP,
                consumed_length=0,
            ),
            DRAIN_CAP,
        )
        self.assertEqual(stream.tell(), DRAIN_CAP)

        oversized = io.BytesIO(b"x" * (DRAIN_CAP + 1))
        self.assertEqual(
            _drain_unread_request_body(
                oversized,
                declared_length=DRAIN_CAP + 1,
                consumed_length=0,
            ),
            0,
        )
        self.assertEqual(oversized.tell(), 0)

        partially_consumed = io.BytesIO(b"x" * 5)
        self.assertEqual(
            _drain_unread_request_body(
                partially_consumed,
                declared_length=12,
                consumed_length=7,
            ),
            5,
        )
        self.assertEqual(partially_consumed.tell(), 5)

    def test_drain_tolerates_a_short_body(self) -> None:
        stream = _ShortReadStream([b"ab", b"c", b""])
        self.assertEqual(
            _drain_unread_request_body(
                stream,
                declared_length=10,
                consumed_length=0,
            ),
            3,
        )
        self.assertEqual(stream.read_sizes, [10, 8, 7])

    def test_drain_ignores_zero_declared_length(self) -> None:
        stream = io.BytesIO(b"x")
        self.assertEqual(
            _drain_unread_request_body(
                stream,
                declared_length=0,
                consumed_length=0,
            ),
            0,
        )
        self.assertEqual(stream.tell(), 0)


if __name__ == "__main__":
    unittest.main()
