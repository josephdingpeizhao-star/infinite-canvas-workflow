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

from batch_intake_contract import (  # noqa: E402
    batch_intake_contract_sha256,
    canonical_contract_bytes,
)


class BatchIntakeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(ROOT / "categories", self.repo / "categories")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_digest_is_canonical_and_covers_only_payload_field_contract(self) -> None:
        expected = batch_intake_contract_sha256(ROOT)
        self.assertEqual(expected, batch_intake_contract_sha256(self.repo))
        self.assertEqual(
            json.loads(
                (self.repo / "categories" / "_shared" / "batch-intake-contract.json").read_text(
                    encoding="utf-8"
                )
            ),
            json.loads(canonical_contract_bytes(self.repo).decode("utf-8")),
        )

        form_path = self.repo / "categories" / "杯类" / "form.json"
        form = json.loads(form_path.read_text(encoding="utf-8"))
        form["handheld"]["main"]["default"] = 5
        form["advanced_options"][0]["label"] = "仅测试文案变更"
        form_path.write_text(
            json.dumps(form, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(expected, batch_intake_contract_sha256(self.repo))

    def test_real_payload_field_change_requires_a_new_digest(self) -> None:
        before = batch_intake_contract_sha256(self.repo)
        contract_path = (
            self.repo / "categories" / "_shared" / "batch-intake-contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["payload"]["properties"]["facts"]["properties"]["new_field"] = {
            "type": "string"
        }
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.assertNotEqual(before, batch_intake_contract_sha256(self.repo))


if __name__ == "__main__":
    unittest.main()
