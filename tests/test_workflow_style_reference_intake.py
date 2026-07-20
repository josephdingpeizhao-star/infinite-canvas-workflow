from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import workflow_style_reference_intake as style_intake  # noqa: E402
import ic_client  # noqa: E402


class StyleReferencePublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}), encoding="utf-8"
        )
        (self.workspace / "inputs" / "white_bg").mkdir(parents=True)
        (self.workspace / "manifests").mkdir()
        self.original = self.workspace / "inputs" / "white_bg" / "original.jpg"
        self.original.write_bytes(b"original-bytes")
        self.asset_manifest = self.workspace / "manifests" / "asset_manifest.json"
        self.receipt = self.workspace / "manifests" / "batch_intake_receipt.json"
        self.asset_manifest.write_text('{"fixed":true}\n', encoding="utf-8")
        self.receipt.write_text('{"fixed":true}\n', encoding="utf-8")
        self.fixed_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.original, self.asset_manifest, self.receipt)
        }
        self.manifest = root / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "inputs": {"style_reference_images": [str(self.workspace / "inputs" / "style_refs")]},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_bytes_are_published_with_new_receipt_and_existing_evidence_is_unchanged(self) -> None:
        data = b"\xff\xd8\xffstyle-reference"
        upload = style_intake.StyleReferenceUpload(
            node_id="style-node",
            name="风格参考图.jpg",
            mime_type="image/jpeg",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
        result = style_intake.publish_style_references(self.manifest, "request-001", (upload,))
        published = self.workspace / "inputs" / "style_refs" / "风格参考图.jpg"
        self.assertEqual(data, published.read_bytes())
        receipt = Path(result.receipt_path)
        self.assertTrue(receipt.name.startswith("style_reference_intake_receipt.request-001"))
        self.assertEqual(upload.sha256, json.loads(receipt.read_text(encoding="utf-8"))["files"][0]["sha256"])
        for path, expected in self.fixed_hashes.items():
            self.assertEqual(expected, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_hash_mismatch_creates_no_official_file_or_receipt(self) -> None:
        upload = style_intake.StyleReferenceUpload(
            node_id="style-node",
            name="style.jpg",
            mime_type="image/jpeg",
            size=3,
            sha256="0" * 64,
            data=b"abc",
        )
        with self.assertRaises(style_intake.StyleReferenceIntakeError):
            style_intake.publish_style_references(self.manifest, "request-002", (upload,))
        self.assertFalse((self.workspace / "inputs" / "style_refs").exists())
        self.assertEqual([], list((self.workspace / "manifests").glob("style_reference_intake_receipt.*")))

    def test_duplicate_name_with_different_bytes_stops_without_overwrite(self) -> None:
        target = self.workspace / "inputs" / "style_refs" / "style.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"first")
        upload = style_intake.StyleReferenceUpload(
            node_id="style-node",
            name="style.jpg",
            mime_type="image/jpeg",
            size=6,
            sha256=hashlib.sha256(b"second").hexdigest(),
            data=b"second",
        )
        with self.assertRaises(style_intake.StyleReferenceIntakeError):
            style_intake.publish_style_references(self.manifest, "request-003", (upload,))
        self.assertEqual(b"first", target.read_bytes())


class FakeCanvasClient:
    def __init__(self, *, sha256: str, requested_at: int = 1_000) -> None:
        self.state = {
            "nodes": [
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": "cup", "imageCount": 2},
                        },
                        "styleReferenceIntake": {
                            "status": "queued",
                            "requestId": "style-request-001",
                            "requestedAt": requested_at,
                            "batchId": "cup",
                            "sources": [
                                {
                                    "nodeId": "style-image",
                                    "name": "look.jpg",
                                    "mimeType": "image/jpeg",
                                    "size": 9,
                                    "sha256": sha256,
                                }
                            ],
                        },
                    },
                },
                {
                    "id": "style-image",
                    "type": "image",
                    "metadata": {
                        "sourceFile": {
                            "name": "look.jpg",
                            "type": "image/jpeg",
                            "size": 9,
                            "sha256": sha256,
                        }
                    },
                },
            ],
            "connections": [
                {"id": "style-card", "fromNodeId": "style-image", "toNodeId": "card"}
            ],
        }
        self.ops: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.ops.append(ops)
        for op in ops:
            if op.get("type") != "update_node":
                continue
            node = next(item for item in self.state["nodes"] if item["id"] == op["id"])
            node["metadata"] = {**node.get("metadata", {}), **op.get("metadata", {})}
        return len(ops)


class StyleReferenceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        (self.repo / "manifests").mkdir(parents=True)
        (self.workspace / "manifests").mkdir(parents=True)
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}), encoding="utf-8"
        )
        self.manifest = self.repo / "manifests" / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "inputs": {
                        "style_reference_images": [str(self.workspace / "inputs" / "style_refs")]
                    },
                }
            ),
            encoding="utf-8",
        )
        self.data = b"\xff\xd8\xffstyle!"
        self.sha256 = hashlib.sha256(self.data).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_connected_registered_card_opens_one_exact_byte_upload_session(self) -> None:
        client = FakeCanvasClient(sha256=self.sha256)
        service = style_intake.WorkflowStyleReferenceService(
            self.repo, client=client, clock_ms=lambda: 1_100, upload_port=17_373
        )
        service.poll_once()
        intake = client.state["nodes"][0]["metadata"]["styleReferenceIntake"]
        self.assertEqual("upload_ready", intake["status"])
        self.assertEqual("http://127.0.0.1:17373", intake["uploadBaseUrl"])

        outcome = service.accept_upload("cup", "style-request-001", "style-image", self.data)
        self.assertTrue(outcome.completed)
        self.assertEqual(self.sha256, outcome.sha256)
        completed = client.state["nodes"][0]["metadata"]["styleReferenceIntake"]
        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, completed["receipt"]["fileCount"])
        self.assertEqual(self.data, (self.workspace / "inputs" / "style_refs" / "look.jpg").read_bytes())

    def test_hash_mismatch_hard_stops_session_without_official_file_or_auto_retry(self) -> None:
        client = FakeCanvasClient(sha256=self.sha256)
        service = style_intake.WorkflowStyleReferenceService(
            self.repo, client=client, clock_ms=lambda: 1_100
        )
        service.poll_once()
        with self.assertRaises(style_intake.StyleReferenceUploadRejected):
            service.accept_upload("cup", "style-request-001", "style-image", b"wrong")
        intake = client.state["nodes"][0]["metadata"]["styleReferenceIntake"]
        self.assertEqual("integrity_blocked", intake["status"])
        with self.assertRaises(style_intake.StyleReferenceUploadRejected):
            service.accept_upload("cup", "style-request-001", "style-image", self.data)
        self.assertFalse((self.workspace / "inputs" / "style_refs").exists())

    def test_unconnected_source_or_wrong_batch_fails_closed(self) -> None:
        client = FakeCanvasClient(sha256=self.sha256)
        client.state["connections"] = []
        service = style_intake.WorkflowStyleReferenceService(
            self.repo, client=client, clock_ms=lambda: 1_100
        )
        service.poll_once()
        intake = client.state["nodes"][0]["metadata"]["styleReferenceIntake"]
        self.assertEqual("failed", intake["status"])
        self.assertEqual({}, service.sessions)

    def test_stale_request_is_rejected_without_session(self) -> None:
        client = FakeCanvasClient(sha256=self.sha256, requested_at=1_000)
        service = style_intake.WorkflowStyleReferenceService(
            self.repo, client=client, clock_ms=lambda: 20_000
        )
        service.poll_once()
        intake = client.state["nodes"][0]["metadata"]["styleReferenceIntake"]
        self.assertEqual("failed", intake["status"])
        self.assertEqual({}, service.sessions)

    def test_canvas_disconnect_keeps_worker_alive_and_reports_reconnecting_then_running(self) -> None:
        class ReconnectingClient:
            def __init__(self) -> None:
                self.calls = 0

            def call_tool(self, name: str):
                self.calls += 1
                if self.calls == 1:
                    raise ic_client.CanvasAgentError("secret disconnect detail")
                return {"nodes": [], "connections": []}

        client = ReconnectingClient()
        statuses: list[str] = []
        service = style_intake.WorkflowStyleReferenceService(
            self.repo,
            client=client,
            sleep=lambda _seconds: setattr(service, "stopping", client.calls >= 2),
        )
        service.set_status_callback(statuses.append)

        service.serve_forever()

        self.assertEqual(["waiting_canvas", "running"], statuses)
        self.assertEqual(2, client.calls)


if __name__ == "__main__":
    unittest.main()
