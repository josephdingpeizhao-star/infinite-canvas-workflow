from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error


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
from final_prompt_integrity_fixtures import build_final_prompt_bundle, write_json  # noqa: E402
from image_production_executor import ImageProductionExecutor  # noqa: E402
from openai_image_executor import (  # noqa: E402
    HttpResponse,
    OpenAIImageExecutor,
    UrllibTransport,
    _extract_upstream_failure,
)
from render_task_assembler import RenderTaskPlan  # noqa: E402
from workflow_production_controller import ProductionGateError  # noqa: E402
from workflow_production_service import WorkflowProductionService  # noqa: E402


class RecordingTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {"url": url, "headers": headers, "body": body, "timeout": timeout}
        )
        return self.response


class FailureExecutor:
    name = "failure-fixture"

    def __init__(self, failure: ExecutorExecutionError):
        self.failure = failure

    def execute(self, _request) -> ExecutionResult:
        raise self.failure


class DeceptiveString(str):
    def __str__(self) -> str:
        return r"D:\leak"

    def __format__(self, _format_spec: str) -> str:
        return r"D:\leak"


class AlwaysEqualFailureCode:
    def __eq__(self, _other: object) -> bool:
        return True


class ExplodingEqualFailureCode:
    def __eq__(self, _other: object) -> bool:
        raise RuntimeError("untrusted equality must not run")


class FakeCanvasClient:
    def __init__(self, batch_id: str):
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "position": {"x": 0, "y": 0},
                    "width": 420,
                    "height": 300,
                    "metadata": {
                        "content": "# workflow-production\n# request-id: req-er01\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-er01",
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


class Er01RenderFailureReportingTest(unittest.TestCase):
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
        tasks = tuple(
            ImageGenerationTask(
                prompt=f"safe fixture prompt {index}",
                output_path=renders_dir / f"main_{index:02d}.png",
            )
            for index in range(1, 8)
        )
        return RenderTaskPlan(
            tasks=tasks,
            planned=tuple(task.output_path.stem for task in tasks),
            skipped=(),
        )

    def _run_failure(self, executor_factory) -> tuple[str, dict[str, object]]:
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
                "OPENAI_IMAGE_TIMEOUT_SECONDS": "90",
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
            card_message = machine["metadata"]["workflowProduction"]["errorMessage"]
            events = [
                json.loads(line)
                for line in (repo / "manifests" / f"{batch_id}.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            failed_event = next(event for event in events if event["event"] == "step_failed")
            return str(card_message), failed_event

    def _run_transport_failure(self, transport) -> tuple[str, dict[str, object]]:
        def make_executor(context: ExecutorContext, bundle):
            image_executor = OpenAIImageExecutor(context, transport=transport)
            return ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: image_executor,
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
            )

        return self._run_failure(make_executor)

    @staticmethod
    def _http_response(
        error_value: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        return HttpResponse(
            status=403,
            headers=headers or {},
            body=json.dumps({"error": error_value}).encode("utf-8"),
        )

    def test_real_incident_bad_response_status_code_reaches_card_and_journal(self) -> None:
        transport = RecordingTransport(
            self._http_response(
                {
                    "message": "openai_error",
                    "type": "bad_response_status_code",
                    "param": "",
                    "code": "bad_response_status_code",
                }
            )
        )

        card, event = self._run_transport_failure(transport)

        self.assertEqual(1, len(transport.calls))
        self.assertEqual(
            "图片服务返回错误（HTTP 403，类型 bad_response_status_code，代码 "
            "bad_response_status_code）。本轮成功 0 张、计划 7 张、跳过 0 张。"
            "机器已停下，未自动重试，已完成的成果都保留了。",
            card,
        )
        self.assertEqual(
            "渲染失败：HTTP 403；类型 bad_response_status_code；代码 "
            "bad_response_status_code；成功 0/计划 7/跳过 0",
            event["detail"],
        )
        self.assertEqual("render_http_error", event["failure_code"])

    def test_real_incident_body_request_id_is_extracted_without_message(self) -> None:
        request_id = "202608010609292743161288268d9d6H2J50dKu"
        provider_message = (
            "This token has no access to model gpt-image-2-probe-invalid "
            f"(request id: {request_id})"
        )
        transport = RecordingTransport(
            self._http_response(
                {"code": "", "message": provider_message, "type": "new_api_error"}
            )
        )

        card, event = self._run_transport_failure(transport)

        self.assertEqual(
            f"图片服务返回错误（HTTP 403，类型 new_api_error）。本轮成功 0 张、计划 "
            f"7 张、跳过 0 张。机器已停下，未自动重试，已完成的成果都保留了。"
            f"服务商请求编号：{request_id}（可凭此联系服务商）。",
            card,
        )
        self.assertEqual(
            f"渲染失败：HTTP 403；类型 new_api_error；成功 0/计划 7/跳过 0；"
            f"请求编号 {request_id}",
            event["detail"],
        )
        self.assertEqual("render_http_error", event["failure_code"])
        self.assertNotIn("This token has no access", card)
        self.assertNotIn("This token has no access", event["detail"])

    def test_header_request_id_has_priority_and_invalid_header_falls_back_to_body(self) -> None:
        body_request_id = "body-request-id-001"
        valid_header = self._http_response(
            {
                "type": "new_api_error",
                "message": f"failure (request id: {body_request_id})",
            },
            headers={"X-Request-ID": "header-request-id-001"},
        )
        invalid_header = self._http_response(
            {
                "type": "new_api_error",
                "message": f"failure (request id: {body_request_id})",
            },
            headers={"x-request-id": "https://unsafe.test/request"},
        )

        self.assertEqual(
            "header-request-id-001",
            _extract_upstream_failure(valid_header)["provider_request_id"],
        )
        self.assertEqual(
            body_request_id,
            _extract_upstream_failure(invalid_header)["provider_request_id"],
        )

    def test_sensitive_token_matrix_applies_the_documented_security_tradeoff(self) -> None:
        cases = (
            ("sk-fake123456789", ""),
            ("bearer_abcdef", ""),
            ("invalid_api_key", ""),
            ("bad_response_status_code", "bad_response_status_code"),
            ("new_api_error", "new_api_error"),
            ("rate_limit_exceeded", "rate_limit_exceeded"),
            ("token_expired", ""),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                extracted = _extract_upstream_failure(
                    self._http_response({"type": value, "code": value})
                )
                self.assertEqual(expected, extracted["provider_error_type"])
                self.assertEqual(expected, extracted["provider_error_code"])

    def test_extractor_rejects_string_subclasses_before_returning_fields(self) -> None:
        response = HttpResponse(
            status=403,
            headers={"x-request-id": DeceptiveString("safe-request-id")},
            body=b"{}",
        )
        with mock.patch(
            "openai_image_executor.json.loads",
            return_value={
                "error": {
                    "type": DeceptiveString("safe-type"),
                    "code": DeceptiveString("safe-code"),
                    "message": DeceptiveString("request id: safe-body-id"),
                }
            },
        ):
            extracted = _extract_upstream_failure(response)

        self.assertEqual("", extracted["provider_error_type"])
        self.assertEqual("", extracted["provider_error_code"])
        self.assertEqual("", extracted["provider_request_id"])

    def test_malicious_provider_fields_and_message_never_reach_surfaces(self) -> None:
        unsafe_values = (
            r"D:\x",
            "/etc/private",
            "https://unsafe.test/private",
            "sk-fake123456789",
            "x" * 65,
            "含中文",
            "contains space",
        )
        for index, unsafe in enumerate(unsafe_values, start=1):
            with self.subTest(unsafe=unsafe):
                message_marker = f"message-secret-{index}-sk-fake987654321"
                transport = RecordingTransport(
                    self._http_response(
                        {
                            "type": unsafe,
                            "code": unsafe,
                            "message": message_marker,
                        },
                        headers={"x-request-id": unsafe},
                    )
                )

                card, event = self._run_transport_failure(transport)

                self.assertIn("HTTP 403", card)
                self.assertIn("成功 0/计划 7/跳过 0", event["detail"])
                self.assertEqual("render_http_error", event["failure_code"])
                self.assertNotIn(unsafe, card)
                self.assertNotIn(unsafe, event["detail"])
                self.assertNotIn(message_marker, card)
                self.assertNotIn(message_marker, event["detail"])

    def test_timeout_and_network_failures_use_fixed_templates_without_free_text(self) -> None:
        cases = (
            (
                TimeoutError("private timeout detail"),
                "render_timeout",
                "图片服务等待超时（90 秒）。本轮成功 0 张、计划 7 张、跳过 0 张。"
                "机器已停下，未自动重试，已完成的成果都保留了。",
                "渲染失败：图片服务等待超时 90 秒；成功 0/计划 7/跳过 0",
            ),
            (
                error.URLError("private network detail"),
                "render_network_error",
                "无法连接图片服务。本轮成功 0 张、计划 7 张、跳过 0 张。"
                "机器已停下，未自动重试，已完成的成果都保留了。",
                "渲染失败：无法连接图片服务；成功 0/计划 7/跳过 0",
            ),
        )
        for failure, expected_code, expected_card, expected_detail in cases:
            with self.subTest(expected_code=expected_code), mock.patch(
                "openai_image_executor.request.urlopen",
                side_effect=failure,
            ):
                card, event = self._run_transport_failure(UrllibTransport())

                self.assertEqual(expected_card, card)
                self.assertEqual(expected_detail, event["detail"])
                self.assertEqual(expected_code, event["failure_code"])
                self.assertNotIn("private", card)
                self.assertNotIn("private", event["detail"])

    def test_unknown_failure_keeps_existing_fallback_and_has_no_failure_code(self) -> None:
        failure = ExecutorExecutionError(r"provider failed C:\private\secret.json")
        card, event = self._run_failure(
            lambda _context, _bundle: FailureExecutor(failure)
        )

        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            card,
        )
        self.assertEqual("执行已停止，未自动重试", event["detail"])
        self.assertNotIn("failure_code", event)

    def test_http_error_with_maximum_length_tokens_keeps_event_within_limit(self) -> None:
        token = "z" * 64
        card, event = self._run_transport_failure(
            RecordingTransport(
                self._http_response(
                    {"type": token, "code": token, "message": "safe"},
                    headers={"x-request-id": "r" * 64},
                )
            )
        )

        self.assertIn(token, card)
        self.assertLessEqual(len(event["detail"]), 160)
        self.assertEqual("render_http_error", event["failure_code"])

    def test_factory_construction_failure_forwards_structured_fields_and_counts(self) -> None:
        def make_executor(context: ExecutorContext, bundle):
            failure = ExecutorExecutionError("safe constructor failure")
            failure.code = "render_http_error"
            failure.http_status = 503
            failure.provider_error_type = "upstream_unavailable"
            failure.provider_error_code = "service_unavailable"
            failure.provider_request_id = "factory-request-001"

            def fail_factory(_context):
                raise failure

            return ImageProductionExecutor(
                context,
                image_executor_factory=fail_factory,
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
            )

        card, event = self._run_failure(make_executor)

        self.assertIn("HTTP 503", card)
        self.assertIn("本轮成功 0 张、计划 7 张、跳过 0 张", card)
        self.assertIn("成功 0/计划 7/跳过 0", event["detail"])
        self.assertEqual("render_http_error", event["failure_code"])

    def test_existing_special_failure_branches_remain_unchanged(self) -> None:
        empty_response = ExecutorExecutionError("codex-dev 本轮没有返回内容")
        empty_response.code = "empty_assistant_response"
        integrity_failure = ExecutorExecutionError("完整性门禁未通过")
        integrity_failure.code = "integrity_check_failed"
        integrity_failure.blocking_issue_count = 3
        real_execution_disabled = ExecutorExecutionError("internal switch detail")
        real_execution_disabled.code = "real_execution_disabled"
        cases = (
            (
                ProductionGateError("闸门提示原文"),
                "闸门提示原文",
            ),
            (
                ExecutorExecutionError("详情图尺寸不是 2:3"),
                "详情图返回 2:3，原图已保留。机器已停下，等待人工尺寸处理批准。",
            ),
            (
                ExecutorExecutionError("OPENAI_API_KEY missing"),
                "前面的成果已保留。本机还没有准备图片服务凭据，当前未出图、未产生新的图片费用。",
            ),
            (
                empty_response,
                "本地 Codex 本轮没有返回内容，机器已停下，未自动重试。",
            ),
            (
                integrity_failure,
                "完整性检查未通过：3 项阻塞，报告已写入 reports。机器已停下，未自动重试。",
            ),
            (
                real_execution_disabled,
                "本机真实执行开关未开启，本次没有调用模型、没有产生费用。请先关闭工作台窗口，按闸门流程用带开关的命令重新启动工作台，再回到画布重新开始。",
            ),
        )

        for failure, expected in cases:
            with self.subTest(failure=str(failure)):
                self.assertEqual(
                    expected,
                    WorkflowProductionService._safe_failure(failure),
                )

        self.assertEqual(
            "完整性检查未通过：3 项阻塞，报告已写入 reports",
            WorkflowProductionService._safe_event_detail(integrity_failure),
        )

    def test_service_rejects_any_invalid_structured_field_before_slash_bypass(self) -> None:
        cases = (
            ("http_status", 99_999),
            ("successful_count", True),
            ("provider_error_type", r"D:\x"),
            ("provider_error_code", "invalid_api_key"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                failure = ExecutorExecutionError("渲染中止：成功 0/计划 7")
                failure.code = "render_http_error"
                failure.http_status = 403
                failure.successful_count = 0
                failure.planned_count = 7
                failure.skipped_count = 0
                setattr(failure, name, value)

                self.assertIsNone(
                    WorkflowProductionService._structured_render_failure(failure)
                )
                self.assertEqual(
                    "执行已停止，未自动重试",
                    WorkflowProductionService._safe_event_detail(failure),
                )
                self.assertEqual(
                    "这一步没做好，机器已停下。已经完成的成果都保留了。",
                    WorkflowProductionService._safe_failure(failure),
                )

        invalid_status = ExecutorExecutionError("渲染中止：成功 0/计划 7")
        invalid_status.code = "render_http_error"
        invalid_status.http_status = 99_999
        invalid_status.successful_count = 0
        invalid_status.planned_count = 7
        invalid_status.skipped_count = 0
        card, event = self._run_failure(
            lambda _context, _bundle: FailureExecutor(invalid_status)
        )
        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            card,
        )
        self.assertEqual("执行已停止，未自动重试", event["detail"])
        self.assertNotIn("failure_code", event)

    def test_service_rejects_deceptive_string_subclass_without_rendering_it(self) -> None:
        failure = ExecutorExecutionError("渲染中止：成功 0/计划 7")
        failure.code = "render_http_error"
        failure.http_status = 403
        failure.provider_error_type = DeceptiveString("safe-looking-type")
        failure.successful_count = 0
        failure.planned_count = 7
        failure.skipped_count = 0

        card, event = self._run_failure(
            lambda _context, _bundle: FailureExecutor(failure)
        )

        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            card,
        )
        self.assertEqual("执行已停止，未自动重试", event["detail"])
        self.assertNotIn(r"D:\leak", card)
        self.assertNotIn(r"D:\leak", event["detail"])
        self.assertNotIn("failure_code", event)

    def test_non_string_code_cannot_impersonate_controlled_failure_codes(self) -> None:
        failure = ExecutorExecutionError("渲染中止：成功 0/计划 7")
        failure.code = AlwaysEqualFailureCode()
        failure.blocking_issue_count = 2

        card, event = self._run_failure(
            lambda _context, _bundle: FailureExecutor(failure)
        )

        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            card,
        )
        self.assertEqual("执行已停止，未自动重试", event["detail"])
        self.assertNotIn("failure_code", event)

    def test_failure_code_equality_is_never_invoked_for_non_string_objects(self) -> None:
        failure = ExecutorExecutionError("渲染中止：成功 0/计划 7")
        failure.code = ExplodingEqualFailureCode()

        self.assertEqual(
            "执行已停止，未自动重试",
            WorkflowProductionService._safe_event_detail(failure),
        )
        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            WorkflowProductionService._safe_failure(failure),
        )

    def test_http_error_with_unparseable_or_non_object_body_is_classified(self) -> None:
        cases = (
            (
                HttpResponse(status=403, headers={}, body=b"not-json"),
                "图片服务返回了无法解析的响应（HTTP 403）",
            ),
            (
                HttpResponse(status=403, headers={}, body=b"[]"),
                "图片服务响应格式不正确",
            ),
        )
        context = ExecutorContext(
            manifest={},
            environment={"OPENAI_API_KEY": "server-secret"},
        )
        executor = OpenAIImageExecutor(
            context,
            transport=RecordingTransport(HttpResponse(200, {}, b"{}")),
        )
        for response, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor._response_payload(response, "server-secret")
                self.assertEqual(expected_message, str(caught.exception))
                self.assertEqual("render_http_error", caught.exception.code)
                self.assertEqual(403, caught.exception.http_status)

                card, event = self._run_transport_failure(
                    RecordingTransport(response)
                )
                self.assertIn("HTTP 403", card)
                self.assertIn("成功 0/计划 7/跳过 0", event["detail"])
                self.assertEqual("render_http_error", event["failure_code"])

    def test_status_3xx_keeps_existing_non_error_behavior(self) -> None:
        context = ExecutorContext(
            manifest={},
            environment={"OPENAI_API_KEY": "server-secret"},
        )
        executor = OpenAIImageExecutor(
            context,
            transport=RecordingTransport(HttpResponse(200, {}, b"{}")),
        )
        payload = {"data": [{"b64_json": "unused"}]}

        self.assertEqual(
            payload,
            executor._response_payload(
                HttpResponse(
                    status=302,
                    headers={},
                    body=json.dumps(payload).encode("utf-8"),
                ),
                "server-secret",
            ),
        )
        for body, expected_message in (
            (b"not-json", "图片服务返回了无法解析的响应（HTTP 302）"),
            (b"[]", "图片服务响应格式不正确"),
        ):
            with self.subTest(body=body):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor._response_payload(
                        HttpResponse(status=302, headers={}, body=body),
                        "server-secret",
                    )
                self.assertEqual(expected_message, str(caught.exception))
                self.assertFalse(hasattr(caught.exception, "code"))


if __name__ == "__main__":
    unittest.main()
