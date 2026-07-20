from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionResult, ExecutorExecutionError  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_projection import artifact_from_path  # noqa: E402
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


class ProductionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        (self.repo / "manifests").mkdir(parents=True)
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
        )
        service.poll_once()

        self.assertEqual(["identity", "style_master"], executed)
        saved = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(["main", "detail", "final_prompts", "qc_reports"], saved["requested_outputs"])
        machine = client.state["nodes"][0]
        self.assertEqual("failed", machine["metadata"]["workflowProduction"]["status"])
        self.assertNotIn("secret", machine["metadata"]["workflowProduction"]["errorMessage"])

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
        )
        service.poll_once()
        self.assertEqual(["renders"], executed)

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


if __name__ == "__main__":
    unittest.main()
