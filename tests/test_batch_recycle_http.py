from __future__ import annotations

import email.message
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_recycle_service import (  # noqa: E402
    CANVAS_UNAVAILABLE_MESSAGE,
    BatchRecycleError,
)
from workflow_production_http_server import (  # noqa: E402
    WorkflowProductionHttpServer,
)


class _FakeRecycleService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def recycle(self, batch_id: str, *, source_entry: str) -> Any:
        self.calls.append((batch_id, source_entry))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(batch_id=batch_id, status="recycled")


class BatchRecycleHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temp.name) / "repo"
        (self.repository_root / "manifests").mkdir(parents=True)
        self.recycle_service = _FakeRecycleService()
        self.server = WorkflowProductionHttpServer(
            repository_root=self.repository_root,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            batch_recycle_service=self.recycle_service,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _post(
        self,
        path: str,
        *,
        payload: Any = None,
        raw_body: bytes | None = None,
        token: str | None = "canvas-token",
        origin: str | None = "http://localhost:3000",
        content_type: str = "application/json",
        extra_content_types: tuple[str, ...] = (),
        transfer_encoding: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        body = (
            raw_body
            if raw_body is not None
            else json.dumps({} if payload is None else payload).encode("utf-8")
        )
        headers = email.message.Message()
        if token is not None:
            headers["x-canvas-agent-token"] = token
        if origin is not None:
            headers["Origin"] = origin
        headers["Content-Type"] = content_type
        for extra_content_type in extra_content_types:
            headers["Content-Type"] = extra_content_type
        headers["Content-Length"] = str(len(body))
        if transfer_encoding is not None:
            headers["Transfer-Encoding"] = transfer_encoding

        handler_type = self.server._handler_type()
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        statuses: list[int] = []
        response_headers: dict[str, str] = {}
        handler.send_response = statuses.append
        handler.send_header = response_headers.__setitem__
        handler.end_headers = lambda: None

        handler.do_POST()

        self.assertEqual(1, len(statuses))
        return (
            statuses[0],
            response_headers,
            json.loads(handler.wfile.getvalue().decode("utf-8")),
        )

    def test_success_returns_only_safe_receipt_and_uses_workbench_source(self) -> None:
        batch_id = "杯子_一次性验收"
        encoded = urllib.parse.quote(batch_id, safe="")

        status, headers, body = self._post(
            f"/batch-recycle/{encoded}",
            content_type="application/json; charset=utf-8",
        )

        self.assertEqual(200, status)
        self.assertEqual(
            {
                "ok": True,
                "batchId": batch_id,
                "status": "recycled",
                "message": "批次已移入回收站。",
            },
            body,
        )
        self.assertEqual([(batch_id, "workbench")], self.recycle_service.calls)
        self.assertEqual(
            "http://localhost:3000",
            headers["Access-Control-Allow-Origin"],
        )
        serialized = json.dumps(body, ensure_ascii=False)
        for forbidden in (
            "workspace",
            "requestId",
            "deletedCanvasNodes",
            "resumed",
            str(self.repository_root),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_authentication_happens_before_service_invocation(self) -> None:
        status, _headers, body = self._post(
            "/batch-recycle/cup",
            token=None,
        )

        self.assertEqual(401, status)
        self.assertEqual(
            {"ok": False, "error": "request_rejected"},
            body,
        )
        self.assertEqual([], self.recycle_service.calls)

    def test_route_and_body_are_strict_and_reject_client_control_fields(self) -> None:
        cases = (
            ("/batch-recycle/cup/extra", {}, 404, {}),
            ("/batch-recycle/cup?source=cli", {}, 404, {}),
            ("/batch-recycle/cup%2Fother", {}, 400, {}),
            ("/batch-recycle/cup", [], 400, {}),
            (
                "/batch-recycle/cup",
                {"source_entry": "cli"},
                400,
                {},
            ),
            (
                "/batch-recycle/cup",
                {"workspace_target": "D:/elsewhere"},
                400,
                {},
            ),
            (
                "/batch-recycle/cup",
                {},
                400,
                {"transfer_encoding": "chunked"},
            ),
            (
                "/batch-recycle/cup",
                {},
                415,
                {"content_type": "application/jsonx"},
            ),
            (
                "/batch-recycle/cup",
                {},
                415,
                {"content_type": "application/json-patch+json"},
            ),
            (
                "/batch-recycle/cup",
                {},
                415,
                {"extra_content_types": ("application/json",)},
            ),
            (
                "/batch-recycle/cup",
                {},
                400,
                {"raw_body": b"{"},
            ),
        )

        for path, payload, expected_status, request_options in cases:
            with self.subTest(path=path, payload=payload):
                status, _headers, body = self._post(
                    path,
                    payload=payload,
                    **request_options,
                )
                self.assertEqual(expected_status, status)
                self.assertEqual(
                    {"ok": False, "error": "request_rejected"},
                    body,
                )
        self.assertEqual([], self.recycle_service.calls)

    def test_domain_error_keeps_human_message_and_unknown_error_is_redacted(self) -> None:
        self.recycle_service.error = BatchRecycleError(
            CANVAS_UNAVAILABLE_MESSAGE,
            code="canvas_unavailable",
        )
        domain_status, _headers, domain_body = self._post(
            "/batch-recycle/cup"
        )
        self.assertEqual(409, domain_status)
        self.assertEqual(
            {
                "ok": False,
                "error": "batch_recycle_rejected",
                "batchId": "cup",
                "message": CANVAS_UNAVAILABLE_MESSAGE,
            },
            domain_body,
        )

        secret = "secret-token D:/private/workspace"
        self.recycle_service.error = RuntimeError(secret)
        unknown_status, _headers, unknown_body = self._post(
            "/batch-recycle/cup"
        )
        self.assertEqual(500, unknown_status)
        self.assertEqual(
            {"ok": False, "error": "request_rejected"},
            unknown_body,
        )
        self.assertNotIn(
            secret,
            json.dumps(unknown_body, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
