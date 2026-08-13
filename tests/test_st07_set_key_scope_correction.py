from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_downstream import (  # noqa: E402
    SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS,
    _reject_unicode_damage_or_forbidden_keys,
    parse_detail_variable_config_chunk,
)
from codex_dev_executor import CodexDevExecutor, CodexTurnResult  # noqa: E402
from content_correction import (  # noqa: E402
    ContentPredicateViolation,
    build_content_correction_instruction,
)
from executor_contract import ExecutionRequest, ExecutorExecutionError  # noqa: E402
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    valid_main_variable_response,
)
from tests.test_st03b_set_variable_config import (  # noqa: E402
    SetVariableConfigFixture,
    set_detail_chunks,
    set_requirements,
    valid_component_identity,
    valid_set_identity,
    valid_set_layout_inventory,
    valid_set_variable_response,
)


SET_LABEL = "套装主图变量配置"
SET_SCOPE_KEY = "套装身份锁定"
SET_SCOPE_MESSAGE = f"codex-dev 收到的{SET_LABEL}包含越界字段"


def set_key_scope_violation() -> ContentPredicateViolation:
    try:
        _reject_unicode_damage_or_forbidden_keys(
            {"common_constraints": {SET_SCOPE_KEY: "越界描述"}},
            SET_LABEL,
            allowed_set_keys=SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS,
        )
    except ContentPredicateViolation as error:
        return error
    raise AssertionError("expected a set-key scope violation")


class SetKeyScopeGuardTests(SetVariableConfigFixture):
    def test_unregistered_set_key_is_a_correctable_scope_violation(self) -> None:
        error = set_key_scope_violation()

        self.assertEqual("set_key_scope", error.code)
        self.assertEqual(SET_SCOPE_KEY, error.details.field)
        self.assertEqual(SET_SCOPE_MESSAGE, str(error))

    def test_unregistered_set_key_matches_when_set_term_is_not_at_the_start(self) -> None:
        key = "本批套装说明"
        with self.assertRaises(ContentPredicateViolation) as caught:
            _reject_unicode_damage_or_forbidden_keys(
                {"common_constraints": {key: "越界描述"}},
                SET_LABEL,
                allowed_set_keys=SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS,
            )

        self.assertEqual("set_key_scope", caught.exception.code)
        self.assertEqual(key, caught.exception.details.field)
        self.assertEqual(SET_SCOPE_MESSAGE, str(caught.exception))

    def test_forbidden_envelope_key_remains_a_non_correctable_execution_error(self) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            _reject_unicode_damage_or_forbidden_keys(
                {"common_constraints": {"product_id": "PRIVATE_ST07_ID"}},
                SET_LABEL,
                allowed_set_keys=SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS,
            )

        self.assertNotIsInstance(caught.exception, ContentPredicateViolation)
        self.assertEqual(SET_SCOPE_MESSAGE, str(caught.exception))

    def test_detail_chunk_reports_unregistered_set_key_as_content_violation(self) -> None:
        response = valid_set_variable_response("detail", count=7, handheld_target=1)
        chunk = set_detail_chunks(response)[0]
        chunk["common_constraints"][SET_SCOPE_KEY] = "越界描述"

        with self.assertRaises(ContentPredicateViolation) as caught:
            parse_detail_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False),
                1,
                requirements=set_requirements(),
                angle_inventory={},
                prior_chunks=[],
                set_identity=valid_set_identity(),
                component_identities=(
                    valid_component_identity(1),
                    valid_component_identity(2),
                ),
                set_angle_layout_inventory=valid_set_layout_inventory(),
            )

        self.assertEqual("set_key_scope", caught.exception.code)
        self.assertIn("详情图变量配置分段包含越界字段", str(caught.exception))

    def test_correction_instruction_names_the_key_without_repeating_teaching_tables(self) -> None:
        instruction = build_content_correction_instruction(set_key_scope_violation())

        self.assertIn(SET_SCOPE_KEY, instruction)
        self.assertIn("删除", instruction)
        for field in SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS:
            self.assertNotIn(field, instruction)

    def test_unicode_damage_remains_a_non_correctable_execution_error(self) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            _reject_unicode_damage_or_forbidden_keys(
                {"notes": "损坏\ufffd内容"},
                SET_LABEL,
                allowed_set_keys=SET_VARIABLE_CONFIG_ALLOWED_SET_KEYS,
            )

        self.assertNotIsInstance(caught.exception, ContentPredicateViolation)
        self.assertIn("包含损坏字符", str(caught.exception))

    def test_full_set_parser_keeps_set_and_envelope_key_failures_separate(self) -> None:
        cases = (
            (SET_SCOPE_KEY, ContentPredicateViolation, True),
            ("product_id", ExecutorExecutionError, False),
        )
        for key, error_type, is_content_violation in cases:
            with self.subTest(key=key):
                response = valid_set_variable_response("main", count=2, handheld_target=1)
                response["common_constraints"][key] = "越界描述"

                with self.assertRaises(error_type) as caught:
                    self.parse(response)

                self.assertEqual(
                    is_content_violation,
                    isinstance(caught.exception, ContentPredicateViolation),
                )
                self.assertIn("套装主图变量配置包含越界字段", str(caught.exception))


class SetKeyScopeExecutorTests(CodexDevFixture):
    def test_envelope_key_with_callback_still_does_not_correct_or_continue(self) -> None:
        response = copy.deepcopy(valid_main_variable_response())
        response["common_constraints"]["product_id"] = "PRIVATE_ST07_ID"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, output_path = self.make_downstream_fixture(root)
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(response, ensure_ascii=False),
                    thread_id="thread-st07-envelope",
                )
            )
            events: list[tuple[int, str, str]] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_content_correction_callback(
                lambda chunk, code, config_id: events.append((chunk, code, config_id))
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="main_vc"))

            self.assertNotIsInstance(caught.exception, ContentPredicateViolation)
            self.assertEqual([], events)
            self.assertEqual([], transport.continuation_calls)
            self.assertFalse(output_path.exists())
            self.assertNotIn("PRIVATE_ST07_ID", str(caught.exception))

    def test_unregistered_set_key_with_callback_corrects_once_and_succeeds(self) -> None:
        invalid = copy.deepcopy(valid_main_variable_response())
        invalid["common_constraints"][SET_SCOPE_KEY] = "越界描述"
        thread_id = "thread-st07-set-key"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, output_path = self.make_downstream_fixture(root)
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id=thread_id,
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                        thread_id=thread_id,
                    ),
                ]
            )
            events: list[tuple[int, str, str]] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_content_correction_callback(
                lambda chunk, code, config_id: events.append((chunk, code, config_id))
            )

            result = executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual([(1, "set_key_scope", "")], events)
            self.assertEqual(1, len(transport.continuation_calls))
            continuation_thread, continuation_prompt, attachments = (
                transport.continuation_calls[0]
            )
            self.assertEqual(thread_id, continuation_thread)
            self.assertIn(SET_SCOPE_KEY, continuation_prompt)
            self.assertIn("删除", continuation_prompt)
            self.assertEqual((), attachments)
            self.assertEqual((output_path,), result.outputs)
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
