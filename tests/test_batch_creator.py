from __future__ import annotations

import copy
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import (  # noqa: E402
    STATE_MARKER_NAME,
    BatchCreationError,
    BatchCreator,
    UploadedFile,
    prepare_state_root,
    require_state_root,
)
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from state_reader import read_batch_route  # noqa: E402


FACTS = ConfirmedFacts(
    product_type="餐具",
    height_cm=25,
    handheld_main=2,
    handheld_detail=1,
    allow_clear_water=True,
    forbid_pouring_and_heating=True,
    missing_d_no_retake=True,
)


class BatchCreatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.production_parent = self.base / "production"
        self.test_root = self.base / "isolated-test-root"
        self.state_root = self.base / "state"
        self.upload_root = self.base / "uploads"
        self.production_parent.mkdir()
        self.test_root.mkdir()
        (self.test_root / ".canvas_intake_test_root").write_text(
            "canvas-intake-test-root-v1\n", encoding="utf-8"
        )
        self.upload_root.mkdir()
        self._make_repo_fixture()
        prepare_state_root(self.state_root)
        self.creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            test_root=self.test_root,
            today=lambda: date(2026, 7, 18),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_repo_fixture(self) -> None:
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "manifests").mkdir()
        (self.repo / "canvas-bridge").mkdir()
        shutil.copy2(ROOT / "scripts" / "build_batch_manifest.py", self.repo / "scripts")
        shutil.copy2(
            ROOT / "canvas-bridge" / "category_recipes.py",
            self.repo / "canvas-bridge",
        )
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        shutil.copy2(
            ROOT / "manifests" / "batch_manifest.template.json",
            self.repo / "manifests",
        )
        shutil.copy2(
            ROOT / "manifests" / "asset_manifest.template.json",
            self.repo / "manifests",
        )
        frozen = {
            "product_id": "shuiping_20260712",
            "workspace": {
                "mode": "external",
                "root": str(self.production_parent / "shuiping_20260712"),
            },
        }
        (self.repo / "manifests" / "shuiping_20260712.batch_manifest.json").write_text(
            json.dumps(frozen, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def request(
        self,
        *,
        request_id: str = "batch-req-0001",
        facts: ConfirmedFacts = FACTS,
        sources: tuple[SourceImage, ...] | None = None,
        category: str = "杯类",
        contract_hash: str = "",
    ) -> BatchIntakeRequest:
        if sources is None:
            payload = b"original-png-bytes\x00\x01"
            sources = (
                SourceImage(
                    node_id="image-1",
                    storage_key="image:one",
                    name="餐具正面.png",
                    size=len(payload),
                    mime_type="image/png",
                    last_modified=1_720_000_000_000,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                ),
            )
        return BatchIntakeRequest(
            request_id=request_id,
            requested_at=19_000,
            info_node_id="info-1",
            workflow_node_id="workflow-1",
            facts=facts,
            source_images=sources,
            category=category,
            contract_hash=contract_hash,
        )

    def upload(self, request: BatchIntakeRequest, index: int = 0, *, data: bytes | None = None) -> UploadedFile:
        source = request.source_images[index]
        if data is None:
            data = b"original-png-bytes\x00\x01" if index == 0 else f"image-{index}".encode()
        path = self.upload_root / f"upload-{index}.bin"
        path.write_bytes(data)
        return UploadedFile(
            source_node_id=source.node_id,
            path=path,
            name=source.name,
            size=len(data),
            mime_type=source.mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def snapshot(self, root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def assert_no_staging(self, root: Path) -> None:
        self.assertEqual([], list(root.glob(".*.batch-intake-staging")))
        self.assertEqual([], list((self.repo / "manifests").glob(".batch-intake-*")))

    def test_state_root_marker_is_created_and_required_without_overwriting_foreign_root(self) -> None:
        marker = self.state_root / STATE_MARKER_NAME
        self.assertEqual("canvas-batch-intake-state-v1\n", marker.read_text(encoding="utf-8"))
        self.assertEqual(self.state_root.resolve(), require_state_root(self.state_root))

        foreign = self.base / "foreign-state"
        foreign.mkdir()
        (foreign / "do-not-touch.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(BatchCreationError):
            prepare_state_root(foreign)
        self.assertEqual({"do-not-touch.txt": b"keep"}, self.snapshot(foreign))

    def test_product_id_for_is_pure_sanitized_chinese_and_date_based(self) -> None:
        request = self.request(facts=replace(FACTS, product_type="  餐具 / 水杯  "))
        before = self.snapshot(self.base)

        product_id = self.creator.product_id_for(request)

        self.assertEqual("餐具_水杯_20260718", product_id)
        self.assertEqual(before, self.snapshot(self.base))

    def test_success_preserves_original_bytes_and_writes_manifest_last_contract(self) -> None:
        request = self.request()
        upload = self.upload(request)

        result = self.creator.create(request, [upload])

        self.assertEqual("餐具_20260718", result.product_id)
        self.assertEqual(1, result.image_count)
        self.assertEqual(FACTS.as_dict(), result.facts)
        self.assertEqual(self.test_root / "餐具_20260718", result.workspace_root)
        copied = result.workspace_root / "inputs" / "white_bg" / "餐具正面.png"
        self.assertEqual(upload.path.read_bytes(), copied.read_bytes())
        self.assertEqual(upload.sha256, hashlib.sha256(copied.read_bytes()).hexdigest())
        self.assertTrue((result.workspace_root / ".canvas_batch").is_file())
        self.assertTrue(result.receipt_path.is_file())
        self.assertTrue(result.manifest_path.is_file())
        self.assertFalse((result.workspace_root / "manifests" / "batch_manifest.json").exists())
        self.assert_no_staging(self.test_root)

    def test_chinese_manifest_workspace_and_receipt_round_trip_as_utf8(self) -> None:
        result = self.creator.create(self.request(), [self.upload(self.request())])
        manifest_bytes = result.manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        receipt_bytes = result.receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        route_bytes = json.dumps(result.receipt_dict(), ensure_ascii=False).encode("utf-8")
        route = json.loads(route_bytes.decode("utf-8"))

        self.assertIn("餐具".encode("utf-8"), manifest_bytes)
        self.assertEqual("餐具_20260718", result.manifest_path.name.removesuffix(".batch_manifest.json"))
        self.assertEqual("餐具_20260718", Path(manifest["workspace"]["root"]).name)
        self.assertEqual("餐具", manifest["user_confirmed_facts"]["product_type"])
        self.assertEqual("餐具正面.png", receipt["assets"][0]["name"])
        self.assertEqual("餐具_20260718", route["batchId"])
        self.assertEqual("餐具", route["facts"]["product_type"])

        routed = read_batch_route(result.manifest_path)
        routed_white_bg = routed["inputs"]["white_bg_images"]["paths"][0]["resolved_path"]
        self.assertEqual("餐具_20260718", routed["product_id"])
        self.assertIn("餐具_20260718", routed["manifest_source_path"])
        self.assertIn("餐具_20260718", routed_white_bg)
        self.assertEqual("needs_product_identity_archive", routed["current_stage"])

    def test_plate_category_and_three_dimensions_reach_manifest_and_receipt(self) -> None:
        facts = ConfirmedFacts(
            product_type="盘子",
            length_cm=28,
            width_cm=28,
            height_cm=3,
            handheld_main=6,
            handheld_detail=8,
            allow_clear_water=True,
            forbid_pouring_and_heating=True,
            missing_d_no_retake=True,
        )
        request = self.request(
            facts=facts,
            category="盘子",
            contract_hash="a" * 64,
        )

        result = self.creator.create(request, [self.upload(request)])
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))

        self.assertEqual("盘子", manifest["category"])
        self.assertEqual(facts.as_dict(), manifest["user_confirmed_facts"])
        self.assertEqual("盘子", receipt["category"])
        self.assertEqual("a" * 64, receipt["contract_hash"])

    def test_receipt_records_browser_uploaded_and_destination_hash_equality(self) -> None:
        request = self.request()
        upload = self.upload(request)
        result = self.creator.create(request, [upload])
        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        asset = receipt["assets"][0]

        self.assertEqual(request.source_images[0].expected_sha256, asset["expected_sha256"])
        self.assertEqual(upload.sha256, asset["uploaded_sha256"])
        self.assertEqual(upload.sha256, asset["destination_sha256"])
        self.assertEqual(upload.size, asset["bytes"])
        self.assertEqual("inputs/white_bg/餐具正面.png", asset["relative_path"])

    def test_canvas_receipt_contains_only_human_batch_facts_not_local_paths_or_hashes(self) -> None:
        request = self.request()
        result = self.creator.create(request, [self.upload(request)])

        receipt = result.receipt_dict()
        serialized = json.dumps(receipt, ensure_ascii=False)

        self.assertEqual(
            {
                "batchId": "餐具_20260718",
                "imageCount": 1,
                "facts": FACTS.as_dict(),
            },
            receipt,
        )
        self.assertNotIn(str(self.base), serialized)
        self.assertNotIn(request.source_images[0].expected_sha256, serialized)

    def test_asset_manifest_uses_existing_fields_without_hash_schema_change(self) -> None:
        request = self.request()
        result = self.creator.create(request, [self.upload(request)])
        path = result.workspace_root / "manifests" / "asset_manifest.json"
        asset = json.loads(path.read_text(encoding="utf-8"))["assets"][0]

        self.assertEqual(
            {
                "asset_id",
                "file_path",
                "asset_role",
                "is_single_product_white_bg",
                "is_set_group_shot",
                "is_style_reference",
                "bound_angle_slot",
                "component_id",
                "notes",
            },
            set(asset),
        )
        self.assertEqual("inputs/white_bg/餐具正面.png", asset["file_path"])
        self.assertTrue(asset["is_single_product_white_bg"])
        self.assertNotIn("sha256", asset)

    def test_production_mode_derives_only_approved_parent_from_frozen_manifest(self) -> None:
        creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            today=lambda: date(2026, 7, 18),
        )
        request = self.request()
        frozen_before = (self.repo / "manifests" / "shuiping_20260712.batch_manifest.json").read_bytes()

        result = creator.create(request, [self.upload(request)])

        self.assertEqual(self.production_parent / "餐具_20260718", result.workspace_root)
        self.assertEqual(
            frozen_before,
            (self.repo / "manifests" / "shuiping_20260712.batch_manifest.json").read_bytes(),
        )

    def test_test_root_requires_exact_marker_and_rejects_reparse_root(self) -> None:
        unmarked = self.base / "unmarked-test"
        unmarked.mkdir()
        with self.assertRaises(BatchCreationError) as caught:
            BatchCreator(repo_root=self.repo, state_root=self.state_root, test_root=unmarked)
        self.assertEqual("unsafe_test_root", caught.exception.code)

        link = self.base / "linked-test"
        try:
            link.symlink_to(self.test_root, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(BatchCreationError) as caught:
            BatchCreator(repo_root=self.repo, state_root=self.state_root, test_root=link)
        self.assertEqual("reparse_point", caught.exception.code)

    def test_frozen_batch_id_existing_target_and_existing_manifest_are_never_overwritten(self) -> None:
        frozen_creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            test_root=self.test_root,
            today=lambda: date(2026, 7, 12),
        )
        frozen_request = self.request(facts=replace(FACTS, product_type="shuiping"))
        with self.assertRaises(BatchCreationError) as caught:
            frozen_creator.create(frozen_request, [self.upload(frozen_request)])
        self.assertEqual("frozen_batch", caught.exception.code)

        target = self.test_root / "餐具_20260718"
        target.mkdir()
        (target / "foreign.txt").write_text("keep", encoding="utf-8")
        before = self.snapshot(target)
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(self.request(), [self.upload(self.request())])
        self.assertEqual("batch_exists", caught.exception.code)
        self.assertEqual(before, self.snapshot(target))

        shutil.rmtree(target)
        manifest = self.repo / "manifests" / "餐具_20260718.batch_manifest.json"
        manifest.write_text("foreign-manifest", encoding="utf-8")
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(self.request(), [self.upload(self.request())])
        self.assertEqual("batch_exists", caught.exception.code)
        self.assertEqual(b"foreign-manifest", manifest.read_bytes())

    def test_duplicate_request_id_with_changed_product_is_zero_side_effect(self) -> None:
        first_request = self.request()
        self.creator.create(first_request, [self.upload(first_request)])
        second_request = self.request(facts=replace(FACTS, product_type="茶具"))
        second_upload = self.upload(second_request)
        before_test = self.snapshot(self.test_root)
        before_repo = self.snapshot(self.repo / "manifests")

        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(second_request, [second_upload])

        self.assertEqual("duplicate_request", caught.exception.code)
        self.assertEqual(before_test, self.snapshot(self.test_root))
        self.assertEqual(before_repo, self.snapshot(self.repo / "manifests"))
        self.assertFalse((self.test_root / "茶具_20260718").exists())

    def test_uploads_must_match_request_exactly_once(self) -> None:
        request = self.request()
        good = self.upload(request)
        invalid_sets = (
            [],
            [good, good],
            [replace(good, source_node_id="other-node")],
            [replace(good, name="renamed.png")],
            [replace(good, mime_type="image/jpeg")],
        )
        for uploads in invalid_sets:
            with self.subTest(uploads=uploads):
                with self.assertRaises(BatchCreationError) as caught:
                    self.creator.create(request, uploads)
                self.assertEqual("invalid_uploads", caught.exception.code)
                self.assertFalse((self.test_root / "餐具_20260718").exists())
                self.assertFalse((self.repo / "manifests" / "餐具_20260718.batch_manifest.json").exists())

    def test_size_expected_hash_uploaded_hash_and_actual_bytes_must_all_match(self) -> None:
        request = self.request()
        good = self.upload(request)
        invalid_uploads = (
            replace(good, size=good.size + 1),
            replace(good, sha256="0" * 64),
        )
        for upload in invalid_uploads:
            with self.subTest(upload=upload):
                with self.assertRaises(BatchCreationError) as caught:
                    self.creator.create(request, [upload])
                self.assertEqual("integrity_mismatch", caught.exception.code)
                self.assertFalse((self.test_root / "餐具_20260718").exists())

        different_expected = replace(
            request,
            source_images=(replace(request.source_images[0], expected_sha256="f" * 64),),
        )
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(different_expected, [good])
        self.assertEqual("integrity_mismatch", caught.exception.code)

    def test_source_file_must_be_regular_and_not_a_reparse_point(self) -> None:
        request = self.request()
        directory_upload = replace(self.upload(request), path=self.upload_root)
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(request, [directory_upload])
        self.assertEqual("unsafe_source", caught.exception.code)

        link = self.upload_root / "linked-upload"
        try:
            link.symlink_to(self.upload(request).path)
        except OSError:
            return
        linked = replace(self.upload(request), path=link)
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(request, [linked])
        self.assertEqual("reparse_point", caught.exception.code)

    def test_unsafe_or_case_insensitive_duplicate_names_write_nothing(self) -> None:
        base_source = self.request().source_images[0]
        unsafe_names = ("../escape.png", "CON.png", "tail. ", "folder/name.png", "\x00bad.png")
        for name in unsafe_names:
            with self.subTest(name=name):
                request = self.request(sources=(replace(base_source, name=name),))
                upload = replace(self.upload(self.request()), name=name)
                with self.assertRaises(BatchCreationError) as caught:
                    self.creator.create(request, [upload])
                self.assertEqual("unsafe_filename", caught.exception.code)

        payload2 = b"second-original"
        sources = (
            replace(base_source, node_id="one", name="原图.PNG"),
            replace(
                base_source,
                node_id="two",
                storage_key="image:two",
                name="原图.png",
                size=len(payload2),
                expected_sha256=hashlib.sha256(payload2).hexdigest(),
            ),
        )
        request = self.request(sources=sources)
        first = replace(self.upload(self.request()), source_node_id="one", name="原图.PNG")
        second_path = self.upload_root / "second.bin"
        second_path.write_bytes(payload2)
        second = UploadedFile("two", second_path, "原图.png", len(payload2), "image/png", hashlib.sha256(payload2).hexdigest())
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(request, [first, second])
        self.assertEqual("unsafe_filename", caught.exception.code)

    def test_dry_run_failure_writes_no_workspace_or_manifest(self) -> None:
        (self.repo / "scripts" / "build_batch_manifest.py").write_text(
            "raise SystemExit('planned failure without secret')\n", encoding="utf-8"
        )
        with self.assertRaises(BatchCreationError) as caught:
            self.creator.create(self.request(), [self.upload(self.request())])
        self.assertEqual("planning_failed", caught.exception.code)
        self.assertFalse((self.test_root / "餐具_20260718").exists())
        self.assertFalse((self.repo / "manifests" / "餐具_20260718.batch_manifest.json").exists())

    def test_failure_before_repository_manifest_compensates_only_owned_workspace(self) -> None:
        request = self.request()
        upload = self.upload(request)
        foreign = self.test_root / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("keep", encoding="utf-8")

        import batch_creator as module

        with mock.patch.object(
            module,
            "_publish_manifest_no_replace",
            side_effect=OSError("injected repository write failure"),
        ):
            with self.assertRaises(BatchCreationError) as caught:
                self.creator.create(request, [upload])

        self.assertEqual("commit_failed", caught.exception.code)
        self.assertFalse((self.test_root / "餐具_20260718").exists())
        self.assertFalse((self.repo / "manifests" / "餐具_20260718.batch_manifest.json").exists())
        self.assertEqual(b"keep", (foreign / "keep.txt").read_bytes())
        self.assert_no_staging(self.test_root)

    def test_final_published_workspace_is_rehashed_before_manifest_commit(self) -> None:
        request = self.request()
        upload = self.upload(request)
        import batch_creator as module

        real_publish = module._publish_workspace

        def publish_then_corrupt(stage: Path, target: Path) -> None:
            real_publish(stage, target)
            (target / "inputs" / "white_bg" / "餐具正面.png").write_bytes(b"changed-after-publish")

        with mock.patch.object(module, "_publish_workspace", side_effect=publish_then_corrupt):
            with self.assertRaises(BatchCreationError) as caught:
                self.creator.create(request, [upload])

        self.assertEqual("integrity_mismatch", caught.exception.code)
        self.assertFalse((self.test_root / "餐具_20260718").exists())
        self.assertFalse((self.repo / "manifests" / "餐具_20260718.batch_manifest.json").exists())
        self.assert_no_staging(self.test_root)

    def test_repository_manifest_publish_never_replaces_racing_foreign_file(self) -> None:
        request = self.request()
        upload = self.upload(request)
        manifest_path = self.repo / "manifests" / "餐具_20260718.batch_manifest.json"
        foreign_bytes = b"foreign-manifest-must-survive"
        import batch_creator as module

        def inject_foreign_then_link(temporary: Path, destination: Path) -> None:
            destination.write_bytes(foreign_bytes)
            os.link(temporary, destination)

        with mock.patch.object(
            module,
            "_publish_manifest_no_replace",
            side_effect=inject_foreign_then_link,
            create=True,
        ):
            with self.assertRaises(BatchCreationError) as caught:
                self.creator.create(request, [upload])

        self.assertEqual("batch_exists", caught.exception.code)
        self.assertEqual(foreign_bytes, manifest_path.read_bytes())
        self.assertFalse((self.test_root / "餐具_20260718").exists())
        self.assert_no_staging(self.test_root)

    def test_marker_write_failure_removes_only_exact_empty_stage(self) -> None:
        request = self.request()
        upload = self.upload(request)
        foreign = self.test_root / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("keep", encoding="utf-8")
        real_write_text = Path.write_text

        def fail_workspace_marker(path: Path, data: str, *args, **kwargs):
            if path.name == ".canvas_batch":
                raise OSError("injected marker failure")
            return real_write_text(path, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", new=fail_workspace_marker):
            with self.assertRaises(BatchCreationError) as caught:
                self.creator.create(request, [upload])

        self.assertEqual("commit_failed", caught.exception.code)
        self.assert_no_staging(self.test_root)
        self.assertEqual(b"keep", (foreign / "keep.txt").read_bytes())

    def test_two_request_ids_for_same_product_id_can_publish_only_once(self) -> None:
        first_request = self.request(request_id="batch-req-race-1")
        second_request = self.request(request_id="batch-req-race-2")
        first_upload = self.upload(first_request)
        second_path = self.upload_root / "concurrent-second.bin"
        second_path.write_bytes(first_upload.path.read_bytes())
        second_upload = replace(first_upload, path=second_path)
        first_creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            test_root=self.test_root,
            today=lambda: date(2026, 7, 18),
        )
        second_creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            test_root=self.test_root,
            today=lambda: date(2026, 7, 18),
        )
        barrier = threading.Barrier(2)
        import batch_creator as module

        real_publish = module._publish_workspace

        def synchronized_publish(stage: Path, target: Path) -> None:
            barrier.wait(timeout=10)
            real_publish(stage, target)

        def run(creator: BatchCreator, request: BatchIntakeRequest, upload: UploadedFile):
            try:
                return creator.create(request, [upload])
            except BatchCreationError as exc:
                return exc

        with mock.patch.object(module, "_publish_workspace", side_effect=synchronized_publish):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(
                        lambda arguments: run(*arguments),
                        (
                            (first_creator, first_request, first_upload),
                            (second_creator, second_request, second_upload),
                        ),
                    )
                )

        successes = [item for item in outcomes if not isinstance(item, BatchCreationError)]
        failures = [item for item in outcomes if isinstance(item, BatchCreationError)]
        self.assertEqual(1, len(successes), outcomes)
        self.assertEqual(1, len(failures), outcomes)
        self.assertIn(failures[0].code, {"batch_exists", "commit_failed"})
        self.assertTrue((self.test_root / "餐具_20260718").is_dir())
        self.assertTrue((self.repo / "manifests" / "餐具_20260718.batch_manifest.json").is_file())
        receipt = json.loads(
            (self.test_root / "餐具_20260718" / "manifests" / "batch_intake_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(receipt["request_id"], {"batch-req-race-1", "batch-req-race-2"})
        self.assert_no_staging(self.test_root)

    def test_compensation_preserves_workspace_if_ownership_marker_changes(self) -> None:
        request = self.request()
        upload = self.upload(request)
        import batch_creator as module

        real_publish = module._publish_workspace

        def publish_then_change_marker(stage: Path, target: Path) -> None:
            real_publish(stage, target)
            (target / ".canvas_batch").write_text("foreign-owner\n", encoding="utf-8")

        with mock.patch.object(module, "_publish_workspace", side_effect=publish_then_change_marker), mock.patch.object(
            module, "_atomic_repository_manifest", side_effect=OSError("injected")
        ):
            with self.assertRaises(BatchCreationError):
                self.creator.create(request, [upload])

        target = self.test_root / "餐具_20260718"
        self.assertTrue(target.is_dir())
        self.assertEqual("foreign-owner\n", (target / ".canvas_batch").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
