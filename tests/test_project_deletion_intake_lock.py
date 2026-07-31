from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import (  # noqa: E402
    BatchCreationError,
    BatchCreator,
    UploadedFile,
    prepare_state_root,
)
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from batch_recycle_lock import BatchOperationLock  # noqa: E402
from workflow_batch_intake_service import WorkflowBatchIntakeService  # noqa: E402


class ProjectDeletionIntakeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.state = self.base / "state"
        self.test_root = self.base / "isolated"
        self.upload = self.base / "source.png"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "manifests").mkdir()
        shutil.copy2(
            ROOT / "scripts" / "build_batch_manifest.py",
            self.repo / "scripts" / "build_batch_manifest.py",
        )
        (self.repo / "canvas-bridge").mkdir()
        shutil.copy2(
            ROOT / "canvas-bridge" / "category_recipes.py",
            self.repo / "canvas-bridge",
        )
        shutil.copy2(
            ROOT / "canvas-bridge" / "image_count_contract.py",
            self.repo / "canvas-bridge",
        )
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        shutil.copy2(
            ROOT / "manifests" / "batch_manifest.template.json",
            self.repo / "manifests" / "batch_manifest.template.json",
        )
        shutil.copy2(
            ROOT / "manifests" / "asset_manifest.template.json",
            self.repo / "manifests" / "asset_manifest.template.json",
        )
        prepare_state_root(self.state)
        self.test_root.mkdir()
        (self.test_root / ".canvas_intake_test_root").write_text(
            "canvas-intake-test-root-v1\n", encoding="utf-8"
        )
        self.upload.write_bytes(b"safe-test-image")
        digest = hashlib.sha256(self.upload.read_bytes()).hexdigest()
        facts = ConfirmedFacts(
            product_type="杯子",
            height_cm=12,
            main_image_count=6,
            detail_image_count=8,
            handheld_main=2,
            handheld_detail=1,
            forbid_pouring_and_heating=True,
            missing_d_no_retake=True,
        )
        source = SourceImage(
            node_id="image-1",
            storage_key="local-image-1",
            name="source.png",
            size=self.upload.stat().st_size,
            mime_type="image/png",
            last_modified=1_000,
            expected_sha256=digest,
        )
        self.request = BatchIntakeRequest(
            request_id="batch-lock-request",
            requested_at=1_000,
            info_node_id="info-1",
            workflow_node_id="workflow-1",
            facts=facts,
            source_images=(source,),
        )
        self.uploaded = UploadedFile(
            source_node_id=source.node_id,
            name=source.name,
            path=self.upload,
            size=source.size,
            mime_type=source.mime_type,
            sha256=digest,
        )
        self.creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            test_root=self.test_root,
            today=lambda: date(2099, 1, 1),
            batch_lock_factory=BatchOperationLock,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_delete_lock_prevents_workspace_publish_then_release_allows_create(self) -> None:
        self.assertEqual(
            self.state.parent / "batch-operation-locks",
            self.creator.batch_lock_root,
        )
        held = threading.Event()
        release = threading.Event()

        def hold_delete_lock() -> None:
            with BatchOperationLock(
                "杯子_20990101",
                lock_root=self.creator.batch_lock_root,
            ):
                held.set()
                release.wait(timeout=10)

        thread = threading.Thread(target=hold_delete_lock)
        thread.start()
        self.assertTrue(held.wait(timeout=10))
        try:
            with self.assertRaises(BatchCreationError) as caught:
                self.creator.create(self.request, [self.uploaded])
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertEqual("batch_busy", caught.exception.code)
        self.assertFalse((self.test_root / "杯子_20990101").exists())
        self.assertFalse(
            (self.repo / "manifests" / "杯子_20990101.batch_manifest.json").exists()
        )

        result = self.creator.create(self.request, [self.uploaded])
        self.assertEqual("杯子_20990101", result.product_id)
        self.assertTrue(result.workspace_root.is_dir())

        def snapshot() -> dict[str, bytes]:
            files = {
                path.relative_to(self.test_root).as_posix(): path.read_bytes()
                for path in self.test_root.rglob("*")
                if path.is_file()
                and path.name != ".canvas_intake_test_root"
            }
            manifest = (
                self.repo / "manifests" / "杯子_20990101.batch_manifest.json"
            )
            files["repo-manifest"] = manifest.read_bytes()
            return files

        locked_snapshot = snapshot()
        shutil.rmtree(result.workspace_root)
        result.manifest_path.unlink()
        completed = (
            self.state
            / "completed"
            / f"{hashlib.sha256(self.request.request_id.encode('utf-8')).hexdigest()}.json"
        )
        completed.unlink()
        unlocked = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            test_root=self.test_root,
            today=lambda: date(2099, 1, 1),
        )
        unlocked.create(self.request, [self.uploaded])
        self.assertEqual(locked_snapshot, snapshot())

    def test_every_real_default_creator_uses_the_shared_batch_lock(self) -> None:
        production_parent = self.base / "Desktop" / "杯类"
        frozen_workspace = production_parent / "shuiping_20260712"
        frozen_workspace.mkdir(parents=True)
        (
            self.repo
            / "manifests"
            / "shuiping_20260712.batch_manifest.json"
        ).write_text(
            json.dumps(
                {
                    "product_id": "shuiping_20260712",
                    "workspace": {"root": str(frozen_workspace)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        direct = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
        )
        explicit_test_disable = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            batch_lock_factory=None,
        )
        service = WorkflowBatchIntakeService(
            self.repo,
            self.state,
            upload_port=0,
        )
        isolated_default = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            test_root=self.test_root,
        )

        self.assertIs(BatchOperationLock, direct.batch_lock_factory)
        self.assertIs(
            BatchOperationLock,
            service.creator.batch_lock_factory,
        )
        self.assertIsNone(explicit_test_disable.batch_lock_factory)
        self.assertIsNone(isolated_default.batch_lock_factory)


if __name__ == "__main__":
    unittest.main()
