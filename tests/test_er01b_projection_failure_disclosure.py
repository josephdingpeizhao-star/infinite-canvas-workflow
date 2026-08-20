from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import ic_client  # noqa: E402
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from failure_text_safety import is_disclosable  # noqa: E402
from final_prompt_integrity_fixtures import (  # noqa: E402
    build_final_prompt_bundle,
    write_json,
)
from image_production_executor import ImageProductionExecutor  # noqa: E402
from render_task_assembler import RenderTaskPlan  # noqa: E402
from workflow_production_projection import (  # noqa: E402
    WorkflowProductionArtifact,
    output_node_id,
)
from workflow_production_service import WorkflowProductionService  # noqa: E402


PERSISTENCE_TIMEOUT_DETAIL = "真实图片没有在规定时间内完成浏览器持久化"
FALLBACK_EVENT = "执行已停止，未自动重试"
FALLBACK_CARD = "这一步没做好，机器已停下。已经完成的成果都保留了。"
CANVAS_TITLE = "画布暂时不可用，真实图片未完成上桌"


class RaisingExecutor:
    name = "raising-fixture"

    def __init__(self, failure: BaseException):
        self.failure = failure

    def execute(self, _request: ExecutionRequest) -> ExecutionResult:
        raise self.failure


class SuccessfulExecutor:
    name = "success-fixture"

    def execute(self, _request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(detail="ok", provider=self.name)


class DisclosureCanvasClient:
    def __init__(self, batch_id: str = "cup") -> None:
        self.state: dict[str, object] = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "position": {"x": 0, "y": 0},
                    "width": 420,
                    "height": 300,
                    "metadata": {
                        "content": "# workflow-production\n# request-id: req-er01b\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-er01b",
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
                            "receipt": {"batchId": batch_id, "imageCount": 1},
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
        self.get_failures: list[BaseException] = []
        self.apply_failures: list[BaseException] = []
        self.call_count = 0
        self.apply_count = 0

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        self.call_count += 1
        if self.get_failures:
            raise self.get_failures.pop(0)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        self.apply_count += 1
        if self.apply_failures:
            raise self.apply_failures.pop(0)
        nodes = self.state["nodes"]
        assert isinstance(nodes, list)
        for op in ops:
            if op.get("type") == "update_node":
                node = next(item for item in nodes if item["id"] == op["id"])
                node["metadata"] = {
                    **node.get("metadata", {}),
                    **op.get("metadata", {}),
                }
            elif op.get("type") == "add_node":
                metadata = dict(op.get("metadata") or {})
                metadata["storageKey"] = f"image:{op['id']}"
                metadata["status"] = "success"
                nodes.append(
                    {
                        "id": op["id"],
                        "type": op["nodeType"],
                        "position": op["position"],
                        "width": op["width"],
                        "height": op["height"],
                        "metadata": metadata,
                    }
                )
        return len(ops)


class Er01bProjectionFailureDisclosureTest(unittest.TestCase):
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
    def _render_plan(renders_dir: Path, count: int = 3) -> RenderTaskPlan:
        tasks = tuple(
            ImageGenerationTask(
                prompt=f"safe fixture prompt {index}",
                output_path=renders_dir / f"main_{index:02d}.png",
            )
            for index in range(1, count + 1)
        )
        return RenderTaskPlan(
            tasks=tasks,
            planned=tuple(task.output_path.stem for task in tasks),
            skipped=(),
        )

    @staticmethod
    def _artifact(root: Path, *, source: str = "renders") -> WorkflowProductionArtifact:
        return WorkflowProductionArtifact(
            batch_id="cup",
            config_id="main_01",
            path=root / "main_01.png",
            sha256="a" * 64,
            width=100,
            height=100,
            byte_count=128,
            source=source,
        )

    @staticmethod
    def _events(path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def _run_failure(
        self,
        cause: BaseException,
    ) -> tuple[str, dict[str, object]]:
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
            client = DisclosureCanvasClient(batch_id)
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
                return ImageProductionExecutor(
                    context,
                    image_executor_factory=lambda _context: RaisingExecutor(cause),
                    task_assembler=lambda _manifest, _index: self._render_plan(
                        bundle.renders_dir
                    ),
                    sleep_fn=lambda _delay: None,
                    jitter_fn=lambda _minimum, _maximum: 0.0,
                )

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
            card = machine["metadata"]["workflowProduction"]["errorMessage"]
            events = self._events(
                repo / "manifests" / f"{batch_id}.events.jsonl"
            )
            failed = next(event for event in events if event["event"] == "step_failed")
            return str(card), failed

    def _wrapped_failure(self, cause: BaseException) -> ExecutorExecutionError:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp) / "fixture")
            context = ExecutorContext(
                manifest=bundle.manifest,
                manifest_path=bundle.manifest_path,
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": "server-secret",
                },
            )
            executor = ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: RaisingExecutor(cause),
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir
                ),
                sleep_fn=lambda _delay: None,
                jitter_fn=lambda _minimum, _maximum: 0.0,
            )
            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="renders"))
            return caught.exception

    def test_is_disclosable_accepts_normal_chinese_and_length_boundary(self) -> None:
        class StringSubclass(str):
            pass

        self.assertTrue(is_disclosable("正式图片不在当前批次登记图位中。"))
        self.assertTrue(is_disclosable("安" * 200))
        self.assertFalse(is_disclosable(""))
        self.assertFalse(is_disclosable(StringSubclass("安全原因")))

    def test_is_disclosable_rejects_sensitive_and_malformed_text(self) -> None:
        unsafe = (
            "http://127.0.0.1:17371/api/tools",
            r"D:\private\image.png",
            r"\\server\share\image.png",
            "/var/private/image.png",
            "sk-fake123456",
            "Bearer abc123",
            "access token expired",
            "api_key missing",
            "secret value",
            "令牌已失效",
            "密钥缺失",
            "第一行\n第二行",
            "第一行\r第二行",
            "长" * 201,
        )
        for value in unsafe:
            with self.subTest(value=value):
                self.assertFalse(is_disclosable(value))

    def test_wrapper_sanitizes_reason_sets_code_and_keeps_structured_counts(self) -> None:
        wrapped = self._wrapped_failure(
            ExecutorExecutionError(
                "正常失败 server-secret safe fixture prompt 1"
            )
        )

        self.assertEqual("正常失败 [REDACTED] [PROMPT]", str(wrapped))
        self.assertEqual("render_pipeline_error", wrapped.code)
        self.assertEqual(0, wrapped.successful_count)
        self.assertEqual(3, wrapped.planned_count)
        self.assertEqual(0, wrapped.skipped_count)
        self.assertNotIn("成功 0/计划 3", str(wrapped))

    def test_safe_pipeline_failures_reach_journal_and_card_with_counts(self) -> None:
        reasons = (
            "图片任务缺少 prompt",
            "图片服务响应格式不正确",
            "图片服务返回了无效 Base64 数据",
            "参考图无法解析为图像，已停止",
            "详情图返回 2:3，等待人工尺寸处理",
            "自动扩边后图片尺寸不正确",
            "主图不是正方形，已停止",
            "详情图比例不正确，已停止",
            "审计发现同名图片冲突",
            "观察器发现正式图片不在登记图位中",
            "正式图片不是有效 PNG",
            "正式图片尺寸无效",
            "正式图片不在当前批次登记图位中。",
            "工作流机器缺少 id",
            "真实工作流服务已停止",
            "长" * 201,
            "界" * 200 + " https://private.test/events",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                card, event = self._run_failure(ExecutorExecutionError(reason))
                display_reason = reason[:200].rstrip("。")
                event_prefix = "渲染失败："
                event_suffix = "；成功 0/计划 3/跳过 0"
                event_reason_limit = 160 - len(event_prefix) - len(event_suffix)
                self.assertEqual(
                    f"{event_prefix}{display_reason[:event_reason_limit]}{event_suffix}",
                    event["detail"],
                )
                self.assertEqual("render_pipeline_error", event["failure_code"])
                self.assertEqual(
                    f"{display_reason}。本轮成功 0 张、计划 3 张、跳过 0 张。"
                    "机器已停下，未自动重试，已完成的成果都保留了。",
                    card,
                )
                if len(reason) > 200:
                    wrapped = self._wrapped_failure(ExecutorExecutionError(reason))
                    self.assertEqual(reason[:200], str(wrapped))
                    self.assertEqual(200, len(str(wrapped)))
                if "https://" in reason:
                    self.assertNotIn("https://", event["detail"])
                    self.assertNotIn("https://", card)

    def test_unsafe_pipeline_failures_keep_existing_fail_closed_fallbacks(self) -> None:
        reasons = (
            r"无法读取 D:\private\main_01.png",
            "审计文件 /var/private/report.json 无法读取",
            "账本写入失败 https://private.test/events",
            r"成图保存失败 D:\private\outputs\main_01.png",
            "文件读取失败 /var/private/input.png",
            r"账本写入失败 D:\private\events.jsonl",
            r"共享文件读取失败 \\server\share\main_01.png",
            "provider token leaked",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                card, event = self._run_failure(ExecutorExecutionError(reason))
                self.assertEqual(FALLBACK_EVENT, event["detail"])
                self.assertEqual(FALLBACK_CARD, card)
                self.assertNotIn("failure_code", event)
                self.assertNotIn(reason, event["detail"])
                self.assertNotIn(reason, card)

    def test_canvas_agent_errors_use_one_fixed_message_without_raw_details(self) -> None:
        messages = (
            "HTTP 500 from http://127.0.0.1:17371/api/tools: internal",
            "cannot reach canvas-agent at http://127.0.0.1:17371: refused",
            'tool canvas_apply_ops failed: {"ok": false, "token": "private"}',
        )
        for raw in messages:
            with self.subTest(raw=raw):
                card, event = self._run_failure(ic_client.CanvasAgentError(raw))
                self.assertEqual(
                    f"渲染失败：{CANVAS_TITLE}；成功 0/计划 3/跳过 0",
                    event["detail"],
                )
                self.assertEqual("render_canvas_unavailable", event["failure_code"])
                self.assertEqual(
                    f"{CANVAS_TITLE}。本轮成功 0 张、计划 3 张、跳过 0 张。"
                    "机器已停下，未自动重试，已完成的成果都保留了。",
                    card,
                )
                combined = f"{event['detail']} {card}".lower()
                for marker in ("127.0.0.1", "http", "api/tools"):
                    self.assertNotIn(marker, combined)

        existing_codes = (
            "render_http_error",
            "render_timeout",
            "render_network_error",
            "render_input_missing",
            "render_inputs_unavailable",
        )
        for code in existing_codes:
            with self.subTest(existing_code=code):
                cause = ic_client.CanvasAgentError("http://127.0.0.1/api/tools")
                cause.code = code
                wrapped = self._wrapped_failure(cause)
                self.assertEqual(code, wrapped.code)

    def test_er01_three_codes_keep_exact_journal_and_card_messages(self) -> None:
        http = ExecutorExecutionError("private HTTP detail")
        http.code = "render_http_error"
        http.http_status = 403
        http.provider_error_type = "bad_response_status_code"
        http.provider_error_code = "bad_response_status_code"
        timeout = ExecutorExecutionError("private timeout detail")
        timeout.code = "render_timeout"
        timeout.timeout_seconds = 90
        network = ExecutorExecutionError("private network detail")
        network.code = "render_network_error"
        cases = (
            (
                http,
                "图片服务返回错误（HTTP 403，类型 bad_response_status_code，代码 "
                "bad_response_status_code）。本轮成功 0 张、计划 3 张、跳过 0 张。"
                "机器已停下，未自动重试，已完成的成果都保留了。",
                "渲染失败：HTTP 403；类型 bad_response_status_code；代码 "
                "bad_response_status_code；成功 0/计划 3/跳过 0",
            ),
            (
                timeout,
                "图片服务等待超时（90 秒）。本轮成功 0 张、计划 3 张、跳过 0 张。"
                "已自动重试 2 次仍失败，机器已停下，已完成的成果都保留了。",
                "渲染失败：图片服务等待超时 90 秒；成功 0/计划 3/跳过 0",
            ),
            (
                network,
                "无法连接图片服务。本轮成功 0 张、计划 3 张、跳过 0 张。"
                "已自动重试 2 次仍失败，机器已停下，已完成的成果都保留了。",
                "渲染失败：无法连接图片服务；成功 0/计划 3/跳过 0",
            ),
        )
        for failure, expected_card, expected_event in cases:
            with self.subTest(code=failure.code):
                card, event = self._run_failure(failure)
                self.assertEqual(expected_card, card)
                self.assertEqual(expected_event, event["detail"])
                self.assertEqual(failure.code, event["failure_code"])

    def test_input_codes_keep_existing_messages_and_are_not_overwritten(self) -> None:
        missing = ExecutorExecutionError("private missing detail")
        missing.code = "render_input_missing"
        missing.missing_count = 1
        missing.missing_files = ("white_01.png",)
        missing.remaining_count = 2
        unavailable = ExecutorExecutionError("private unavailable detail")
        unavailable.code = "render_inputs_unavailable"
        cases = (
            (
                missing,
                "白底图 white_01.png 已不在批次目录里。可恢复文件后重新开始；"
                "或剔除缺失图，用剩余 2 张重新分配角度与绑定"
                "（重排不产生模型费用，出图前会重新报价并由你确认）。",
                "渲染失败：白底图 white_01.png 缺失",
            ),
            (
                unavailable,
                "白底图目录整体无法访问，本次已停止。请恢复 inputs/white_bg 后再重新开始。",
                "渲染失败：白底图目录整体无法访问",
            ),
        )
        for failure, expected_card, expected_event in cases:
            with self.subTest(code=failure.code):
                card, event = self._run_failure(failure)
                self.assertEqual(expected_card, card)
                self.assertEqual(expected_event, event["detail"])
                self.assertEqual(failure.code, event["failure_code"])

    def test_persistence_timeout_pipeline_code_yields_entirely_to_controlled_table(self) -> None:
        bare = ExecutorExecutionError(PERSISTENCE_TIMEOUT_DETAIL)
        bare_event = WorkflowProductionService._safe_event_detail(bare)
        bare_card = WorkflowProductionService._safe_failure(bare)

        card, event = self._run_failure(
            ExecutorExecutionError(PERSISTENCE_TIMEOUT_DETAIL)
        )

        self.assertEqual(PERSISTENCE_TIMEOUT_DETAIL, bare_event)
        self.assertEqual(
            f"{PERSISTENCE_TIMEOUT_DETAIL}。机器已停下，未自动重试。",
            bare_card,
        )
        self.assertEqual(bare_event, event["detail"])
        self.assertEqual(bare_card, card)
        self.assertNotIn("failure_code", event)
        self.assertNotIn("成功", event["detail"])
        self.assertNotIn("计划", card)

    def test_success_detail_keeps_existing_slash_wording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp) / "fixture")
            context = ExecutorContext(
                manifest=bundle.manifest,
                manifest_path=bundle.manifest_path,
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": "server-secret",
                },
            )
            executor = ImageProductionExecutor(
                context,
                image_executor_factory=lambda _context: SuccessfulExecutor(),
                task_assembler=lambda _manifest, _index: self._render_plan(
                    bundle.renders_dir, count=2
                ),
            )

            result = executor.execute(ExecutionRequest(step="renders"))

        self.assertEqual("成功 2/计划 2（跳过 0）", result.detail)

    def test_persisted_keeps_direct_canvas_error_raise_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = DisclosureCanvasClient()
            client.get_failures.append(ic_client.CanvasAgentError("offline"))
            service = WorkflowProductionService(Path(tmp), client=client)

            with self.assertRaisesRegex(ic_client.CanvasAgentError, "offline"):
                service._persisted("node", "sha", "renders")

    def test_wait_for_persistence_turns_disconnects_into_controlled_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = DisclosureCanvasClient()
            client.get_failures.append(
                ic_client.CanvasAgentError("cannot reach http://127.0.0.1")
            )
            service = WorkflowProductionService(
                Path(tmp),
                client=client,
                sleep=lambda _seconds: None,
                persistence_timeout_ms=0,
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                f"^{PERSISTENCE_TIMEOUT_DETAIL}$",
            ):
                service._wait_for_persistence(self._artifact(Path(tmp)))

        self.assertEqual(1, client.call_count)

    def test_wait_for_persistence_recovers_before_existing_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            client = DisclosureCanvasClient()
            client.get_failures.append(ic_client.CanvasAgentError("offline"))
            client.state["nodes"].append(
                {
                    "id": output_node_id("cup", "main_01", "renders"),
                    "type": "image",
                    "metadata": {
                        "storageKey": "image:main_01",
                        "workflowProductionOutput": {
                            "sha256": artifact.sha256,
                            "source": "renders",
                        },
                    },
                }
            )
            sleeps: list[float] = []
            service = WorkflowProductionService(
                root,
                client=client,
                sleep=sleeps.append,
                persistence_timeout_ms=1_000,
            )

            service._wait_for_persistence(artifact)

        self.assertEqual(2, client.call_count)
        self.assertEqual([0.1], sleeps)

    def test_project_artifact_reconnects_get_state_and_apply_then_writes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "cup.events.jsonl"
            artifact = self._artifact(root)
            client = DisclosureCanvasClient()
            client.get_failures.append(ic_client.CanvasAgentError("state offline"))
            client.apply_failures.append(ic_client.CanvasAgentError("apply offline"))
            sleeps: list[float] = []
            service = WorkflowProductionService(
                root,
                client=client,
                sleep=sleeps.append,
                interval=0.25,
                persistence_timeout_ms=1_000,
            )

            service._project_artifact(
                client.state["nodes"][0],
                artifact,
                journal,
                "req-project",
            )

            events = self._events(journal)
        self.assertEqual(1, len(events))
        self.assertEqual("image_persisted", events[0]["event"])
        self.assertEqual(2, client.apply_count)
        self.assertGreaterEqual(client.call_count, 3)
        self.assertEqual([2.0, 2.0], sleeps)

    def test_sync_existing_reconnects_before_backfill_and_stays_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "cup.events.jsonl"
            artifact = self._artifact(root)
            client = DisclosureCanvasClient()
            client.get_failures.append(ic_client.CanvasAgentError("state offline"))
            client.state["nodes"].append(
                {
                    "id": output_node_id("cup", "main_01", "renders"),
                    "type": "image",
                    "metadata": {
                        "storageKey": "image:main_01",
                        "workflowProductionOutput": {
                            "sha256": artifact.sha256,
                            "source": "renders",
                        },
                    },
                }
            )
            sleeps: list[float] = []
            service = WorkflowProductionService(
                root,
                client=client,
                artifact_reader=lambda _manifest: (artifact,),
                sleep=sleeps.append,
                interval=0.25,
            )
            with mock.patch.object(
                service,
                "_expected_ids",
                return_value=("main_01",),
            ):
                service._sync_existing(
                    client.state["nodes"][0], {}, journal, "req-first"
                )
                service._sync_existing(
                    client.state["nodes"][0], {}, journal, "req-second"
                )

            persisted = [
                event
                for event in self._events(journal)
                if event["event"] == "image_persisted"
            ]
        self.assertEqual(1, len(persisted))
        self.assertTrue(persisted[0]["backfilled"])
        self.assertEqual("req-first", persisted[0]["request_id"])
        self.assertEqual(0, client.apply_count)
        self.assertEqual([2.0], sleeps)

    def test_repaired_projection_reconnects_before_idempotent_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir()
            manifest_path = root / "manifests" / "cup.batch_manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            journal = root / "manifests" / "cup.events.jsonl"
            artifact = self._artifact(root, source="repaired")
            client = DisclosureCanvasClient()
            machine = client.state["nodes"][0]
            machine["metadata"]["workflowRepairedProjection"] = {
                "status": "queued",
                "requestId": "req-repaired",
                "batchId": "cup",
                "requestedAt": 1_000,
            }
            client.get_failures.append(ic_client.CanvasAgentError("state offline"))
            client.state["nodes"].append(
                {
                    "id": output_node_id("cup", "main_01", "repaired"),
                    "type": "image",
                    "metadata": {
                        "storageKey": "image:repaired:main_01",
                        "workflowProductionOutput": {
                            "sha256": artifact.sha256,
                            "source": "repaired",
                        },
                    },
                }
            )
            sleeps: list[float] = []
            service = WorkflowProductionService(
                root,
                client=client,
                repaired_artifact_reader=lambda _manifest: (artifact,),
                route_reader=lambda _path: {"current_stage": "ready"},
                clock_ms=lambda: 1_100,
                sleep=sleeps.append,
                interval=0.25,
            )
            with mock.patch.object(
                service,
                "_load_manifest",
                return_value={},
            ):
                WorkflowProductionService._process_repaired_projection.__wrapped__(
                    service,
                    machine,
                    client.state,
                )

            persisted = [
                event
                for event in self._events(journal)
                if event["event"] == "repaired_image_persisted"
            ]
        self.assertEqual(1, len(persisted))
        self.assertTrue(persisted[0]["backfilled"])
        self.assertEqual([2.0], sleeps)
        self.assertEqual(2, client.apply_count)

    def test_poll_once_keeps_top_level_canvas_error_and_does_not_consume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = DisclosureCanvasClient()
            client.get_failures.append(ic_client.CanvasAgentError("top offline"))
            service = WorkflowProductionService(Path(tmp), client=client)

            with self.assertRaisesRegex(ic_client.CanvasAgentError, "top offline"):
                service.poll_once()

        self.assertEqual({}, service.consumed_content)


if __name__ == "__main__":
    unittest.main()
