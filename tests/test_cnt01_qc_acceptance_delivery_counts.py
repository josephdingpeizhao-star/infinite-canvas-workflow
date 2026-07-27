from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_qc import qc_chunk_count  # noqa: E402
from delivery import package_delivery  # noqa: E402
from image_count_contract import expected_config_ids  # noqa: E402
from workflow_batch_acceptance import (  # noqa: E402
    AcceptanceRejected,
    BatchAcceptanceService,
)
from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_http_server import (  # noqa: E402
    WorkflowProductionHttpApplication,
)
from workflow_qc_summary import build_qc_summary  # noqa: E402


class Cnt01QcAcceptanceDeliveryCountsTest(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        main_count: int,
        detail_count: int,
    ) -> tuple[Path, Path, Path, tuple[str, ...]]:
        repo = root / "repo"
        workspace = root / "workspace"
        renders = workspace / "outputs" / "renders"
        repaired = workspace / "outputs" / "repaired"
        (repo / "manifests").mkdir(parents=True)
        (repo / "reports").mkdir()
        renders.mkdir(parents=True)
        repaired.mkdir(parents=True)
        (workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        identifiers = expected_config_ids(main_count, detail_count)
        for ordinal, config_id in enumerate(identifiers, start=1):
            is_main = config_id.startswith("main_")
            write_placeholder_png(
                renders / f"{config_id}.png",
                width=96,
                height=96 if is_main else 128,
                kind="main" if is_main else "detail",
                ordinal=ordinal,
            )
        manifest_path = repo / "manifests" / "cup.batch_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "batch_id": "cup",
                    "product_id": "cup",
                    "user_confirmed_facts": {
                        "main_image_count": main_count,
                        "detail_image_count": detail_count,
                    },
                    "workspace": {"root": str(workspace)},
                    "outputs": {
                        "renders": [str(renders)],
                        "repaired": [str(repaired)],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return repo, workspace, manifest_path, identifiers

    @staticmethod
    def _selections(
        renders: Path,
        identifiers: tuple[str, ...],
    ) -> list[dict[str, str]]:
        return [
            {
                "configId": config_id,
                "source": "renders",
                "sha256": hashlib.sha256(
                    (renders / f"{config_id}.png").read_bytes()
                ).hexdigest(),
            }
            for config_id in identifiers
        ]

    def test_quote_formula_uses_manifest_total_for_14_5_and_60(self) -> None:
        cases = (
            (6, 8, 14, 0.84, 55),
            (3, 2, 5, 0.30, 39),
            (30, 30, 60, 3.60, 138),
        )
        for main_count, detail_count, total, cost, minutes in cases:
            with self.subTest(total=total), tempfile.TemporaryDirectory() as tmp:
                repo, _workspace, manifest_path, identifiers = self._fixture(
                    Path(tmp),
                    main_count,
                    detail_count,
                )
                shutil.copytree(ROOT / "categories", repo / "categories")
                for path in (Path(tmp) / "workspace" / "outputs" / "renders").glob(
                    "*.png"
                ):
                    path.unlink()
                before = manifest_path.read_bytes()
                application = WorkflowProductionHttpApplication(repo, "test-token")
                quote = application.quote("cup")
                self.assertEqual(total, quote["totalCount"])
                self.assertEqual(list(identifiers), quote["expectedConfigIds"])
                self.assertEqual(cost, quote["estimatedTotalUsd"])
                self.assertEqual(minutes, quote["estimatedMinutes"])
                self.assertEqual(before, manifest_path.read_bytes())

    def test_five_image_closeout_delivery_and_qc_summary_use_same_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, workspace, manifest_path, identifiers = self._fixture(
                Path(tmp),
                3,
                2,
            )
            renders = workspace / "outputs" / "renders"
            service = BatchAcceptanceService(repo)
            payload = {
                "requestId": "accept-five",
                "machineId": "machine",
                "selections": self._selections(renders, identifiers),
            }
            with self.assertRaises(AcceptanceRejected):
                service.close(
                    "cup",
                    {**payload, "selections": payload["selections"][:-1]},
                )
            closed = service.close("cup", payload)
            self.assertEqual(5, closed["selectionCount"])
            self.assertEqual(list(identifiers), closed["expectedConfigIds"])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result = package_delivery(
                manifest,
                manifest_path,
                journal_path=repo / "manifests" / "cup.events.jsonl",
                request_id="deliver-five",
                packaged_at="2026-07-27T00:00:00Z",
            )
            self.assertEqual(5, result.item_count)
            self.assertEqual(
                {f"{config_id}.png" for config_id in identifiers},
                {
                    path.name
                    for path in (workspace / "deliveries" / "cup" / "images").iterdir()
                },
            )

            report = {
                "product_id": "cup",
                "artifact_type": "qc_report",
                "checked_assets": [
                    f"{config_id}.png" for config_id in identifiers
                ],
                "results": [
                    {
                        "affected_asset": f"{config_id}.png",
                        "status": "pass",
                    }
                    for config_id in identifiers
                ],
                "issues": [],
            }
            (repo / "reports" / "cup_qc_report.json").write_text(
                json.dumps(report),
                encoding="utf-8",
            )
            summary = build_qc_summary(repo, "cup")
            self.assertEqual(5, summary["totalCount"])
            self.assertEqual(list(identifiers), summary["expectedConfigIds"])
            self.assertEqual(4, qc_chunk_count(5))
            self.assertEqual(31, qc_chunk_count(60))


if __name__ == "__main__":
    unittest.main()
