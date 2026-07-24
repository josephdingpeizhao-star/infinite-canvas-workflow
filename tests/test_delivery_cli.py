from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from test_delivery import DeliveryFixture  # noqa: E402

try:
    import delivery_cli  # noqa: E402
except ModuleNotFoundError:
    delivery_cli = None


class DeliveryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = DeliveryFixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _cli_module(self):
        self.assertIsNotNone(delivery_cli, "delivery_cli 模块尚未实现")
        return delivery_cli

    def test_cli_writes_sanitized_success_event_with_hash_receipts(self) -> None:
        module = self._cli_module()
        output = io.StringIO()

        code = module.run_cli(
            [
                "--batch-manifest",
                str(self.fixture.manifest_path),
                "--command",
                "run: delivery",
            ],
            output=output,
            request_id_factory=lambda: "delivery-001",
            packaged_at_factory=lambda: "2026-07-24T18:00:00",
        )
        event = json.loads(
            self.fixture.journal.read_text(encoding="utf-8").splitlines()[-1]
        )

        self.assertEqual(0, code)
        self.assertEqual("delivery_packaged", event["event"])
        self.assertEqual("delivery-001", event["request_id"])
        self.assertEqual("acceptance-001", event["acceptance_request_id"])
        self.assertEqual(14, event["selection_count"])
        self.assertEqual({"renders": 8, "repaired": 6}, event["source_counts"])
        for key in (
            "selection_sha256",
            "zip_sha256",
            "manifest_sha256",
            "manifest_markdown_sha256",
            "sidecar_sha256",
        ):
            self.assertEqual(64, len(event[key]))
        exposed = self.fixture.journal.read_text(encoding="utf-8") + output.getvalue()
        self.assertNotIn(str(self.fixture.workspace), exposed)
        self.assertNotIn("Traceback", exposed)

    def test_cli_rejects_invalid_command_without_sensitive_output(self) -> None:
        module = self._cli_module()
        output = io.StringIO()
        secret = "SENSITIVE_" + uuid.uuid4().hex

        code = module.run_cli(
            [
                "--batch-manifest",
                str(self.fixture.manifest_path),
                "--command",
                f"run: delivery\n{secret}: value",
            ],
            output=output,
            request_id_factory=lambda: "delivery-bad-command",
            packaged_at_factory=lambda: "2026-07-24T18:00:00",
        )
        event = json.loads(
            self.fixture.journal.read_text(encoding="utf-8").splitlines()[-1]
        )

        self.assertEqual(1, code)
        self.assertEqual("delivery_rejected", event["event"])
        self.assertEqual("invalid_command", event["code"])
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, json.dumps(event, ensure_ascii=False))
        self.assertFalse(self.fixture.delivery_dir.exists())


if __name__ == "__main__":
    unittest.main()
