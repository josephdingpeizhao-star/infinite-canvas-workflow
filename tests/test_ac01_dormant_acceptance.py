from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import canvas_command_assistant  # noqa: E402
import test_qc02_dormant_qc as qc02  # noqa: E402
import workflow_production_service as production_service  # noqa: E402


# close + delivery 能力保留已由
# test_qc02_dormant_qc.Qc02AcceptanceDeliveryTest.test_three_targets_close_and_deliver_without_qc_report
# 覆盖，本文件不重复。
UNSUPPORTED_ACCEPTANCE_MESSAGE = (
    "这个我还不会。收货与关账已休眠，不在当前批次流程中；"
    "返修图上桌请用机器卡上的入口。没有执行任何命令，也没有产生费用。"
)


class Ac01DormantAcceptanceTest(unittest.TestCase):
    @staticmethod
    def _three_target_completion() -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = qc02._build_route_fixture(
                Path(tmp),
                requested_outputs=qc02.THREE_TARGETS,
                main_count=4,
                detail_count=3,
            )
            shutil.copytree(qc02.ROOT / "categories", fixture.repo / "categories")
            fixture.add_images(fixture.config_ids)
            expected_count = len(fixture.config_ids)
            client = qc02.FakeCanvasClient(request_id="req-ac01")

            def unexpected_executor(*_args):
                raise AssertionError("ready route must not execute a production step")

            service = production_service.WorkflowProductionService(
                fixture.repo,
                client=client,
                executor_builder=unexpected_executor,
                route_reader=lambda _path: fixture.route(),
                integrity_reader=lambda _route: {
                    "found": True,
                    "status": "pass",
                    "render_blocked": False,
                },
                artifact_reader=qc02.Qc02ProductionServiceTest._artifact_reader(
                    fixture
                ),
                batch_lock_root=Path(tmp) / "locks",
                clock_ms=lambda: 1_100,
                environment={},
            )
            service.poll_once()
            message = client.machine["metadata"]["workflowProduction"]["message"]
        return expected_count, str(message)

    def test_three_target_completion_uses_dynamic_neutral_message(self) -> None:
        expected_count, message = self._three_target_completion()
        self.assertNotEqual(14, expected_count)
        with self.subTest(contract="exact_dynamic_message"):
            self.assertEqual(
                f"{expected_count} 张真实图片已全部完成。",
                message,
            )
        for forbidden in ("收货", "关账", "质检", "QC", "报告"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, message)

    def test_acceptance_utterances_return_dormant_guidance(self) -> None:
        for utterance in ("把这些图片收货", "现在关账"):
            with self.subTest(utterance=utterance):
                intent = canvas_command_assistant.resolve_rule_intent(utterance)
                self.assertIsNotNone(intent)
                self.assertEqual("unsupported", intent.kind)
                self.assertFalse(intent.command)
                self.assertEqual(UNSUPPORTED_ACCEPTANCE_MESSAGE, intent.message)


if __name__ == "__main__":
    unittest.main()
