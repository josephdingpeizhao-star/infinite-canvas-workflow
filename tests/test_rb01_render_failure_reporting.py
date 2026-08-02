from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from final_prompt_integrity_fixtures import (  # noqa: E402
    build_final_prompt_bundle,
    write_json,
)
from image_production_executor import ImageProductionExecutor  # noqa: E402
from openai_image_executor import OpenAIImageExecutor  # noqa: E402
from render_task_assembler import RenderTaskAssemblyError, _reference_image  # noqa: E402
from workflow_production_service import WorkflowProductionService  # noqa: E402


class FailureExecutor:
    name = "rb01-failure"

    def __init__(self, failure: ExecutorExecutionError):
        self.failure = failure

    def execute(self, _request: ExecutionRequest):
        raise self.failure


class FakeCanvasClient:
    def __init__(self, batch_id: str):
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "metadata": {
                        "content": "# workflow-production\n# request-id: req-rb01\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-rb01",
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
                    "metadata": {"content": "blob:original", "storageKey": "image:original"},
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "image-machine", "fromNodeId": "original", "toNodeId": "machine"},
            ],
        }

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        for op in ops:
            if op.get("type") != "update_node":
                continue
            node = next(item for item in self.state["nodes"] if item["id"] == op["id"])
            node["metadata"] = {
                **node.get("metadata", {}),
                **op.get("metadata", {}),
            }
        return len(ops)


class RenderFailureReportingTest(unittest.TestCase):
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

    def _run_service_failure(
        self,
        failure: ExecutorExecutionError,
        *,
        accepted_artifacts: tuple[object, ...] = (),
    ) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            (repo / "manifests").mkdir(parents=True)
            shutil.copytree(ROOT / "categories", repo / "categories")
            bundle = build_final_prompt_bundle(root / "fixture")
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
                "OPENAI_API_KEY": "unit-test-placeholder",
            }
            artifact_read_count = 0

            def read_accepted_artifacts(_manifest):
                nonlocal artifact_read_count
                artifact_read_count += 1
                # The first read is the normal pre-run disk projection. Tests
                # that model a mid-run render make it visible to the failure
                # qualification read without feeding a fake artifact to that
                # projection path.
                if accepted_artifacts and artifact_read_count == 1:
                    return ()
                return accepted_artifacts

            service = WorkflowProductionService(
                repo,
                client=client,
                executor_builder=lambda _step, _manifest, _path, _on_output: FailureExecutor(
                    failure
                ),
                route_reader=self._route,
                integrity_reader=lambda _route: {
                    "found": True,
                    "status": "pass",
                    "render_blocked": False,
                },
                artifact_reader=read_accepted_artifacts,
                render_artifact_reader=lambda _manifest: (),
                repaired_artifact_reader=lambda _manifest: (),
                clock_ms=lambda: 1_100,
                environment=environment,
                batch_lock_root=root / "locks",
            )
            service.poll_once()
            production = dict(
                client.state["nodes"][0]["metadata"]["workflowProduction"]
            )
            events = [
                json.loads(line)
                for line in (repo / "manifests" / f"{batch_id}.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            failed_event = next(event for event in events if event["event"] == "step_failed")
            return production, failed_event

    @staticmethod
    def _missing_failure() -> ExecutorExecutionError:
        failure = ExecutorExecutionError("internal assembly detail")
        failure.code = "render_input_missing"
        failure.missing_files = ("白底 背面.png",)
        failure.missing_count = 1
        failure.remaining_count = 3
        return failure

    def test_service_writes_fail_closed_recovery_and_readable_card(self) -> None:
        production, event = self._run_service_failure(self._missing_failure())
        self.assertEqual("failed", production["status"])
        self.assertEqual(
            {
                "kind": "missing_reference",
                "files": ["白底 背面.png"],
                "recomputeEligible": True,
            },
            production["recovery"],
        )
        self.assertEqual(
            "白底图 白底 背面.png 已不在批次目录里。可恢复文件后重新开始；"
            "或剔除缺失图，用剩余 3 张重新分配角度与绑定（重排不产生模型费用，"
            "出图前会重新报价并由你确认）。",
            production["errorMessage"],
        )
        self.assertEqual("render_input_missing", event["failure_code"])
        self.assertEqual("渲染失败：白底图 白底 背面.png 缺失", event["detail"])

    def test_existing_render_disables_recompute_but_keeps_readable_reason(self) -> None:
        production, _event = self._run_service_failure(
            self._missing_failure(),
            accepted_artifacts=(object(),),
        )
        self.assertFalse(production["recovery"]["recomputeEligible"])
        self.assertEqual(
            "白底图 白底 背面.png 已不在批次目录里。可恢复文件后重新开始。",
            production["errorMessage"],
        )

    def test_invalid_structured_fields_remove_recovery_and_keep_generic_fallback(self) -> None:
        failure = self._missing_failure()
        failure.missing_count = True
        failure.args = (r"private failure D:\secret\input.jpg",)
        production, event = self._run_service_failure(failure)
        self.assertNotIn("recovery", production)
        self.assertEqual(
            "这一步没做好，机器已停下。已经完成的成果都保留了。",
            production["errorMessage"],
        )
        self.assertEqual("执行已停止，未自动重试", event["detail"])
        self.assertNotIn("failure_code", event)
        self.assertNotIn("secret", json.dumps(production, ensure_ascii=False))

    def test_inputs_unavailable_has_readable_noneligible_recovery(self) -> None:
        failure = ExecutorExecutionError("internal input root detail")
        failure.code = "render_inputs_unavailable"
        production, event = self._run_service_failure(failure)
        self.assertEqual(
            {
                "kind": "inputs_unavailable",
                "files": [],
                "recomputeEligible": False,
            },
            production["recovery"],
        )
        self.assertEqual(
            "白底图目录整体无法访问，本次已停止。请恢复 inputs/white_bg 后再重新开始。",
            production["errorMessage"],
        )
        self.assertEqual("render_inputs_unavailable", event["failure_code"])

    def test_existing_er01_timeout_shape_still_uses_its_structured_message(self) -> None:
        failure = ExecutorExecutionError("private timeout detail")
        failure.code = "render_timeout"
        failure.timeout_seconds = 90
        failure.successful_count = 0
        failure.planned_count = 7
        failure.skipped_count = 0
        self.assertEqual(
            "图片服务等待超时（90 秒）。本轮成功 0 张、计划 7 张、跳过 0 张。"
            "机器已停下，未自动重试，已完成的成果都保留了。",
            WorkflowProductionService._safe_failure(failure),
        )
        self.assertEqual(
            "渲染失败：图片服务等待超时 90 秒；成功 0/计划 7/跳过 0",
            WorkflowProductionService._safe_event_detail(failure),
        )

    def test_assembler_and_image_production_wrapper_preserve_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            white_bg = root / "inputs" / "white_bg"
            white_bg.mkdir(parents=True)
            (white_bg / "正面.jpg").write_bytes(b"image")
            manifest = {
                "inputs": {"white_bg_images": [str(white_bg)]},
                "artifacts": {"final_prompts": [str(root / "final_prompts")]},
            }
            with self.assertRaises(RenderTaskAssemblyError) as assembly_caught:
                _reference_image(manifest, "背面.png")
            assembly = assembly_caught.exception
            self.assertEqual("render_input_missing", assembly.code)
            self.assertEqual(("背面.png",), assembly.missing_files)
            self.assertEqual((1, 1), (assembly.missing_count, assembly.remaining_count))

            context = ExecutorContext(
                manifest=manifest,
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": "unit-test-placeholder",
                },
            )

            def fail_assembly(_manifest, _index):
                raise assembly

            executor = ImageProductionExecutor(context, task_assembler=fail_assembly)
            with self.assertRaises(ExecutorExecutionError) as wrapped_caught:
                executor.execute(ExecutionRequest(step="renders"))
            wrapped = wrapped_caught.exception
            self.assertEqual("render_input_missing", wrapped.code)
            self.assertEqual(("背面.png",), wrapped.missing_files)
            self.assertEqual((1, 1), (wrapped.missing_count, wrapped.remaining_count))

    def test_openai_mid_batch_missing_reference_uses_only_sanitized_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "private" / "白底 背面.png"
            executor = OpenAIImageExecutor(ExecutorContext(manifest={}, environment={}))
            task = ImageGenerationTask(
                prompt="safe unit test",
                output_path=root / "output.png",
                reference_images=(missing,),
            )
            with self.assertRaises(ExecutorExecutionError) as caught:
                executor._validate_task(task)
            failure = caught.exception
            self.assertEqual("render_input_missing", failure.code)
            self.assertEqual(("白底 背面.png",), failure.missing_files)
            self.assertEqual(1, failure.missing_count)
            self.assertNotIn(str(root), str(failure))

    def test_inconsistent_missing_file_list_degrades_to_count_only(self) -> None:
        failure = self._missing_failure()
        failure.missing_files = ("白底 背面.png", "白底 背面.png")
        failure.missing_count = 2

        production, event = self._run_service_failure(failure)

        self.assertEqual("failed", production["status"])
        self.assertEqual(
            {
                "kind": "missing_reference",
                "files": [],
                "recomputeEligible": True,
            },
            production["recovery"],
        )
        self.assertEqual(
            "有 2 张白底图已不在批次目录里。可恢复文件后重新开始；"
            "或剔除缺失图，用剩余 3 张重新分配角度与绑定（重排不产生模型费用，"
            "出图前会重新报价并由你确认）。",
            production["errorMessage"],
        )
        self.assertEqual("渲染失败：缺失 2 张白底图", event["detail"])
        self.assertNotIn(
            "白底 背面.png",
            json.dumps({"production": production, "event": event}, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
