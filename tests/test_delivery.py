from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_demo_executor import write_placeholder_png  # noqa: E402

try:
    import delivery  # noqa: E402
except ModuleNotFoundError:
    delivery = None


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)
REPAIRED_IDS = frozenset(
    {"main_01", "main_02", "detail_01", "detail_02", "detail_05", "detail_06"}
)


class DeliveryFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.workspace = root / "workspace"
        self.manifests = self.repo / "manifests"
        self.renders = self.workspace / "outputs" / "renders"
        self.repaired = self.workspace / "outputs" / "repaired"
        self.manifests.mkdir(parents=True)
        self.renders.mkdir(parents=True)
        self.repaired.mkdir(parents=True)
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        for index, config_id in enumerate(CONFIG_IDS, start=1):
            kind = "main" if config_id.startswith("main_") else "detail"
            height = 96 if kind == "main" else 128
            write_placeholder_png(
                self.renders / f"{config_id}.png",
                width=96,
                height=height,
                kind=kind,
                ordinal=index,
            )
            write_placeholder_png(
                self.repaired / f"{config_id}.png",
                width=96,
                height=height,
                kind=kind,
                ordinal=index + 50,
            )
        self.manifest_path = self.manifests / "cup.batch_manifest.json"
        self.manifest = {
            "batch_id": "cup",
            "product_id": "cup",
            "workspace": {"root": str(self.workspace)},
            "outputs": {
                "renders": [str(self.renders)],
                "repaired": [str(self.repaired)],
            },
        }
        self.write_manifest()
        self.journal = self.manifests / "cup.events.jsonl"
        self.write_close_event()

    @property
    def delivery_root(self) -> Path:
        return self.workspace / "deliveries"

    @property
    def delivery_dir(self) -> Path:
        return self.delivery_root / "cup"

    @property
    def zip_path(self) -> Path:
        return self.delivery_root / "cup.zip"

    @property
    def sidecar_path(self) -> Path:
        return self.delivery_root / "cup.zip.sha256"

    @property
    def lock_path(self) -> Path:
        return self.delivery_root / ".cup.delivery.lock"

    def write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def selected_source(self, config_id: str) -> str:
        return "repaired" if config_id in REPAIRED_IDS else "renders"

    def selected_path(self, config_id: str) -> Path:
        root = self.repaired if config_id in REPAIRED_IDS else self.renders
        return root / f"{config_id}.png"

    def selections(self) -> list[dict[str, str]]:
        return [
            {
                "config_id": config_id,
                "source": self.selected_source(config_id),
                "sha256": hashlib.sha256(self.selected_path(config_id).read_bytes()).hexdigest(),
            }
            for config_id in CONFIG_IDS
        ]

    def close_event(self) -> dict:
        return {
            "ts": "2026-07-24T16:23:35",
            "event": "batch_acceptance_closed",
            "request_id": "acceptance-001",
            "selection_count": 14,
            "selections": self.selections(),
        }

    def write_close_event(self, events: list[dict] | None = None) -> None:
        values = [self.close_event()] if events is None else events
        self.journal.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in values),
            encoding="utf-8",
        )


class DeliveryPackagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = DeliveryFixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _delivery_module(self):
        self.assertIsNotNone(delivery, "delivery 模块尚未实现")
        return delivery

    def _package(self):
        module = self._delivery_module()
        return module.package_delivery(
            self.fixture.manifest,
            self.fixture.manifest_path,
            journal_path=self.fixture.journal,
            request_id="delivery-001",
            packaged_at="2026-07-24T18:00:00",
        )

    def _assert_rejected(self, expected_code: str):
        module = self._delivery_module()
        with self.assertRaises(module.DeliveryRejected) as ctx:
            self._package()
        self.assertEqual(expected_code, ctx.exception.code)
        self.assertNotIn(str(self.fixture.workspace), str(ctx.exception))
        return ctx.exception

    def test_mixed_closeout_selections_copy_authoritative_sources(self) -> None:
        result = self._package()
        manifest = json.loads(
            (self.fixture.delivery_dir / "delivery_manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(14, result.item_count)
        self.assertEqual({"renders": 8, "repaired": 6}, result.source_counts)
        self.assertEqual(list(CONFIG_IDS), [item["config_id"] for item in manifest["items"]])
        self.assertEqual(
            [self.fixture.selected_source(config_id) for config_id in CONFIG_IDS],
            [item["source"] for item in manifest["items"]],
        )
        for config_id in CONFIG_IDS:
            copied = self.fixture.delivery_dir / "images" / f"{config_id}.png"
            self.assertEqual(self.fixture.selected_path(config_id).read_bytes(), copied.read_bytes())

    def test_sha_mismatch_stops_before_delivery_artifacts(self) -> None:
        event = self.fixture.close_event()
        event["selections"][0]["sha256"] = "0" * 64
        self.fixture.write_close_event([event])

        self._assert_rejected("selection_sha_mismatch")

        self.assertFalse(self.fixture.delivery_dir.exists())
        self.assertFalse(self.fixture.zip_path.exists())
        self.assertFalse(self.fixture.sidecar_path.exists())

    def test_existing_delivery_event_rejects_duplicate(self) -> None:
        events = [
            self.fixture.close_event(),
            {"event": "delivery_packaged", "request_id": "older-delivery"},
        ]
        self.fixture.write_close_event(events)

        self._assert_rejected("delivery_already_recorded")
        self.assertFalse(self.fixture.delivery_dir.exists())

    def test_complete_delivery_rejects_duplicate(self) -> None:
        self._package()
        before = {
            path.relative_to(self.fixture.delivery_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.fixture.delivery_root.rglob("*")
            if path.is_file()
        }

        self._assert_rejected("delivery_already_exists")

        after = {
            path.relative_to(self.fixture.delivery_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.fixture.delivery_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_partial_delivery_is_preserved_and_rejected(self) -> None:
        images = self.fixture.delivery_dir / "images"
        images.mkdir(parents=True)
        marker = images / "keep-me.txt"
        marker.write_text("人工检查", encoding="utf-8")

        self._assert_rejected("delivery_residue_exists")

        self.assertEqual("人工检查", marker.read_text(encoding="utf-8"))
        self.assertFalse(self.fixture.zip_path.exists())

    def test_missing_or_multiple_closeout_is_rejected(self) -> None:
        cases = {
            "missing": ([], "acceptance_missing"),
            "multiple": (
                [self.fixture.close_event(), self.fixture.close_event()],
                "acceptance_not_unique",
            ),
        }
        for name, (events, expected_code) in cases.items():
            with self.subTest(name=name):
                self.fixture.write_close_event(events)
                self._assert_rejected(expected_code)
                self.assertFalse(self.fixture.delivery_dir.exists())

    def test_incomplete_duplicate_or_unknown_selections_are_rejected(self) -> None:
        cases: dict[str, tuple[list[dict[str, str]], str]] = {}
        selections = self.fixture.selections()
        cases["incomplete"] = (selections[:-1], "selections_invalid")
        selections = self.fixture.selections()
        selections[-1] = dict(selections[0])
        cases["duplicate"] = (selections, "selections_invalid")
        selections = self.fixture.selections()
        selections[0]["source"] = "unknown"
        cases["unknown"] = (selections, "selections_invalid")

        for name, (selection_values, expected_code) in cases.items():
            with self.subTest(name=name):
                event = self.fixture.close_event()
                event["selection_count"] = len(selection_values)
                event["selections"] = selection_values
                self.fixture.write_close_event([event])
                self._assert_rejected(expected_code)
                self.assertFalse(self.fixture.delivery_dir.exists())

    def test_manifest_marker_and_output_boundary_must_match_batch(self) -> None:
        original_manifest = json.loads(json.dumps(self.fixture.manifest))
        original_marker = (self.fixture.workspace / ".canvas_batch").read_text(encoding="utf-8")
        cases = ("manifest", "marker", "outside")
        for name in cases:
            with self.subTest(name=name):
                self.fixture.manifest = json.loads(json.dumps(original_manifest))
                (self.fixture.workspace / ".canvas_batch").write_text(
                    original_marker,
                    encoding="utf-8",
                )
                expected_code = ""
                if name == "manifest":
                    self.fixture.manifest["batch_id"] = "other"
                    expected_code = "batch_mismatch"
                elif name == "marker":
                    (self.fixture.workspace / ".canvas_batch").write_text(
                        json.dumps({"type": "canvas-batch-v1", "product_id": "other"}),
                        encoding="utf-8",
                    )
                    expected_code = "workspace_marker_invalid"
                else:
                    self.fixture.manifest["outputs"]["renders"] = [
                        str(self.fixture.root / "outside")
                    ]
                    expected_code = "source_outside_workspace"
                self.fixture.write_manifest()
                self._assert_rejected(expected_code)
                self.assertFalse(self.fixture.delivery_dir.exists())

    def test_zip_entries_match_directory_with_deterministic_metadata(self) -> None:
        result = self._package()
        expected_names = [
            "cup/delivery_manifest.json",
            "cup/delivery_manifest.md",
            *[f"cup/images/{config_id}.png" for config_id in CONFIG_IDS],
        ]

        with zipfile.ZipFile(self.fixture.zip_path) as archive:
            self.assertEqual(expected_names, archive.namelist())
            for info in archive.infolist():
                self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
                self.assertEqual(zipfile.ZIP_DEFLATED, info.compress_type)
                self.assertEqual(3, info.create_system)
                source = self.fixture.delivery_root / info.filename
                self.assertEqual(source.read_bytes(), archive.read(info.filename))
        actual_zip_sha = hashlib.sha256(self.fixture.zip_path.read_bytes()).hexdigest()
        self.assertEqual(actual_zip_sha, result.zip_sha256)
        self.assertEqual(
            f"{actual_zip_sha}  cup.zip\n",
            self.fixture.sidecar_path.read_text(encoding="utf-8"),
        )

    def test_existing_lock_rejects_without_removing_lock(self) -> None:
        self.fixture.delivery_root.mkdir()
        self.fixture.lock_path.write_text("other-request", encoding="utf-8")

        self._assert_rejected("delivery_locked")

        self.assertEqual("other-request", self.fixture.lock_path.read_text(encoding="utf-8"))
        self.assertFalse(self.fixture.delivery_dir.exists())


if __name__ == "__main__":
    unittest.main()
