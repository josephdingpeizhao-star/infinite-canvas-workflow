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

from batch_intake_contract import batch_intake_contract_sha256  # noqa: E402
from category_recipes import installed_category_metadata  # noqa: E402
from workflow_production_http_server import (  # noqa: E402
    ProductionHttpError,
    WorkflowProductionHttpApplication,
)


class CategoryMetadataEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        self.application = WorkflowProductionHttpApplication(
            self.repo,
            "temporary-test-token",
            program_root=self.repo,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.repo).as_posix(): path.read_bytes()
            for path in self.repo.rglob("*")
            if path.is_file()
        }

    def test_authenticated_metadata_is_read_only_and_exactly_recipe_driven(self) -> None:
        before = self._snapshot()
        with self.assertRaises(ProductionHttpError) as caught:
            self.application.authorize("wrong-test-token")
        self.assertEqual(401, caught.exception.status)

        self.application.authorize("temporary-test-token")
        payload = self.application.batch_categories()

        self.assertEqual(
            {
                "ok": True,
                "contractHash": batch_intake_contract_sha256(self.repo),
                "categories": list(installed_category_metadata(self.repo)),
            },
            payload,
        )
        public_json = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.repo), public_json)
        self.assertNotIn("temporary-test-token", public_json)
        self.assertEqual(before, self._snapshot())

    def test_malformed_installed_recipe_fails_closed_without_fallback(self) -> None:
        (self.repo / "categories" / "盘子" / "recipe.json").write_text(
            "{broken",
            encoding="utf-8",
        )
        with self.assertRaises(ProductionHttpError) as caught:
            self.application.batch_categories()
        self.assertEqual(503, caught.exception.status)
        self.assertEqual("batch categories unavailable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
