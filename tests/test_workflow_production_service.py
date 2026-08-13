from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionResult, ExecutorExecutionError  # noqa: E402
from codex_dev_executor import CodexDevExecutionError  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_projection import artifact_from_path  # noqa: E402
import ic_client  # noqa: E402
import workflow_production_service as production_service  # noqa: E402


STEPS = ["identity", "style_master", "angle_inventory", "main_vc", "detail_vc", "final_prompts", "integrity", "renders"]
STEP_ROUTES = {
    "identity": ("needs_product_identity_archive", "product-identity-archive"),
    "style_master": ("needs_style_master", "style-master-extractor"),
    "angle_inventory": ("needs_angle_inventory", "angle-inventory"),
    "main_vc": ("needs_main_variable_configs", "main-variable-config"),
    "detail_vc": ("needs_detail_variable_configs", "detail-variable-config"),
    "final_prompts": ("needs_final_prompts", "final-prompt-compiler"),
}


class FakeCanvasClient:
    def __init__(self, command: str = "run: next"):
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "position": {"x": 0, "y": 0},
                    "width": 420,
                    "height": 300,
                    "metadata": {
                        "content": f"# workflow-production\n# request-id: req-001\n{command}",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-001",
                            "batchId": "cup",
                            "requestedAt": 1_000,
                            "producedCount": 0,
                        },
                    },
                },
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {"batchIntake": {"status": "completed", "receipt": {"batchId": "cup", "imageCount": 2}}},
                },
                {
                    "id": "original",
                    "type": "image",
                    "metadata": {"content": "blob:original", "storageKey": "image:original"},
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "image-machine", "fromNodeId": "original", "toNodeId": "machine"},
            ],
        }
        self.ops: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.ops.append(ops)
        nodes = self.state["nodes"]
        for op in ops:
            if op.get("type") == "update_node":
                node = next(item for item in nodes if item["id"] == op["id"])
                node["metadata"] = {**node.get("metadata", {}), **op.get("metadata", {})}
            if op.get("type") == "add_node":
                metadata = dict(op.get("metadata") or {})
                output = metadata.get("workflowProductionOutput") or {}
                metadata.update({"storageKey": f"image:{op['id']}", "status": "success"})
                metadata["workflowProductionOutput"] = {**output, "persistedAt": 1_100}
                nodes.append({"id": op["id"], "type": op["nodeType"], "position": op["position"], "width": op["width"], "height": op["height"], "metadata": metadata})
        return len(ops)


class FakeExecutor:
    name = "fake"

    def __init__(self, step: str, executed: list[str], *, fail_step: str | None = None, on_output=None, artifact=None):
        self.step = step
        self.executed = executed
        self.fail_step = fail_step
        self.on_output = on_output
        self.artifact = artifact

    def execute(self, request):
        self.executed.append(request.step)
        if request.step == self.fail_step:
            raise ExecutorExecutionError("provider secret payload")
        if request.step == "renders" and self.on_output and self.artifact:
            self.on_output(self.artifact)
            return ExecutionResult(detail="成功 1/计划 1", outputs=(self.artifact.path,), provider=self.name)
        return ExecutionResult(detail="ok", provider=self.name)


class EmptyResponseExecutor:
    name = "fake-empty-response"

    def __init__(self, executed: list[str]):
        self.executed = executed

    def execute(self, request):
        self.executed.append(request.step)
        failure = ExecutorExecutionError("codex-dev 本轮没有返回内容")
        failure.code = "empty_assistant_response"
        raise failure


class MessageFailureExecutor:
    name = "fake-message-failure"

    def __init__(self, executed: list[str], message: str):
        self.executed = executed
        self.message = message

    def execute(self, request):
        self.executed.append(request.step)
        raise ExecutorExecutionError(self.message)


class RealExecutionDisabledExecutor:
    name = "fake-real-execution-disabled"

    def __init__(self, executed: list[str]):
        self.executed = executed

    def execute(self, request):
        self.executed.append(request.step)
        raise CodexDevExecutionError(
            "codex-dev 未获准真实执行；阶段 B 批准前保持禁用",
            "real_execution_disabled",
        )


class IntegrityFailureExecutor:
    name = "fake-integrity-failure"

    def __init__(self, executed: list[str], report_path: Path, report: object):
        self.executed = executed
        self.report_path = report_path
        self.report = report

    def execute(self, request):
        self.executed.append(request.step)
        if request.step == "integrity":
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, ensure_ascii=False),
                encoding="utf-8",
            )
            raise ExecutorExecutionError("完整性门禁未通过，渲染保持阻断")
        return ExecutionResult(detail="ok", provider=self.name)


class QcReportExecutor:
    name = "fake-qc-report"

    def __init__(self, executed: list[str], report_path: Path):
        self.executed = executed
        self.report_path = report_path

    def execute(self, request):
        self.executed.append(request.step)
        if request.step != "qc":
            raise AssertionError(request.step)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps({"product_id": "cup", "artifact_type": "qc_report"}),
            encoding="utf-8",
        )
        return ExecutionResult(
            detail="QC 报告已生成",
            outputs=(self.report_path,),
            provider=self.name,
        )


class ProgressQcReportExecutor:
    name = "fake-progress-qc-report"

    def __init__(
        self,
        executed: list[str],
        report_path: Path,
        *,
        failure: BaseException | None = None,
    ):
        self.executed = executed
        self.report_path = report_path
        self.failure = failure
        self.progress_callback = None

    def set_qc_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def execute(self, request):
        self.executed.append(request.step)
        if request.step != "qc":
            raise AssertionError(request.step)
        progress_count = 2 if self.failure is not None else 8
        for completed in range(1, progress_count + 1):
            if self.progress_callback is not None:
                self.progress_callback(completed, 8)
        if self.failure is not None:
            raise self.failure
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps({"product_id": "cup", "artifact_type": "qc_report"}),
            encoding="utf-8",
        )
        return ExecutionResult(
            detail="QC 报告已生成",
            outputs=(self.report_path,),
            provider=self.name,
        )


class ProductionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        (self.repo / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        (self.workspace / "inputs" / "style_refs").mkdir(parents=True)
        (self.workspace / "inputs" / "style_refs" / "style.jpg").write_bytes(b"style")
        (self.workspace / "outputs" / "renders").mkdir(parents=True)
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (self.workspace / ".canvas_batch").write_text(json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}), encoding="utf-8")
        self.manifest = self.repo / "manifests" / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "requested_outputs": [],
                    "workspace": {"root": str(self.workspace)},
                    "inputs": {"style_reference_images": [str(self.workspace / "inputs" / "style_refs")]},
                    "drafts": {},
                    "artifacts": {},
                    "outputs": {"renders": [str(self.workspace / "outputs" / "renders")], "repaired": []},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _route_reader(self, executed: list[str]):
        def read(_path):
            if len(executed) < 6:
                step = STEPS[len(executed)]
                stage, skill = STEP_ROUTES[step]
                return {
                    "current_stage": stage,
                    "next_required_skill": skill,
                    "blocked_reasons": [],
                    "available_artifacts": [],
                    "outputs": {"renders": {"file_count": 0}, "repaired": {"file_count": 0}},
                    "inputs": {"style_reference_images": {"file_count": 1}},
                }
            if "renders" in executed:
                return {
                    "current_stage": "needs_qc_reports",
                    "next_required_skill": "qc-inspector",
                    "blocked_reasons": [],
                    "available_artifacts": ["final_prompts"],
                    "outputs": {"renders": {"file_count": 1}, "repaired": {"file_count": 0}},
                    "inputs": {"style_reference_images": {"file_count": 1}},
                }
            return {
                "current_stage": "needs_generated_images_before_qc",
                "next_required_skill": None,
                "blocked_reasons": ["QC is post-generation only"],
                "available_artifacts": ["final_prompts"],
                "outputs": {"renders": {"file_count": 0}, "repaired": {"file_count": 0}},
                "inputs": {"style_reference_images": {"file_count": 1}},
            }

        return read

    @staticmethod
    def _integrity_reader(executed: list[str]):
        return lambda _route: {"found": "integrity" in executed, "status": "pass" if "integrity" in executed else "", "render_blocked": False}

    def _fourteen_artifacts(self):
        artifacts = []
        for prefix, count, width, height in (
            ("main", 6, 96, 96),
            ("detail", 8, 96, 128),
        ):
            for ordinal in range(1, count + 1):
                path = self.workspace / "outputs" / "renders" / f"{prefix}_{ordinal:02d}.png"
                write_placeholder_png(
                    path,
                    width=width,
                    height=height,
                    kind=prefix,
                    ordinal=ordinal,
                )
                artifacts.append(artifact_from_path("cup", path))
        return tuple(artifacts)

    def test_missing_category_recipe_blocks_production_before_workspace_use(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["category"] = "未安装品类"
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        service = production_service.WorkflowProductionService(
            self.repo,
            client=FakeCanvasClient(),
        )

        with self.assertRaisesRegex(
            production_service.ProductionGateError,
            "产品品类配方不可用",
        ):
            service._load_manifest(self.manifest, "cup")

    def test_failure_stops_pipeline_without_retry_and_hides_provider_detail(self) -> None:
        client = FakeCanvasClient()
        executed: list[str] = []
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(step, executed, fail_step="style_master", on_output=on_output),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            step_auto_retry_limit=0,
        )
        service.poll_once()

        self.assertEqual(["identity", "style_master"], executed)
        saved = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(["main", "detail", "final_prompts"], saved["requested_outputs"])
        machine = client.state["nodes"][0]
        self.assertEqual("failed", machine["metadata"]["workflowProduction"]["status"])
        self.assertNotIn("secret", machine["metadata"]["workflowProduction"]["errorMessage"])

    def test_empty_codex_response_stops_once_with_human_message_and_safe_diagnostic(self) -> None:
        client = FakeCanvasClient()
        executed: list[str] = []
        diagnostics: list[tuple[str, str]] = []
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: EmptyResponseExecutor(executed),
            route_reader=self._route_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            diagnostic_recorder=lambda step, code: diagnostics.append((step, code)),
            step_auto_retry_limit=0,
        )

        service.poll_once()

        self.assertEqual(["identity"], executed)
        self.assertEqual([("identity", "empty_assistant_response")], diagnostics)
        machine = client.state["nodes"][0]
        self.assertEqual(
            "本地 Codex 本轮没有返回内容，机器已停下，未自动重试。",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        events = [json.loads(line) for line in (self.repo / "manifests" / "cup.events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, len([event for event in events if event["event"] == "step_failed"]))
        self.assertEqual("codex-dev 本轮没有返回内容", events[-1]["detail"])
        artifact_root = self.workspace / "artifacts"
        self.assertEqual([], list(artifact_root.rglob("*")) if artifact_root.exists() else [])

    def test_real_execution_disabled_uses_human_workbench_copy(self) -> None:
        message = production_service.WorkflowProductionService._safe_failure(
            CodexDevExecutionError(
                "codex-dev 未获准真实执行；阶段 B 批准前保持禁用",
                "real_execution_disabled",
            )
        )

        self.assertEqual(
            "本机真实执行开关未开启，本次没有调用模型、没有产生费用。请先关闭工作台窗口，按闸门流程用带开关的命令重新启动工作台，再回到画布重新开始。",
            message,
        )
        self.assertNotIn("CODEX_DEV_ALLOW_REAL_EXECUTION", message)

    def test_real_execution_disabled_writes_fixed_event_detail_without_retry(self) -> None:
        client = FakeCanvasClient()
        executed: list[str] = []
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: RealExecutionDisabledExecutor(
                executed
            ),
            route_reader=self._route_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            step_auto_retry_limit=0,
        )

        service.poll_once()

        self.assertEqual(["identity"], executed)
        machine = client.state["nodes"][0]
        self.assertEqual("failed", machine["metadata"]["workflowProduction"]["status"])
        self.assertEqual(
            "本机真实执行开关未开启，本次没有调用模型、没有产生费用。请先关闭工作台窗口，按闸门流程用带开关的命令重新启动工作台，再回到画布重新开始。",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failed_events = [event for event in events if event["event"] == "step_failed"]
        self.assertEqual(1, len(failed_events))
        self.assertEqual(
            "真实执行开关未开启，执行已停止，未自动重试",
            failed_events[0]["detail"],
        )
        artifact_root = self.workspace / "artifacts"
        self.assertEqual([], list(artifact_root.rglob("*")) if artifact_root.exists() else [])

    def test_existing_failure_copy_branches_remain_unchanged(self) -> None:
        empty_response = ExecutorExecutionError("codex-dev 本轮没有返回内容")
        empty_response.code = "empty_assistant_response"
        controlled_detail = "codex-dev 收到的主图变量配置违反用户确认场景边界"
        cases = (
            (
                production_service.ProductionGateError("闸门提示原文"),
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
                ExecutorExecutionError(controlled_detail),
                "主图变量配置未通过：违反用户确认场景边界。机器已停下，未自动重试。",
            ),
            (
                ExecutorExecutionError("unknown provider failure"),
                "这一步没做好，机器已停下。已经完成的成果都保留了。",
            ),
        )

        for failure, expected in cases:
            with self.subTest(failure=str(failure)):
                self.assertEqual(
                    expected,
                    production_service.WorkflowProductionService._safe_failure(failure),
                )

    def test_sanitized_codex_failure_reaches_event_and_workbench(self) -> None:
        detail = (
            "codex-dev 收到的主图变量配置包含未确认商品事实（10 处："
            "configs/0/per_image_overrides/风格贴合锚点调用；"
            "configs/0/per_image_overrides/背景层次配置；"
            "configs/0/per_image_overrides/道具生成；"
            "configs/1/per_image_overrides/道具生成；等 6 处）"
        )
        client = FakeCanvasClient()
        executed = STEPS[:3].copy()
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: MessageFailureExecutor(
                executed, detail
            ),
            route_reader=self._route_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            step_auto_retry_limit=0,
        )

        service.poll_once()

        self.assertEqual(STEPS[:4], executed)
        machine = client.state["nodes"][0]
        self.assertEqual(
            detail.removeprefix("codex-dev 收到的").replace(
                "包含", "未通过：包含", 1
            )
            + "。机器已停下，未自动重试。",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual("step_failed", events[-1]["event"])
        self.assertEqual(detail[:160], events[-1]["detail"])

    def test_sanitized_final_prompt_failure_reaches_event_and_workbench(self) -> None:
        detail = (
            "codex-dev 收到的详情图最终提示词包含未确认商品事实"
            "（1 处：prompts/5/negative_prompt）"
        )
        client = FakeCanvasClient()
        executed = STEPS[:3].copy()
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: MessageFailureExecutor(
                executed, detail
            ),
            route_reader=self._route_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            step_auto_retry_limit=0,
        )

        service.poll_once()

        machine = client.state["nodes"][0]
        self.assertEqual(
            "详情图最终提示词未通过：包含未确认商品事实"
            "（1 处：prompts/5/negative_prompt）。机器已停下，未自动重试。",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual("step_failed", events[-1]["event"])
        self.assertEqual(detail, events[-1]["detail"])

    def test_persistence_timeout_reaches_event_and_workbench_without_retry(self) -> None:
        detail = "真实图片没有在规定时间内完成浏览器持久化"
        client = FakeCanvasClient(command="run: renders")
        executed = STEPS[:-1].copy()
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: MessageFailureExecutor(
                executed, detail
            ),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )

        service.poll_once()

        self.assertEqual(STEPS, executed)
        machine = client.state["nodes"][0]
        self.assertEqual("failed", machine["metadata"]["workflowProduction"]["status"])
        self.assertEqual(
            f"{detail}。机器已停下，未自动重试。",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failed_events = [event for event in events if event["event"] == "step_failed"]
        self.assertEqual(1, len(failed_events))
        self.assertEqual(detail, failed_events[0]["detail"])

    def test_unsafe_executor_failure_remains_redacted_everywhere(self) -> None:
        unsafe_details = (
            r"codex-dev 收到的主图变量配置包含未确认商品事实（1 处：C:\private\reply.json）",
            "codex-dev 收到的主图变量配置违反用户确认场景边界 https://example.test/private",
            "codex-dev 收到的主图变量配置角度绑定异常 bearer token-123",
            "codex-dev 收到的主图变量配置角度绑定异常 api_key=private-value",
        )
        for index, detail in enumerate(unsafe_details, start=1):
            with self.subTest(detail=detail):
                client = FakeCanvasClient()
                request_id = f"req-unsafe-{index}"
                client.state["nodes"][0]["metadata"]["content"] = (
                    f"# workflow-production\n# request-id: {request_id}\nrun: next"
                )
                client.state["nodes"][0]["metadata"]["workflowProduction"][
                    "requestId"
                ] = request_id
                executed = STEPS[:3].copy()
                service = production_service.WorkflowProductionService(
                    self.repo,
                    client=client,
                    executor_builder=lambda _step, _manifest, _path, _on_output: MessageFailureExecutor(
                        executed, detail
                    ),
                    route_reader=self._route_reader(executed),
                    artifact_reader=lambda _manifest: (),
                    clock_ms=lambda: 1_100,
                    step_auto_retry_limit=0,
                )

                service.poll_once()

                machine = client.state["nodes"][0]
                self.assertEqual(
                    "这一步没做好，机器已停下。已经完成的成果都保留了。",
                    machine["metadata"]["workflowProduction"]["errorMessage"],
                )
                events = [
                    json.loads(line)
                    for line in (self.repo / "manifests" / "cup.events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual("执行已停止，未自动重试", events[-1]["detail"])
                self.assertNotIn(detail, events[-1]["detail"])

    def test_failed_handheld_contract_keeps_counts_and_records_real_reason(self) -> None:
        client = FakeCanvasClient()
        production = client.state["nodes"][0]["metadata"]["workflowProduction"]
        expected_ids = [
            *(f"main_{index:02d}" for index in range(1, 7)),
            *(f"detail_{index:02d}" for index in range(1, 9)),
        ]
        production["totalCount"] = len(expected_ids)
        production["expectedConfigIds"] = expected_ids
        executed = STEPS[:3].copy()
        detail = "codex-dev 收到的主图变量配置手持规则调用异常"
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: MessageFailureExecutor(
                executed, detail
            ),
            route_reader=self._route_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            step_auto_retry_limit=0,
        )

        service.poll_once()

        saved = client.state["nodes"][0]["metadata"]["workflowProduction"]
        self.assertEqual("failed", saved["status"])
        self.assertEqual(14, saved["totalCount"])
        self.assertEqual(expected_ids, saved["expectedConfigIds"])
        self.assertEqual(
            "主图变量配置未通过：手持规则调用异常。机器已停下，未自动重试。",
            saved["errorMessage"],
        )
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(detail, events[-1]["detail"])

    def test_rejected_request_keeps_established_counts(self) -> None:
        client = FakeCanvasClient()
        production = client.state["nodes"][0]["metadata"]["workflowProduction"]
        expected_ids = [
            *(f"main_{index:02d}" for index in range(1, 4)),
            *(f"detail_{index:02d}" for index in range(1, 3)),
        ]
        production["totalCount"] = len(expected_ids)
        production["expectedConfigIds"] = expected_ids
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            clock_ms=lambda: 10_000,
        )

        service.poll_once()

        saved = client.state["nodes"][0]["metadata"]["workflowProduction"]
        self.assertEqual("failed", saved["status"])
        self.assertEqual(5, saved["totalCount"])
        self.assertEqual(expected_ids, saved["expectedConfigIds"])
        self.assertEqual(
            "本机工作台没有及时接单，请重新开始一次。",
            saved["errorMessage"],
        )

    def test_event_detail_is_capped_and_untrusted_paths_remain_hidden(self) -> None:
        long_detail = "安全原因" * 60

        saved = production_service.WorkflowProductionService._safe_event_detail(
            ExecutorExecutionError(long_detail)
        )

        self.assertEqual(160, len(saved))
        self.assertEqual(long_detail[:160], saved)
        self.assertEqual(
            "执行已停止，未自动重试",
            production_service.WorkflowProductionService._safe_event_detail(
                ExecutorExecutionError(r"报告位于 private\report.json")
            ),
        )

    def test_full_fake_pipeline_streams_one_persisted_render_then_pauses_without_qc(self) -> None:
        image = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(image, width=1254, height=1254, kind="main", ordinal=1)
        artifact = artifact_from_path("cup", image)
        client = FakeCanvasClient()
        executed: list[str] = []

        def artifacts(_manifest):
            return (artifact,) if "renders" in executed else ()

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(step, executed, on_output=on_output, artifact=artifact),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=artifacts,
            clock_ms=lambda: 1_100,
            sleep=lambda _seconds: None,
            persistence_timeout_ms=50,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )
        service.poll_once()

        self.assertEqual(STEPS, executed)
        self.assertNotIn("qc", executed)
        machine = client.state["nodes"][0]
        self.assertEqual("paused", machine["metadata"]["workflowProduction"]["status"])
        self.assertEqual(1, machine["metadata"]["workflowProduction"]["producedCount"])
        output = next(node for node in client.state["nodes"] if node["id"].startswith("wfprod-output:"))
        self.assertTrue(output["metadata"]["storageKey"].startswith("image:"))
        events = [json.loads(line) for line in (self.repo / "manifests" / "cup.events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("image_persisted", [event["event"] for event in events])

    def test_unusual_detail_completes_and_records_actual_dimensions_without_padding_event(self) -> None:
        image = self.workspace / "outputs" / "renders" / "detail_01.png"
        write_placeholder_png(image, width=43, height=64, kind="detail", ordinal=1)
        artifact = artifact_from_path("cup", image)
        client = FakeCanvasClient()
        executed: list[str] = []

        def artifacts(_manifest):
            return (artifact,) if "renders" in executed else ()

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(
                step,
                executed,
                on_output=on_output,
                artifact=artifact,
            ),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=artifacts,
            clock_ms=lambda: 1_100,
            sleep=lambda _seconds: None,
            persistence_timeout_ms=50,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )

        service.poll_once()

        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        persisted = next(event for event in events if event["event"] == "image_persisted")
        self.assertEqual((43, 64), (persisted["width"], persisted["height"]))
        self.assertNotIn("render_auto_padded", [event["event"] for event in events])
        self.assertIn("renders", executed)

    def test_gate_one_pauses_before_integrity_when_image_gate_is_closed(self) -> None:
        client = FakeCanvasClient()
        executed: list[str] = []
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(step, executed, on_output=on_output),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            environment={},
        )

        service.poll_once()

        self.assertEqual(STEPS[:6], executed)
        machine = client.state["nodes"][0]
        production = machine["metadata"]["workflowProduction"]
        self.assertEqual("paused", production["status"])
        self.assertEqual("上游准备完成，已停在出图前。等待批准下一闸门。", production["message"])
        events = [json.loads(line) for line in (self.repo / "manifests" / "cup.events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertNotIn(
            ("step_started", "integrity"),
            [(event["event"], event.get("step")) for event in events],
        )
        self.assertIn("production_paused", [event["event"] for event in events])

    def test_offline_status_projection_never_blocks_upstream_execution_and_replays_latest(self) -> None:
        class OfflineStatusCanvas(FakeCanvasClient):
            def __init__(self) -> None:
                super().__init__()
                self.online = threading.Event()
                self.apply_attempted = threading.Event()

            def apply_ops(self, ops: list[dict]):
                self.apply_attempted.set()
                if not self.online.is_set():
                    raise ic_client.CanvasAgentError("canvas offline")
                return super().apply_ops(ops)

        client = OfflineStatusCanvas()
        executed: list[str] = []
        executor_started = threading.Event()

        class SignalingExecutor(FakeExecutor):
            def execute(self, request):
                executor_started.set()
                return super().execute(request)

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: SignalingExecutor(
                step,
                executed,
                on_output=on_output,
            ),
            route_reader=self._route_reader(executed),
            integrity_reader=self._integrity_reader(executed),
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            sleep=lambda _seconds: None,
            interval=0.05,
            environment={},
        )
        worker = threading.Thread(target=service.poll_once)
        worker.start()

        self.assertTrue(client.apply_attempted.wait(timeout=1.0))
        started_while_offline = executor_started.wait(timeout=1.0)
        worker.join(timeout=2.0)
        completed_while_offline = not worker.is_alive()

        client.online.set()
        worker.join(timeout=3.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = client.state["nodes"][0]["metadata"]["workflowProduction"]["status"]
            if status == "paused":
                break
            time.sleep(0.02)

        self.assertTrue(started_while_offline)
        self.assertTrue(completed_while_offline)
        self.assertEqual(STEPS[:6], executed)
        self.assertEqual(
            "paused",
            client.state["nodes"][0]["metadata"]["workflowProduction"]["status"],
        )

    def test_partial_batch_requires_existing_retry_renders_gate(self) -> None:
        image = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(image, width=1254, height=1254, kind="main", ordinal=1)
        artifact = artifact_from_path("cup", image)
        client = FakeCanvasClient(command="retry: renders")
        executed: list[str] = []
        route = {
            "current_stage": "needs_qc_reports",
            "next_required_skill": "qc-inspector",
            "blocked_reasons": [],
            "available_artifacts": ["final_prompts"],
            "outputs": {"renders": {"file_count": 1}, "repaired": {"file_count": 0}},
            "inputs": {"style_reference_images": {"file_count": 1}},
        }
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(step, executed),
            route_reader=lambda _path: route,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: (artifact,),
            clock_ms=lambda: 1_100,
            persistence_timeout_ms=0,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )
        service.poll_once()
        self.assertEqual(["renders"], executed)

    def test_qc_step_uses_codex_dev_executor_without_image_executor(self) -> None:
        client = FakeCanvasClient()
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            environment={},
        )
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        expected = object()

        with mock.patch.object(
            production_service.executor_factory,
            "build_executor",
            return_value=expected,
        ) as build:
            actual = service._build_executor(
                "qc",
                manifest,
                self.manifest,
                lambda _artifact: None,
            )

        self.assertIs(expected, actual)
        build.assert_called_once_with("codex-dev", manifest, self.manifest)

    def test_existing_fourteen_images_run_qc_and_complete_without_duplicate_production_event(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["requested_outputs"] = ["main", "detail", "final_prompts", "qc_reports"]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        artifacts = self._fourteen_artifacts()
        report_path = self.workspace / "artifacts" / "qc_reports" / "qc_report.json"
        journal = self.repo / "manifests" / "cup.events.jsonl"
        production_service.run_controller.append_event(
            journal,
            "production_completed",
            request_id="req-rendered",
            produced_count=14,
        )
        client = FakeCanvasClient()
        executed: list[str] = []

        def route_reader(_path):
            ready = report_path.is_file()
            return {
                "current_stage": "ready" if ready else "needs_qc_reports",
                "next_required_skill": None if ready else "qc-inspector",
                "blocked_reasons": [],
                "available_artifacts": ["final_prompts", *(["qc_reports"] if ready else [])],
                "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
                "inputs": {"style_reference_images": {"file_count": 1}},
            }

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: QcReportExecutor(
                executed,
                report_path,
            ),
            route_reader=route_reader,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: artifacts,
            clock_ms=lambda: 1_100,
            environment={},
        )

        service.poll_once()

        self.assertEqual(["qc"], executed)
        self.assertTrue(report_path.is_file())
        production = client.state["nodes"][0]["metadata"]["workflowProduction"]
        self.assertEqual("completed", production["status"])
        self.assertEqual("质检完成，QC 报告已生成。", production["message"])
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, sum(event["event"] == "production_completed" for event in events))

    def test_qc_progress_updates_machine_eight_times_and_closes_worker(self) -> None:
        artifacts = self._fourteen_artifacts()
        report_path = self.workspace / "artifacts" / "qc_reports" / "qc_report.json"
        client = FakeCanvasClient()
        executed: list[str] = []
        ticks = iter(range(1_100, 1_200))

        def route_reader(_path):
            ready = report_path.is_file()
            return {
                "current_stage": "ready" if ready else "needs_qc_reports",
                "next_required_skill": None if ready else "qc-inspector",
                "blocked_reasons": [],
                "available_artifacts": ["final_prompts", *(["qc_reports"] if ready else [])],
                "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
                "inputs": {"style_reference_images": {"file_count": 1}},
            }

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: ProgressQcReportExecutor(
                executed,
                report_path,
            ),
            route_reader=route_reader,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: artifacts,
            clock_ms=lambda: next(ticks),
            environment={},
            persistence_timeout_ms=0,
        )

        service.poll_once()

        heartbeat_states = [
            op["metadata"]["workflowProduction"]
            for operation_batch in client.ops
            for op in operation_batch
            if op.get("type") == "update_node"
            and "组完成" in str((op.get("metadata") or {}).get("workflowProduction", {}).get("message") or "")
        ]
        self.assertEqual(8, len(heartbeat_states))
        self.assertEqual(["running"] * 8, [state["status"] for state in heartbeat_states])
        self.assertEqual(["qc"] * 8, [state["step"] for state in heartbeat_states])
        self.assertEqual([14] * 8, [state["producedCount"] for state in heartbeat_states])
        self.assertEqual(
            sorted(state["updatedAt"] for state in heartbeat_states),
            [state["updatedAt"] for state in heartbeat_states],
        )
        self.assertEqual(
            "completed",
            client.state["nodes"][0]["metadata"]["workflowProduction"]["status"],
        )
        self.assertEqual(set(), service._qc_heartbeat_workers)
        self.assertFalse(
            any(
                thread.is_alive() and thread.name.startswith("qc-heartbeat-")
                for thread in threading.enumerate()
            )
        )

    def test_qc_terminal_paths_close_workers_before_failed_or_exceptional_exit(self) -> None:
        artifacts = self._fourteen_artifacts()
        route = {
            "current_stage": "needs_qc_reports",
            "next_required_skill": "qc-inspector",
            "blocked_reasons": [],
            "available_artifacts": ["final_prompts"],
            "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
            "inputs": {"style_reference_images": {"file_count": 1}},
        }
        journal = self.repo / "manifests" / "cup.events.jsonl"
        cases = (
            ("failed", ExecutorExecutionError("simulated qc failure")),
            ("exception", RuntimeError("simulated unexpected qc exception")),
        )
        for case_name, failure in cases:
            with self.subTest(case=case_name):
                if journal.exists():
                    journal.unlink()
                client = FakeCanvasClient()
                executed: list[str] = []
                service = production_service.WorkflowProductionService(
                    self.repo,
                    client=client,
                    executor_builder=lambda _step, _manifest, _path, _on_output, failure=failure: ProgressQcReportExecutor(
                        executed,
                        self.workspace / "artifacts" / "qc_reports" / f"{case_name}.json",
                        failure=failure,
                    ),
                    route_reader=lambda _path: route,
                    integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
                    artifact_reader=lambda _manifest: artifacts,
                    clock_ms=lambda: 1_100,
                    environment={},
                    persistence_timeout_ms=0,
                )

                if case_name == "exception":
                    with self.assertRaisesRegex(RuntimeError, "unexpected qc exception"):
                        service.poll_once()
                else:
                    service.poll_once()
                    terminal_states = [
                        op["metadata"]["workflowProduction"]["status"]
                        for operation_batch in client.ops
                        for op in operation_batch
                        if op.get("type") == "update_node"
                        and "workflowProduction" in (op.get("metadata") or {})
                    ]
                    self.assertEqual("failed", terminal_states[-1])
                    self.assertNotIn("running", terminal_states[terminal_states.index("failed") + 1 :])

                self.assertEqual(set(), service._qc_heartbeat_workers)
                self.assertFalse(
                    any(
                        thread.is_alive() and thread.name.startswith("qc-heartbeat-")
                        for thread in threading.enumerate()
                    )
                )

    def test_render_completion_records_once_then_continues_to_qc(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["requested_outputs"] = ["main", "detail", "final_prompts", "qc_reports"]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        report_path = self.workspace / "artifacts" / "qc_reports" / "qc_report.json"
        journal = self.repo / "manifests" / "cup.events.jsonl"
        client = FakeCanvasClient(command="run: renders")
        executed: list[str] = []

        def route_reader(_path):
            if report_path.is_file():
                stage, skill = "ready", None
            elif "renders" in executed:
                stage, skill = "needs_qc_reports", "qc-inspector"
            else:
                stage, skill = "needs_generated_images_before_qc", None
            return {
                "current_stage": stage,
                "next_required_skill": skill,
                "blocked_reasons": [],
                "available_artifacts": ["final_prompts", *(["qc_reports"] if stage == "ready" else [])],
                "outputs": {
                    "renders": {"file_count": 14 if "renders" in executed else 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            }

        def executor_builder(step, _manifest, _path, _on_output):
            if step == "qc":
                return QcReportExecutor(executed, report_path)
            return FakeExecutor(step, executed)

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=executor_builder,
            route_reader=route_reader,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: tuple(range(14)) if "renders" in executed else (),
            clock_ms=lambda: 1_100,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )

        service.poll_once()

        self.assertEqual(["renders", "qc"], executed)
        production = client.state["nodes"][0]["metadata"]["workflowProduction"]
        self.assertEqual("completed", production["status"])
        self.assertEqual("质检完成，QC 报告已生成。", production["message"])
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, sum(event["event"] == "production_completed" for event in events))

    def test_ready_repeated_click_is_idempotent_and_keeps_production_event_count(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["requested_outputs"] = ["main", "detail", "final_prompts", "qc_reports"]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        artifacts = self._fourteen_artifacts()
        report_path = self.workspace / "artifacts" / "qc_reports" / "qc_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        journal = self.repo / "manifests" / "cup.events.jsonl"
        production_service.run_controller.append_event(
            journal,
            "production_completed",
            request_id="req-rendered",
            produced_count=14,
        )
        client = FakeCanvasClient()
        executed: list[str] = []
        ready_route = {
            "current_stage": "ready",
            "next_required_skill": None,
            "blocked_reasons": [],
            "available_artifacts": ["final_prompts", "qc_reports"],
            "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
            "inputs": {"style_reference_images": {"file_count": 1}},
        }
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, _on_output: FakeExecutor(step, executed),
            route_reader=lambda _path: ready_route,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: artifacts,
            clock_ms=lambda: 1_100,
            environment={},
        )

        service.poll_once()
        machine = client.state["nodes"][0]
        machine["metadata"]["content"] = "# workflow-production\n# request-id: req-002\nrun: next"
        machine["metadata"]["workflowProduction"].update(
            {"status": "queued", "requestId": "req-002", "requestedAt": 1_000}
        )
        service.poll_once()

        self.assertEqual([], executed)
        self.assertEqual("completed", machine["metadata"]["workflowProduction"]["status"])
        self.assertEqual("质检完成，QC 报告已生成。", machine["metadata"]["workflowProduction"]["message"])
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, sum(event["event"] == "production_completed" for event in events))

    def test_ready_rejects_post_qc_render_repair_and_comfyui_commands(self) -> None:
        artifacts = self._fourteen_artifacts()
        route = {
            "current_stage": "ready",
            "next_required_skill": None,
            "blocked_reasons": [],
            "available_artifacts": ["final_prompts", "qc_reports"],
            "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
            "inputs": {"style_reference_images": {"file_count": 1}},
        }
        for command in ("retry: renders", "run: repaired", "run: comfyui"):
            with self.subTest(command=command):
                client = FakeCanvasClient(command=command)
                executed: list[str] = []
                service = production_service.WorkflowProductionService(
                    self.repo,
                    client=client,
                    executor_builder=lambda step, _manifest, _path, _on_output: FakeExecutor(step, executed),
                    route_reader=lambda _path: route,
                    integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
                    artifact_reader=lambda _manifest: artifacts,
                    clock_ms=lambda: 1_100,
                    environment={},
                )

                service.poll_once()

                self.assertEqual([], executed)
                self.assertEqual(
                    "failed",
                    client.state["nodes"][0]["metadata"]["workflowProduction"]["status"],
                )

    def test_qc_without_codex_execution_switch_stops_with_existing_safe_copy(self) -> None:
        artifacts = self._fourteen_artifacts()
        journal = self.repo / "manifests" / "cup.events.jsonl"
        production_service.run_controller.append_event(
            journal,
            "production_completed",
            request_id="req-rendered",
            produced_count=14,
        )
        client = FakeCanvasClient()
        executed: list[str] = []
        route = {
            "current_stage": "needs_qc_reports",
            "next_required_skill": "qc-inspector",
            "blocked_reasons": [],
            "available_artifacts": ["final_prompts"],
            "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
            "inputs": {"style_reference_images": {"file_count": 1}},
        }
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: RealExecutionDisabledExecutor(executed),
            route_reader=lambda _path: route,
            integrity_reader=lambda _route: {"found": True, "status": "pass", "render_blocked": False},
            artifact_reader=lambda _manifest: artifacts,
            clock_ms=lambda: 1_100,
            environment={},
        )

        service.poll_once()

        self.assertEqual(["qc"], executed)
        production = client.state["nodes"][0]["metadata"]["workflowProduction"]
        self.assertEqual("failed", production["status"])
        self.assertEqual(
            production_service._REAL_EXECUTION_DISABLED_WORKBENCH_MESSAGE,
            production["errorMessage"],
        )
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, sum(event["event"] == "production_completed" for event in events))

    def test_missing_style_reference_stops_before_manifest_write_or_executor(self) -> None:
        (self.workspace / "inputs" / "style_refs" / "style.jpg").unlink()
        client = FakeCanvasClient()
        executed: list[str] = []
        route = self._route_reader(executed)(self.manifest)
        route["inputs"]["style_reference_images"]["file_count"] = 0
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, _manifest, _path, on_output: FakeExecutor(step, executed),
            route_reader=lambda _path: route,
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
        )
        service.poll_once()
        self.assertEqual([], executed)
        self.assertEqual([], json.loads(self.manifest.read_text(encoding="utf-8"))["requested_outputs"])

    def test_missing_image_credential_is_translated_without_echoing_environment_detail(self) -> None:
        message = production_service.WorkflowProductionService._safe_failure(
            ExecutorExecutionError("OPENAI_API_KEY missing; provider detail must stay private")
        )
        self.assertEqual(
            "前面的成果已保留。本机还没有准备图片服务凭据，当前未出图、未产生新的图片费用。",
            message,
        )
        self.assertNotIn("OPENAI_API_KEY", message)

    def test_integrity_failure_uses_controlled_blocking_count_without_path_leak(self) -> None:
        report_path = self.workspace / "artifacts" / "qc_reports" / "final_prompt_integrity_report.json"
        report = {
            "status": "fail",
            "render_blocked": True,
            "blocking_issue_count": 11,
        }
        client = FakeCanvasClient()
        executed: list[str] = []

        def integrity_reader(_route):
            if not report_path.is_file():
                return {"found": False, "path": "", "status": "", "render_blocked": False}
            return {
                "found": True,
                "path": str(report_path),
                "status": "fail",
                "render_blocked": True,
            }

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda _step, _manifest, _path, _on_output: IntegrityFailureExecutor(
                executed, report_path, report
            ),
            route_reader=self._route_reader(executed),
            integrity_reader=integrity_reader,
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
        )

        service.poll_once()

        self.assertEqual(STEPS[:7], executed)
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        expected = "完整性检查未通过：11 项阻塞，报告已写入 reports"
        self.assertEqual(expected, events[-1]["detail"])
        message = client.state["nodes"][0]["metadata"]["workflowProduction"]["errorMessage"]
        self.assertEqual(f"{expected}。机器已停下，未自动重试。", message)
        self.assertNotIn(str(report_path), events[-1]["detail"])
        self.assertNotIn(str(report_path), message)

    def test_invalid_integrity_report_count_keeps_generic_failure_copy(self) -> None:
        report_path = self.workspace / "artifacts" / "qc_reports" / "final_prompt_integrity_report.json"
        invalid_reports = (
            {"status": "fail", "render_blocked": True, "blocking_issue_count": None},
            {"status": "fail", "render_blocked": True, "blocking_issue_count": "11"},
            {"status": "fail", "render_blocked": True, "blocking_issue_count": -1},
            {"status": "fail", "render_blocked": True, "blocking_issue_count": True},
            {"status": "pass", "render_blocked": False, "blocking_issue_count": 11},
            ["invalid", "report"],
        )
        for index, report in enumerate(invalid_reports, start=1):
            with self.subTest(report=report):
                manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
                manifest["requested_outputs"] = []
                self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
                client = FakeCanvasClient()
                request_id = f"req-integrity-invalid-{index}"
                client.state["nodes"][0]["metadata"]["content"] = (
                    f"# workflow-production\n# request-id: {request_id}\nrun: next"
                )
                client.state["nodes"][0]["metadata"]["workflowProduction"]["requestId"] = request_id
                executed: list[str] = []

                def integrity_reader(_route):
                    if not report_path.is_file():
                        return {"found": False, "path": "", "status": "", "render_blocked": False}
                    return {
                        "found": True,
                        "path": str(report_path),
                        "status": "fail",
                        "render_blocked": True,
                    }

                service = production_service.WorkflowProductionService(
                    self.repo,
                    client=client,
                    executor_builder=lambda _step, _manifest, _path, _on_output: IntegrityFailureExecutor(
                        executed, report_path, report
                    ),
                    route_reader=self._route_reader(executed),
                    integrity_reader=integrity_reader,
                    artifact_reader=lambda _manifest: (),
                    clock_ms=lambda: 1_100,
                    environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
                )

                service.poll_once()

                events = [
                    json.loads(line)
                    for line in (self.repo / "manifests" / "cup.events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual("完整性门禁未通过，渲染保持阻断", events[-1]["detail"])
                self.assertEqual(
                    "这一步没做好，机器已停下。已经完成的成果都保留了。",
                    client.state["nodes"][0]["metadata"]["workflowProduction"]["errorMessage"],
                )

    def test_source_discovery_keeps_renders_and_repaired_distinct(self) -> None:
        repaired_root = self.workspace / "outputs" / "repaired"
        repaired_root.mkdir()
        render_path = self.workspace / "outputs" / "renders" / "main_01.png"
        repaired_path = repaired_root / "main_01.png"
        write_placeholder_png(render_path, width=96, height=96, kind="main", ordinal=1)
        write_placeholder_png(repaired_path, width=96, height=96, kind="main", ordinal=9)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["outputs"]["repaired"] = [str(repaired_root)]

        renders = production_service.discover_source_artifacts(manifest, "renders")
        repaired = production_service.discover_source_artifacts(manifest, "repaired")
        combined = production_service.discover_accepted_artifacts(manifest)

        self.assertEqual(["renders"], [item.source for item in renders])
        self.assertEqual(["repaired"], [item.source for item in repaired])
        self.assertEqual(render_path, combined[0].path)

    def test_source_discovery_accepts_unusual_ratios_but_filters_bad_and_unregistered_pngs(self) -> None:
        renders_root = self.workspace / "outputs" / "renders"
        write_placeholder_png(renders_root / "main_01.png", width=43, height=64, kind="main", ordinal=1)
        write_placeholder_png(renders_root / "detail_01.png", width=43, height=64, kind="detail", ordinal=1)
        (renders_root / "detail_02.png").write_bytes(b"not-a-png")
        write_placeholder_png(renders_root / "main_07.png", width=43, height=64, kind="main", ordinal=7)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))

        artifacts = production_service.discover_source_artifacts(manifest, "renders")

        self.assertEqual(["main_01", "detail_01"], [item.config_id for item in artifacts])
        self.assertEqual([(43, 64), (43, 64)], [(item.width, item.height) for item in artifacts])

    def test_repaired_projection_is_pure_and_never_builds_an_executor(self) -> None:
        repaired_root = self.workspace / "outputs" / "repaired"
        repaired_root.mkdir()
        repaired_path = repaired_root / "main_01.png"
        write_placeholder_png(repaired_path, width=96, height=96, kind="main", ordinal=9)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["outputs"]["repaired"] = [str(repaired_root)]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        client = FakeCanvasClient()
        machine = client.state["nodes"][0]
        machine["metadata"]["workflowProduction"]["status"] = "completed"
        machine["metadata"]["workflowRepairedProjection"] = {
            "status": "queued",
            "requestId": "repair-projection-1",
            "requestedAt": 1_000,
            "batchId": "cup",
        }

        def forbidden_builder(*_args, **_kwargs):
            raise AssertionError("projection must never build an executor")

        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=forbidden_builder,
            route_reader=lambda _path: {"current_stage": "ready"},
            clock_ms=lambda: 1_100,
        )
        service.poll_once()

        repaired_node = next(
            node for node in client.state["nodes"] if node["id"] == "wfprod-repaired:cup:main_01"
        )
        self.assertEqual(
            "repaired",
            repaired_node["metadata"]["workflowProductionOutput"]["source"],
        )
        self.assertEqual(
            "completed",
            machine["metadata"]["workflowRepairedProjection"]["status"],
        )

    def test_repaired_projection_retrigger_is_idempotent_by_source_and_sha(self) -> None:
        repaired_root = self.workspace / "outputs" / "repaired"
        repaired_root.mkdir()
        repaired_path = repaired_root / "detail_01.png"
        write_placeholder_png(repaired_path, width=96, height=128, kind="detail", ordinal=1)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["outputs"]["repaired"] = [str(repaired_root)]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        client = FakeCanvasClient()
        machine = client.state["nodes"][0]
        machine["metadata"]["workflowProduction"]["status"] = "completed"
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda *_args: (_ for _ in ()).throw(AssertionError("executor")),
            route_reader=lambda _path: {"current_stage": "ready"},
            clock_ms=lambda: 1_100,
        )
        for request_id in ("repair-projection-a", "repair-projection-b"):
            machine["metadata"]["workflowRepairedProjection"] = {
                "status": "queued",
                "requestId": request_id,
                "requestedAt": 1_000,
                "batchId": "cup",
            }
            service.poll_once()

        nodes = [
            node
            for node in client.state["nodes"]
            if node["id"] == "wfprod-repaired:cup:detail_01"
        ]
        events = [
            json.loads(line)
            for line in (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(1, len(nodes))
        self.assertEqual(
            1,
            len([event for event in events if event["event"] == "repaired_image_persisted"]),
        )

    def test_failed_source_backfill_records_evidence_and_remains_unreceivable(self) -> None:
        render_path = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(render_path, width=96, height=96, kind="main", ordinal=1)
        client = FakeCanvasClient()
        machine = client.state["nodes"][0]
        machine["metadata"]["workflowProduction"]["status"] = "completed"
        client.state["nodes"].append(
            {
                "id": "wfprod-output:cup:main_01",
                "type": "image",
                "metadata": {
                    "storageKey": "image:legacy",
                    "workflowProductionOutput": {
                        "batchId": "cup",
                        "configId": "main_01",
                        "sha256": "0" * 64,
                    },
                },
            }
        )
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            route_reader=lambda _path: {"current_stage": "ready"},
            clock_ms=lambda: 1_100,
        )
        service.poll_once()

        proof = client.state["nodes"][-1]["metadata"]["workflowProductionOutput"]
        self.assertNotIn("source", proof)
        self.assertEqual("source_proof_mismatch", proof["sourceBackfillCode"])

    def _mark_batch_closed(self) -> bytes:
        before = self.manifest.read_bytes()
        (self.repo / "manifests" / "cup.events.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-07-24T12:00:00",
                    "event": "batch_acceptance_closed",
                    "selection_count": 14,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return before

    def test_closed_batch_rejects_before_manifest_edit_or_executor(self) -> None:
        before = self._mark_batch_closed()
        client = FakeCanvasClient()
        built: list[str] = []
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda step, *_args: built.append(step),
            route_reader=lambda _path: {
                "current_stage": "ready",
                "inputs": {"style_reference_images": {"file_count": 1}},
            },
            clock_ms=lambda: 1_100,
        )
        service.poll_once()

        machine = client.state["nodes"][0]
        self.assertEqual([], built)
        self.assertEqual(before, self.manifest.read_bytes())
        self.assertEqual(
            production_service.BATCH_CLOSED_MESSAGE,
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )

    def test_closed_batch_refuses_qc_render_and_repair_commands_with_same_copy(self) -> None:
        for index, command in enumerate(
            ("run: next", "run: qc", "run: renders", "run: repair"),
            start=1,
        ):
            with self.subTest(command=command):
                self._mark_batch_closed()
                client = FakeCanvasClient(command)
                client.state["nodes"][0]["metadata"]["workflowProduction"]["requestId"] = (
                    f"closed-{index}"
                )
                service = production_service.WorkflowProductionService(
                    self.repo,
                    client=client,
                    executor_builder=lambda *_args: (_ for _ in ()).throw(
                        AssertionError("closed batch must not build executor")
                    ),
                    route_reader=lambda _path: {
                        "current_stage": "ready",
                        "inputs": {"style_reference_images": {"file_count": 1}},
                    },
                    clock_ms=lambda: 1_100,
                )
                service.poll_once()
                self.assertEqual(
                    production_service.BATCH_CLOSED_MESSAGE,
                    client.state["nodes"][0]["metadata"]["workflowProduction"][
                        "errorMessage"
                    ],
                )

    def test_closed_batch_refuses_repaired_projection_without_adding_image(self) -> None:
        self._mark_batch_closed()
        client = FakeCanvasClient()
        machine = client.state["nodes"][0]
        machine["metadata"]["workflowProduction"]["status"] = "completed"
        machine["metadata"]["workflowRepairedProjection"] = {
            "status": "queued",
            "requestId": "closed-repaired",
            "requestedAt": 1_000,
            "batchId": "cup",
        }
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            route_reader=lambda _path: {"current_stage": "ready"},
            clock_ms=lambda: 1_100,
        )
        service.poll_once()

        self.assertFalse(
            any(node["id"].startswith("wfprod-repaired:") for node in client.state["nodes"])
        )
        self.assertEqual(
            production_service.BATCH_CLOSED_MESSAGE,
            machine["metadata"]["workflowRepairedProjection"]["message"],
        )

    def test_closed_refusal_does_not_append_any_post_close_event(self) -> None:
        self._mark_batch_closed()
        journal = self.repo / "manifests" / "cup.events.jsonl"
        before = journal.read_bytes()
        service = production_service.WorkflowProductionService(
            self.repo,
            client=FakeCanvasClient(),
            route_reader=lambda _path: {
                "current_stage": "ready",
                "inputs": {"style_reference_images": {"file_count": 1}},
            },
            clock_ms=lambda: 1_100,
        )
        service.poll_once()
        self.assertEqual(before, journal.read_bytes())

    def _existing_persisted_artifact(
        self,
        config_id: str,
        *,
        source: str = "renders",
        width: int = 96,
        height: int = 96,
    ):
        output_root = self.workspace / "outputs" / source
        output_root.mkdir(parents=True, exist_ok=True)
        path = output_root / f"{config_id}.png"
        kind, ordinal = config_id.rsplit("_", 1)
        write_placeholder_png(
            path,
            width=width,
            height=height,
            kind=kind,
            ordinal=int(ordinal),
        )
        artifact = artifact_from_path("cup", path, source=source)
        client = FakeCanvasClient()
        node_id = production_service.output_node_id(
            artifact.batch_id,
            artifact.config_id,
            artifact.source,
        )
        client.state["nodes"].append(
            {
                "id": node_id,
                "type": "image",
                "metadata": {
                    "storageKey": f"image:{source}:{config_id}",
                    "workflowProductionOutput": {
                        "batchId": artifact.batch_id,
                        "configId": artifact.config_id,
                        "source": artifact.source,
                        "sha256": artifact.sha256,
                    },
                },
            }
        )
        return client, artifact

    def test_sync_existing_backfills_persisted_event_with_artifact_evidence(self) -> None:
        client, artifact = self._existing_persisted_artifact("main_05")
        journal = self.repo / "manifests" / "cup.events.jsonl"
        production_service.run_controller.append_event(
            journal,
            "image_persisted",
            request_id="req-existing",
            config_id="main_01",
        )
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            artifact_reader=lambda _manifest: (artifact,),
            clock_ms=lambda: 1_100,
        )

        service._sync_existing(
            client.state["nodes"][0],
            json.loads(self.manifest.read_text(encoding="utf-8")),
            journal,
            "req-backfill",
        )

        events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        persisted = [
            event
            for event in events
            if event.get("event") == "image_persisted"
            and event.get("config_id") == artifact.config_id
        ]
        self.assertEqual(1, len(persisted))
        self.assertEqual(
            {
                "event": "image_persisted",
                "request_id": "req-backfill",
                "config_id": artifact.config_id,
                "source": artifact.source,
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
                "width": artifact.width,
                "height": artifact.height,
                "backfilled": True,
            },
            {key: value for key, value in persisted[0].items() if key != "ts"},
        )
        self.assertEqual([], client.ops)

    def test_sync_existing_backfill_is_idempotent_per_config_id(self) -> None:
        client, artifact = self._existing_persisted_artifact("main_05")
        journal = self.repo / "manifests" / "cup.events.jsonl"
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            artifact_reader=lambda _manifest: (artifact,),
            clock_ms=lambda: 1_100,
        )

        service._sync_existing(
            client.state["nodes"][0],
            manifest,
            journal,
            "req-backfill-first",
        )
        service._sync_existing(
            client.state["nodes"][0],
            manifest,
            journal,
            "req-backfill-second",
        )

        events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        persisted = [
            event
            for event in events
            if event.get("event") == "image_persisted"
            and event.get("config_id") == artifact.config_id
        ]
        self.assertEqual(1, len(persisted))
        self.assertEqual("req-backfill-first", persisted[0]["request_id"])
        self.assertTrue(persisted[0]["backfilled"])
        self.assertEqual([], client.ops)

    def test_normal_projection_event_does_not_claim_backfill(self) -> None:
        path = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(path, width=96, height=96, kind="main", ordinal=1)
        artifact = artifact_from_path("cup", path)
        client = FakeCanvasClient()
        journal = self.repo / "manifests" / "cup.events.jsonl"
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            clock_ms=lambda: 1_100,
            sleep=lambda _seconds: None,
            persistence_timeout_ms=50,
        )

        service._project_artifact(
            client.state["nodes"][0],
            artifact,
            journal,
            "req-first-projection",
        )

        events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(events))
        self.assertEqual("image_persisted", events[0]["event"])
        self.assertNotIn("backfilled", events[0])

    def test_journal_has_event_preserves_name_lookup_and_matches_config_id(self) -> None:
        journal = self.repo / "manifests" / "cup.events.jsonl"
        for config_id in ("main_01", "main_02"):
            production_service.run_controller.append_event(
                journal,
                "image_persisted",
                config_id=config_id,
            )
        production_service.run_controller.append_event(
            journal,
            "production_completed",
        )

        service_has_event = production_service.WorkflowProductionService._journal_has_event
        self.assertTrue(service_has_event(journal, "image_persisted"))
        self.assertTrue(service_has_event(journal, "image_persisted", config_id=None))
        self.assertTrue(service_has_event(journal, "image_persisted", config_id="main_01"))
        self.assertTrue(service_has_event(journal, "image_persisted", config_id="main_02"))
        self.assertFalse(service_has_event(journal, "image_persisted", config_id="main_03"))
        self.assertTrue(service_has_event(journal, "production_completed"))
        self.assertFalse(
            service_has_event(journal, "production_completed", config_id="main_01")
        )

    def test_repaired_projection_backfills_once_when_persisted_node_is_skipped(self) -> None:
        client, artifact = self._existing_persisted_artifact(
            "detail_01",
            source="repaired",
            width=96,
            height=128,
        )
        repaired_root = artifact.path.parent
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["outputs"]["repaired"] = [str(repaired_root)]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        journal = self.repo / "manifests" / "cup.events.jsonl"
        production_service.run_controller.append_event(
            journal,
            "repaired_image_persisted",
            request_id="repair-existing",
            config_id="main_01",
        )
        machine = client.state["nodes"][0]
        machine["metadata"]["workflowProduction"]["status"] = "completed"
        service = production_service.WorkflowProductionService(
            self.repo,
            client=client,
            executor_builder=lambda *_args: (_ for _ in ()).throw(
                AssertionError("repaired projection must not build an executor")
            ),
            route_reader=lambda _path: {"current_stage": "ready"},
            clock_ms=lambda: 1_100,
            batch_lock_root=self.root / "batch-locks",
        )

        for request_id in ("repair-backfill-first", "repair-backfill-second"):
            machine["metadata"]["workflowRepairedProjection"] = {
                "status": "queued",
                "requestId": request_id,
                "requestedAt": 1_000,
                "batchId": "cup",
            }
            service.poll_once()

        events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        persisted = [
            event
            for event in events
            if event.get("event") == "repaired_image_persisted"
            and event.get("config_id") == artifact.config_id
        ]
        self.assertEqual(1, len(persisted))
        self.assertEqual(
            {
                "event": "repaired_image_persisted",
                "request_id": "repair-backfill-first",
                "config_id": artifact.config_id,
                "source": artifact.source,
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
                "width": artifact.width,
                "height": artifact.height,
                "backfilled": True,
            },
            {key: value for key, value in persisted[0].items() if key != "ts"},
        )
        repaired_node_id = production_service.output_node_id(
            artifact.batch_id,
            artifact.config_id,
            artifact.source,
        )
        self.assertFalse(
            any(
                op.get("id") == repaired_node_id
                for ops in client.ops
                for op in ops
            )
        )


if __name__ == "__main__":
    unittest.main()
