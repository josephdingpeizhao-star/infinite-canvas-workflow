from __future__ import annotations

import email.message
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from project_deletion_service import (  # noqa: E402
    MAX_PROJECT_DELETION_REQUEST_ID_LENGTH,
    ProjectDeletionError,
    valid_project_deletion_request_id,
)
from workflow_production_http_server import (  # noqa: E402
    MAX_PROJECT_DELETION_BODY_BYTES,
    WorkflowProductionHttpServer,
)


class FakeProjectDeletionService:
    def __init__(self) -> None:
        self.preview_calls: list[Any] = []
        self.execute_calls: list[Any] = []
        self.preview_error: ProjectDeletionError | None = None
        self.preview_request_id = "delete-request-0001"

    def preview(self, batch_ids: Any) -> dict[str, Any]:
        self.preview_calls.append(batch_ids)
        if self.preview_error:
            raise self.preview_error
        return {
            "ok": True,
            "requestId": self.preview_request_id,
            "batches": [
                {
                    "batchId": batch_id,
                    "status": "closed",
                    "closed": True,
                    "delivered": False,
                    "recycled": False,
                    "requiresTypedConfirmation": True,
                }
                for batch_id in batch_ids
            ],
        }

    def execute(self, request_id: Any, batch_ids: Any) -> dict[str, Any]:
        self.execute_calls.append((request_id, batch_ids))
        return {
            "ok": False,
            "requestId": request_id,
            "status": "stopped",
            "batches": [
                {
                    "batchId": batch_id,
                    "status": "failed" if index == 0 else "not_started",
                    "message": (
                        "批次有任务正在运行。" if index == 0 else "尚未开始。"
                    ),
                }
                for index, batch_id in enumerate(batch_ids)
            ],
        }


class ProjectDeletionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "manifests").mkdir(parents=True)
        self.service = FakeProjectDeletionService()
        self.server = WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            project_deletion_service=self.service,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _post(
        self,
        path: str,
        payload: Any,
        *,
        token: str | None = "canvas-token",
        origin: str | None = "http://localhost:3000",
        content_type: str = "application/json",
        transfer_encoding: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = email.message.Message()
        if token is not None:
            headers["x-canvas-agent-token"] = token
        if origin is not None:
            headers["Origin"] = origin
        headers["Content-Type"] = content_type
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
        handler.send_response = statuses.append
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None

        handler.do_POST()

        self.assertEqual(1, len(statuses))
        return statuses[0], json.loads(handler.wfile.getvalue().decode("utf-8"))

    def test_preview_and_execute_use_exact_safe_contracts(self) -> None:
        status, preview = self._post(
            "/project-deletion/preview",
            {"batchIds": ["cup"]},
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {"ok", "requestId", "batches"},
            set(preview),
        )
        self.assertEqual([["cup"]], self.service.preview_calls)

        status, execute = self._post(
            "/project-deletion/execute",
            {"requestId": "delete-request-0001", "batchIds": ["cup", "next"]},
        )
        self.assertEqual(200, status)
        self.assertFalse(execute["ok"])
        self.assertEqual("stopped", execute["status"])
        self.assertEqual(
            [("delete-request-0001", ["cup", "next"])],
            self.service.execute_calls,
        )
        self.assertNotIn("path", json.dumps(execute))

    @staticmethod
    def _structured_request_id(batch_ids: list[str]) -> str:
        return "pd1." + ".".join(
            hashlib.sha256(f"{index}:{batch_id}".encode("utf-8")).hexdigest()
            for index, batch_id in enumerate(batch_ids)
        )

    def _assert_preview_execute_round_trip(self, batch_ids: list[str]) -> int:
        request_id = self._structured_request_id(batch_ids)
        self.assertTrue(valid_project_deletion_request_id(request_id))
        self.assertLessEqual(
            len(request_id),
            MAX_PROJECT_DELETION_REQUEST_ID_LENGTH,
        )
        self.service.preview_request_id = request_id

        status, preview = self._post(
            "/project-deletion/preview",
            {"batchIds": batch_ids},
        )
        self.assertEqual(200, status)
        self.assertEqual(request_id, preview["requestId"])
        self.assertEqual(batch_ids, [item["batchId"] for item in preview["batches"]])

        execute_payload = {
            "requestId": preview["requestId"],
            "batchIds": batch_ids,
        }
        execute_bytes = json.dumps(
            execute_payload,
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertLessEqual(
            len(execute_bytes),
            MAX_PROJECT_DELETION_BODY_BYTES,
        )
        status, execute = self._post(
            "/project-deletion/execute",
            execute_payload,
        )
        self.assertEqual(200, status)
        self.assertEqual(request_id, execute["requestId"])
        self.assertEqual((request_id, batch_ids), self.service.execute_calls[-1])
        return len(execute_bytes)

    def test_three_batch_preview_execute_contract(self) -> None:
        body_bytes = self._assert_preview_execute_round_trip(
            ["batch_001", "batch_002", "batch_003"],
        )
        self.assertEqual(268, body_bytes)

    def test_hundred_batch_preview_execute_contract_exceeds_old_cap(self) -> None:
        batch_ids = []
        for index in range(100):
            prefix = f"批次{index:03d}_"
            batch_ids.append(prefix + ("甲" * (120 - len(prefix))))

        body_bytes = self._assert_preview_execute_round_trip(batch_ids)

        self.assertEqual(6503, len(self.service.preview_request_id))
        self.assertEqual(42_134, body_bytes)
        self.assertGreater(body_bytes, 16 * 1024)

    def test_project_deletion_body_over_64_kib_is_rejected(self) -> None:
        status, body = self._post(
            "/project-deletion/execute",
            {
                "requestId": "x" * MAX_PROJECT_DELETION_BODY_BYTES,
                "batchIds": ["cup"],
            },
        )

        self.assertEqual(413, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertFalse(self.service.execute_calls)

    def test_extra_fields_bad_origin_and_bad_token_are_rejected(self) -> None:
        status, body = self._post(
            "/project-deletion/preview",
            {"batchIds": ["cup"], "path": "C:/forbidden"},
        )
        self.assertEqual(400, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertFalse(self.service.preview_calls)

        status, _body = self._post(
            "/project-deletion/preview",
            {"batchIds": ["cup"]},
            origin="https://example.com",
        )
        self.assertEqual(403, status)
        status, _body = self._post(
            "/project-deletion/preview",
            {"batchIds": ["cup"]},
            token="wrong",
        )
        self.assertEqual(401, status)

    def test_preview_unknown_is_human_readable_409(self) -> None:
        self.service.preview_error = ProjectDeletionError(
            "找不到这个批次。",
            code="batch_not_found",
            batch_id="missing",
        )
        status, body = self._post(
            "/project-deletion/preview",
            {"batchIds": ["missing"]},
        )
        self.assertEqual(409, status)
        self.assertEqual(
            {
                "ok": False,
                "error": "project_deletion_rejected",
                "batchId": "missing",
                "message": "找不到这个批次。",
            },
            body,
        )


if __name__ == "__main__":
    unittest.main()
