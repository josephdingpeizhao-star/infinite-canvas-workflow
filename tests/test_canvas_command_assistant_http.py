from __future__ import annotations

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

from canvas_command_assistant import (  # noqa: E402
    CommandAssistantBusy,
)
import workflow_production_http_server as production_http  # noqa: E402


class FakeCommandAssistant:
    def __init__(self) -> None:
        self.submissions: list[str] = []

    def submit(self, utterance: str) -> dict[str, object]:
        self.submissions.append(utterance)
        if utterance == "busy":
            raise CommandAssistantBusy(
                "上一条指令仍在辨认或安全收尾，请稍后再试；本次没有排队。"
            )
        if utterance == "开始做图":
            return {
                "ok": True,
                "requestId": "draft-rule",
                "status": "completed",
                "message": "命令草稿已准备好。",
                "startedAt": 1,
                "updatedAt": 1,
                "deadlineAt": 301,
                "draft": {
                    "command": "run: next",
                    "verb": "run",
                    "target": "next",
                    "title": "继续下一步",
                    "description": "让机器按当前状态选择下一项允许的工作。",
                },
            }
        return {
            "ok": True,
            "requestId": "draft-model",
            "status": "working",
            "message": "助手正在辨认你要执行的步骤…",
            "startedAt": 1,
            "updatedAt": 1,
            "deadlineAt": 301,
        }

    def status(self, request_id: str) -> dict[str, object]:
        if request_id != "draft-model":
            raise KeyError(request_id)
        return {
            "ok": True,
            "requestId": request_id,
            "status": "completed",
            "message": "命令草稿已准备好。",
            "startedAt": 1,
            "updatedAt": 2,
            "deadlineAt": 301,
            "draft": {
                "command": "retry: qc",
                "verb": "retry",
                "target": "qc",
                "title": "重新执行成图质检",
                "description": "重新逐张检查 14 张成图质量。",
            },
        }


class CanvasCommandAssistantHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "manifests").mkdir(parents=True)
        self.assistant = FakeCommandAssistant()
        self.server = production_http.WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            command_assistant_service=self.assistant,
        )
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = "canvas-token",
        origin: str | None = "http://localhost:3000",
        payload: object | None = None,
        content_type: str = "application/json",
    ):
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if origin is not None:
            request.add_header("Origin", origin)
        if token is not None:
            request.add_header("x-canvas-agent-token", token)
        if data is not None:
            request.add_header("content-type", content_type)
        return urllib.request.urlopen(request, timeout=2)

    def test_rule_draft_returns_200_and_model_fallback_returns_202(self) -> None:
        with self.request(
            "POST",
            "/command-assistant/drafts",
            payload={"utterance": "开始做图"},
        ) as response:
            self.assertEqual(response.status, 200)
            rule = json.loads(response.read().decode("utf-8"))
        self.assertEqual(rule["draft"]["command"], "run: next")

        with self.request(
            "POST",
            "/command-assistant/drafts",
            payload={"utterance": "最后那道质量关再走一遍"},
        ) as response:
            self.assertEqual(response.status, 202)
            working = json.loads(response.read().decode("utf-8"))
        self.assertEqual(working["requestId"], "draft-model")

    def test_status_returns_terminal_validated_draft(self) -> None:
        with self.request(
            "GET",
            "/command-assistant/drafts/draft-model",
        ) as response:
            self.assertEqual(response.status, 200)
            finished = json.loads(response.read().decode("utf-8"))
        self.assertEqual(finished["draft"]["command"], "retry: qc")

    def test_existing_token_and_origin_are_required(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request(
                "POST",
                "/command-assistant/drafts",
                token=None,
                payload={"utterance": "开始做图"},
            )
        self.assertEqual(unauthorized.exception.code, 401)

        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.request(
                "POST",
                "/command-assistant/drafts",
                origin="https://example.com",
                payload={"utterance": "开始做图"},
            )
        self.assertEqual(forbidden.exception.code, 403)
        self.assertEqual(self.assistant.submissions, [])

    def test_payload_shape_content_type_and_body_limit_fail_closed(self) -> None:
        for payload in (
            {"utterance": "开始做图", "command": "run: next"},
            {"question": "开始做图"},
            ["开始做图"],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    self.request(
                        "POST",
                        "/command-assistant/drafts",
                        payload=payload,
                    )
                self.assertEqual(invalid.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as media:
            self.request(
                "POST",
                "/command-assistant/drafts",
                payload={"utterance": "开始做图"},
                content_type="text/plain",
            )
        self.assertEqual(media.exception.code, 415)

        with self.assertRaises(urllib.error.HTTPError) as oversized:
            self.request(
                "POST",
                "/command-assistant/drafts",
                payload={"utterance": "做" * 20_000},
            )
        self.assertEqual(oversized.exception.code, 413)

    def test_busy_error_is_human_readable_and_not_queued(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as busy:
            self.request(
                "POST",
                "/command-assistant/drafts",
                payload={"utterance": "busy"},
            )
        self.assertEqual(busy.exception.code, 409)
        body = json.loads(busy.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "command_assistant_busy")
        self.assertIn("没有排队", body["message"])

    def test_unknown_draft_id_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("GET", "/command-assistant/drafts/missing")
        self.assertEqual(missing.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
