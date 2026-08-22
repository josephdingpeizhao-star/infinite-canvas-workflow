from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from delivery import package_delivery  # noqa: E402
from executor_contract import ExecutionResult  # noqa: E402
from image_count_contract import expected_config_ids  # noqa: E402
import state_reader  # noqa: E402
from workflow_batch_acceptance import BatchAcceptanceService  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
import workflow_production_controller as controller  # noqa: E402
from workflow_production_projection import artifact_from_path  # noqa: E402
import workflow_production_service as production_service  # noqa: E402


THREE_TARGETS = ["main", "detail", "final_prompts"]
FOUR_TARGETS = [*THREE_TARGETS, "qc_reports"]
FORBIDDEN_QC_WORDS = ("质检", "QC", "报告")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@dataclass
class RouteFixture:
    repo: Path
    workspace: Path
    manifest_path: Path
    manifest: dict[str, Any]
    config_ids: tuple[str, ...]
    renders: Path
    repaired: Path
    qc_reports: Path

    def route(self) -> dict[str, Any]:
        return state_reader.route_manifest(
            self.manifest,
            self.manifest_path,
            repository_root=self.repo,
        )

    def save(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def add_images(
        self,
        config_ids: tuple[str, ...],
        *,
        destination: str = "renders",
    ) -> None:
        target = self.renders if destination == "renders" else self.repaired
        for ordinal, config_id in enumerate(config_ids, start=1):
            is_main = config_id.startswith("main_")
            write_placeholder_png(
                target / f"{config_id}.png",
                width=96,
                height=96 if is_main else 128,
                kind="main" if is_main else "detail",
                ordinal=ordinal,
            )


def _build_route_fixture(
    root: Path,
    *,
    requested_outputs: list[str],
    main_count: int = 2,
    detail_count: int = 1,
    prompt_ids: tuple[str, ...] | None = None,
) -> RouteFixture:
    repo = root / "repo"
    workspace = root / "workspace"
    manifests = repo / "manifests"
    manifests.mkdir(parents=True)
    renders = workspace / "outputs" / "renders"
    repaired = workspace / "outputs" / "repaired"
    renders.mkdir(parents=True)
    repaired.mkdir(parents=True)
    style_refs = workspace / "inputs" / "style_refs"
    style_refs.mkdir(parents=True)
    (style_refs / "style.jpg").write_bytes(b"style")
    (workspace / ".canvas_batch").write_text(
        json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
        encoding="utf-8",
    )
    (workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")

    identity = workspace / "artifacts" / "identity" / "identity.json"
    style = workspace / "artifacts" / "style_master" / "style.json"
    angles = workspace / "artifacts" / "angle_inventory" / "angles.json"
    variables = workspace / "artifacts" / "variable_configs"
    final_prompts = workspace / "artifacts" / "final_prompts"
    qc_reports = workspace / "artifacts" / "qc_reports"
    comfyui_jobs = workspace / "artifacts" / "comfyui_jobs"
    _write_json(identity, {"artifact_type": "product_identity_archive"})
    _write_json(style, {"artifact_type": "style_master"})
    _write_json(angles, {"artifact_type": "angle_inventory"})
    _write_json(
        variables / "main_variable_configs.json",
        {"artifact_type": "main_variable_config"},
    )
    _write_json(
        variables / "detail_variable_configs.json",
        {"artifact_type": "detail_variable_config"},
    )
    all_ids = expected_config_ids(main_count, detail_count)
    config_ids = prompt_ids if prompt_ids is not None else all_ids
    for config_id in config_ids:
        _write_json(
            final_prompts / f"{config_id}_final_prompt.json",
            {"artifact_type": "final_prompt", "config_id": config_id},
        )
    _write_json(
        final_prompts / "final_prompt_index.json",
        {
            "artifact_type": "final_prompt_index",
            "product_id": "cup",
            "prompt_count": len(config_ids),
            "items": [{"config_id": config_id} for config_id in config_ids],
        },
    )
    qc_reports.mkdir(parents=True)
    comfyui_jobs.mkdir(parents=True)

    manifest_path = manifests / "cup.batch_manifest.json"
    manifest = {
        "batch_id": "cup",
        "product_id": "cup",
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": list(requested_outputs),
        "user_confirmed_facts": {
            "main_image_count": main_count,
            "detail_image_count": detail_count,
        },
        "workspace": {"root": str(workspace)},
        "inputs": {"style_reference_images": [str(style_refs)]},
        "drafts": {},
        "artifacts": {
            "product_identity_archive": str(identity.parent),
            "style_master": str(style.parent),
            "angle_inventory": str(angles.parent),
            "main_variable_configs": str(variables),
            "detail_variable_configs": str(variables),
            "final_prompts": str(final_prompts),
            "comfyui_jobs": str(comfyui_jobs),
            "qc_reports": str(qc_reports),
        },
        "outputs": {
            "renders": [str(renders)],
            "repaired": [str(repaired)],
        },
        "notes": "",
    }
    fixture = RouteFixture(
        repo=repo,
        workspace=workspace,
        manifest_path=manifest_path,
        manifest=manifest,
        config_ids=config_ids,
        renders=renders,
        repaired=repaired,
        qc_reports=qc_reports,
    )
    fixture.save()
    return fixture


class FakeCanvasClient:
    def __init__(self, request_id: str = "req-qc02-1") -> None:
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "position": {"x": 0, "y": 0},
                    "width": 420,
                    "height": 300,
                    "metadata": {
                        "content": f"# workflow-production\n# request-id: {request_id}\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": request_id,
                            "batchId": "cup",
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
                            "receipt": {"batchId": "cup", "imageCount": 2},
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

    @property
    def machine(self) -> dict[str, Any]:
        return self.state["nodes"][0]

    def queue(self, request_id: str) -> None:
        metadata = self.machine["metadata"]
        metadata["content"] = (
            f"# workflow-production\n# request-id: {request_id}\nrun: next"
        )
        metadata["workflowProduction"] = {
            **metadata["workflowProduction"],
            "status": "queued",
            "requestId": request_id,
            "requestedAt": 1_000,
        }

    def call_tool(self, name: str) -> dict[str, Any]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, Any]]) -> int:
        nodes = self.state["nodes"]
        for op in ops:
            if op.get("type") == "update_node":
                node = next(item for item in nodes if item["id"] == op["id"])
                node["metadata"] = {
                    **node.get("metadata", {}),
                    **op.get("metadata", {}),
                }
            elif op.get("type") == "add_node":
                metadata = dict(op.get("metadata") or {})
                output = metadata.get("workflowProductionOutput") or {}
                metadata.update(
                    {
                        "storageKey": f"image:{op['id']}",
                        "status": "success",
                        "workflowProductionOutput": {
                            **output,
                            "persistedAt": 1_100,
                        },
                    }
                )
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


class RecordingExecutor:
    name = "qc02-recording"

    def __init__(
        self,
        step: str,
        executed: list[str],
        execute_step: Callable[[str], None],
    ) -> None:
        self.step = step
        self.executed = executed
        self.execute_step = execute_step

    def execute(self, request: Any) -> ExecutionResult:
        self.executed.append(request.step)
        self.execute_step(request.step)
        return ExecutionResult(detail="ok", provider=self.name)


class Qc02DefaultAndRoutingTest(unittest.TestCase):
    def test_default_declaration_is_exactly_three_targets(self) -> None:
        self.assertEqual(tuple(THREE_TARGETS), controller.PRODUCTION_REQUESTED_OUTPUTS)
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "cup.batch_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "product_id": "cup",
                        "requested_outputs": [],
                        "inputs": {},
                        "drafts": {},
                        "artifacts": {},
                        "outputs": {},
                    }
                ),
                encoding="utf-8",
            )
            result = controller.apply_production_requested_outputs(manifest_path)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(result["changed"])
        self.assertEqual(THREE_TARGETS, saved["requested_outputs"])
        self.assertNotIn("qc_reports", saved["requested_outputs"])

    def test_nonempty_declaration_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "cup.batch_manifest.json"
            original = {"product_id": "cup", "requested_outputs": ["main"]}
            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(controller.ProductionGateError):
                controller.apply_production_requested_outputs(manifest_path)
            self.assertEqual(
                original,
                json.loads(manifest_path.read_text(encoding="utf-8")),
            )

    def test_manual_four_target_declaration_is_preserved_for_qc_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "cup.batch_manifest.json"
            original = {
                "product_id": "cup",
                "requested_outputs": FOUR_TARGETS,
            }
            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            result = controller.apply_production_requested_outputs(manifest_path)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(result["changed"])
        self.assertEqual(FOUR_TARGETS, result["requested_outputs"])
        self.assertEqual(original, saved)

    def test_zero_images_without_qc_must_not_be_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=THREE_TARGETS,
            )
            route = fixture.route()
        self.assertEqual("needs_generated_images_before_qc", route["current_stage"])
        self.assertIsNone(route["next_skill"])
        self.assertIsNone(route["next_required_skill"])
        self.assertEqual(["generated_images"], route["missing_required_artifacts"])
        self.assertEqual(["尚未生成任何图片，需先完成出图。"], route["blocked_reasons"])
        combined = " ".join(route["blocked_reasons"])
        for forbidden in FORBIDDEN_QC_WORDS:
            self.assertNotIn(forbidden, combined)

    def test_dynamic_partial_and_full_coverage_uses_renders_and_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=THREE_TARGETS,
                main_count=2,
                detail_count=1,
            )
            self.assertEqual(3, len(fixture.config_ids))
            fixture.add_images(fixture.config_ids[:-1])
            partial = fixture.route()
            fixture.add_images(
                fixture.config_ids[-1:],
                destination="repaired",
            )
            complete = fixture.route()
        self.assertEqual(
            "needs_generated_images_before_qc",
            partial["current_stage"],
        )
        self.assertIsNone(partial["next_skill"])
        self.assertIsNone(partial["next_required_skill"])
        self.assertEqual(["generated_images"], partial["missing_required_artifacts"])
        self.assertEqual(["尚有图片未生成，需先完成出图。"], partial["blocked_reasons"])
        for forbidden in FORBIDDEN_QC_WORDS:
            self.assertNotIn(forbidden, " ".join(partial["blocked_reasons"]))
        self.assertEqual("ready", complete["current_stage"])
        self.assertEqual([], complete["missing_required_artifacts"])
        self.assertIsNone(
            controller.next_gated_command(
                complete,
                accepted_render_count=3,
                total_count=3,
            )
        )

    def test_non_typical_main_and_final_prompts_still_require_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main_ids = ("main_01", "main_02")
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=["main", "final_prompts"],
                main_count=2,
                detail_count=1,
                prompt_ids=main_ids,
            )
            before = fixture.route()
            fixture.add_images(main_ids)
            after = fixture.route()
        self.assertEqual("needs_generated_images_before_qc", before["current_stage"])
        self.assertEqual("ready", after["current_stage"])

    def test_four_targets_keep_qc_route_and_resume_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=FOUR_TARGETS,
            )
            zero = fixture.route()
            fixture.add_images(fixture.config_ids[:-1])
            partial = fixture.route()
            fixture.add_images(fixture.config_ids[-1:])
            full = fixture.route()
            command = controller.next_gated_command(
                full,
                accepted_render_count=len(fixture.config_ids),
                total_count=len(fixture.config_ids),
            )
            _write_json(
                fixture.qc_reports / "qc_report.json",
                {"artifact_type": "qc_report", "product_id": "cup"},
            )
            ready = fixture.route()
        self.assertEqual("needs_generated_images_before_qc", zero["current_stage"])
        self.assertEqual("needs_generated_images_before_qc", partial["current_stage"])
        self.assertEqual("needs_qc_reports", full["current_stage"])
        self.assertEqual("run: qc", command)
        self.assertEqual("ready", ready["current_stage"])


class Qc02ProductionServiceTest(unittest.TestCase):
    @staticmethod
    def _artifact_reader(fixture: RouteFixture):
        def read(_manifest: dict[str, Any]):
            paths = sorted(fixture.renders.glob("*.png"))
            return tuple(artifact_from_path("cup", path) for path in paths)

        return read

    def test_three_targets_complete_with_truthful_dynamic_message_and_idempotent_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=[],
                main_count=2,
                detail_count=1,
            )
            shutil.copytree(ROOT / "categories", fixture.repo / "categories")
            client = FakeCanvasClient()
            executed: list[str] = []
            integrity_passed = False

            def execute_step(step: str) -> None:
                nonlocal integrity_passed
                if step == "integrity":
                    integrity_passed = True
                    return
                if step != "renders":
                    raise AssertionError(step)
                for ordinal, config_id in enumerate(fixture.config_ids, start=1):
                    is_main = config_id.startswith("main_")
                    path = fixture.renders / f"{config_id}.png"
                    write_placeholder_png(
                        path,
                        width=96,
                        height=96 if is_main else 128,
                        kind="main" if is_main else "detail",
                        ordinal=ordinal,
                    )

            def executor_builder(step, _manifest, _path, on_output):
                def execute_and_project(current_step: str) -> None:
                    execute_step(current_step)
                    if current_step == "renders":
                        for config_id in fixture.config_ids:
                            on_output(
                                artifact_from_path(
                                    "cup",
                                    fixture.renders / f"{config_id}.png",
                                )
                            )

                return RecordingExecutor(step, executed, execute_and_project)

            service = production_service.WorkflowProductionService(
                fixture.repo,
                client=client,
                executor_builder=executor_builder,
                integrity_reader=lambda _route: {
                    "found": integrity_passed,
                    "status": "pass" if integrity_passed else "",
                    "render_blocked": False,
                },
                artifact_reader=self._artifact_reader(fixture),
                clock_ms=lambda: 1_100,
                environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
            )
            service.poll_once()
            production = client.machine["metadata"]["workflowProduction"]
            journal = fixture.repo / "manifests" / "cup.events.jsonl"
            first_events = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

            client.queue("req-qc02-2")
            service.poll_once()
            second_events = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(fixture.manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(["integrity", "renders"], executed)
        self.assertNotIn("qc", executed)
        self.assertEqual(THREE_TARGETS, saved["requested_outputs"])
        self.assertEqual("completed", production["status"])
        self.assertEqual(3, production["producedCount"])
        self.assertEqual(
            "3 张真实图片已全部完成。",
            production["message"],
        )
        for forbidden in FORBIDDEN_QC_WORDS:
            self.assertNotIn(forbidden, production["message"])
        self.assertNotIn(
            "production_paused",
            [event["event"] for event in first_events],
        )
        self.assertEqual(
            1,
            sum(
                event["event"] == "production_completed"
                for event in first_events
            ),
        )
        self.assertEqual(
            1,
            sum(
                event["event"] == "production_completed"
                for event in second_events
            ),
        )

    def test_four_targets_still_run_qc_and_keep_original_completion_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=FOUR_TARGETS,
                main_count=2,
                detail_count=1,
            )
            shutil.copytree(ROOT / "categories", fixture.repo / "categories")
            fixture.add_images(fixture.config_ids)
            client = FakeCanvasClient()
            executed: list[str] = []

            def executor_builder(step, _manifest, _path, _on_output):
                def execute_step(current_step: str) -> None:
                    if current_step != "qc":
                        raise AssertionError(current_step)
                    _write_json(
                        fixture.qc_reports / "qc_report.json",
                        {"artifact_type": "qc_report", "product_id": "cup"},
                    )

                return RecordingExecutor(step, executed, execute_step)

            service = production_service.WorkflowProductionService(
                fixture.repo,
                client=client,
                executor_builder=executor_builder,
                artifact_reader=self._artifact_reader(fixture),
                clock_ms=lambda: 1_100,
                environment={},
            )
            service.poll_once()
            production = client.machine["metadata"]["workflowProduction"]

        self.assertEqual(["qc"], executed)
        self.assertEqual("completed", production["status"])
        self.assertEqual("质检完成，QC 报告已生成。", production["message"])


class Qc02AcceptanceDeliveryTest(unittest.TestCase):
    def test_three_targets_close_and_deliver_without_qc_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _build_route_fixture(
                Path(tmp),
                requested_outputs=THREE_TARGETS,
                main_count=2,
                detail_count=1,
            )
            fixture.add_images(fixture.config_ids)
            selections = [
                {
                    "configId": config_id,
                    "source": "renders",
                    "sha256": hashlib.sha256(
                        (fixture.renders / f"{config_id}.png").read_bytes()
                    ).hexdigest(),
                }
                for config_id in fixture.config_ids
            ]
            closed = BatchAcceptanceService(fixture.repo).close(
                "cup",
                {
                    "requestId": "accept-qc02",
                    "machineId": "machine",
                    "selections": selections,
                },
            )
            journal = fixture.repo / "manifests" / "cup.events.jsonl"
            result = package_delivery(
                fixture.manifest,
                fixture.manifest_path,
                journal_path=journal,
                request_id="deliver-qc02",
                packaged_at="2026-07-30T00:00:00Z",
            )

        self.assertEqual(3, closed["selectionCount"])
        self.assertEqual(3, result.item_count)
        self.assertEqual({"renders": 3, "repaired": 0}, result.source_counts)


if __name__ == "__main__":
    unittest.main()
