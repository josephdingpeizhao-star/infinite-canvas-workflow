from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_batch_manifest.py"


class DataRootCollisionGateTest(unittest.TestCase):
    def test_existing_external_data_ledger_rejects_same_batch_during_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_repository = base / "data" / "workflow-runtime"
            manifest = data_repository / "manifests" / "杯子_20990101.batch_manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            workspace = base / "data" / "杯类" / "杯子_20990101"
            command = [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--product-id",
                "杯子_20990101",
                "--product-type",
                "杯子",
                "--category",
                "杯类",
                "--height-cm",
                "10",
                "--main-count",
                "1",
                "--detail-count",
                "1",
                "--handheld-main",
                "0",
                "--handheld-detail",
                "0",
                "--forbid-pouring-and-heating",
                "true",
                "--missing-d-no-retake",
                "true",
                "--workspace-root",
                str(workspace),
                "--data-repo-root",
                str(data_repository),
                "--dry-run",
            ]
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"

            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                check=False,
            )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertIn("manifest already exists, not overwriting", completed.stdout)
        self.assertIn(str(manifest), completed.stdout)
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
