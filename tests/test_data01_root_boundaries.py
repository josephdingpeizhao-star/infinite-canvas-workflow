from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import BatchCreator, UploadedFile, prepare_state_root  # noqa: E402
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from delivery import package_delivery  # noqa: E402
from image_production_executor import ImageProductionExecutor  # noqa: E402
import runtime_roots  # noqa: E402
from workflow_batch_acceptance import BatchAcceptanceService  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_service import WorkflowProductionService  # noqa: E402
from workflow_qc_summary import build_qc_summary  # noqa: E402


class DataRootBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    def tearDown(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    @staticmethod
    def _copy_program_fixture(program_root: Path) -> None:
        (program_root / "scripts").mkdir(parents=True)
        (program_root / "canvas-bridge").mkdir()
        (program_root / "manifests").mkdir()
        shutil.copy2(
            ROOT / "scripts" / "build_batch_manifest.py",
            program_root / "scripts",
        )
        for name in ("category_recipes.py", "image_count_contract.py", "runtime_roots.py"):
            shutil.copy2(
                ROOT / "canvas-bridge" / name,
                program_root / "canvas-bridge",
            )
        shutil.copytree(ROOT / "categories", program_root / "categories")
        shutil.copy2(
            ROOT / "manifests" / "batch_manifest.template.json",
            program_root / "manifests",
        )
        template_path = program_root / "manifests" / "asset_manifest.template.json"
        template = json.loads(
            (ROOT / "manifests" / "asset_manifest.template.json").read_text(
                encoding="utf-8"
            )
        )
        template["data01_program_template_marker"] = True
        template_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_program_assets_are_read_only_while_ledgers_reports_and_workspace_use_data_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            program_root = base / "program"
            data_root = base / "data"
            state_root = base / "state"
            upload_path = base / "upload.bin"
            self._copy_program_fixture(program_root)
            program_before = self._snapshot(program_root)
            payload = b"data01-boundary-image"
            upload_path.write_bytes(payload)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        runtime_roots.DATA_ROOT_ENV: str(data_root),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    clear=False,
                ),
            ):
                runtime_roots.ensure_data_layout()
                prepare_state_root(state_root)
                data_repository = runtime_roots.repository_root()
                creator = BatchCreator(
                    repo_root=data_repository,
                    program_root=program_root,
                    state_root=state_root,
                    now=lambda: datetime(2099, 1, 2, 3, 4, 5),
                )
                source = SourceImage(
                    node_id="image-1",
                    storage_key="image:data01",
                    name="杯子正面.png",
                    size=len(payload),
                    mime_type="image/png",
                    last_modified=1,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )
                request = BatchIntakeRequest(
                    request_id="data01-boundary-request",
                    requested_at=1,
                    info_node_id="info-1",
                    workflow_node_id="workflow-1",
                    facts=ConfirmedFacts(
                        product_type="杯子",
                        height_cm=10,
                        main_image_count=1,
                        detail_image_count=1,
                        handheld_main=0,
                        handheld_detail=0,
                        forbid_pouring_and_heating=True,
                        missing_d_no_retake=True,
                    ),
                    source_images=(source,),
                    category="杯类",
                )
                result = creator.create(
                    request,
                    [
                        UploadedFile(
                            source_node_id=source.node_id,
                            path=upload_path,
                            name=source.name,
                            size=len(payload),
                            mime_type=source.mime_type,
                            sha256=hashlib.sha256(payload).hexdigest(),
                        )
                    ],
                )

            self.assertEqual(data_root / "杯类", creator.workspace_parent)
            self.assertEqual(data_root / "杯类" / "杯子_20990102_030405", result.workspace_root)
            self.assertEqual(
                data_root
                / "workflow-runtime"
                / "manifests"
                / "杯子_20990102_030405.batch_manifest.json",
                result.manifest_path,
            )
            self.assertTrue(result.manifest_path.is_file())
            self.assertTrue((data_root / "workflow-runtime" / "reports").is_dir())
            asset_manifest = json.loads(
                (result.workspace_root / "manifests" / "asset_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(asset_manifest["data01_program_template_marker"], True)
            self.assertEqual(program_before, self._snapshot(program_root))
            self.assertFalse((data_root / "workflow-runtime" / "categories").exists())
            self.assertFalse((data_root / "workflow-runtime" / "scripts").exists())

    def test_legacy_acceptance_qc_and_delivery_read_categories_only_from_program_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            program_root = base / "program"
            data_repository = base / "data" / "workflow-runtime"
            workspace = base / "data" / "杯类" / "legacy-cup"
            renders = workspace / "outputs" / "renders"
            repaired = workspace / "outputs" / "repaired"
            (program_root / "categories").mkdir(parents=True)
            shutil.copytree(
                ROOT / "categories" / "杯类",
                program_root / "categories" / "杯类",
            )
            (data_repository / "manifests").mkdir(parents=True)
            (data_repository / "reports").mkdir()
            renders.mkdir(parents=True)
            repaired.mkdir()
            (workspace / ".canvas_batch").write_text(
                json.dumps(
                    {"type": "canvas-batch-v1", "product_id": "legacy-cup"}
                ),
                encoding="utf-8",
            )
            (workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            config_ids = tuple(
                [f"main_{index:02d}" for index in range(1, 7)]
                + [f"detail_{index:02d}" for index in range(1, 9)]
            )
            for ordinal, config_id in enumerate(config_ids, start=1):
                kind = "main" if config_id.startswith("main_") else "detail"
                write_placeholder_png(
                    renders / f"{config_id}.png",
                    width=96,
                    height=96 if kind == "main" else 128,
                    kind=kind,
                    ordinal=ordinal,
                )
            manifest = {
                "batch_id": "legacy-cup",
                "product_id": "legacy-cup",
                "category": "杯类",
                "workspace": {"root": str(workspace)},
                "outputs": {
                    "renders": [str(renders)],
                    "repaired": [str(repaired)],
                },
            }
            manifest_path = (
                data_repository / "manifests" / "legacy-cup.batch_manifest.json"
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            selections = [
                {
                    "config_id": config_id,
                    "source": "renders",
                    "sha256": hashlib.sha256(
                        (renders / f"{config_id}.png").read_bytes()
                    ).hexdigest(),
                }
                for config_id in config_ids
            ]
            journal = data_repository / "manifests" / "legacy-cup.events.jsonl"
            journal.write_text(
                json.dumps(
                    {
                        "ts": "2099-01-02T03:04:05Z",
                        "event": "batch_acceptance_closed",
                        "request_id": "legacy-acceptance",
                        "selection_count": len(selections),
                        "selections": selections,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            qc_report = {
                "product_id": "legacy-cup",
                "artifact_type": "qc_report",
                "checked_assets": [f"{config_id}.png" for config_id in config_ids],
                "results": [
                    {
                        "affected_asset": f"{config_id}.png",
                        "check_item": "identity",
                        "status": "pass",
                        "notes": "safe",
                    }
                    for config_id in config_ids
                ],
                "issues": [],
            }
            (data_repository / "reports" / "legacy-cup_qc_report.json").write_text(
                json.dumps(qc_report, ensure_ascii=False),
                encoding="utf-8",
            )

            acceptance = BatchAcceptanceService(
                data_repository,
                program_root=program_root,
            ).status("legacy-cup")
            qc = build_qc_summary(
                data_repository,
                "legacy-cup",
                program_root=program_root,
            )
            delivery = package_delivery(
                manifest,
                manifest_path,
                journal_path=journal,
                request_id="legacy-delivery",
                packaged_at="2099-01-02T04:05:06Z",
                program_root=program_root,
                batch_lock_root=base / "locks",
            )

            self.assertFalse((data_repository / "categories").exists())
            self.assertEqual("closed", acceptance["status"])
            self.assertEqual(list(config_ids), acceptance["expectedConfigIds"])
            self.assertEqual(list(config_ids), qc["expectedConfigIds"])
            self.assertEqual(14, delivery.item_count)
            self.assertTrue((workspace / "deliveries" / "legacy-cup.zip").is_file())

    def test_production_service_binds_integrity_assets_to_program_and_reports_to_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_repository = base / "data" / "workflow-runtime"
            program_root = base / "program"
            (data_repository / "manifests").mkdir(parents=True)
            (data_repository / "reports").mkdir()
            (program_root / "scripts").mkdir(parents=True)
            manifest_path = data_repository / "manifests" / "cup.batch_manifest.json"
            manifest = {"product_id": "cup"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            service = WorkflowProductionService(
                data_repository,
                program_root=program_root,
            )

            executor = service._build_executor(
                "integrity",
                manifest,
                manifest_path,
                lambda _artifact: None,
            )

            self.assertIsInstance(executor, ImageProductionExecutor)
            self.assertEqual(data_repository.resolve() / "reports", executor.repo_report_dir)
            self.assertEqual(
                program_root.resolve()
                / "scripts"
                / "validate_final_prompt_integrity.py",
                executor.integrity_script,
            )


if __name__ == "__main__":
    unittest.main()
