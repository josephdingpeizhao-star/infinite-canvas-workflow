from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_demo_executor import write_placeholder_png  # noqa: E402
import workflow_batch_acceptance as acceptance  # noqa: E402


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)


class WorkflowBatchAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        self.renders = self.workspace / "outputs" / "renders"
        self.repaired = self.workspace / "outputs" / "repaired"
        (self.repo / "manifests").mkdir(parents=True)
        self.renders.mkdir(parents=True)
        self.repaired.mkdir(parents=True)
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        for index, config_id in enumerate(CONFIG_IDS, start=1):
            kind = "main" if config_id.startswith("main_") else "detail"
            path = self.renders / f"{config_id}.png"
            write_placeholder_png(
                path,
                width=96,
                height=96 if kind == "main" else 128,
                kind=kind,
                ordinal=index,
            )
        self.manifest = self.repo / "manifests" / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "outputs": {
                        "renders": [str(self.renders)],
                        "repaired": [str(self.repaired)],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.service = acceptance.BatchAcceptanceService(self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _selections(self) -> list[dict[str, str]]:
        return [
            {
                "configId": config_id,
                "source": "renders",
                "sha256": hashlib.sha256(
                    (self.renders / f"{config_id}.png").read_bytes()
                ).hexdigest(),
            }
            for config_id in CONFIG_IDS
        ]

    def _close(self, selections=None):
        return self.service.close(
            "cup",
            {
                "requestId": "acceptance-001",
                "machineId": "machine",
                "selections": self._selections() if selections is None else selections,
            },
        )

    def test_valid_closeout_writes_all_fourteen_ordered_sha_selections(self) -> None:
        payload = self._close()

        event = json.loads(
            (self.repo / "manifests" / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual("closed", payload["status"])
        self.assertEqual("batch_acceptance_closed", event["event"])
        self.assertEqual(14, event["selection_count"])
        self.assertEqual(list(CONFIG_IDS), [item["config_id"] for item in event["selections"]])
        self.assertTrue(all(len(item["sha256"]) == 64 for item in event["selections"]))
        self.assertEqual(acceptance.ACCEPTANCE_STATEMENT, event["final_review_statement"])

    def test_missing_config_is_rejected_without_writing_event(self) -> None:
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close(self._selections()[:-1])
        self.assertEqual(400, ctx.exception.status)
        self.assertFalse((self.repo / "manifests" / "cup.events.jsonl").exists())

    def test_duplicate_config_is_rejected(self) -> None:
        selections = self._selections()
        selections[-1] = dict(selections[0])
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close(selections)
        self.assertEqual(400, ctx.exception.status)

    def test_sha_mismatch_is_rejected_without_echoing_path(self) -> None:
        selections = self._selections()
        selections[0]["sha256"] = "0" * 64
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close(selections)
        self.assertEqual(409, ctx.exception.status)
        self.assertNotIn(str(self.workspace), str(ctx.exception))

    def test_unknown_source_is_rejected(self) -> None:
        selections = self._selections()
        selections[0]["source"] = "unknown"
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close(selections)
        self.assertEqual(400, ctx.exception.status)

    def test_output_root_escape_is_rejected(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["outputs"]["renders"] = [str(self.root / "outside")]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close()
        self.assertEqual(409, ctx.exception.status)

    def test_duplicate_closeout_is_rejected_without_second_event(self) -> None:
        self._close()
        with self.assertRaises(acceptance.AcceptanceRejected) as ctx:
            self._close()
        self.assertEqual(409, ctx.exception.status)
        events = (
            self.repo / "manifests" / "cup.events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(events))

    def test_status_is_open_then_closed_without_returning_selections(self) -> None:
        before = self.service.status("cup")
        self._close()
        after = self.service.status("cup")
        self.assertEqual("open", before["status"])
        self.assertEqual("closed", after["status"])
        self.assertNotIn("selections", after)
