from __future__ import annotations

import json
import socket
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

from workflow_demo_executor import write_placeholder_png  # noqa: E402
import workflow_production_http_server as production_http  # noqa: E402


class FakeStyleAcceptor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bytes]] = []

    def accept_upload(self, batch_id: str, request_id: str, node_id: str, data: bytes):
        self.calls.append((batch_id, request_id, node_id, data))
        return type("Outcome", (), {"sha256": "a" * 64, "completed": True})()


class ProductionHttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        (self.repo / "manifests").mkdir(parents=True)
        (self.workspace / "outputs" / "renders").mkdir(parents=True)
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}), encoding="utf-8"
        )
        self.image = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(self.image, width=1254, height=1254, kind="main", ordinal=1)
        self.manifest = self.repo / "manifests" / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "outputs": {"renders": [str(self.workspace / "outputs" / "renders")], "repaired": []},
                    "artifacts": {"final_prompts": [str(self.workspace / "artifacts" / "final_prompts")]},
                }
            ),
            encoding="utf-8",
        )
        self.style_acceptor = FakeStyleAcceptor()
        self.health_ok = True
        self.health_workers = {
            "workflow_demo": {"status": "running", "lastStatusAt": 1_000},
            "batch_intake": {"status": "running", "lastStatusAt": 1_001},
            "workflow_production": {"status": "running", "lastStatusAt": 1_002},
            "style_reference_intake": {"status": "running", "lastStatusAt": 1_003},
        }
        self.server = production_http.WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            style_acceptor=self.style_acceptor,
            health_provider=lambda: (self.health_ok, self.health_workers),
        )
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def _get(self, path: str, *, token: str | None = "canvas-token"):
        request = urllib.request.Request(self.base + path)
        if token is not None:
            request.add_header("x-canvas-agent-token", token)
        return urllib.request.urlopen(request, timeout=2)

    def test_quote_is_read_only_and_estimates_only_missing_images(self) -> None:
        before = self.manifest.read_bytes()
        with self._get("/workflow-production/cup/quote") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(14, payload["totalCount"])
        self.assertEqual(1, payload["readyCount"])
        self.assertEqual(13, payload["remainingCount"])
        self.assertEqual(0.78, payload["estimatedTotalUsd"])
        self.assertEqual(before, self.manifest.read_bytes())

    def test_health_is_read_only_sanitized_and_returns_503_for_a_dead_critical_worker(self) -> None:
        with self._get("/workbench-health") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(200, response.status)
        self.assertEqual({"workers"}, set(payload))
        self.assertEqual({"status", "lastStatusAt"}, set(payload["workers"]["batch_intake"]))

        self.health_workers["style_reference_intake"] = {
            "status": "stopped",
            "lastStatusAt": 2_000,
            "exception": "secret payload",
            "path": "D:/secret/workspace",
            "token": "secret-token",
        }
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/workbench-health")
        self.assertEqual(503, ctx.exception.code)
        failed = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual({"workers"}, set(failed))
        self.assertEqual(
            {"status": "stopped", "lastStatusAt": 2_000},
            failed["workers"]["style_reference_intake"],
        )
        self.assertNotIn("secret", json.dumps(failed))

    def test_output_requires_token_and_returns_hash_proof_without_exposing_path(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/workflow-production/cup/outputs/main_01", token=None)
        self.assertEqual(401, ctx.exception.code)

        with self._get("/workflow-production/cup/outputs/main_01") as response:
            body = response.read()
            headers = response.headers
        self.assertEqual(self.image.read_bytes(), body)
        self.assertEqual("image/png", headers.get_content_type())
        self.assertEqual(64, len(headers["x-content-sha256"]))
        self.assertNotIn(str(self.workspace), str(headers))

    def test_unknown_or_traversal_output_is_rejected(self) -> None:
        for path in (
            "/workflow-production/cup/outputs/unknown",
            "/workflow-production/cup/outputs/%2e%2e%2fsecret",
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(path)
            self.assertIn(ctx.exception.code, {400, 404})

    def test_stop_releases_listener(self) -> None:
        port = self.server.bound_port
        self.server.stop()
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()

    def test_style_upload_requires_canvas_origin_and_forwards_exact_bytes(self) -> None:
        data = b"\xff\xd8\xffstyle"
        request = urllib.request.Request(
            self.base + "/style-reference-intake/cup/style-request-001/files/style-image",
            data=data,
            method="POST",
            headers={
                "Origin": "http://localhost:3000",
                "x-canvas-agent-token": "canvas-token",
                "content-type": "application/octet-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual("http://localhost:3000", response.headers["Access-Control-Allow-Origin"])
        self.assertTrue(payload["completed"])
        self.assertEqual([("cup", "style-request-001", "style-image", data)], self.style_acceptor.calls)

        denied = urllib.request.Request(
            self.base + "/style-reference-intake/cup/style-request-001/files/style-image",
            data=data,
            method="POST",
            headers={
                "Origin": "https://example.com",
                "x-canvas-agent-token": "canvas-token",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(denied, timeout=2)
        self.assertEqual(403, ctx.exception.code)
        self.assertEqual(1, len(self.style_acceptor.calls))

    def test_browser_preflight_and_output_get_return_cors_headers(self) -> None:
        preflight = urllib.request.Request(
            self.base + "/style-reference-intake/cup/style-request-001/files/style-image",
            method="OPTIONS",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-canvas-agent-token, content-type",
            },
        )
        with urllib.request.urlopen(preflight, timeout=2) as response:
            self.assertEqual(204, response.status)
            self.assertEqual("http://127.0.0.1:3000", response.headers["Access-Control-Allow-Origin"])

        get_preflight = urllib.request.Request(
            self.base + "/workflow-production/cup/outputs/main_01",
            method="OPTIONS",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-canvas-agent-token",
            },
        )
        with urllib.request.urlopen(get_preflight, timeout=2) as response:
            self.assertEqual(204, response.status)
            self.assertEqual("http://localhost:3000", response.headers["Access-Control-Allow-Origin"])

        request = urllib.request.Request(
            self.base + "/workflow-production/cup/outputs/main_01",
            headers={
                "Origin": "http://localhost:3000",
                "x-canvas-agent-token": "canvas-token",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read()
            self.assertEqual("http://localhost:3000", response.headers["Access-Control-Allow-Origin"])


if __name__ == "__main__":
    unittest.main()
