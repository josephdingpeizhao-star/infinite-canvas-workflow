from __future__ import annotations

import hashlib
import json
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
from batch_recycle_lock import BatchOperationLock  # noqa: E402
from canvas_workbench_service import WorkbenchEventLedger  # noqa: E402
import project_deletion_service as deletion_module  # noqa: E402
from project_deletion_service import (  # noqa: E402
    ProjectDeletionError,
    ProjectDeletionService,
)


class FakeAuditLedger:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.entries: list[dict[str, str]] = []

    def record_project_deletion(
        self,
        batch_id: str,
        request_id: str,
    ) -> None:
        self.order.append(f"audit:{batch_id}")
        self.entries.append(
            {
                "batch_id": batch_id,
                "request_id": request_id,
            }
        )

    def has_project_deletion(
        self,
        batch_id: str,
        request_id: str | None = None,
        *,
        instance_commit: str | None = None,
    ) -> bool:
        return any(
            item["batch_id"] == batch_id
            and (
                request_id is None
                or item["request_id"] == request_id
            )
            and (
                instance_commit is None
                or deletion_module.project_deletion_request_has_instance(
                    item["request_id"],
                    instance_commit,
                )
            )
            for item in self.entries
        )


class FakeRecycleExecutor:
    def __init__(
        self,
        trash_root: Path,
        order: list[str],
        *,
        fail_name: str | None = None,
    ) -> None:
        self.trash_root = trash_root
        self.order = order
        self.fail_name = fail_name
        self.paths: list[Path] = []

    def __call__(self, path: Path) -> None:
        self.order.append(f"delete:{path.name}")
        self.paths.append(path)
        if path.name == self.fail_name:
            raise RuntimeError("injected recycle failure")
        target = self.trash_root / f"{len(self.paths):03d}-{path.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))


class ProjectDeletionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.manifests = self.repo / "manifests"
        self.reports = self.repo / "reports"
        self.workspace_parent = self.base / "Desktop" / "杯类"
        self.state_root = self.base / ".infinite-canvas" / "batch-intake"
        self.lock_root = self.base / ".infinite-canvas" / "batch-operation-locks"
        self.trash = self.base / "fake-windows-recycle-bin"
        self.manifests.mkdir(parents=True)
        self.reports.mkdir()
        self.workspace_parent.mkdir(parents=True)
        prepare_state_root(self.state_root)
        self.order: list[str] = []
        self.audit = FakeAuditLedger(self.order)
        self.executor = FakeRecycleExecutor(self.trash, self.order)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(
        self,
        *,
        executor: FakeRecycleExecutor | None = None,
        lock_root: Path | None = None,
    ) -> ProjectDeletionService:
        return ProjectDeletionService(
            self.repo,
            workspace_parent=self.workspace_parent,
            state_root=self.state_root,
            audit_ledger=self.audit,
            recycle_executor=executor or self.executor,
            lock_root=lock_root if lock_root is not None else self.lock_root,
        )

    def _write_batch(
        self,
        batch_id: str,
        *,
        events: list[dict[str, object]] | None = None,
        recycled: bool = False,
        include_intake_residue: bool = False,
        include_repository_residue: bool = False,
    ) -> Path:
        request_id = f"intake-{batch_id}"
        active = self.workspace_parent / batch_id
        workspace = (
            self.workspace_parent
            / "_回收站"
            / f"{batch_id}__20260726T010203000004Z"
            if recycled
            else active
        )
        workspace.mkdir(parents=True)
        (workspace / ".canvas_batch").write_text(
            json.dumps(
                {
                    "type": "canvas-batch-v1",
                    "request_id": request_id,
                    "product_id": batch_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (workspace / "image.png").write_bytes(b"image")
        (self.manifests / f"{batch_id}.batch_manifest.json").write_text(
            json.dumps(
                {
                    "product_id": batch_id,
                    "workspace": {"root": str(active)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if events is not None:
            (self.manifests / f"{batch_id}.events.jsonl").write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False) + "\n" for item in events
                ),
                encoding="utf-8",
            )
        if include_intake_residue:
            digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            completed = self.state_root / "completed"
            completed.mkdir()
            (completed / f"{digest}.json").write_text(
                json.dumps(
                    {"request_id": request_id, "product_id": batch_id},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            spool = self.state_root / "spool" / digest
            spool.mkdir(parents=True)
            (spool / ".canvas_batch_intake_request").write_text(
                request_id + "\n", encoding="utf-8"
            )
            (spool / "001.upload").write_bytes(b"upload")
            staging = (
                self.workspace_parent
                / f".{batch_id}.{digest[:12]}.batch-intake-staging"
            )
            staging.mkdir()
            (staging / ".canvas_batch").write_text(
                json.dumps(
                    {
                        "type": "canvas-batch-v1",
                        "request_id": request_id,
                        "product_id": batch_id,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if include_repository_residue:
            (self.manifests / f"{batch_id}.canvas_layout.json").write_text(
                "{}", encoding="utf-8"
            )
            for suffix in (
                "_final_prompt_integrity_report.json",
                "_final_prompt_integrity_report.md",
                "_qc_report.json",
            ):
                (self.reports / f"{batch_id}{suffix}").write_text(
                    "report", encoding="utf-8"
                )
        return workspace

    def test_preview_reports_independent_lifecycle_flags(self) -> None:
        self._write_batch("open")
        self._write_batch(
            "closed",
            events=[{"event": "batch_acceptance_closed"}],
        )
        self._write_batch(
            "delivered",
            events=[
                {"event": "batch_acceptance_closed"},
                {"event": "delivery_packaged"},
            ],
        )
        self._write_batch(
            "recycled",
            events=[
                {"event": "batch_acceptance_closed"},
                {"event": "delivery_packaged"},
                {"event": "batch_recycled"},
            ],
            recycled=True,
        )

        service = self._service()
        batch_ids = ["recycled", "open", "delivered", "closed", "open"]
        preview = service.preview(batch_ids)
        repeated = service.preview(batch_ids)

        self.assertEqual(preview["requestId"], repeated["requestId"])
        self.assertEqual(
            ["closed", "delivered", "open", "recycled"],
            [item["batchId"] for item in preview["batches"]],
        )
        by_id = {item["batchId"]: item for item in preview["batches"]}
        self.assertEqual("in_production", by_id["open"]["status"])
        self.assertEqual("closed", by_id["closed"]["status"])
        self.assertEqual("delivered", by_id["delivered"]["status"])
        self.assertEqual("recycled", by_id["recycled"]["status"])
        self.assertTrue(by_id["recycled"]["closed"])
        self.assertTrue(by_id["recycled"]["delivered"])
        self.assertTrue(by_id["recycled"]["recycled"])
        self.assertTrue(by_id["recycled"]["requiresTypedConfirmation"])

    def test_delete_uses_exact_scope_and_manifest_is_last(self) -> None:
        batch_id = "杯子_20990101"
        self._write_batch(
            batch_id,
            events=[{"event": "production_completed"}],
            include_intake_residue=True,
            include_repository_residue=True,
        )
        other_workspace = self._write_batch("其他_20990101")
        global_event = self.state_root / "canvas_workbench.events.jsonl"
        global_event.write_text("keep\n", encoding="utf-8")
        global_lock = self.state_root / ".batch_intake_service.lock"
        global_lock.write_bytes(b"0")
        service = self._service()
        preview = service.preview([batch_id])

        result = service.execute(preview["requestId"], [batch_id])

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["status"])
        self.assertEqual("deleted", result["batches"][0]["status"])
        names = [path.name for path in self.executor.paths]
        self.assertEqual(f"{batch_id}.events.jsonl", names[-2])
        self.assertEqual(f"{batch_id}.batch_manifest.json", names[-1])
        self.assertEqual(f"audit:{batch_id}", self.order[0])
        self.assertEqual(
            preview["requestId"],
            self.audit.entries[0]["request_id"],
        )
        self.assertTrue(other_workspace.is_dir())
        self.assertTrue(
            (self.manifests / "其他_20990101.batch_manifest.json").is_file()
        )
        self.assertEqual("keep\n", global_event.read_text(encoding="utf-8"))
        self.assertEqual(b"0", global_lock.read_bytes())
        self.assertTrue(self.lock_root.is_dir())

    def test_failure_stops_following_batches_and_retry_is_idempotent(self) -> None:
        self._write_batch("a")
        self._write_batch("b")
        self._write_batch("c")
        failing = FakeRecycleExecutor(
            self.trash,
            self.order,
            fail_name="b",
        )
        service = self._service(executor=failing)
        preview = service.preview(["c", "b", "a"])

        stopped = service.execute(preview["requestId"], ["c", "b", "a"])

        self.assertFalse(stopped["ok"])
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual(
            ["deleted", "failed", "not_started"],
            [item["status"] for item in stopped["batches"]],
        )
        self.assertTrue((self.workspace_parent / "c").is_dir())

        service.recycle_executor = self.executor
        retry = service.preview(["a", "b", "c"])
        completed = service.execute(retry["requestId"], ["a", "b", "c"])
        self.assertTrue(completed["ok"])
        self.assertEqual("already_deleted", completed["batches"][0]["status"])
        self.assertEqual("deleted", completed["batches"][1]["status"])
        self.assertEqual("deleted", completed["batches"][2]["status"])

    def test_recreated_same_batch_gets_a_new_deletion_audit(self) -> None:
        batch_id = "可复用_20990101"
        self._write_batch(batch_id)
        service = self._service()
        first_preview = service.preview([batch_id])
        first = service.execute(first_preview["requestId"], [batch_id])
        self.assertTrue(first["ok"])

        self._write_batch(batch_id)
        second_preview = service.preview([batch_id])
        self.assertEqual("in_production", second_preview["batches"][0]["status"])
        second = service.execute(second_preview["requestId"], [batch_id])

        self.assertTrue(second["ok"])
        self.assertEqual(2, len(self.audit.entries))
        audit_ids = [entry["request_id"] for entry in self.audit.entries]
        self.assertEqual(
            [first_preview["requestId"], second_preview["requestId"]],
            audit_ids,
        )
        self.assertNotEqual(audit_ids[0], audit_ids[1])

    def test_status_change_after_preview_requires_a_new_confirmation(self) -> None:
        batch_id = "状态变化_20990101"
        self._write_batch(
            batch_id,
            events=[{"event": "production_completed"}],
        )
        service = self._service()
        preview = service.preview([batch_id])
        journal = self.manifests / f"{batch_id}.events.jsonl"
        with journal.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {"event": "batch_acceptance_closed"},
                    ensure_ascii=False,
                )
                + "\n"
            )

        result = service.execute(preview["requestId"], [batch_id])

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["batches"][0]["status"])
        self.assertIn("重新查看删除清单", result["batches"][0]["message"])
        self.assertFalse(self.audit.entries)
        self.assertTrue((self.workspace_parent / batch_id).is_dir())
        refreshed = service.preview([batch_id])
        self.assertNotEqual(preview["requestId"], refreshed["requestId"])
        self.assertTrue(
            refreshed["batches"][0]["requiresTypedConfirmation"]
        )

    def test_old_preview_cannot_delete_a_recreated_same_name_batch(self) -> None:
        batch_id = "旧确认_20990101"
        self._write_batch(batch_id)
        service = self._service()
        old_preview = service.preview([batch_id])
        first = service.execute(old_preview["requestId"], [batch_id])
        self.assertTrue(first["ok"])

        recreated = self._write_batch(batch_id)
        stale = service.execute(old_preview["requestId"], [batch_id])

        self.assertFalse(stale["ok"])
        self.assertEqual("failed", stale["batches"][0]["status"])
        self.assertIn("重新查看删除清单", stale["batches"][0]["message"])
        self.assertTrue(recreated.is_dir())
        self.assertEqual(1, len(self.audit.entries))

    def test_old_audit_cannot_claim_recreated_batch_missing_manifest(self) -> None:
        batch_id = "缺清单重建_20990101"
        self._write_batch(batch_id)
        service = self._service()
        first_preview = service.preview([batch_id])
        self.assertTrue(
            service.execute(first_preview["requestId"], [batch_id])["ok"]
        )

        recreated = self._write_batch(batch_id)
        (self.manifests / f"{batch_id}.batch_manifest.json").unlink()

        with self.assertRaises(ProjectDeletionError) as caught:
            service.preview([batch_id])
        self.assertEqual("manifest_missing", caught.exception.code)
        self.assertTrue(recreated.is_dir())
        self.assertEqual(1, len(self.audit.entries))

    def test_partial_delete_matches_instance_after_workspace_is_gone(self) -> None:
        batch_id = "续做_20990101"
        workspace = self._write_batch(
            batch_id,
            events=[{"event": "production_completed"}],
            include_repository_residue=True,
        )
        failing = FakeRecycleExecutor(
            self.trash,
            self.order,
            fail_name=f"{batch_id}_final_prompt_integrity_report.json",
        )
        service = self._service(executor=failing)
        preview = service.preview([batch_id])
        first = service.execute(preview["requestId"], [batch_id])
        self.assertFalse(first["ok"])
        self.assertFalse(workspace.exists())
        self.assertEqual(1, len(self.audit.entries))

        resumed_service = self._service()
        resumed_preview = resumed_service.preview([batch_id])
        self.assertEqual(
            "deletion_pending",
            resumed_preview["batches"][0]["status"],
        )
        resumed = resumed_service.execute(
            resumed_preview["requestId"],
            [batch_id],
        )

        self.assertTrue(resumed["ok"])
        self.assertEqual("deleted", resumed["batches"][0]["status"])
        self.assertNotEqual(
            preview["requestId"],
            resumed_preview["requestId"],
        )
        self.assertEqual(
            [preview["requestId"], resumed_preview["requestId"]],
            [entry["request_id"] for entry in self.audit.entries],
        )

    def test_runtime_root_reparse_change_stops_before_deleting(self) -> None:
        batch_id = "根变化_20990101"
        workspace = self._write_batch(batch_id)
        service = self._service()
        preview = service.preview([batch_id])
        original_is_reparse = deletion_module._is_reparse

        def injected_reparse(path: Path) -> bool:
            if Path(path) == self.workspace_parent:
                return True
            return original_is_reparse(path)

        with mock.patch.object(
            deletion_module,
            "_is_reparse",
            side_effect=injected_reparse,
        ):
            result = service.execute(preview["requestId"], [batch_id])

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["batches"][0]["status"])
        self.assertTrue(workspace.is_dir())
        self.assertFalse(self.executor.paths)
        self.assertFalse(self.audit.entries)

    def test_lock_busy_and_broken_lock_fail_closed(self) -> None:
        self._write_batch("busy")
        service = self._service()
        preview = service.preview(["busy"])
        held = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with BatchOperationLock("busy", lock_root=self.lock_root):
                held.set()
                release.wait(timeout=10)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(held.wait(timeout=10))
        try:
            result = service.execute(preview["requestId"], ["busy"])
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["batches"][0]["status"])
        self.assertFalse(self.audit.entries)
        self.assertTrue((self.workspace_parent / "busy").is_dir())

        bad_root = self.base / "not-a-directory"
        bad_root.write_text("blocked", encoding="utf-8")
        broken = self._service(lock_root=bad_root)
        preview = broken.preview(["busy"])
        result = broken.execute(preview["requestId"], ["busy"])
        self.assertFalse(result["ok"])
        self.assertFalse(self.audit.entries)
        self.assertTrue((self.workspace_parent / "busy").is_dir())

    def test_unknown_damaged_and_ambiguous_batches_are_rejected(self) -> None:
        service = self._service()
        with self.assertRaises(ProjectDeletionError) as unknown:
            service.preview(["missing"])
        self.assertEqual("batch_not_found", unknown.exception.code)

        self._write_batch("damaged", events=[{"event": "production_completed"}])
        (self.manifests / "damaged.events.jsonl").write_text(
            "{not-json}\n", encoding="utf-8"
        )
        with self.assertRaises(ProjectDeletionError) as damaged:
            service.preview(["damaged"])
        self.assertEqual("journal_invalid", damaged.exception.code)

        self._write_batch("ambiguous", recycled=True)
        second = (
            self.workspace_parent
            / "_回收站"
            / "ambiguous__20260726T010203000005Z"
        )
        second.mkdir()
        (second / ".canvas_batch").write_text(
            json.dumps(
                {
                    "type": "canvas-batch-v1",
                    "request_id": "intake-ambiguous",
                    "product_id": "ambiguous",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ProjectDeletionError) as ambiguous:
            service.preview(["ambiguous"])
        self.assertEqual("workspace_ambiguous", ambiguous.exception.code)

    def test_recycled_lifecycle_with_active_workspace_has_no_side_effects(self) -> None:
        batch_id = "recycled-but-active"
        workspace = self._write_batch(
            batch_id,
            events=[{"event": "batch_recycled"}],
        )

        with self.assertRaises(ProjectDeletionError) as caught:
            self._service().preview([batch_id])

        self.assertEqual("workspace_ambiguous", caught.exception.code)
        self.assertTrue(workspace.is_dir())
        self.assertTrue((workspace / "image.png").is_file())
        self.assertFalse(self.audit.entries)
        self.assertFalse(self.executor.paths)

    def test_manifest_workspace_outside_root_is_rejected(self) -> None:
        batch_id = "outside"
        outside = self.base / "outside" / batch_id
        outside.mkdir(parents=True)
        (outside / ".canvas_batch").write_text(
            json.dumps(
                {
                    "type": "canvas-batch-v1",
                    "request_id": "intake-outside",
                    "product_id": batch_id,
                }
            ),
            encoding="utf-8",
        )
        (self.manifests / f"{batch_id}.batch_manifest.json").write_text(
            json.dumps(
                {"product_id": batch_id, "workspace": {"root": str(outside)}}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ProjectDeletionError) as caught:
            self._service().preview([batch_id])
        self.assertEqual("workspace_outside_root", caught.exception.code)

    def test_real_workbench_audit_is_minimal_append_only_evidence(self) -> None:
        ledger = WorkbenchEventLedger(self.state_root, clock_ms=lambda: 123_456)
        request_id = "pd1." + ("a" * 64)

        ledger.record_project_deletion(
            "杯子_20990101",
            request_id,
        )

        entry = json.loads(
            (self.state_root / "canvas_workbench.events.jsonl").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {
                "event": "project_batch_deletion_requested",
                "batch_id": "杯子_20990101",
                "request_id": request_id,
                "source_entry": "workbench",
                "recorded_at": 123_456,
            },
            entry,
        )
        self.assertTrue(ledger.has_project_deletion("杯子_20990101"))
        self.assertTrue(
            ledger.has_project_deletion(
                "杯子_20990101",
                request_id,
            )
        )
        self.assertTrue(
            ledger.has_project_deletion(
                "杯子_20990101",
                instance_commit="a" * 32,
            )
        )
        self.assertFalse(
            ledger.has_project_deletion(
                "杯子_20990101",
                "pd1." + ("b" * 64),
            )
        )
        serialized = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn(str(self.base), serialized)
        self.assertNotIn("token", serialized.lower())

    def test_service_audit_request_id_matches_preview_request_id(self) -> None:
        batch_id = "审计同号_20990101"
        self._write_batch(batch_id)
        ledger = WorkbenchEventLedger(
            self.state_root,
            clock_ms=lambda: 456_789,
        )
        service = ProjectDeletionService(
            self.repo,
            workspace_parent=self.workspace_parent,
            state_root=self.state_root,
            audit_ledger=ledger,
            recycle_executor=self.executor,
            lock_root=self.lock_root,
        )
        preview = service.preview([batch_id])

        result = service.execute(preview["requestId"], [batch_id])

        self.assertTrue(result["ok"])
        entry = json.loads(
            (self.state_root / "canvas_workbench.events.jsonl").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(preview["requestId"], entry["request_id"])
        self.assertEqual(
            {
                "event",
                "batch_id",
                "request_id",
                "source_entry",
                "recorded_at",
            },
            set(entry),
        )


if __name__ == "__main__":
    unittest.main()
