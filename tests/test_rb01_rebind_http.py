from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_recycle_lock import BatchOperationLock  # noqa: E402
from white_bg_recovery import WhiteBgRecoveryError  # noqa: E402
from workflow_demo_executor import write_placeholder_png  # noqa: E402
from workflow_production_http_server import WorkflowProductionHttpServer  # noqa: E402
from tests.test_rb01_white_bg_recovery import RecoveryFixture  # noqa: E402


class RebindRecomputeHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = RecoveryFixture(Path(self.temp.name))
        self.repo = self.fixture.repository_root
        self.workspace = self.fixture.workspace
        self.lock_root = Path(self.temp.name) / "locks"
        (self.repo / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        (self.workspace / "outputs" / "renders").mkdir(parents=True)
        (self.workspace / "outputs" / "repaired").mkdir(parents=True)
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        (self.workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        self.fixture.manifest["user_confirmed_facts"] = {
            "main_image_count": 1,
            "detail_image_count": 1,
        }
        self.manifest_path = self.repo / "manifests" / "cup.batch_manifest.json"
        self._write_manifest()
        self.event_path = self.repo / "manifests" / "cup.events.jsonl"
        self.server = WorkflowProductionHttpServer(
            repository_root=self.repo,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
            batch_lock_root=self.lock_root,
        )
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.fixture.manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def _post(
        self,
        batch_id: str = "cup",
        *,
        token: str | None = "canvas-token",
        body: bytes = b"{}",
        origin: str | None = "http://localhost:3000",
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, object]]:
        encoded = urllib.parse.quote(batch_id, safe="")
        request = urllib.request.Request(
            f"{self.base}/workflow-production/{encoded}/rebind-recompute",
            data=body,
            method="POST",
        )
        if token is not None:
            request.add_header("x-canvas-agent-token", token)
        if origin is not None:
            request.add_header("Origin", origin)
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _assert_no_side_effect(self) -> None:
        self.assertFalse(self.event_path.exists())
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(self.fixture.angle.is_dir())
        self.assertTrue(self.fixture.repo_integrity_json.is_file())

    def test_authentication_and_unknown_batch_are_rejected_without_side_effects(self) -> None:
        status, body = self._post(token=None)
        self.assertEqual(401, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self._assert_no_side_effect()

        status, body = self._post("unknown")
        self.assertEqual(404, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self._assert_no_side_effect()

    def test_busy_batch_is_rejected_with_readable_reason_and_no_ledger(self) -> None:
        with BatchOperationLock("cup", lock_root=self.lock_root):
            status, body = self._post()
        self.assertEqual(409, status)
        self.assertEqual("batch_busy", body["error"])
        self.assertIn("正在运行", body["message"])
        self._assert_no_side_effect()

    def test_restored_files_and_unavailable_inputs_are_terminal_rejections(self) -> None:
        (self.fixture.white_bg / self.fixture.missing_name).write_bytes(b"restored")
        status, restored = self._post()
        self.assertEqual(409, status)
        self.assertEqual("missing_files_restored", restored["error"])
        self.assertEqual("白底图已齐全，直接重新开始即可。", restored["message"])
        self._assert_no_side_effect()

        (self.fixture.white_bg / self.fixture.missing_name).unlink()
        self.fixture.remaining.unlink()
        status, unavailable = self._post()
        self.assertEqual(409, status)
        self.assertEqual("inputs_unavailable", unavailable["error"])
        self.assertEqual(
            "白底图目录整体无法访问，本次已停止。请恢复 inputs/white_bg 后再重新开始。",
            unavailable["message"],
        )
        self._assert_no_side_effect()

    def test_existing_render_is_rejected_using_quote_ready_count(self) -> None:
        render = self.workspace / "outputs" / "renders" / "main_01.png"
        write_placeholder_png(render, width=1254, height=1254, kind="main", ordinal=1)
        status, body = self._post()
        self.assertEqual(409, status)
        self.assertEqual("render_outputs_exist", body["error"])
        self.assertEqual(
            "本批已有 1 张成图，不能整体重排。请恢复缺失文件后重新开始。",
            body["message"],
        )
        self._assert_no_side_effect()

    def test_success_archives_then_appends_exact_safe_event(self) -> None:
        status, body = self._post()
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("cup", body["batchId"])
        self.assertEqual([self.fixture.missing_name], body["missing"])
        self.assertEqual(1, body["remainingCount"])
        self.assertTrue(body["supersededDir"].startswith("artifacts/_superseded/"))

        event_lines = self.event_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(event_lines))
        event = json.loads(event_lines[0])
        self.assertEqual("white_bg_rebind_recompute", event["event"])
        self.assertEqual([self.fixture.missing_name], event["missing"])
        self.assertEqual(1, event["remaining_count"])
        self.assertEqual(body["superseded"], event["superseded"])
        self.assertEqual(body["supersededDir"], event["superseded_dir"])
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.repo_integrity_json.exists())
        self.assertTrue(self.fixture.repo_integrity_md.is_file())
        self.assertTrue(self.fixture.identity.is_dir())
        self.assertTrue(self.fixture.style.is_dir())

    def test_journal_oserror_restores_every_archive_and_returns_readable_503(self) -> None:
        with mock.patch(
            "workflow_production_http_server.run_controller.append_event",
            side_effect=OSError("simulated journal failure"),
        ):
            status, body = self._post()

        self.assertEqual(503, status)
        self.assertEqual("recompute_journal_failed", body["error"])
        self.assertEqual(
            "本次重排记录写入失败，原有派生产物已恢复。请稍后重试。",
            body["message"],
        )
        self.assertNotIn(str(self.workspace), json.dumps(body, ensure_ascii=False))
        self.assertFalse(self.event_path.exists())
        for source in (
            self.fixture.angle,
            self.fixture.variables,
            self.fixture.final,
            self.fixture.jobs,
            self.fixture.qc,
            self.fixture.repo_integrity_json,
        ):
            self.assertTrue(source.exists(), source)
        for superseded_root in (
            self.fixture.artifacts_root / "_superseded",
            self.repo / "reports" / "_superseded",
        ):
            if superseded_root.exists():
                self.assertEqual([], list(superseded_root.iterdir()))

    def test_journal_oserror_reports_distinct_failure_when_archive_rollback_fails(self) -> None:
        with (
            mock.patch(
                "workflow_production_http_server.run_controller.append_event",
                side_effect=OSError("simulated journal failure"),
            ),
            mock.patch(
                "workflow_production_http_server.rollback_recompute_archive",
                side_effect=WhiteBgRecoveryError("simulated rollback failure"),
            ),
        ):
            status, body = self._post()

        self.assertEqual(500, status)
        self.assertEqual("recompute_journal_rollback_failed", body["error"])
        self.assertEqual(
            "重排记录写入失败，且无法自动恢复派生产物。请停止操作并检查本批。",
            body["message"],
        )
        self.assertNotIn(str(self.workspace), json.dumps(body, ensure_ascii=False))
        self.assertFalse(self.fixture.final.exists())
        self.assertFalse(self.fixture.repo_integrity_json.exists())

    def test_journal_symlink_shape_is_rejected_before_archive(self) -> None:
        real_is_symlink = Path.is_symlink

        def selective_is_symlink(path: Path) -> bool:
            return path == self.event_path or real_is_symlink(path)

        with mock.patch.object(Path, "is_symlink", new=selective_is_symlink):
            status, body = self._post()

        self.assertEqual(503, status)
        self.assertEqual("recompute_journal_unavailable", body["error"])
        self.assertFalse(self.event_path.exists())
        self.assertTrue(self.fixture.final.is_dir())
        self.assertTrue(self.fixture.repo_integrity_json.is_file())
        self.assertFalse((self.fixture.artifacts_root / "_superseded").exists())

    def test_route_and_json_body_are_closed(self) -> None:
        status, body = self._post(body=b'{"archive":"elsewhere"}')
        self.assertEqual(400, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self._assert_no_side_effect()

        status, body = self._post(content_type="text/plain")
        self.assertEqual(415, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self._assert_no_side_effect()


if __name__ == "__main__":
    unittest.main()
