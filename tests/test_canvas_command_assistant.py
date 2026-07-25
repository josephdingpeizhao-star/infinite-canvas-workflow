from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import canvas_command_assistant as command_assistant  # noqa: E402
from codex_dev_executor import CodexTurnResult  # noqa: E402


ALLOWED_ENVIRONMENT = {"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"}


class FakeTransport:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def run_turn(self, prompt: str, attachments: tuple[object, ...]) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        return CodexTurnResult(text=self.text, thread_id="thread-command")


class BlockingTransport:
    def __init__(self, text: str) -> None:
        self.text = text
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run_turn(self, _prompt: str, _attachments: tuple[object, ...]) -> CodexTurnResult:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return CodexTurnResult(text=self.text, thread_id="thread-blocked")


def wait_for_terminal(
    service: command_assistant.CanvasCommandAssistant,
    request_id: str,
    *,
    timeout: float = 1.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.status(request_id)
        if snapshot["status"] != "working":
            return snapshot
        time.sleep(0.005)
    raise AssertionError("command assistant did not reach a terminal state")


class CanvasCommandAssistantContractTest(unittest.TestCase):
    def test_closed_command_vocabulary_contains_exactly_nineteen_commands(self) -> None:
        self.assertEqual(len(command_assistant.CLOSED_COMMANDS), 19)
        self.assertIn("run: next", command_assistant.CLOSED_COMMANDS)
        for step in command_assistant.COMMAND_STEPS:
            self.assertIn(f"run: {step}", command_assistant.CLOSED_COMMANDS)
            self.assertIn(f"retry: {step}", command_assistant.CLOSED_COMMANDS)

    def test_closed_command_validator_rejects_case_typos_and_retry_next(self) -> None:
        self.assertEqual(command_assistant.validate_closed_command("run", "qc"), "run: qc")
        for verb, target in (
            ("Run", "qc"),
            ("run", "QC"),
            ("rerun", "qc"),
            ("run", "quality"),
            ("retry", "next"),
        ):
            with self.subTest(verb=verb, target=target):
                with self.assertRaises(command_assistant.CommandIntentRejected):
                    command_assistant.validate_closed_command(verb, target)

    def test_generic_phrases_map_to_run_next_and_start_making_images_is_locked(self) -> None:
        for utterance in ("开始", "继续", "下一步", "继续下一步", "开始做图", "请继续下一步吧"):
            with self.subTest(utterance=utterance):
                intent = command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, "command")
                self.assertEqual(intent.command, "run: next")

    def test_run_aliases_cover_all_nine_steps(self) -> None:
        samples = {
            "产品识别": "identity",
            "提取风格": "style_master",
            "检查角度": "angle_inventory",
            "生成主图配置": "main_vc",
            "生成详情配置": "detail_vc",
            "整理最终提示词": "final_prompts",
            "做完整性检查": "integrity",
            "生成图片": "renders",
            "开始质检": "qc",
        }
        for utterance, target in samples.items():
            with self.subTest(utterance=utterance):
                intent = command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.command, f"run: {target}")

    def test_retry_aliases_cover_all_nine_steps(self) -> None:
        samples = {
            "重跑产品识别": "identity",
            "再来一次风格母版": "style_master",
            "重新跑角度盘点": "angle_inventory",
            "再做一次主图配置": "main_vc",
            "重跑详情配置": "detail_vc",
            "再来一次最终提示词": "final_prompts",
            "重跑完整性检查": "integrity",
            "再做一次生成图片": "renders",
            "再查一遍质量": "qc",
        }
        for utterance, target in samples.items():
            with self.subTest(utterance=utterance):
                intent = command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.command, f"retry: {target}")

    def test_excluded_actions_are_rejected_without_becoming_commands(self) -> None:
        for utterance in (
            "帮我建个批次",
            "补登风格参考图",
            "把这些图片收货",
            "现在关账",
            "交付这一批",
            "repair 单图",
            "调用 ComfyUI",
            "替我拖图连线",
        ):
            with self.subTest(utterance=utterance):
                intent = command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, "unsupported")
                self.assertFalse(intent.command)

    def test_clear_batch_questions_route_to_the_existing_readonly_assistant(self) -> None:
        for utterance in ("第三批现在什么状态？", "QC 发现了什么问题？", "还缺多少张图"):
            with self.subTest(utterance=utterance):
                intent = command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, "question")

    def test_model_contract_accepts_only_exact_structured_intents(self) -> None:
        command = command_assistant.parse_model_intent(
            '{"intent":"command","verb":"retry","target":"qc"}'
        )
        self.assertEqual(command.command, "retry: qc")
        self.assertEqual(
            command_assistant.parse_model_intent('{"intent":"question"}').kind,
            "question",
        )
        self.assertEqual(
            command_assistant.parse_model_intent('{"intent":"unsupported"}').kind,
            "unsupported",
        )
        for value in (
            '```json\n{"intent":"command","verb":"run","target":"qc"}\n```',
            '说明：{"intent":"command","verb":"run","target":"qc"}',
            '["command","run","qc"]',
            '{"intent":"command","verb":"Run","target":"qc"}',
            '{"intent":"command","verb":"run","target":"quality"}',
            '{"intent":"command","verb":"run","target":"qc","command":"run: qc"}',
            '{"intent":"question","answer":"anything"}',
        ):
            with self.subTest(value=value):
                with self.assertRaises(command_assistant.CommandIntentRejected):
                    command_assistant.parse_model_intent(value)

    def test_prompt_contains_only_the_utterance_and_exact_json_contract(self) -> None:
        prompt = command_assistant.build_command_intent_prompt("最后那道质量关再走一遍")
        self.assertIn('"intent":"command"', prompt)
        self.assertIn('"intent":"question"', prompt)
        self.assertIn('"intent":"unsupported"', prompt)
        self.assertIn("最后那道质量关再走一遍", prompt)
        self.assertNotIn("canvas-agent-token", prompt)
        self.assertNotIn("OPENAI_API_KEY", prompt)

    def test_rule_hit_is_immediate_and_does_not_call_codex(self) -> None:
        transport = FakeTransport('{"intent":"unsupported"}')
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=transport,
            environment={},
            id_factory=lambda: "rule-1",
        )
        snapshot = service.submit("开始做图")
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["draft"]["command"], "run: next")
        self.assertEqual(transport.calls, [])

    def test_question_rule_routes_without_calling_codex(self) -> None:
        transport = FakeTransport('{"intent":"unsupported"}')
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=transport,
            environment={},
            id_factory=lambda: "question-1",
        )
        snapshot = service.submit("第三批现在什么状态？")
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["route"], "readonly")
        self.assertEqual(transport.calls, [])

    def test_fallback_calls_codex_once_without_attachments_and_returns_validated_draft(self) -> None:
        transport = FakeTransport('{"intent":"command","verb":"retry","target":"qc"}')
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=transport,
            environment=ALLOWED_ENVIRONMENT,
            id_factory=lambda: "fallback-1",
        )
        started = service.submit("最后那道质量关再走一遍")
        self.assertEqual(started["status"], "working")
        finished = wait_for_terminal(service, "fallback-1")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["draft"]["command"], "retry: qc")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], ())

    def test_default_transport_keeps_the_approved_model_and_effort(self) -> None:
        service = command_assistant.CanvasCommandAssistant(ROOT, environment={})
        self.assertEqual(service.transport.model, "gpt-5.5")
        self.assertEqual(service.transport.effort, "xhigh")

    def test_disabled_real_execution_blocks_only_model_fallback(self) -> None:
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=FakeTransport('{"intent":"unsupported"}'),
            environment={},
        )
        with self.assertRaises(command_assistant.CommandAssistantRealExecutionDisabled):
            service.submit("最后那道质量关再走一遍")
        self.assertEqual(service.submit("继续")["draft"]["command"], "run: next")

    def test_busy_fallback_is_not_queued(self) -> None:
        transport = BlockingTransport('{"intent":"command","verb":"retry","target":"qc"}')
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=transport,
            environment=ALLOWED_ENVIRONMENT,
        )
        first = service.submit("最后那道质量关再走一遍")
        self.assertTrue(transport.started.wait(timeout=1))
        with self.assertRaises(command_assistant.CommandAssistantBusy):
            service.submit("把末尾那关重新处理")
        self.assertEqual(transport.calls, 1)
        transport.release.set()
        wait_for_terminal(service, str(first["requestId"]))

    def test_timeout_is_terminal_and_late_model_result_cannot_overwrite_it(self) -> None:
        transport = BlockingTransport('{"intent":"command","verb":"retry","target":"qc"}')
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=transport,
            environment=ALLOWED_ENVIRONMENT,
            timeout_seconds=0.05,
            id_factory=lambda: "timeout-1",
        )
        service.submit("最后那道质量关再走一遍")
        self.assertTrue(transport.started.wait(timeout=1))
        time.sleep(0.08)
        timed_out = service.status("timeout-1")
        self.assertEqual(timed_out["status"], "failed")
        self.assertIn("超时", str(timed_out["message"]))
        transport.release.set()
        time.sleep(0.03)
        self.assertEqual(service.status("timeout-1")["status"], "failed")
        self.assertNotIn("draft", service.status("timeout-1"))

    def test_invalid_model_output_fails_closed_without_a_draft(self) -> None:
        service = command_assistant.CanvasCommandAssistant(
            ROOT,
            transport=FakeTransport(
                json.dumps(
                    {
                        "intent": "command",
                        "verb": "run",
                        "target": "build_batch",
                    }
                )
            ),
            environment=ALLOWED_ENVIRONMENT,
            id_factory=lambda: "invalid-1",
        )
        service.submit("替我处理这个动作")
        finished = wait_for_terminal(service, "invalid-1")
        self.assertEqual(finished["status"], "failed")
        self.assertNotIn("draft", finished)
        self.assertIn("安全确定", str(finished["message"]))


if __name__ == "__main__":
    unittest.main()
