from __future__ import annotations

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

from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_http_server import (  # noqa: E402
    ProductionHttpError,
    WorkflowProductionHttpApplication,
)


class RatioRemovalHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        (self.repo / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        self.renders = self.workspace / "outputs" / "renders"
        self.renders.mkdir(parents=True)
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        (self.repo / "manifests" / "cup.batch_manifest.json").write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "outputs": {"renders": [str(self.renders)], "repaired": []},
                }
            ),
            encoding="utf-8",
        )
        self.application = WorkflowProductionHttpApplication(self.repo, "fixture-token")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_quote_and_output_accept_unusual_ratios_while_bad_png_remains_rejected(self) -> None:
        main = self.renders / "main_01.png"
        detail = self.renders / "detail_01.png"
        bad = self.renders / "detail_02.png"
        write_placeholder_png(main, width=43, height=64, kind="main", ordinal=1)
        write_placeholder_png(detail, width=43, height=64, kind="detail", ordinal=1)
        bad.write_bytes(b"not-a-png")

        quote = self.application.quote("cup")

        self.assertEqual(2, quote["readyCount"])
        self.assertEqual(12, quote["remainingCount"])
        self.assertEqual(0.72, quote["estimatedTotalUsd"])
        for config_id, path in (("main_01", main), ("detail_01", detail)):
            data, proof = self.application.output_bytes("cup", config_id)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(64, len(proof))

        with self.assertRaises(ProductionHttpError) as caught:
            self.application.output_bytes("cup", "detail_02")
        self.assertEqual(409, caught.exception.status)


if __name__ == "__main__":
    unittest.main()
