from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_downstream import (  # noqa: E402
    _SLOT_D_NEGATION_CONTEXT_WINDOW,
    _SLOT_D_NEGATION_PREFIXES,
    _validate_bound_angle,
)
from codex_dev_executor import CodexDevExecutor  # noqa: E402
from content_correction import ContentPredicateViolation  # noqa: E402
from executor_contract import ExecutionRequest, ExecutorExecutionError  # noqa: E402
from test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    detail_chunk_turns,
    valid_detail_chunk_responses,
    valid_detail_variable_response,
)


_REAL_INCIDENT_BINDINGS = (
    "唯一合格源图：img_001；绑定 B 槽位；本张使用侧前方斜视角证明把手、杯口局部、杯身外壁和杯碟空间关系，不调用 D 槽位、缺失槽位或被拒绝源图。",
    "唯一合格源图：img_004；绑定 A 槽位；本张使用正面微俯视角，承接正式主图中的整体外观识别，不调用 D 槽位、缺失槽位或被拒绝源图。",
)
_EXPECTED_NEGATION_PREFIXES = frozenset(
    (
        "不调用",
        "不使用",
        "不绑定",
        "不得调用",
        "不得使用",
        "不得绑定",
        "禁止调用",
        "禁止使用",
        "禁止绑定",
        "禁止",
        "避免",
        "排除",
        "无",
    )
)
_QUALIFIED_AB = {
    "img_001": {"angle_slot": "B"},
    "img_004": {"angle_slot": "A"},
}


class Cat09SlotDNegationTest(unittest.TestCase):
    def test_real_incident_bindings_are_accepted_verbatim(self) -> None:
        for binding in _REAL_INCIDENT_BINDINGS:
            with self.subTest(binding=binding):
                _validate_bound_angle(binding, _QUALIFIED_AB, "详情图变量配置")

    def test_closed_negation_prefix_matrix_is_accepted(self) -> None:
        self.assertEqual(8, _SLOT_D_NEGATION_CONTEXT_WINDOW)
        self.assertEqual(_EXPECTED_NEGATION_PREFIXES, _SLOT_D_NEGATION_PREFIXES)
        for prefix in sorted(_EXPECTED_NEGATION_PREFIXES):
            for mention in (f"{prefix} D 槽位", f"{prefix} 槽位 D"):
                with self.subTest(prefix=prefix, mention=mention):
                    _validate_bound_angle(
                        f"唯一合格源图：img_001；绑定 B 槽位；{mention}。",
                        {"img_001": {"angle_slot": "B"}},
                        "详情图变量配置",
                    )

    def test_true_slot_d_source_remains_rejected_as_angle_binding(self) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            _validate_bound_angle(
                "唯一合格源图：img_004；绑定 D 槽位。",
                {"img_004": {"angle_slot": "D"}},
                "详情图变量配置",
            )

        self.assertEqual(
            "codex-dev 收到的详情图变量配置角度绑定异常",
            str(caught.exception),
        )

    def test_affirmative_slot_d_mentions_keep_original_failure(self) -> None:
        cases = (
            "对应槽位：D 槽位",
            "对应槽位：槽位 D",
        )
        for mention in cases:
            with self.subTest(mention=mention):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    _validate_bound_angle(
                        f"唯一合格源图：img_001；绑定 B 槽位；{mention}。",
                        {"img_001": {"angle_slot": "B"}},
                        "详情图变量配置",
                    )
                self.assertEqual(
                    "codex-dev 收到的详情图变量配置使用了缺失的 D 槽位",
                    str(caught.exception),
                )

    def test_any_affirmative_mention_in_mixed_binding_is_rejected(self) -> None:
        binding = (
            "唯一合格源图：img_001；绑定 B 槽位；不调用 D 槽位；"
            "对应槽位：D 槽位。"
        )

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "使用了缺失的 D 槽位",
        ):
            _validate_bound_angle(
                binding,
                {"img_001": {"angle_slot": "B"}},
                "详情图变量配置",
            )

    def test_distant_or_unrelated_negation_remains_rejected(self) -> None:
        cases = (
            "唯一合格源图：img_001；绑定 B 槽位；不调用超出八字窗口说明 D 槽位。",
            "唯一合格源图：img_001；绑定 B 槽位；不调用 A 槽位；改用 D 槽位。",
            "唯一合格源图：img_001；绑定 B 槽位；D 槽位已被排除。",
        )
        for binding in cases:
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(
                    ExecutorExecutionError,
                    "使用了缺失的 D 槽位",
                ):
                    _validate_bound_angle(
                        binding,
                        {"img_001": {"angle_slot": "B"}},
                        "详情图变量配置",
                    )

    def test_non_d_and_existing_angle_failures_are_unchanged(self) -> None:
        _validate_bound_angle(
            "唯一合格源图：img_001；绑定 B 槽位。",
            _QUALIFIED_AB,
            "详情图变量配置",
        )
        cases = (
            "绑定 B 槽位。",
            "同时参考 img_001 与 img_004；绑定 B 槽位。",
            "唯一合格源图：img_001；绑定 A 槽位。",
        )
        for binding in cases:
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(
                    ExecutorExecutionError,
                    "角度绑定异常",
                ):
                    _validate_bound_angle(
                        binding,
                        _QUALIFIED_AB,
                        "详情图变量配置",
                    )

    def test_affirmative_d_keeps_angle_binding_correction_metadata(self) -> None:
        with self.assertRaises(ContentPredicateViolation) as caught:
            _validate_bound_angle(
                "唯一合格源图：img_001；绑定 B 槽位；对应槽位：D 槽位。",
                {"img_001": {"angle_slot": "B"}},
                "详情图变量配置",
                correction_config_id="detail_01",
            )

        self.assertEqual("angle_binding", caught.exception.code)
        self.assertEqual("detail_01", caught.exception.details.config_id)
        self.assertEqual("绑定角度槽位", caught.exception.details.field)
        self.assertEqual(
            "codex-dev 收到的详情图变量配置使用了缺失的 D 槽位",
            str(caught.exception),
        )

    def test_negation_must_end_immediately_before_slot_d_mention(self) -> None:
        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "使用了缺失的 D 槽位",
        ):
            _validate_bound_angle(
                "唯一合格源图：img_001；绑定 B 槽位；不调用A，D 槽位可用。",
                {"img_001": {"angle_slot": "B"}},
                "详情图变量配置",
            )


class Cat09PublicExecutionPathTest(CodexDevFixture):
    @staticmethod
    def _angle_inventory_path(context) -> Path:
        return (
            Path(context.manifest["artifacts"]["angle_inventory"])
            / "angle_inventory.json"
        )

    def _prepare_incident_case(
        self,
        context,
        response: dict[str, object],
        incident_index: int,
    ) -> None:
        angle_path = self._angle_inventory_path(context)
        angle = json.loads(angle_path.read_text(encoding="utf-8"))
        source_asset_id = "img_001" if incident_index == 0 else "img_004"
        for record in angle["angle_slots"]:
            if record["source_asset_id"] == source_asset_id:
                record["angle_slot"] = "B" if incident_index == 0 else "A"
        angle_path.write_text(json.dumps(angle, ensure_ascii=False), encoding="utf-8")

        if incident_index == 0:
            for config in response["configs"]:
                overrides = config["per_image_overrides"]
                if overrides["绑定角度槽位"].startswith("img_001"):
                    overrides["绑定角度槽位"] = overrides["绑定角度槽位"].replace(
                        "A 槽位",
                        "B 槽位",
                    )
                    overrides["产品角度依据"] = overrides["产品角度依据"].replace(
                        "A 槽位",
                        "B 槽位",
                    )
        else:
            first_overrides = response["configs"][0]["per_image_overrides"]
            first_overrides["产品角度依据"] = (
                "以 img_004 的 A 槽位白底图为唯一角度依据"
            )
            first_overrides["产品颜色依据"] = "以 img_004 的商品本色为唯一颜色参照"
        response["configs"][0]["per_image_overrides"]["绑定角度槽位"] = (
            _REAL_INCIDENT_BINDINGS[incident_index]
        )

    def test_real_incidents_succeed_through_fake_transport_detail_flow(self) -> None:
        for incident_index, expected_binding in enumerate(_REAL_INCIDENT_BINDINGS):
            with self.subTest(binding=expected_binding):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    context, output_path, _main_path = self.make_detail_fixture(root)
                    response = valid_detail_variable_response()
                    self._prepare_incident_case(context, response, incident_index)
                    transport = FakeTransport(
                        detail_chunk_turns(
                            valid_detail_chunk_responses(response),
                            thread_id=f"thread-cat09-incident-{incident_index}",
                        )
                    )

                    result = CodexDevExecutor(
                        context,
                        transport=transport,
                        repository_root=root,
                    ).execute(ExecutionRequest(step="detail_vc"))

                    artifact = json.loads(output_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        expected_binding,
                        artifact["configs"][0]["per_image_overrides"]["绑定角度槽位"],
                    )
                    self.assertEqual("详情图变量配置已生成", result.detail)
                    self.assertEqual(1, len(transport.calls))
                    self.assertEqual(3, len(transport.continuation_calls))

    def test_true_d_source_fails_through_fake_transport_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            angle_path = self._angle_inventory_path(context)
            angle = json.loads(angle_path.read_text(encoding="utf-8"))
            angle["missing_angle_slots"] = []
            angle_path.write_text(json.dumps(angle, ensure_ascii=False), encoding="utf-8")
            response = valid_detail_variable_response()
            response["configs"][0]["per_image_overrides"]["绑定角度槽位"] = (
                "唯一合格源图：img_004；绑定 D 槽位。"
            )
            transport = FakeTransport(
                detail_chunk_turns(
                    valid_detail_chunk_responses(response),
                    thread_id="thread-cat09-true-d-source",
                )
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "角度绑定异常",
            ):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertFalse(output_path.exists())
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(0, len(transport.continuation_calls))


if __name__ == "__main__":
    unittest.main()
