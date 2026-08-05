from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from executor_contract import (  # noqa: E402
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from failure_text_safety import is_disclosable  # noqa: E402
from final_prompt_integrity_fixtures import build_final_prompt_bundle, write_json  # noqa: E402
from image_count_contract import (  # noqa: E402
    _DETAIL_EXTRA_MODULE_CYCLE,
    _NON_DIMENSION_MODULES,
    detail_module_groups,
)
from image_production_executor import ImageProductionExecutor  # noqa: E402
from openai_image_executor import (  # noqa: E402
    HttpResponse,
    OpenAIImageExecutor,
    _extract_response_shape,
)
from render_task_assembler import RenderTaskPlan  # noqa: E402
from workflow_production_service import (  # noqa: E402
    WorkflowProductionService,
    _StatusProjectionOutbox,
)


class RecordingTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls = 0

    def post(
        self,
        _url: str,
        _headers: dict[str, str],
        _body: bytes,
        _timeout: float,
    ) -> HttpResponse:
        self.calls += 1
        return self.response


class FailureExecutor:
    name = "failure-fixture"

    def __init__(self, failure: ExecutorExecutionError):
        self.failure = failure

    def execute(self, _request) -> ExecutionResult:
        raise self.failure


class FakeCanvasClient:
    def __init__(self, batch_id: str):
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "metadata": {
                        "content": "# workflow-production\n# request-id: req-er02\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-er02",
                            "batchId": batch_id,
                            "requestedAt": 1_000,
                            "producedCount": 0,
                        },
                    },
                },
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": batch_id, "imageCount": 14},
                        }
                    },
                },
                {
                    "id": "original",
                    "type": "image",
                    "metadata": {
                        "content": "blob:original",
                        "storageKey": "image:original",
                    },
                },
            ],
            "connections": [
                {
                    "id": "card-machine",
                    "fromNodeId": "card",
                    "toNodeId": "machine",
                },
                {
                    "id": "image-machine",
                    "fromNodeId": "original",
                    "toNodeId": "machine",
                },
            ],
        }

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        nodes = self.state["nodes"]
        for op in ops:
            if op.get("type") != "update_node":
                continue
            node = next(item for item in nodes if item["id"] == op["id"])
            node["metadata"] = {
                **node.get("metadata", {}),
                **op.get("metadata", {}),
            }
        return len(ops)


class Er02ProviderAttributionTest(unittest.TestCase):
    @staticmethod
    def _sensitive_response_payload() -> dict[str, object]:
        sensitive = {
            "authorization": "hidden",
            "Password": "hidden",
            "CREDENTIAL": "hidden",
            "access_key": "hidden",
            "凭据": "hidden",
        }
        return {
            "created": 1,
            "data": [
                {
                    "url": "hidden",
                    "revised_prompt": "hidden",
                    **sensitive,
                }
            ],
            **sensitive,
        }

    @staticmethod
    def _route(_manifest_path: Path) -> dict[str, object]:
        return {
            "current_stage": "needs_generated_images_before_qc",
            "next_required_skill": None,
            "blocked_reasons": ["QC is post-generation only"],
            "available_artifacts": ["final_prompts"],
            "outputs": {
                "renders": {"file_count": 0},
                "repaired": {"file_count": 0},
            },
            "inputs": {"style_reference_images": {"file_count": 1}},
        }

    @staticmethod
    def _render_plan(renders_dir: Path) -> RenderTaskPlan:
        task = ImageGenerationTask(
            prompt="safe fixture prompt",
            output_path=renders_dir / "main_01.png",
        )
        return RenderTaskPlan(tasks=(task,), planned=("main_01",), skipped=())

    def _run_service_failure(self, executor_factory):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            repo = temp_root / "repo"
            (repo / "manifests").mkdir(parents=True)
            shutil.copytree(ROOT / "categories", repo / "categories")
            bundle = build_final_prompt_bundle(temp_root / "fixture")
            batch_id = str(bundle.manifest["product_id"])
            manifest_path = repo / "manifests" / f"{batch_id}.batch_manifest.json"
            write_json(manifest_path, bundle.manifest)
            (bundle.root / "workspace" / ".canvas_batch").write_text(
                json.dumps({"type": "canvas-batch-v1", "product_id": batch_id}),
                encoding="utf-8",
            )
            client = FakeCanvasClient(batch_id)
            environment = {
                "RENDER_ALLOW_REAL_EXECUTION": "1",
                "OPENAI_API_KEY": "server-secret",
            }

            def build_executor(step, manifest, path, _on_output):
                self.assertEqual("renders", step)
                context = ExecutorContext(
                    manifest=manifest,
                    manifest_path=path,
                    environment=environment,
                )
                return executor_factory(context, bundle)

            service = WorkflowProductionService(
                repo,
                client=client,
                executor_builder=build_executor,
                route_reader=self._route,
                integrity_reader=lambda _route: {
                    "found": True,
                    "status": "pass",
                    "render_blocked": False,
                },
                artifact_reader=lambda _manifest: (),
                render_artifact_reader=lambda _manifest: (),
                repaired_artifact_reader=lambda _manifest: (),
                clock_ms=lambda: 1_100,
                environment=environment,
                batch_lock_root=temp_root / "locks",
            )
            service.poll_once()
            machine = client.state["nodes"][0]
            production = machine["metadata"]["workflowProduction"]
            events = [
                json.loads(line)
                for line in (repo / "manifests" / f"{batch_id}.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            failed_event = next(event for event in events if event["event"] == "step_failed")
            return service, copy.deepcopy(machine), copy.deepcopy(production), failed_event

    @staticmethod
    def _openai_executor() -> OpenAIImageExecutor:
        return OpenAIImageExecutor(
            ExecutorContext(manifest={}, environment={"OPENAI_API_KEY": "server-secret"}),
            transport=RecordingTransport(HttpResponse(200, {}, b"{}")),
        )

    def test_four_invalid_provider_responses_keep_reason_and_new_code(self) -> None:
        executor = self._openai_executor()
        cases = (
            (
                lambda: executor._decode_image({"data": [{"url": "opaque"}]}),
                "图片服务响应缺少 data[0].b64_json",
            ),
            (
                lambda: executor._decode_image({"data": [{"b64_json": "***"}]}),
                "图片服务返回的 Base64 图片无效",
            ),
            (
                lambda: executor._response_payload(HttpResponse(200, {}, b"{"), "server-secret"),
                "图片服务返回了无法解析的响应（HTTP 200）",
            ),
            (
                lambda: executor._response_payload(HttpResponse(200, {}, b"[]"), "server-secret"),
                "图片服务响应格式不正确",
            ),
        )
        for operation, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason), self.assertRaises(
                ExecutorExecutionError
            ) as caught:
                operation()
            self.assertEqual(expected_reason, str(caught.exception))
            self.assertEqual("render_response_invalid", caught.exception.code)

    def test_executor_shape_extraction_drops_unsafe_keys_and_limits_each_layer(self) -> None:
        unsafe_keys: dict[object, object] = {
            "bad/key": 1,
            "https://example.invalid": 2,
            "sk-secret": 3,
            "x" * 65: 4,
            123: 5,
            "中文键": 6,
        }
        payload: dict[object, object] = {
            **unsafe_keys,
            **{letter: letter for letter in "abcdefghij"},
            "data": [
                {
                    **unsafe_keys,
                    **{letter: letter for letter in "klmnopqrst"},
                }
            ],
        }

        shape = _extract_response_shape(payload)

        self.assertEqual(
            ("a", "b", "c", "d", "data", "e", "f", "g"),
            shape["response_top_keys"],
        )
        self.assertEqual(
            ("k", "l", "m", "n", "o", "p", "q", "r"),
            shape["response_data0_keys"],
        )
        self.assertNotIn("bad/key", repr(shape))
        self.assertNotIn("example.invalid", repr(shape))
        self.assertNotIn("sk-secret", repr(shape))
        self.assertNotIn("中文键", repr(shape))

    def test_executor_shape_extraction_drops_sensitive_identifiers(self) -> None:
        shape = _extract_response_shape(self._sensitive_response_payload())

        self.assertEqual(("created", "data"), shape["response_top_keys"])
        self.assertEqual(
            ("revised_prompt", "url"),
            shape["response_data0_keys"],
        )
        for sensitive in (
            "authorization",
            "Password",
            "CREDENTIAL",
            "access_key",
            "凭据",
        ):
            self.assertNotIn(sensitive, repr(shape))

    def test_sensitive_provider_keys_never_reach_final_failure_text(self) -> None:
        response = HttpResponse(
            status=200,
            headers={},
            body=json.dumps(self._sensitive_response_payload()).encode("utf-8"),
        )

        def executor_factory(context: ExecutorContext, bundle):
            image_executor = OpenAIImageExecutor(
                context,
                transport=RecordingTransport(response),
            )
            return ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: image_executor,
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
            )

        _service, _machine, production, event = self._run_service_failure(
            executor_factory
        )

        self.assertIn("响应字段：created、data", production["errorMessage"])
        self.assertIn(
            "data[0] 字段：revised_prompt、url",
            production["errorMessage"],
        )
        for sensitive in (
            "authorization",
            "Password",
            "CREDENTIAL",
            "access_key",
            "凭据",
        ):
            self.assertNotIn(sensitive, production["errorMessage"])
            self.assertNotIn(sensitive, event["detail"])
        self.assertEqual("image_service", production["failureSource"])
        self.assertLessEqual(len(event["detail"]), 160)
        self.assertTrue(is_disclosable(event["detail"]))
        self.assertTrue(is_disclosable(production["errorMessage"]))

    def test_3xx_missing_image_content_does_not_receive_the_2xx_failure_code(self) -> None:
        executor = self._openai_executor()
        with self.assertRaises(ExecutorExecutionError) as caught:
            executor._decode_image(
                {"data": [{"url": "opaque"}]},
                response_status=302,
            )
        self.assertEqual(
            "图片服务响应缺少 data[0].b64_json",
            str(caught.exception),
        )
        self.assertFalse(hasattr(caught.exception, "code"))

    def test_shape_reaches_journal_and_card_through_image_production_wrapper(self) -> None:
        response = HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "created": 1,
                    "data": [{"revised_prompt": "hidden", "url": "hidden"}],
                }
            ).encode("utf-8"),
        )
        transport = RecordingTransport(response)

        def executor_factory(context: ExecutorContext, bundle):
            image_executor = OpenAIImageExecutor(context, transport=transport)
            return ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: image_executor,
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
            )

        _service, _machine, production, event = self._run_service_failure(
            executor_factory
        )

        expected_shape = "响应字段：created、data；data[0] 字段：revised_prompt、url。"
        self.assertEqual(1, transport.calls)
        self.assertIn(expected_shape, production["errorMessage"])
        self.assertIn(expected_shape.rstrip("。"), event["detail"])
        self.assertEqual("render_response_invalid", event["failure_code"])
        self.assertEqual("image_service", production["failureSource"])
        self.assertTrue(is_disclosable(event["detail"]))
        self.assertTrue(is_disclosable(production["errorMessage"]))

    def test_service_resanitizes_shape_elements_without_destroying_failure(self) -> None:
        failure = ExecutorExecutionError("图片服务响应缺少 data[0].b64_json")
        failure.code = "render_response_invalid"
        failure.successful_count = 0
        failure.planned_count = 1
        failure.skipped_count = 0
        failure.response_top_keys = (
            *tuple("abcdefghij"),
            "bad/key",
            "https://example.invalid",
            "sk-secret",
            "x" * 65,
            123,
            "中文键",
        )
        failure.response_data0_keys = ("url", "revised_prompt")

        event, card, code = WorkflowProductionService._structured_render_failure_messages(
            failure
        )

        self.assertEqual("render_response_invalid", code)
        self.assertIn("响应字段：a、b、c、d、e、f、g、h", event)
        self.assertIn("data[0] 字段：revised_prompt、url", card)
        for rejected in ("i、j", "bad/key", "example.invalid", "sk-secret", "中文键"):
            self.assertNotIn(rejected, event)
            self.assertNotIn(rejected, card)
        self.assertTrue(is_disclosable(event))
        self.assertTrue(is_disclosable(card))

    def test_service_drops_sensitive_shape_elements_and_keeps_failure_source(self) -> None:
        failure = ExecutorExecutionError("图片服务响应缺少 data[0].b64_json")
        failure.code = "render_response_invalid"
        failure.response_top_keys = (
            "created",
            "data",
            "authorization",
            "Password",
            "CREDENTIAL",
            "access_key",
            "凭据",
        )
        failure.response_data0_keys = (
            "url",
            "revised_prompt",
            "authorization",
            "Password",
            "CREDENTIAL",
            "access_key",
            "凭据",
        )

        _service, _machine, production, event = self._run_service_failure(
            lambda _context, _bundle: FailureExecutor(failure)
        )

        self.assertIn("响应字段：created、data", production["errorMessage"])
        self.assertIn(
            "data[0] 字段：revised_prompt、url",
            production["errorMessage"],
        )
        for sensitive in (
            "authorization",
            "Password",
            "CREDENTIAL",
            "access_key",
            "凭据",
        ):
            self.assertNotIn(sensitive, production["errorMessage"])
            self.assertNotIn(sensitive, event["detail"])
        self.assertEqual("render_response_invalid", event["failure_code"])
        self.assertEqual("image_service", production["failureSource"])
        self.assertLessEqual(len(event["detail"]), 160)
        self.assertTrue(is_disclosable(event["detail"]))
        self.assertTrue(is_disclosable(production["errorMessage"]))

    def test_oversized_shape_sentence_is_dropped_without_losing_failure(self) -> None:
        failure = ExecutorExecutionError("图片服务响应缺少 data[0].b64_json")
        failure.code = "render_response_invalid"
        failure.successful_count = 0
        failure.planned_count = 1
        failure.skipped_count = 0
        failure.response_top_keys = tuple(
            f"k{index}{'x' * 61}" for index in range(8)
        )

        event, card, code = WorkflowProductionService._structured_render_failure_messages(
            failure
        )

        self.assertEqual("render_response_invalid", code)
        self.assertNotIn("响应字段：", event)
        self.assertNotIn("响应字段：", card)
        self.assertIn("图片服务响应缺少 data[0].b64_json", event)
        self.assertIn("图片服务响应缺少 data[0].b64_json", card)
        self.assertLessEqual(len(event), 160)
        self.assertTrue(is_disclosable(event))
        self.assertTrue(is_disclosable(card))

    def test_unsafe_invalid_response_reason_uses_fixed_text_and_keeps_structure(self) -> None:
        failure = ExecutorExecutionError(r"provider wrote C:\private\response.json")
        failure.code = "render_response_invalid"
        failure.response_top_keys = ("data",)

        event, card, code = WorkflowProductionService._structured_render_failure_messages(
            failure
        )

        self.assertEqual("render_response_invalid", code)
        self.assertTrue(event.startswith("渲染失败：图片服务返回的内容无法使用。"))
        self.assertTrue(card.startswith("图片服务返回的内容无法使用。"))
        self.assertNotIn("private", event)
        self.assertNotIn("private", card)
        self.assertTrue(card.endswith("机器已停下，未自动重试，已完成的成果都保留了。"))

    def test_new_code_copy_and_existing_failure_messages_keep_their_boundaries(self) -> None:
        failures: dict[str, ExecutorExecutionError] = {}
        for code in (
            "render_response_invalid",
            "render_http_error",
            "render_timeout",
            "render_network_error",
            "render_pipeline_error",
            "render_input_missing",
        ):
            failure = ExecutorExecutionError("图片服务响应格式不正确")
            failure.code = code
            if code == "render_http_error":
                failure.http_status = 503
            if code == "render_timeout":
                failure.timeout_seconds = 90
            if code == "render_input_missing":
                failure.missing_count = 1
                failure.missing_files = ("front.png",)
            failures[code] = failure

        for code, failure in failures.items():
            with self.subTest(code=code):
                result = WorkflowProductionService._structured_render_failure_messages(
                    failure
                )
                self.assertIsNotNone(result)
                event, card, returned_code = result
                self.assertEqual(code, returned_code)
                self.assertLessEqual(len(event), 160)
                self.assertNotIn("不是工作流的问题", event)
                self.assertNotIn("待服务恢复后", event)
                self.assertNotIn("不是工作流的问题", card)
                self.assertNotIn("待服务恢复后", card)

    def test_failure_source_is_absent_for_local_failure_and_clears_on_new_run(self) -> None:
        attributed_response = HttpResponse(
            status=200,
            headers={},
            body=json.dumps({"data": [{"url": "hidden"}]}).encode("utf-8"),
        )

        def attributed_factory(context: ExecutorContext, bundle):
            image_executor = OpenAIImageExecutor(
                context,
                transport=RecordingTransport(attributed_response),
            )
            return ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: image_executor,
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
            )

        service, machine, production, _event = self._run_service_failure(
            attributed_factory
        )
        self.assertEqual("image_service", production["failureSource"])

        for code in (
            "render_http_error",
            "render_timeout",
            "render_network_error",
        ):
            with self.subTest(code=code):
                service_failure = ExecutorExecutionError("safe provider failure")
                service_failure.code = code
                _service, _machine, attributed, _failed_event = (
                    self._run_service_failure(
                        lambda _context, _bundle, failure=service_failure: FailureExecutor(
                            failure
                        )
                    )
                )
                self.assertEqual("image_service", attributed["failureSource"])

        local_failure = ExecutorExecutionError("参考图不存在：front.png")
        local_failure.code = "render_input_missing"
        local_failure.missing_count = 1
        local_failure.missing_files = ("front.png",)
        _local_service, _local_machine, local_production, _local_event = (
            self._run_service_failure(
                lambda _context, _bundle: FailureExecutor(local_failure)
            )
        )
        self.assertNotIn("failureSource", local_production)

        queued = service._machine_update(
            machine,
            status="queued",
            content="# queued",
        )
        self.assertNotIn(
            "failureSource", queued["metadata"]["workflowProduction"]
        )
        queued_node = {"id": queued["id"], "metadata": queued["metadata"]}
        running = service._machine_update(
            queued_node,
            status="running",
            content="# running",
            step="renders",
        )
        self.assertNotIn(
            "failureSource", running["metadata"]["workflowProduction"]
        )

    def test_disclosure_rule_rejects_relative_slash_but_accepts_same_words(self) -> None:
        self.assertFalse(is_disclosable("成果目录 outputs" + "/" + "renders 不存在"))
        self.assertTrue(is_disclosable("成果目录 outputs renders 不存在"))

    def test_status_projection_outbox_rejects_non_update_and_empty_id(self) -> None:
        outbox = _StatusProjectionOutbox(
            lambda _ops: None,
            retry_seconds=1,
            should_stop=lambda: False,
        )
        with self.assertRaises(ValueError):
            outbox.submit([{"type": "create_node", "id": "x"}])
        with self.assertRaises(ValueError):
            outbox.submit([{"type": "update_node", "id": ""}])

    def test_non_dimension_module_name_preserves_the_existing_cycle_and_mapping(self) -> None:
        expected = (1, 2, 3, 4, 6, 7, 8)
        self.assertEqual(expected, _NON_DIMENSION_MODULES)
        self.assertEqual(expected, _DETAIL_EXTRA_MODULE_CYCLE)
        self.assertEqual((expected,), detail_module_groups(1))


if __name__ == "__main__":
    unittest.main()
