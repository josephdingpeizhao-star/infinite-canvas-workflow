from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import batch_recycle_cli  # noqa: E402
import batch_recycle_lock  # noqa: E402
import batch_recycle_service as recycle_service  # noqa: E402
import ic_client  # noqa: E402
from batch_recycle_lock import (  # noqa: E402
    BatchOperationBusy,
)
from batch_recycle_state import read_batch_lifecycle  # noqa: E402


class FakeCanvas:
    DEFAULT_STATE = object()

    def __init__(self) -> None:
        self.nodes = [
            {
                "id": "card",
                "type": "batch-info",
                "metadata": {
                    "batchIntake": {
                        "status": "completed",
                        "receipt": {"batchId": "cup"},
                    }
                },
            },
            {"id": "wfprod-output:cup:main_01"},
            {"id": "other", "type": "text"},
        ]
        self.ops: list[list[dict]] = []
        self.error: BaseException | None = None
        self.state_override: object = self.DEFAULT_STATE

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        if self.error is not None:
            raise self.error
        if self.state_override is not self.DEFAULT_STATE:
            return self.state_override
        return {"nodes": list(self.nodes)}

    def apply_ops(self, ops: list[dict]):
        if self.error is not None:
            raise self.error
        self.ops.append(ops)
        deleted = {
            node_id
            for op in ops
            if op.get("type") == "delete_node"
            for node_id in op.get("ids", [])
        }
        self.nodes = [node for node in self.nodes if node.get("id") not in deleted]
        return 1


class BatchRecycleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "production" / "cup"
        self.manifests = self.repo / "manifests"
        self.manifests.mkdir(parents=True)
        self.workspace.mkdir(parents=True)
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        (self.workspace / "asset.bin").write_bytes(b"asset")
        self.manifest = self.manifests / "cup.batch_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "batch_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                }
            ),
            encoding="utf-8",
        )
        self.journal = self.manifests / "cup.events.jsonl"
        self.lock_root = self.root / "locks"
        self.canvas = FakeCanvas()
        self.now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.request_number = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request_id(self) -> str:
        self.request_number += 1
        return f"request-{self.request_number}"

    def _service(self) -> recycle_service.BatchRecycleService:
        return recycle_service.BatchRecycleService(
            self.repo,
            client=self.canvas,
            lock_root=self.lock_root,
            clock=lambda: self.now,
            request_id_factory=self._request_id,
        )

    def _events(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
        ]

    def test_recycle_writes_schema_clears_canvas_then_renames_top_directory(self) -> None:
        result = self._service().recycle("cup")
        event = self._events()[-1]
        self.assertFalse(self.workspace.exists())
        self.assertTrue(result.workspace_target.is_dir())
        self.assertEqual("batch_recycled", event["event"])
        self.assertEqual("cli", event["source_entry"])
        self.assertEqual(str(self.workspace), event["workspace_source"])
        self.assertEqual(2, result.deleted_canvas_nodes)

    def test_restore_renames_first_then_appends_event_without_canvas_rebuild(self) -> None:
        service = self._service()
        service.recycle("cup")
        canvas_writes = len(self.canvas.ops)
        result = service.restore("cup")
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual("batch_restored", self._events()[-1]["event"])
        self.assertEqual("restored", result.status)
        self.assertEqual(canvas_writes, len(self.canvas.ops))

    def test_closed_batch_may_still_be_recycled(self) -> None:
        self.journal.write_text(
            json.dumps({"event": "batch_acceptance_closed"}) + "\n",
            encoding="utf-8",
        )
        self._service().recycle("cup")
        self.assertEqual("recycled", read_batch_lifecycle(self.journal).status)

    def test_canvas_unreachable_has_fixed_terminal_and_idempotent_resume(self) -> None:
        service = self._service()
        self.canvas.error = ic_client.CanvasAgentError("offline")
        with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
            service.recycle("cup")
        self.assertEqual(recycle_service.CANVAS_UNAVAILABLE_MESSAGE, str(ctx.exception))
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(["batch_recycled"], [item["event"] for item in self._events()])
        self.canvas.error = None
        self.canvas.state_override = {}
        with self.assertRaises(recycle_service.BatchRecycleError) as malformed_ctx:
            service.recycle("cup")
        self.assertEqual(
            recycle_service.CANVAS_UNAVAILABLE_MESSAGE,
            str(malformed_ctx.exception),
        )
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(["batch_recycled"], [item["event"] for item in self._events()])
        self.canvas.state_override = self.canvas.DEFAULT_STATE
        original_resolve = Path.resolve

        def reject_source_resolve(path: Path, *, strict: bool = False) -> Path:
            if path == self.workspace:
                raise AssertionError("event source link must be rejected before resolve")
            return original_resolve(path, strict=strict)

        with (
            mock.patch.object(
                recycle_service,
                "_is_junction",
                side_effect=lambda path: path == self.workspace,
            ),
            mock.patch.object(Path, "resolve", new=reject_source_resolve),
            mock.patch.object(recycle_service.os, "rename") as rename,
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as link_ctx:
                service.recycle("cup")
        self.assertEqual("recycle_event_invalid", link_ctx.exception.code)
        rename.assert_not_called()
        self.assertTrue((self.workspace / "asset.bin").is_file())
        result = service.recycle("cup")
        self.assertTrue(result.workspace_target.is_dir())
        self.assertEqual(1, len(self._events()))

    def test_permission_error_is_translated_and_source_remains(self) -> None:
        with mock.patch.object(
            recycle_service.os,
            "rename",
            side_effect=PermissionError(13, "locked"),
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                self._service().recycle("cup")
        self.assertIn("看图软件", str(ctx.exception))
        self.assertTrue(self.workspace.is_dir())

    def test_existing_destination_uses_native_rename_error_without_precheck(self) -> None:
        linked_workspace = self.root / "production" / "cup-link"
        manifest_data = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest_data["workspace"]["root"] = str(linked_workspace)
        self.manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        original_is_dir = Path.is_dir
        original_is_symlink = Path.is_symlink
        original_resolve = Path.resolve

        def linked_is_dir(path: Path) -> bool:
            return True if path == linked_workspace else original_is_dir(path)

        def linked_is_symlink(path: Path) -> bool:
            return True if path == linked_workspace else original_is_symlink(path)

        def reject_link_resolve(path: Path, *, strict: bool = False) -> Path:
            if path == linked_workspace:
                raise AssertionError("manifest workspace link must be checked before resolve")
            return original_resolve(path, strict=strict)

        with (
            mock.patch.object(Path, "is_dir", new=linked_is_dir),
            mock.patch.object(Path, "is_symlink", new=linked_is_symlink),
            mock.patch.object(Path, "resolve", new=reject_link_resolve),
            mock.patch.object(recycle_service.os, "rename") as linked_rename,
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as link_ctx:
                self._service().recycle("cup")
        self.assertEqual("workspace_marker_invalid", link_ctx.exception.code)
        linked_rename.assert_not_called()
        self.assertTrue((self.workspace / "asset.bin").is_file())
        manifest_data["workspace"]["root"] = str(self.workspace)
        self.manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

        target = (
            self.workspace.parent
            / "_回收站"
            / "cup__20260726T120000000000Z"
        )
        target.mkdir(parents=True)
        with mock.patch.object(
            recycle_service.os,
            "rename",
            side_effect=FileExistsError(17, "exists"),
        ) as rename:
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                self._service().recycle("cup")
        self.assertEqual(recycle_service.DESTINATION_EXISTS_MESSAGE, str(ctx.exception))
        rename.assert_called_once_with(self.workspace, target)

    def test_other_os_error_keeps_numeric_code_and_aborted_message(self) -> None:
        with mock.patch.object(
            recycle_service.os,
            "rename",
            side_effect=OSError(123, "failure"),
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                self._service().recycle("cup")
        self.assertIn("123", str(ctx.exception))
        self.assertIn("已中止，未做任何改动", str(ctx.exception))

    def test_completed_recycle_rerun_does_not_duplicate_event(self) -> None:
        service = self._service()
        first = service.recycle("cup")
        canvas_writes = len(self.canvas.ops)
        self.canvas.error = ic_client.CanvasAgentError("offline")
        second = service.recycle("cup")
        self.assertEqual(first.workspace_target, second.workspace_target)
        self.assertTrue(second.resumed)
        self.assertEqual(canvas_writes, len(self.canvas.ops))
        self.assertEqual(1, len(self._events()))
        held_for_manual_check = self.root / "held-for-manual-check"
        second.workspace_target.rename(held_for_manual_check)
        with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
            service.recycle("cup")
        self.assertEqual("workspace_missing", ctx.exception.code)
        self.assertIn("批次已冻结", str(ctx.exception))
        self.assertIn("交由顾问核对", str(ctx.exception))
        self.assertFalse(second.workspace_source.exists())
        self.assertFalse(second.workspace_target.exists())
        self.assertTrue(read_batch_lifecycle(self.journal).recycled)
        self.assertEqual(1, len(self._events()))

    def test_busy_lock_rejects_recycle_with_zero_event(self) -> None:
        with mock.patch.object(
            recycle_service.BatchOperationLock,
            "__enter__",
            side_effect=BatchOperationBusy("busy"),
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                self._service().recycle("cup")
        self.assertEqual("batch_busy", ctx.exception.code)
        self.assertFalse(self.journal.exists())

    def test_unavailable_lock_rejects_recycle_with_zero_event(self) -> None:
        service = self._service()
        access_denied = PermissionError(13, "access denied")
        access_denied.winerror = 5
        with mock.patch.object(
            batch_recycle_lock,
            "_lock_one_byte",
            side_effect=access_denied,
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                service.recycle("cup")
        self.assertEqual("lock_unavailable", ctx.exception.code)
        self.assertFalse(self.journal.exists())

        recycled = service.recycle("cup")
        before_restore = self.journal.read_bytes()
        with mock.patch.object(
            batch_recycle_lock,
            "_lock_one_byte",
            side_effect=access_denied,
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as ctx:
                service.restore("cup")
        self.assertEqual("lock_unavailable", ctx.exception.code)
        self.assertEqual(before_restore, self.journal.read_bytes())
        self.assertFalse(self.workspace.exists())
        self.assertTrue(recycled.workspace_target.is_dir())

    def test_restore_rename_failure_keeps_recycled_state_and_zero_restore_event(self) -> None:
        service = self._service()
        recycled = service.recycle("cup")
        before_link_rejection = self.journal.read_bytes()
        original_resolve = Path.resolve

        def reject_target_resolve(path: Path, *, strict: bool = False) -> Path:
            if path == recycled.workspace_target:
                raise AssertionError("event target link must be rejected before resolve")
            return original_resolve(path, strict=strict)

        with (
            mock.patch.object(
                recycle_service,
                "_is_junction",
                side_effect=lambda path: path == recycled.workspace_target,
            ),
            mock.patch.object(Path, "resolve", new=reject_target_resolve),
            mock.patch.object(recycle_service.os, "rename") as linked_rename,
        ):
            with self.assertRaises(recycle_service.BatchRecycleError) as link_ctx:
                service.restore("cup")
        self.assertEqual("recycle_event_invalid", link_ctx.exception.code)
        self.assertEqual(before_link_rejection, self.journal.read_bytes())
        linked_rename.assert_not_called()
        self.assertTrue((recycled.workspace_target / "asset.bin").is_file())

        with mock.patch.object(
            recycle_service.os,
            "rename",
            side_effect=PermissionError(13, "locked"),
        ):
            with self.assertRaises(recycle_service.BatchRecycleError):
                service.restore("cup")
        self.assertTrue(read_batch_lifecycle(self.journal).recycled)
        self.assertEqual(["batch_recycled"], [item["event"] for item in self._events()])

    def test_cli_exposes_recycle_and_cli_only_restore_contract(self) -> None:
        def factory(repository_root, *, lock_root=None):
            return recycle_service.BatchRecycleService(
                repository_root,
                client=self.canvas,
                lock_root=lock_root,
                clock=lambda: self.now,
                request_id_factory=self._request_id,
            )

        recycle_output = io.StringIO()
        cli_help = batch_recycle_cli._parser().format_help()
        self.assertNotIn("--lock-root", cli_help)
        self.assertNotIn("--repository-root", cli_help)
        with mock.patch.object(
            batch_recycle_lock,
            "_unlock_one_byte",
            side_effect=OSError(5, "unlock failed"),
        ):
            recycle_code = batch_recycle_cli.run_cli(
                [
                    "recycle",
                    "cup",
                ],
                output=recycle_output,
                service_factory=factory,
                repository_root=self.repo,
                lock_root=self.lock_root,
            )
        restore_output = io.StringIO()
        restore_code = batch_recycle_cli.run_cli(
            [
                "restore",
                "cup",
            ],
            output=restore_output,
            service_factory=factory,
            repository_root=self.repo,
            lock_root=self.lock_root,
        )
        self.assertEqual((0, 0), (recycle_code, restore_code))
        self.assertEqual("recycled", json.loads(recycle_output.getvalue())["status"])
        self.assertEqual("restored", json.loads(restore_output.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
