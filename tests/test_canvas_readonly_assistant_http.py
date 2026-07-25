from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from canvas_readonly_assistant import (  # noqa: E402
    AssistantRealExecutionDisabled,
)
import workflow_production_http_server as production_http  # noqa: E402


class FakeAssistant:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, list[dict[str, str]]]] = []
        self.snapshot = {
            "ok": True,
            "requestId": "question-1",
            "status": "working",
            "message": "助手正在代你查看机器内部…",
            "startedAt": 1,
            "updatedAt": 1,
            "deadlineAt": 301,
        }

    def submit(self, question: str, history: list[dict[str, str]]) -> dict[str, object]:
        self.submissions.append((question, history))
        return dict(self.snapshot)

    def status(self, request_id: str) -> dict[str, object]:
        if request_id != "question-1":
            raise KeyError(request_id)
        return {
            **self.snapshot,
            "status": "completed",
            "answer": "第三批已关账并完成交付。",
            "message": "助手已查看完成。",
            "updatedAt": 2,
        }


class DisabledAssistant(FakeAssistant):
    def submit(self, question: str, history: list[dict[str, str]]) -> dict[str, object]:
        raise AssistantRealExecutionDisabled("只读助手尚未获准查看机器内部。")


class CanvasReadonlyAssistantHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "manifests").mkdir(parents=True)
        self.assistant = FakeAssistant()
        self.server = production_http.WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            assistant_service=self.assistant,
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
        payload: object | None = None,
    ):
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Origin", "http://localhost:3000")
        if token is not None:
            request.add_header("x-canvas-agent-token", token)
        if data is not None:
            request.add_header("content-type", "application/json")
        return urllib.request.urlopen(request, timeout=2)

    def test_post_requires_existing_canvas_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST",
                "/readonly-assistant/questions",
                token=None,
                payload={"question": "第三批现在什么状态？", "history": []},
            )
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.assistant.submissions, [])

    def test_post_starts_one_question_and_get_returns_terminal_answer(self) -> None:
        with self.request(
            "POST",
            "/readonly-assistant/questions",
            payload={"question": "第三批现在什么状态？", "history": []},
        ) as response:
            self.assertEqual(response.status, 202)
            started = json.loads(response.read().decode("utf-8"))
        self.assertEqual(started["requestId"], "question-1")
        self.assertEqual(started["status"], "working")
        self.assertEqual(
            self.assistant.submissions,
            [("第三批现在什么状态？", [])],
        )
        with self.request("GET", "/readonly-assistant/questions/question-1") as response:
            self.assertEqual(response.status, 200)
            finished = json.loads(response.read().decode("utf-8"))
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["answer"], "第三批已关账并完成交付。")

    def test_execution_switch_rejection_is_human_readable(self) -> None:
        self.server.stop()
        self.server = production_http.WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            assistant_service=DisabledAssistant(),
        )
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST",
                "/readonly-assistant/questions",
                payload={"question": "第三批现在什么状态？", "history": []},
            )
        self.assertEqual(caught.exception.code, 403)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "assistant_not_allowed")
        self.assertIn("尚未获准", body["message"])

    def test_unknown_question_id_and_oversized_body_terminate(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("GET", "/readonly-assistant/questions/missing")
        self.assertEqual(missing.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as oversized:
            self.request(
                "POST",
                "/readonly-assistant/questions",
                payload={"question": "问" * 20_000, "history": []},
            )
        self.assertEqual(oversized.exception.code, 413)


if __name__ == "__main__":
    unittest.main()
