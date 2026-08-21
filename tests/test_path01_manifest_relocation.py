from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import prepare_state_root  # noqa: E402
from batch_recycle_lock import (  # noqa: E402
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
import manifest_relocation  # noqa: E402
import project_deletion_service as deletion_module  # noqa: E402
import workflow_production_http_server as production_http  # noqa: E402


def _write_marker(
    workspace: Path,
    batch_id: str,
    *,
    marker_type: str = "canvas-batch-v1",
    marker_product_id: str | None = None,
    request_id: str = "intake-path01",
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".canvas_batch").write_text(
        json.dumps(
            {
                "type": marker_type,
                "product_id": marker_product_id or batch_id,
                "request_id": request_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _expected_relocation(
    value: object,
    *,
    old_install_root: str,
    new_install_root: str,
) -> tuple[object, int]:
    if isinstance(value, str):
        normalized = os.path.normcase(value)
        normalized_old = os.path.normcase(old_install_root)
        if normalized.startswith(normalized_old):
            suffix = value[len(old_install_root) :]
            if (
                not suffix
                or suffix[0] in {"/", "\\"}
                or old_install_root[-1:] in {"/", "\\"}
            ):
                return new_install_root + suffix, 1
        return value, 0
    if isinstance(value, list):
        output: list[object] = []
        count = 0
        for item in value:
            relocated, item_count = _expected_relocation(
                item,
                old_install_root=old_install_root,
                new_install_root=new_install_root,
            )
            output.append(relocated)
            count += item_count
        return output, count
    if isinstance(value, dict):
        output: dict[str, object] = {}
        count = 0
        for key, item in value.items():
            relocated, item_count = _expected_relocation(
                item,
                old_install_root=old_install_root,
                new_install_root=new_install_root,
            )
            output[key] = relocated
            count += item_count
        return output, count
    return value, 0


class ManifestRelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.new_install_root = self.base / "portable-new"
        self.repository_root = self.new_install_root / "workflow-runtime"
        self.manifests_root = self.repository_root / "manifests"
        self.manifests_root.mkdir(parents=True)
        self.old_install_root = self.base / "portable-old"
        self.parent_name = "陶瓷类"
        self.batch_id = "杯子_20990101_010203"
        self.old_workspace = (
            self.old_install_root / self.parent_name / self.batch_id
        )
        self.new_workspace = (
            self.new_install_root / self.parent_name / self.batch_id
        )
        self.manifest_path = (
            self.manifests_root / f"{self.batch_id}.batch_manifest.json"
        )
        self.journal_path = self.manifests_root / f"{self.batch_id}.events.jsonl"
        self.lock_root = self.base / "locks"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self, *, workspace_root: Path | None = None) -> dict[str, object]:
        workspace = workspace_root or self.old_workspace
        old = self.old_install_root
        return {
            "product_id": self.batch_id,
            "workspace": {
                "root": str(workspace),
                "manifests_root": str(workspace / "manifests"),
                "inputs_root": str(workspace / "inputs"),
                "drafts_root": str(workspace / "drafts"),
                "artifacts_root": str(workspace / "artifacts"),
                "outputs_root": str(workspace / "outputs"),
            },
            "inputs": {
                "white_bg_images": [
                    str(workspace / "inputs" / "white_bg" / "001.png")
                ],
                "style_reference_images": [
                    str(workspace / "inputs" / "style_refs")
                ],
                "set_group_images": [],
                "component_white_bg_images": [],
            },
            "drafts": {
                "product_identity_draft": str(
                    workspace / "drafts" / "product_identity.json"
                ),
                "style_master_draft": "",
            },
            "artifacts": {
                "asset_manifest": str(
                    workspace / "manifests" / "asset_manifest.json"
                ),
                "main_variable_configs": [
                    str(workspace / "artifacts" / "main" / "main_01.json")
                ],
                "nested": {
                    "final_prompts": [
                        str(workspace / "artifacts" / "final" / "main_01.json"),
                        7,
                        False,
                    ]
                },
            },
            "outputs": {
                "renders": [str(workspace / "outputs" / "renders")],
                "repaired": [],
            },
            "unchanged": {
                str(old): "键名不改",
                "embedded": f"历史说明：{old}",
                "lookalike": f"{old}-other",
            },
        }

    def _write_manifest(self, value: dict[str, object]) -> None:
        self.manifest_path.write_bytes(_json_bytes(value))

    def _relocate(self) -> int:
        with mock.patch.object(
            manifest_relocation,
            "BatchOperationLock",
            side_effect=lambda batch_id: BatchOperationLock(
                batch_id,
                lock_root=self.lock_root,
            ),
        ):
            return manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )

    def _assert_fail_closed(self) -> None:
        before = self.manifest_path.read_bytes()
        lock = mock.Mock(side_effect=AssertionError("锁不应被触碰"))
        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )
        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertFalse(self.journal_path.exists())
        lock.assert_not_called()

    def _assert_semantically_equivalent_workspace_is_hot_path(
        self,
        workspace_root: Path,
    ) -> None:
        _write_marker(self.new_workspace, self.batch_id)
        self.assertEqual(self.batch_id, workspace_root.name)
        self.assertNotEqual(str(self.new_workspace), str(workspace_root))
        self.assertTrue(
            manifest_relocation._same_path(workspace_root, self.new_workspace)
        )
        self._write_manifest(self._manifest(workspace_root=workspace_root))
        before = self.manifest_path.read_bytes()
        before_mtime = self.manifest_path.stat().st_mtime_ns
        lock = mock.Mock(side_effect=AssertionError("规范化热路径不应获取锁"))

        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )

        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertEqual(before_mtime, self.manifest_path.stat().st_mtime_ns)
        self.assertFalse(self.journal_path.exists())
        lock.assert_not_called()

    def test_correct_path_is_byte_and_mtime_stable_without_lock_interaction(self) -> None:
        self._write_manifest(self._manifest(workspace_root=self.new_workspace))
        before = self.manifest_path.read_bytes()
        before_mtime = self.manifest_path.stat().st_mtime_ns
        lock = mock.Mock(side_effect=AssertionError("热路径不应获取锁"))

        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )

        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertEqual(before_mtime, self.manifest_path.stat().st_mtime_ns)
        self.assertFalse(self.journal_path.exists())
        lock.assert_not_called()

    def test_semantically_equal_workspace_with_install_root_case_change_is_hot_path(
        self,
    ) -> None:
        equivalent_install_root = (
            self.new_install_root.parent / self.new_install_root.name.upper()
        )
        self._assert_semantically_equivalent_workspace_is_hot_path(
            equivalent_install_root / self.parent_name / self.batch_id
        )

    def test_semantically_equal_workspace_with_redundant_parent_segment_is_hot_path(
        self,
    ) -> None:
        self._assert_semantically_equivalent_workspace_is_hot_path(
            self.new_install_root
            / self.parent_name
            / ".."
            / self.parent_name
            / self.batch_id
        )

    def test_moved_manifest_relocates_every_nested_string_value_and_appends_event(self) -> None:
        _write_marker(self.new_workspace, self.batch_id)
        original = self._manifest()
        self._write_manifest(original)
        existing = (
            json.dumps(
                {
                    "event": "existing",
                    "historical_path": str(self.old_workspace),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        self.journal_path.write_bytes(existing)
        expected, expected_count = _expected_relocation(
            original,
            old_install_root=str(self.old_install_root),
            new_install_root=str(self.new_install_root.resolve()),
        )

        result = self._relocate()

        self.assertEqual(expected_count, result)
        self.assertGreater(result, 0)
        self.assertEqual(_json_bytes(expected), self.manifest_path.read_bytes())
        relocated = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(self.batch_id, relocated["product_id"])
        self.assertIn(str(self.old_install_root), relocated["unchanged"])
        self.assertEqual(
            f"历史说明：{self.old_install_root}",
            relocated["unchanged"]["embedded"],
        )
        journal = self.journal_path.read_bytes()
        self.assertTrue(journal.startswith(existing))
        events = [
            json.loads(line)
            for line in journal.decode("utf-8").splitlines()
        ]
        self.assertEqual(2, len(events))
        event = events[-1]
        self.assertEqual("workspace_relocated", event["event"])
        self.assertEqual(str(self.old_install_root), event["old_install_root"])
        self.assertEqual(
            str(self.new_install_root.resolve()),
            event["new_install_root"],
        )
        self.assertEqual(expected_count, event["replaced_count"])
        self.assertEqual([], list(self.manifests_root.glob("*.tmp")))

    def test_invalid_manifest_json_is_unchanged(self) -> None:
        self.manifest_path.write_bytes(b"{broken json\n")
        self._assert_fail_closed()

    def test_product_id_mismatch_is_unchanged(self) -> None:
        manifest = self._manifest()
        manifest["product_id"] = "其他批次"
        self._write_manifest(manifest)
        self._assert_fail_closed()

    def test_workspace_tail_mismatch_is_unchanged(self) -> None:
        manifest = self._manifest(
            workspace_root=self.old_install_root / self.parent_name / "其他批次"
        )
        self._write_manifest(manifest)
        _write_marker(self.new_workspace, self.batch_id)
        self._assert_fail_closed()

    def test_missing_expected_workspace_is_unchanged(self) -> None:
        self._write_manifest(self._manifest())
        self._assert_fail_closed()

    def test_missing_workspace_marker_is_unchanged(self) -> None:
        self.new_workspace.mkdir(parents=True)
        self._write_manifest(self._manifest())
        self._assert_fail_closed()

    def test_workspace_marker_product_mismatch_is_unchanged(self) -> None:
        _write_marker(
            self.new_workspace,
            self.batch_id,
            marker_product_id="其他批次",
        )
        self._write_manifest(self._manifest())
        self._assert_fail_closed()

    def test_workspace_marker_type_mismatch_is_unchanged(self) -> None:
        _write_marker(
            self.new_workspace,
            self.batch_id,
            marker_type="not-a-canvas-batch",
        )
        self._write_manifest(self._manifest())
        self._assert_fail_closed()

    def test_busy_lock_defers_relocation_without_writing_or_raising(self) -> None:
        _write_marker(self.new_workspace, self.batch_id)
        self._write_manifest(self._manifest())
        before = self.manifest_path.read_bytes()
        acquired = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with BatchOperationLock(self.batch_id, lock_root=self.lock_root):
                acquired.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(acquired.wait(timeout=5))
        try:
            with mock.patch.object(
                manifest_relocation,
                "BatchOperationLock",
                side_effect=lambda batch_id: BatchOperationLock(
                    batch_id,
                    lock_root=self.lock_root,
                ),
            ):
                result = manifest_relocation.relocate_manifest_if_moved(
                    self.repository_root,
                    self.batch_id,
                )
        finally:
            release.set()
            holder.join(timeout=5)

        self.assertFalse(holder.is_alive())
        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertFalse(self.journal_path.exists())

    def test_unavailable_lock_defers_relocation_without_writing_or_raising(self) -> None:
        _write_marker(self.new_workspace, self.batch_id)
        self._write_manifest(self._manifest())
        before = self.manifest_path.read_bytes()

        with mock.patch.object(
            manifest_relocation,
            "BatchOperationLock",
            side_effect=BatchOperationLockUnavailable("injected"),
        ):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )

        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertFalse(self.journal_path.exists())

    def test_second_call_after_relocation_is_fully_idempotent(self) -> None:
        _write_marker(self.new_workspace, self.batch_id)
        self._write_manifest(self._manifest())
        self.assertGreater(self._relocate(), 0)
        manifest_before = self.manifest_path.read_bytes()
        journal_before = self.journal_path.read_bytes()
        mtime_before = self.manifest_path.stat().st_mtime_ns
        lock = mock.Mock(side_effect=AssertionError("二次调用不应获取锁"))

        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )

        self.assertEqual(0, result)
        self.assertEqual(manifest_before, self.manifest_path.read_bytes())
        self.assertEqual(journal_before, self.journal_path.read_bytes())
        self.assertEqual(mtime_before, self.manifest_path.stat().st_mtime_ns)
        lock.assert_not_called()

    def test_public_entry_swallows_unexpected_internal_failure(self) -> None:
        self._write_manifest(self._manifest())
        before = self.manifest_path.read_bytes()
        with mock.patch.object(
            manifest_relocation,
            "_candidate",
            side_effect=RuntimeError("injected"),
        ):
            result = manifest_relocation.relocate_manifest_if_moved(
                self.repository_root,
                self.batch_id,
            )
        self.assertEqual(0, result)
        self.assertEqual(before, self.manifest_path.read_bytes())


class _NoAuditLedger:
    def has_project_deletion(self, *_args, **_kwargs) -> bool:
        return False

    def record_project_deletion(self, *_args, **_kwargs) -> None:
        raise AssertionError("预检不应写删除审计")


class ManifestRelocationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.install_root = self.base / "portable-new"
        self.repository_root = self.install_root / "workflow-runtime"
        self.manifests_root = self.repository_root / "manifests"
        self.manifests_root.mkdir(parents=True)
        shutil.copytree(ROOT / "categories", self.repository_root / "categories")
        self.old_install_root = self.base / "portable-old"
        self.workspace_parent = self.install_root / "杯类"
        self.workspace_parent.mkdir(parents=True)
        self.state_root = self.base / "state"
        prepare_state_root(self.state_root)
        self.lock_root = self.base / "locks"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _portable_manifest(self, batch_id: str) -> tuple[Path, Path]:
        old_workspace = self.old_install_root / "杯类" / batch_id
        new_workspace = self.workspace_parent / batch_id
        _write_marker(new_workspace, batch_id, request_id=f"intake-{batch_id}")
        (new_workspace / "outputs" / "renders").mkdir(parents=True)
        manifest_path = self.manifests_root / f"{batch_id}.batch_manifest.json"
        manifest_path.write_bytes(
            _json_bytes(
                {
                    "product_id": batch_id,
                    "user_confirmed_facts": {
                        "main_image_count": 1,
                        "detail_image_count": 1,
                    },
                    "workspace": {"root": str(old_workspace)},
                    "outputs": {
                        "renders": [str(old_workspace / "outputs" / "renders")],
                        "repaired": [str(old_workspace / "outputs" / "repaired")],
                    },
                    "artifacts": {
                        "final_prompts": [
                            str(old_workspace / "artifacts" / "final_prompts")
                        ]
                    },
                }
            )
        )
        return manifest_path, new_workspace

    def _real_temp_lock(self):
        return mock.patch.object(
            manifest_relocation,
            "BatchOperationLock",
            side_effect=lambda batch_id: BatchOperationLock(
                batch_id,
                lock_root=self.lock_root,
            ),
        )

    def _deletion_service(self) -> deletion_module.ProjectDeletionService:
        return deletion_module.ProjectDeletionService(
            self.repository_root,
            workspace_parent=self.workspace_parent,
            state_root=self.state_root,
            audit_ledger=_NoAuditLedger(),
            recycle_executor=lambda _path: None,
            lock_root=self.lock_root,
        )

    def test_http_quote_loader_recovers_a_moved_batch(self) -> None:
        batch_id = "杯子_20990101_111111"
        manifest_path, new_workspace = self._portable_manifest(batch_id)
        application = production_http.WorkflowProductionHttpApplication(
            self.repository_root,
            "test-token",
        )
        with mock.patch.object(
            production_http,
            "relocate_manifest_if_moved",
            return_value=0,
        ):
            with self.assertRaises(production_http.ProductionHttpError) as caught:
                application.quote(batch_id)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual("workspace marker missing", str(caught.exception))

        with self._real_temp_lock():
            quote = application.quote(batch_id)

        self.assertEqual(2, quote["totalCount"])
        self.assertEqual(0, quote["readyCount"])
        self.assertEqual(2, quote["remainingCount"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(str(new_workspace), manifest["workspace"]["root"])

    def test_deletion_preview_recovers_a_moved_batch_and_stays_signature_stable(self) -> None:
        batch_id = "杯子_20990101_222222"
        manifest_path, new_workspace = self._portable_manifest(batch_id)
        (new_workspace / "asset.bin").write_bytes(b"asset")
        service = self._deletion_service()
        with mock.patch.object(
            deletion_module,
            "relocate_manifest_if_moved",
            return_value=0,
        ):
            with self.assertRaises(deletion_module.ProjectDeletionError) as caught:
                service.preview([batch_id])
        self.assertEqual("workspace_outside_root", caught.exception.code)

        with self._real_temp_lock():
            preview = service.preview([batch_id])
            manifest_mtime = manifest_path.stat().st_mtime_ns
            repeated = service.preview([batch_id])

        self.assertEqual([batch_id], [item["batchId"] for item in preview["batches"]])
        self.assertEqual(preview["requestId"], repeated["requestId"])
        self.assertEqual(manifest_mtime, manifest_path.stat().st_mtime_ns)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(str(new_workspace), manifest["workspace"]["root"])

    def test_current_path_quote_and_deletion_preview_keep_existing_behavior_and_bytes(self) -> None:
        quote_id = "杯子_20990101_333333"
        manifest_path, new_workspace = self._portable_manifest(quote_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected, count = _expected_relocation(
            manifest,
            old_install_root=str(self.old_install_root),
            new_install_root=str(self.install_root.resolve()),
        )
        self.assertGreater(count, 0)
        manifest_path.write_bytes(_json_bytes(expected))
        before = manifest_path.read_bytes()
        before_mtime = manifest_path.stat().st_mtime_ns
        application = production_http.WorkflowProductionHttpApplication(
            self.repository_root,
            "test-token",
        )
        lock = mock.Mock(side_effect=AssertionError("正确路径不应获取重定位锁"))

        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            quote = application.quote(quote_id)

        self.assertEqual(2, quote["totalCount"])
        self.assertEqual(before, manifest_path.read_bytes())
        self.assertEqual(before_mtime, manifest_path.stat().st_mtime_ns)
        self.assertFalse(
            (self.manifests_root / f"{quote_id}.events.jsonl").exists()
        )
        self.assertTrue(new_workspace.is_dir())
        lock.assert_not_called()

        delete_id = "杯子_20990101_444444"
        delete_manifest, _delete_workspace = self._portable_manifest(delete_id)
        delete_value = json.loads(delete_manifest.read_text(encoding="utf-8"))
        delete_expected, delete_count = _expected_relocation(
            delete_value,
            old_install_root=str(self.old_install_root),
            new_install_root=str(self.install_root.resolve()),
        )
        self.assertGreater(delete_count, 0)
        delete_manifest.write_bytes(_json_bytes(delete_expected))
        delete_before = delete_manifest.read_bytes()
        service = self._deletion_service()

        with mock.patch.object(manifest_relocation, "BatchOperationLock", lock):
            preview = service.preview([delete_id])

        self.assertEqual(delete_id, preview["batches"][0]["batchId"])
        self.assertEqual(delete_before, delete_manifest.read_bytes())
        self.assertFalse(
            (self.manifests_root / f"{delete_id}.events.jsonl").exists()
        )
        lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
