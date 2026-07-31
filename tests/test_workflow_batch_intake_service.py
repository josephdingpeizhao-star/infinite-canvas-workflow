from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import socket
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_intake_controller import (  # noqa: E402
    BatchIntakeGateError,
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from batch_creator import BatchCreationError  # noqa: E402
from workflow_batch_intake_service import (  # noqa: E402
    DEFAULT_UPLOAD_HOST,
    SERVICE_LOCK_NAME,
    SERVICE_OWNER_NAME,
    BatchIntakeServiceLock,
    BatchUploadServer,
    UploadRejected,
    WorkflowBatchIntakeService,
    constant_time_token_matches,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"offline-original-pixels"
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
TOKEN = "local-canvas-token-for-tests"


def confirmed_facts() -> ConfirmedFacts:
    return ConfirmedFacts(
        product_type="餐具",
        height_cm=25,
        main_image_count=6,
        detail_image_count=8,
        handheld_main=2,
        handheld_detail=1,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
    )


def source_image(
    *,
    node_id: str = "image-original-1",
    payload: bytes = PNG_BYTES,
    expected_sha256: str | None = None,
) -> SourceImage:
    return SourceImage(
        node_id=node_id,
        storage_key=f"image:{node_id}",
        name="白底原图.png",
        size=len(payload),
        mime_type="image/png",
        last_modified=1_700_000_000_000,
        expected_sha256=expected_sha256 or hashlib.sha256(payload).hexdigest(),
    )


def intake_request(
    *,
    request_id: str = "req-123456",
    requested_at: int = 10_000,
    sources: tuple[SourceImage, ...] | None = None,
) -> BatchIntakeRequest:
    return BatchIntakeRequest(
        request_id=request_id,
        requested_at=requested_at,
        info_node_id="batch-info-1",
        workflow_node_id="workflow-1",
        facts=confirmed_facts(),
        source_images=sources or (source_image(),),
    )


def info_node(*, status: str = "queued") -> dict:
    return {
        "id": "batch-info-1",
        "type": "batch-info",
        "metadata": {
            "content": "# batch-intake\n# request-id: req-123456\n# requested-at: 10000\nbuild: batch",
            "batchIntake": {"status": status, "requestId": "req-123456", "requestedAt": 10_000},
        },
    }


class FakeClient:
    def __init__(self, nodes: list[dict] | None = None):
        self.state = {"nodes": nodes or [], "connections": []}
        self.applied: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.applied.append(ops)
        return 1


class FakeController:
    def __init__(self, request: BatchIntakeRequest | None = None, error: Exception | None = None):
        self.request = request
        self.error = error
        self.calls: list[dict] = []

    @staticmethod
    def queued_info_nodes(state: dict):
        return tuple(
            node
            for node in state.get("nodes") or []
            if node.get("type") == "batch-info"
            and (node.get("metadata") or {}).get("batchIntake", {}).get("status") == "queued"
        )

    def parse_queued_request(self, state, node, **kwargs):
        self.calls.append({"state": state, "node": node, **kwargs})
        if self.error is not None:
            raise self.error
        if self.request is None:
            raise AssertionError("test controller has no request")
        return self.request


class FakeCreator:
    def __init__(self, state_root: Path, *, product_id: str = "餐具_20260718"):
        self.state_root = state_root
        self.workspace_parent = state_root.parent / "workspace-parent"
        self.workspace_parent.mkdir(exist_ok=True)
        self.product_id = product_id
        self.created: list[tuple[BatchIntakeRequest, tuple[object, ...], tuple[bytes, ...]]] = []

    def product_id_for(self, request: BatchIntakeRequest) -> str:
        return self.product_id

    def create(self, request: BatchIntakeRequest, uploads):
        frozen_uploads = tuple(uploads)
        payloads = tuple(Path(upload.path).read_bytes() for upload in frozen_uploads)
        self.created.append((request, frozen_uploads, payloads))
        return SimpleNamespace(
            request_id=request.request_id,
            product_id=self.product_id,
            image_count=len(frozen_uploads),
            facts=request.facts,
            receipt_dict=lambda: {
                "request_id": request.request_id,
                "product_id": self.product_id,
                "image_count": len(frozen_uploads),
                "facts": request.facts.as_dict(),
            },
        )


def last_batch_intake_update(client: FakeClient) -> dict:
    flattened = [op for batch in client.applied for op in batch]
    updates = [op for op in flattened if op.get("id") == "batch-info-1"]
    if not updates:
        raise AssertionError("no batch-info update")
    return updates[-1]["metadata"]["batchIntake"]


class WorkflowBatchIntakeServiceTests(unittest.TestCase):
    def build_service(
        self,
        root: Path,
        *,
        request: BatchIntakeRequest | None = None,
        controller: FakeController | None = None,
        client: FakeClient | None = None,
        creator: FakeCreator | None = None,
    ) -> tuple[WorkflowBatchIntakeService, FakeClient, FakeCreator, FakeController]:
        state_root = root / "state"
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / ".canvas_batch_intake_state").write_text(
            "canvas-batch-intake-state-v1\n", encoding="utf-8"
        )
        selected_request = request or intake_request()
        selected_controller = controller or FakeController(selected_request)
        selected_client = client or FakeClient(
            [
                info_node(),
                {
                    "id": "workflow-1",
                    "type": "workflow",
                    "metadata": {"workflowDemo": {"status": "queued", "runId": "m1-request"}},
                },
            ]
        )
        selected_creator = creator or FakeCreator(state_root)
        service = WorkflowBatchIntakeService(
            repo_root=ROOT,
            state_root=state_root,
            client=selected_client,
            creator=selected_creator,
            controller=selected_controller,
            clock_ms=lambda: 11_000,
            sleep=lambda _seconds: None,
        )
        return service, selected_client, selected_creator, selected_controller

    def test_poll_accepts_only_batch_info_and_keeps_workflow_demo_metadata_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, client, creator, _controller = self.build_service(Path(tmp))
            service.poll_once()

            self.assertTrue((service._spool_root("req-123456") / ".canvas_batch_intake_request").is_file())
            update = last_batch_intake_update(client)
            self.assertEqual("upload_ready", update["status"])
            self.assertEqual("餐具_20260718", update["batchId"])
            self.assertEqual(["image-original-1"], update["sourceImageNodeIds"])
            flattened = [op for batch in client.applied for op in batch]
            self.assertTrue(all(op.get("id") != "workflow-1" for op in flattened))
            self.assertTrue(all("workflowDemo" not in op.get("metadata", {}) for op in flattened))

    def test_existing_derived_batch_is_rejected_before_spool_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            creator = FakeCreator(state_root)
            (creator.workspace_parent / "餐具_20260718").mkdir()
            service, client, creator, _controller = self.build_service(root, creator=creator)
            service.poll_once()

            self.assertFalse(service._spool_root("req-123456").exists())
            self.assertEqual([], creator.created)
            self.assertEqual("failed", last_batch_intake_update(client)["status"])
            self.assertIn("已经存在", last_batch_intake_update(client)["errorMessage"])

    def test_service_forces_zero_future_tolerance_and_rejects_with_human_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            error = BatchIntakeGateError(
                "future_request",
                "这次登记请求来自未来时间，已经停止。",
                info_node_id="batch-info-1",
            )
            controller = FakeController(error=error)
            service, client, creator, controller = self.build_service(Path(tmp), controller=controller)
            service.poll_once()

            self.assertEqual(0, controller.calls[0]["future_tolerance_ms"])
            self.assertEqual(8_000, controller.calls[0]["max_age_ms"])
            self.assertFalse((service._spool_root("req-123456")).exists())
            self.assertEqual("failed", last_batch_intake_update(client)["status"])
            self.assertIn("未来时间", last_batch_intake_update(client)["errorMessage"])

    def test_corrupt_ledger_stops_service_instead_of_skipping_bad_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            (state_root / "batch_intake_service.events.jsonl").write_text(
                '{"event":"request_received","request_id":"req-ok"}\nnot-json\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "账本"):
                WorkflowBatchIntakeService(
                    repo_root=ROOT,
                    state_root=state_root,
                    client=FakeClient(),
                    creator=FakeCreator(state_root),
                    controller=FakeController(intake_request()),
                )

    def test_restart_ledger_prevents_same_request_from_beginning_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _client, first_creator, _controller = self.build_service(root)
            first.poll_once()
            self.assertTrue(first._spool_root("req-123456").is_dir())

            second_client = FakeClient([info_node()])
            second_creator = FakeCreator(root / "state")
            second = WorkflowBatchIntakeService(
                repo_root=ROOT,
                state_root=root / "state",
                client=second_client,
                creator=second_creator,
                controller=FakeController(intake_request()),
                clock_ms=lambda: 11_000,
                sleep=lambda _seconds: None,
            )
            second.poll_once()
            self.assertFalse(second._spool_root("req-123456").joinpath("unexpected").exists())
            self.assertEqual("failed", last_batch_intake_update(second_client)["status"])
            self.assertIn("已经处理", last_batch_intake_update(second_client)["errorMessage"])

    def test_request_id_contract_accepts_dot_at_minimum_and_rejects_over_64_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dotted = intake_request(request_id="a.bcdef1")
            node = info_node()
            node["metadata"]["batchIntake"]["requestId"] = dotted.request_id
            client = FakeClient([node])
            service, _client, _creator, _controller = self.build_service(
                root,
                request=dotted,
                client=client,
            )
            service.poll_once()
            self.assertTrue(service._spool_root(dotted.request_id).is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            too_long = "r" * 65
            (state_root / "batch_intake_service.events.jsonl").write_text(
                json.dumps({"event": "request_received", "request_id": too_long, "recorded_at": 1}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "账本"):
                WorkflowBatchIntakeService(
                    repo_root=ROOT,
                    state_root=state_root,
                    client=FakeClient(),
                    creator=FakeCreator(state_root),
                    controller=FakeController(intake_request()),
                )

    def test_event_path_replacement_with_symlink_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, _client, creator, _controller = self.build_service(root)
            service.event_path.write_text("", encoding="utf-8")
            original_is_symlink = Path.is_symlink

            def replaced_after_start(path: Path) -> bool:
                if path == service.event_path:
                    return True
                return original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", new=replaced_after_start):
                with self.assertRaisesRegex(RuntimeError, "账本"):
                    service.poll_once()
            self.assertEqual("", service.event_path.read_text(encoding="utf-8"))
            self.assertEqual([], creator.created)

    def test_marker_write_failure_removes_only_new_empty_request_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, client, creator, _controller = self.build_service(root)
            original_open = Path.open

            def fail_request_marker(path: Path, mode="r", *args, **kwargs):
                if path.name == ".canvas_batch_intake_request" and mode == "x":
                    raise OSError("simulated marker failure")
                return original_open(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", new=fail_request_marker):
                service.poll_once()

            self.assertFalse(service._spool_root("req-123456").exists())
            self.assertEqual([], creator.created)
            self.assertEqual("failed", last_batch_intake_update(client)["status"])

    def test_hash_mismatch_blocks_integrity_closes_session_and_never_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, client, creator, _controller = self.build_service(Path(tmp))
            service.poll_once()
            with self.assertRaises(UploadRejected) as caught:
                service.accept_upload(
                    batch_id="餐具_20260718",
                    request_id="req-123456",
                    source_node_id="image-original-1",
                    file_name=urllib.parse.quote("白底原图.png", safe=""),
                    declared_size=len(PNG_BYTES),
                    declared_sha256="0" * 64,
                    declared_last_modified=1_700_000_000_000,
                    content_type="image/png",
                    content_length=len(PNG_BYTES),
                    stream=io.BytesIO(PNG_BYTES),
                )
            self.assertEqual("integrity_blocked", caught.exception.code)
            self.assertEqual([], creator.created)
            self.assertFalse(service._spool_root("req-123456").exists())
            self.assertEqual("integrity_blocked", last_batch_intake_update(client)["status"])
            with self.assertRaises(UploadRejected):
                service.accept_upload(
                    batch_id="餐具_20260718",
                    request_id="req-123456",
                    source_node_id="image-original-1",
                    file_name=urllib.parse.quote("白底原图.png", safe=""),
                    declared_size=len(PNG_BYTES),
                    declared_sha256=PNG_SHA256,
                    declared_last_modified=1_700_000_000_000,
                    content_type="image/png",
                    content_length=len(PNG_BYTES),
                    stream=io.BytesIO(PNG_BYTES),
                )

    def test_creator_final_hash_mismatch_is_integrity_blocked_and_not_retryable(self) -> None:
        class FinalHashMismatchCreator(FakeCreator):
            def __init__(self, state_root: Path):
                super().__init__(state_root)
                self.create_calls = 0

            def create(self, request: BatchIntakeRequest, uploads):
                self.create_calls += 1
                raise BatchCreationError(
                    "integrity_mismatch",
                    "浏览器保存的图片与磁盘原图不一致，已立即停止且未创建批次。",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            creator = FinalHashMismatchCreator(state_root)
            service, client, creator, _controller = self.build_service(root, creator=creator)
            service.poll_once()
            server = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=0)
            server.start()
            self.addCleanup(server.stop)
            connection = http.client.HTTPConnection(DEFAULT_UPLOAD_HOST, server.bound_port, timeout=3)
            connection.request(
                "POST",
                "/batch-intake/%E9%A4%90%E5%85%B7_20260718/req-123456/files/image-original-1",
                body=PNG_BYTES,
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Canvas-Agent-Token": TOKEN,
                    "X-Canvas-File-Name": urllib.parse.quote("白底原图.png", safe=""),
                    "X-Canvas-File-Size": str(len(PNG_BYTES)),
                    "X-Canvas-File-Sha256": PNG_SHA256,
                    "X-Canvas-File-Last-Modified": "1700000000000",
                    "Content-Type": "image/png",
                    "Content-Length": str(len(PNG_BYTES)),
                },
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()

            self.assertEqual(409, response.status)
            self.assertEqual("integrity_blocked", body["errorCode"])
            self.assertNotIn("sha256", body)
            self.assertEqual(1, creator.create_calls)
            self.assertEqual("integrity_blocked", service.sessions["req-123456"].status)
            self.assertEqual("integrity_blocked", last_batch_intake_update(client)["status"])
            self.assertFalse(service._spool_root("req-123456").exists())

    def test_integrity_ledger_failure_cannot_hide_canvas_hard_stop_or_protocol_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, client, creator, _controller = self.build_service(Path(tmp))
            service.poll_once()
            service.event_path.unlink()
            service.event_path.mkdir()

            with self.assertRaises(UploadRejected) as caught:
                service.accept_upload(
                    batch_id="餐具_20260718",
                    request_id="req-123456",
                    source_node_id="image-original-1",
                    file_name=urllib.parse.quote("白底原图.png", safe=""),
                    declared_size=len(PNG_BYTES),
                    declared_sha256="0" * 64,
                    declared_last_modified=1_700_000_000_000,
                    content_type="image/png",
                    content_length=len(PNG_BYTES),
                    stream=io.BytesIO(PNG_BYTES),
                )

            self.assertEqual("integrity_blocked", caught.exception.code)
            self.assertTrue(service.stopping)
            self.assertEqual([], creator.created)
            self.assertEqual("integrity_blocked", service.sessions["req-123456"].status)
            self.assertEqual("integrity_blocked", last_batch_intake_update(client)["status"])

    def test_mime_magic_and_size_are_checked_before_commit(self) -> None:
        invalid_magic = b"not-a-png".ljust(len(PNG_BYTES), b"x")
        cases = (
            ("image/jpeg", len(PNG_BYTES), PNG_BYTES, PNG_BYTES, "文件类型"),
            ("image/png", len(invalid_magic), invalid_magic, invalid_magic, "文件内容"),
            ("image/png", len(PNG_BYTES) - 1, PNG_BYTES[:-1], PNG_BYTES, "文件大小"),
        )
        for content_type, content_length, payload, expected_source_payload, expected_message in cases:
            with self.subTest(content_type=content_type, content_length=content_length, payload=payload[:4]):
                with tempfile.TemporaryDirectory() as tmp:
                    request = intake_request(sources=(source_image(payload=expected_source_payload),))
                    service, _client, creator, _controller = self.build_service(Path(tmp), request=request)
                    service.poll_once()
                    with self.assertRaisesRegex(UploadRejected, expected_message):
                        service.accept_upload(
                            batch_id="餐具_20260718",
                            request_id="req-123456",
                            source_node_id="image-original-1",
                            file_name=urllib.parse.quote("白底原图.png", safe=""),
                            declared_size=content_length,
                            declared_sha256=hashlib.sha256(payload).hexdigest(),
                            declared_last_modified=1_700_000_000_000,
                            content_type=content_type,
                            content_length=content_length,
                            stream=io.BytesIO(payload),
                        )
                    self.assertEqual([], creator.created)

    def test_http_upload_round_trips_chinese_route_and_commits_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, client, creator, _controller = self.build_service(Path(tmp))
            service.poll_once()
            server = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=0)
            server.start()
            self.addCleanup(server.stop)
            path = "/batch-intake/{}/{}/files/{}".format(
                urllib.parse.quote("餐具_20260718", safe=""),
                urllib.parse.quote("req-123456", safe=""),
                urllib.parse.quote("image-original-1", safe=""),
            )
            connection = http.client.HTTPConnection(DEFAULT_UPLOAD_HOST, server.bound_port, timeout=3)
            connection.request(
                "POST",
                path,
                body=PNG_BYTES,
                headers={
                    "Origin": "http://localhost:3000",
                    "X-Canvas-Agent-Token": TOKEN,
                    "X-Canvas-File-Name": urllib.parse.quote("白底原图.png", safe=""),
                    "X-Canvas-File-Size": str(len(PNG_BYTES)),
                    "X-Canvas-File-Sha256": PNG_SHA256,
                    "X-Canvas-File-Last-Modified": "1700000000000",
                    "Content-Type": "image/png",
                    "Content-Length": str(len(PNG_BYTES)),
                },
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()

            self.assertEqual(200, response.status)
            self.assertEqual({"ok": True, "sha256": PNG_SHA256}, body)
            self.assertEqual(1, len(creator.created))
            upload = creator.created[0][1][0]
            self.assertEqual((PNG_BYTES,), creator.created[0][2])
            self.assertEqual("白底原图.png", upload.name)
            self.assertEqual(PNG_SHA256, upload.sha256)
            receipt = last_batch_intake_update(client)
            self.assertEqual("completed", receipt["status"])
            self.assertEqual("餐具_20260718", receipt["receipt"]["batchId"])

    def test_committed_upload_still_returns_sha_when_ledger_or_receipt_update_fails(self) -> None:
        for failure in ("ledger", "receipt"):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)

                    class ReceiptFailClient(FakeClient):
                        def apply_ops(self, ops: list[dict]):
                            status = ops[0].get("metadata", {}).get("batchIntake", {}).get("status")
                            if failure == "receipt" and status == "completed":
                                raise RuntimeError("secret receipt body")
                            return super().apply_ops(ops)

                    client = ReceiptFailClient([info_node()])
                    service, _client, creator, _controller = self.build_service(root, client=client)
                    service.poll_once()
                    original_append = service._append_event

                    def maybe_fail(event: str, request_id: str, **fields):
                        if failure == "ledger" and event == "batch_completed":
                            raise RuntimeError("secret ledger body")
                        return original_append(event, request_id, **fields)

                    service._append_event = maybe_fail
                    server = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=0)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        server.start()
                        connection = http.client.HTTPConnection(DEFAULT_UPLOAD_HOST, server.bound_port, timeout=3)
                        connection.request(
                            "POST",
                            "/batch-intake/%E9%A4%90%E5%85%B7_20260718/req-123456/files/image-original-1",
                            body=PNG_BYTES,
                            headers={
                                "Origin": "http://localhost:3000",
                                "X-Canvas-Agent-Token": TOKEN,
                                "X-Canvas-File-Name": urllib.parse.quote("白底原图.png", safe=""),
                                "X-Canvas-File-Size": str(len(PNG_BYTES)),
                                "X-Canvas-File-Sha256": PNG_SHA256,
                                "X-Canvas-File-Last-Modified": "1700000000000",
                                "Content-Type": "image/png",
                                "Content-Length": str(len(PNG_BYTES)),
                            },
                        )
                        response = connection.getresponse()
                        body = json.loads(response.read().decode("utf-8"))
                        connection.close()
                        server.stop()

                    self.assertEqual(200, response.status)
                    self.assertEqual({"ok": True, "sha256": PNG_SHA256}, body)
                    self.assertEqual(1, len(creator.created))
                    self.assertEqual("completed", service.sessions["req-123456"].status)
                    self.assertIn("receipt_pending", output.getvalue())
                    self.assertNotIn("secret", output.getvalue())

    def test_http_requires_allowed_origin_and_token_without_logging_secrets_or_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _client, creator, _controller = self.build_service(Path(tmp))
            service.poll_once()
            server = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                server.start()
                path = "/batch-intake/%E9%A4%90%E5%85%B7_20260718/req-123456/files/image-original-1"
                for origin, token, expected_status in (
                    ("https://evil.example", TOKEN, 403),
                    ("http://127.0.0.1:3000", "secret-wrong-token", 401),
                ):
                    connection = http.client.HTTPConnection(DEFAULT_UPLOAD_HOST, server.bound_port, timeout=3)
                    connection.request(
                        "POST",
                        path,
                        body=PNG_BYTES,
                        headers={
                            "Origin": origin,
                            "X-Canvas-Agent-Token": token,
                            "X-Canvas-File-Name": urllib.parse.quote("白底原图.png", safe=""),
                            "X-Canvas-File-Size": str(len(PNG_BYTES)),
                            "X-Canvas-File-Sha256": PNG_SHA256,
                            "X-Canvas-File-Last-Modified": "1700000000000",
                            "Content-Type": "image/png",
                            "Content-Length": str(len(PNG_BYTES)),
                        },
                    )
                    response = connection.getresponse()
                    self.assertEqual(expected_status, response.status)
                    error_body = json.loads(response.read().decode("utf-8"))
                    self.assertEqual("forbidden_origin" if expected_status == 403 else "unauthorized", error_body["errorCode"])
                    connection.close()
                server.stop()

            log_text = output.getvalue()
            self.assertNotIn(TOKEN, log_text)
            self.assertNotIn("secret-wrong-token", log_text)
            self.assertNotIn("offline-original-pixels", log_text)
            self.assertEqual([], creator.created)

    def test_token_check_uses_constant_time_compare_and_non_loopback_bind_is_refused(self) -> None:
        with mock.patch("workflow_batch_intake_service.hmac.compare_digest", return_value=True) as compared:
            self.assertTrue(constant_time_token_matches("expected", "provided"))
        compared.assert_called_once_with(b"expected", b"provided")

        with tempfile.TemporaryDirectory() as tmp:
            service, _client, _creator, _controller = self.build_service(Path(tmp))
            with self.assertRaisesRegex(ValueError, "回环"):
                BatchUploadServer(service, token=TOKEN, host="0.0.0.0", port=0)

    def test_server_stop_releases_its_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _client, _creator, _controller = self.build_service(Path(tmp))
            first = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=0)
            first.start()
            port = first.bound_port
            with self.assertRaises(OSError):
                BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=port).start()
            first.stop()

            second = BatchUploadServer(service, token=TOKEN, host=DEFAULT_UPLOAD_HOST, port=port)
            second.start()
            second.stop()
            with self.assertRaises(OSError):
                socket.create_connection((DEFAULT_UPLOAD_HOST, port), timeout=0.2)

    def test_service_lock_is_independent_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            with BatchIntakeServiceLock(state_root):
                owner_before = (state_root / SERVICE_OWNER_NAME).read_bytes()
                with self.assertRaisesRegex(RuntimeError, "建批服务已在运行"):
                    with BatchIntakeServiceLock(state_root):
                        pass
                self.assertEqual(owner_before, (state_root / SERVICE_OWNER_NAME).read_bytes())
            with BatchIntakeServiceLock(state_root):
                pass
            self.assertEqual(1, (state_root / SERVICE_LOCK_NAME).stat().st_size)
            self.assertFalse((state_root / SERVICE_OWNER_NAME).exists())

    def test_abnormally_terminated_child_releases_system_lock_before_owner_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            (state_root / ".canvas_batch_intake_state").write_text(
                "canvas-batch-intake-state-v1\n", encoding="utf-8"
            )
            program = """
import json
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from workflow_batch_intake_service import BatchIntakeServiceLock
lock = BatchIntakeServiceLock(__import__('pathlib').Path(sys.argv[2]))
lock.__enter__()
print(json.dumps({'pid': os.getpid()}), flush=True)
time.sleep(60)
"""
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(BRIDGE), str(state_root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                announced = json.loads(child.stdout.readline())
                stale_owner = json.loads((state_root / SERVICE_OWNER_NAME).read_text(encoding="utf-8"))
                self.assertEqual(announced["pid"], stale_owner["pid"])
                with self.assertRaisesRegex(RuntimeError, "建批服务已在运行"):
                    with BatchIntakeServiceLock(state_root):
                        pass

                child.terminate()
                child.wait(timeout=5)
                self.assertNotEqual(0, child.returncode)
                with BatchIntakeServiceLock(state_root):
                    new_owner = json.loads((state_root / SERVICE_OWNER_NAME).read_text(encoding="utf-8"))
                    self.assertEqual(os.getpid(), new_owner["pid"])
                    self.assertNotEqual(stale_owner["pid"], new_owner["pid"])
                    self.assertEqual(1, (state_root / SERVICE_LOCK_NAME).stat().st_size)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()


if __name__ == "__main__":
    unittest.main()
