from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_recycle_lock import BatchOperationBusy  # noqa: E402
import workflow_style_reference_intake as style_intake  # noqa: E402
import workflow_style_reference_removal as style_removal  # noqa: E402


def _jpeg(label: bytes) -> bytes:
    return b"\xff\xd8\xff" + label


class StyleReferenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.workspace = self.root / "workspace"
        self.repository.mkdir()
        (self.repository / "manifests").mkdir()
        (self.workspace / "inputs" / "white_bg").mkdir(parents=True)
        (self.workspace / "inputs" / "style_refs").mkdir()
        (self.workspace / "manifests").mkdir()
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        original = self.workspace / "inputs" / "white_bg" / "original.jpg"
        original.write_bytes(_jpeg(b"product"))
        asset_manifest = self.workspace / "manifests" / "asset_manifest.json"
        asset_manifest.write_text(
            json.dumps(
                {
                    "assets": [
                        {
                            "asset_id": "white_bg_001",
                            "file_path": "inputs/white_bg/original.jpg",
                            "asset_role": "white_bg",
                            "is_single_product_white_bg": True,
                            "is_set_group_shot": False,
                            "is_style_reference": False,
                            "bound_angle_slot": "",
                            "component_id": "",
                            "notes": "",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.manifest = (
            self.repository / "manifests" / "cup.batch_manifest.json"
        )
        self.manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "workspace": {"root": str(self.workspace)},
                    "inputs": {
                        "style_reference_images": [
                            str(self.workspace / "inputs" / "style_refs")
                        ]
                    },
                    "artifacts": {"asset_manifest": str(asset_manifest)},
                    "outputs": {
                        "renders": [str(self.workspace / "outputs" / "renders")],
                        "repaired": [str(self.workspace / "outputs" / "repaired")],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.style_root = self.workspace / "inputs" / "style_refs"
        self.lock_root = self.root / "locks"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def upload(self, name: str, data: bytes) -> style_intake.StyleReferenceUpload:
        return style_intake.StyleReferenceUpload(
            node_id=f"node-{name}",
            name=name,
            mime_type="image/jpeg",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    @staticmethod
    def empty_route(_manifest_path: Path) -> dict:
        return {
            "outputs": {
                "renders": {"file_count": 0},
                "repaired": {"file_count": 0},
            }
        }

    @staticmethod
    def open_lifecycle(_journal_path: Path) -> SimpleNamespace:
        return SimpleNamespace(recycled=False, closed=False)


class StyleReferenceSingleFileRuleTest(StyleReferenceFixture):
    def test_zero_one_and_two_uploads_follow_exactly_one_rule(self) -> None:
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "先连接 1 张",
        ):
            style_intake.publish_style_references(
                self.manifest,
                "zero",
                (),
                batch_lock_root=self.lock_root,
            )

        one = self.upload("one.jpg", _jpeg(b"one"))
        result = style_intake.publish_style_references(
            self.manifest,
            "one",
            (one,),
            batch_lock_root=self.lock_root,
        )
        self.assertEqual(1, result.file_count)

        self.style_root.joinpath("one.jpg").unlink()
        two = self.upload("two.jpg", _jpeg(b"two"))
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "多张风格会互相冲突",
        ):
            style_intake.publish_style_references(
                self.manifest,
                "two",
                (one, two),
                batch_lock_root=self.lock_root,
            )

    def test_same_name_and_same_hash_is_idempotent(self) -> None:
        data = _jpeg(b"same")
        target = self.style_root / "same.jpg"
        target.write_bytes(data)
        before = target.stat().st_mtime_ns

        result = style_intake.publish_style_references(
            self.manifest,
            "same-hash",
            (self.upload("same.jpg", data),),
            batch_lock_root=self.lock_root,
        )

        self.assertEqual(("same.jpg",), result.files)
        self.assertEqual(data, target.read_bytes())
        self.assertEqual(before, target.stat().st_mtime_ns)
        self.assertTrue(Path(result.receipt_path).is_file())

    def test_existing_same_name_different_hash_is_rejected(self) -> None:
        target = self.style_root / "same.jpg"
        target.write_bytes(_jpeg(b"old"))
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "同名但不同内容.*先移除再补登",
        ):
            style_intake.publish_style_references(
                self.manifest,
                "different-hash",
                (self.upload("same.jpg", _jpeg(b"new")),),
                batch_lock_root=self.lock_root,
            )
        self.assertEqual(_jpeg(b"old"), target.read_bytes())

    def test_existing_different_name_is_rejected(self) -> None:
        (self.style_root / "old.jpg").write_bytes(_jpeg(b"old"))
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "本批已有风格参考图.*先移除再补登",
        ):
            style_intake.publish_style_references(
                self.manifest,
                "different-name",
                (self.upload("new.jpg", _jpeg(b"new")),),
                batch_lock_root=self.lock_root,
            )

    def test_historical_multiple_files_are_rejected(self) -> None:
        (self.style_root / "a.jpg").write_bytes(_jpeg(b"a"))
        (self.style_root / "b.jpg").write_bytes(_jpeg(b"b"))
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "已有多张.*先移除",
        ):
            style_intake.publish_style_references(
                self.manifest,
                "historical-multiple",
                (self.upload("c.jpg", _jpeg(b"c")),),
                batch_lock_root=self.lock_root,
            )


class StyleReferenceRemovalGateTest(StyleReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.target = self.style_root / "style.jpg"
        self.target.write_bytes(_jpeg(b"style"))
        self.calls: list[Path] = []

    def fake_recycle(self, path: Path) -> None:
        self.calls.append(path)
        path.unlink()

    def remove(self, **overrides):
        arguments = {
            "batch_lock_root": self.lock_root,
            "recycle_executor": self.fake_recycle,
            "route_reader": self.empty_route,
            "lifecycle_reader": self.open_lifecycle,
        }
        arguments.update(overrides)
        return style_removal.remove_style_references(
            self.manifest,
            "remove-001",
            **arguments,
        )

    def test_invalid_request_id_fails_before_any_side_effect(self) -> None:
        with self.assertRaisesRegex(
            style_removal.StyleReferenceRemovalError,
            "请求编号无效",
        ):
            style_removal.remove_style_references(
                self.manifest,
                "../bad",
                batch_lock_root=self.lock_root,
                recycle_executor=self.fake_recycle,
                route_reader=self.empty_route,
                lifecycle_reader=self.open_lifecycle,
            )
        self.assertEqual([], self.calls)
        self.assertTrue(self.target.is_file())

    def test_busy_lock_fails_closed_without_recycle_call(self) -> None:
        with mock.patch.object(
            style_removal,
            "BatchOperationLock",
            side_effect=BatchOperationBusy("busy"),
        ):
            with self.assertRaisesRegex(
                style_removal.StyleReferenceRemovalError,
                "有操作正在进行",
            ):
                self.remove()
        self.assertEqual([], self.calls)
        self.assertTrue(self.target.is_file())

    def test_renders_or_repaired_files_block_removal(self) -> None:
        for output_name in ("renders", "repaired"):
            with self.subTest(output_name=output_name):
                route = self.empty_route(self.manifest)
                route["outputs"][output_name]["file_count"] = 1
                with self.assertRaisesRegex(
                    style_removal.StyleReferenceRemovalError,
                    "本批已出图.*来源断链",
                ):
                    self.remove(route_reader=lambda _path, route=route: route)
                self.assertEqual([], self.calls)
                self.assertTrue(self.target.is_file())

    def test_closed_batch_blocks_removal(self) -> None:
        with self.assertRaisesRegex(
            style_removal.StyleReferenceRemovalError,
            "本批已关账",
        ):
            self.remove(
                lifecycle_reader=lambda _path: SimpleNamespace(
                    recycled=False,
                    closed=True,
                )
            )
        self.assertEqual([], self.calls)

    def test_empty_directory_blocks_removal(self) -> None:
        self.target.unlink()
        with self.assertRaisesRegex(
            style_removal.StyleReferenceRemovalError,
            "没有可移除的风格参考图",
        ):
            self.remove()
        self.assertEqual([], self.calls)

    def test_existing_receipt_is_never_rewritten(self) -> None:
        receipt = (
            self.workspace
            / "manifests"
            / "style_reference_removal_receipt.remove-001.json"
        )
        receipt.write_text('{"fixed":true}\n', encoding="utf-8")
        before = receipt.read_bytes()
        with self.assertRaisesRegex(
            style_intake.StyleReferenceIntakeError,
            "已有回执.*不会重写",
        ):
            self.remove()
        self.assertEqual(before, receipt.read_bytes())
        self.assertEqual([], self.calls)
        self.assertTrue(self.target.is_file())


class StyleReferenceRemovalExecutionTest(StyleReferenceFixture):
    def test_partial_executor_failure_stops_in_order_and_writes_truthful_receipt(self) -> None:
        first = self.style_root / "a.jpg"
        second = self.style_root / "b.jpg"
        third = self.style_root / "c.jpg"
        for path in (first, second, third):
            path.write_bytes(_jpeg(path.stem.encode("ascii")))
        calls: list[str] = []
        events: list[dict] = []

        def executor(path: Path) -> None:
            calls.append(path.name)
            if path.name == "b.jpg":
                raise style_removal.RecycleBinError("模拟回收站失败")
            path.unlink()

        def append_event(path: Path, event: str, **fields):
            events.append({"path": path, "event": event, **fields})
            return events[-1]

        with self.assertRaisesRegex(
            style_removal.StyleReferenceRemovalError,
            "已移除 1/3 张",
        ):
            style_removal.remove_style_references(
                self.manifest,
                "partial",
                batch_lock_root=self.lock_root,
                recycle_executor=executor,
                route_reader=self.empty_route,
                lifecycle_reader=self.open_lifecycle,
                event_appender=append_event,
            )

        self.assertEqual(["a.jpg", "b.jpg"], calls)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertEqual([], events)
        receipt = json.loads(
            (
                self.workspace
                / "manifests"
                / "style_reference_removal_receipt.partial.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("failed", receipt["status"])
        self.assertEqual(1, receipt["removed_count"])
        self.assertEqual(
            [True, False, False],
            [item["removed"] for item in receipt["files"]],
        )

    def test_success_hashes_first_recycles_in_order_and_appends_one_event(self) -> None:
        old_receipt = (
            self.workspace
            / "manifests"
            / "style_reference_intake_receipt.old.json"
        )
        old_receipt.write_text('{"fixed":true}\n', encoding="utf-8")
        old_receipt_bytes = old_receipt.read_bytes()
        for name in ("b.jpg", "a.jpg"):
            (self.style_root / name).write_bytes(_jpeg(name.encode("ascii")))
        expected = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.style_root.iterdir()
        }
        calls: list[str] = []

        def executor(path: Path) -> None:
            calls.append(path.name)
            path.unlink()

        result = style_removal.remove_style_references(
            self.manifest,
            "success",
            batch_lock_root=self.lock_root,
            recycle_executor=executor,
            route_reader=self.empty_route,
            lifecycle_reader=self.open_lifecycle,
        )

        self.assertEqual(["a.jpg", "b.jpg"], calls)
        self.assertEqual(2, result.file_count)
        receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
        self.assertEqual("completed", receipt["status"])
        self.assertEqual(2, receipt["removed_count"])
        self.assertEqual(
            expected,
            {item["name"]: item["sha256"] for item in receipt["files"]},
        )
        journal = self.repository / "manifests" / "cup.events.jsonl"
        events = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(events))
        self.assertEqual("style_reference_removed", events[0]["event"])
        self.assertEqual("success", events[0]["request_id"])
        self.assertEqual(
            expected,
            {item["name"]: item["sha256"] for item in events[0]["files"]},
        )
        self.assertEqual(old_receipt_bytes, old_receipt.read_bytes())


class FakeCanvasClient:
    def __init__(self, card: dict) -> None:
        self.card = card
        self.state = {"nodes": [card], "connections": []}
        self.ops: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]) -> None:
        self.ops.append(ops)


class FakeRemovalHandler:
    def __init__(self) -> None:
        self.processed = 0
        self.rejections: list[str] = []

    @staticmethod
    def is_queued(node: dict) -> bool:
        return (
            node["metadata"]["styleReferenceRemoval"]["status"]
            == "queued"
        )

    def reject(self, _node: dict, message: str) -> None:
        self.rejections.append(message)

    def process_node(self, _node: dict) -> None:
        self.processed += 1


class SharedWorkerSerialDispatchTest(StyleReferenceFixture):
    def card(self, *, intake_status: str, removal_status: str) -> dict:
        return {
            "id": "card",
            "type": "batch-info",
            "metadata": {
                "batchIntake": {
                    "status": "completed",
                    "receipt": {"batchId": "cup"},
                },
                "styleReferenceIntake": {
                    "status": intake_status,
                    "requestId": "intake-request",
                    "requestedAt": 1_000,
                    "batchId": "cup",
                    "sources": [],
                },
                "styleReferenceRemoval": {
                    "status": removal_status,
                    "requestId": "remove-request",
                    "requestedAt": 1_000,
                    "batchId": "cup",
                },
            },
        }

    def test_same_card_never_dispatches_intake_and_removal_together(self) -> None:
        card = self.card(intake_status="queued", removal_status="queued")
        client = FakeCanvasClient(card)
        removal = FakeRemovalHandler()
        service = style_intake.WorkflowStyleReferenceService(
            self.repository,
            client=client,
            clock_ms=lambda: 1_000,
            batch_lock_root=self.lock_root,
            removal_handler=removal,
        )

        service.poll_once()

        self.assertEqual(0, removal.processed)
        self.assertEqual(1, len(removal.rejections))
        self.assertEqual({}, service.sessions)
        self.assertEqual(
            "failed",
            client.ops[0][0]["metadata"]["styleReferenceIntake"]["status"],
        )

    def test_removal_uses_existing_worker_poll_when_intake_is_idle(self) -> None:
        card = self.card(intake_status="idle", removal_status="queued")
        client = FakeCanvasClient(card)
        removal = FakeRemovalHandler()
        service = style_intake.WorkflowStyleReferenceService(
            self.repository,
            client=client,
            clock_ms=lambda: 1_000,
            batch_lock_root=self.lock_root,
            removal_handler=removal,
        )

        service.poll_once()

        self.assertEqual(1, removal.processed)
        self.assertEqual([], removal.rejections)
        self.assertEqual({}, service.sessions)


class RemovalCardAcknowledgementTest(StyleReferenceFixture):
    def removal_card(self, requested_at: int = 1_000) -> dict:
        return {
            "id": "card",
            "type": "batch-info",
            "metadata": {
                "batchIntake": {
                    "status": "completed",
                    "receipt": {"batchId": "cup"},
                },
                "styleReferenceRemoval": {
                    "status": "queued",
                    "requestId": "remove-card",
                    "requestedAt": requested_at,
                    "batchId": "cup",
                },
            },
        }

    def test_success_ack_is_written_only_to_independent_removal_state(self) -> None:
        card = self.removal_card()
        client = FakeCanvasClient(card)
        handler = style_removal.WorkflowStyleReferenceRemovalHandler(
            self.repository,
            client=client,
            clock_ms=lambda: 1_000,
            batch_lock_root=self.lock_root,
            recycle_executor=lambda _path: None,
        )
        result = style_removal.StyleReferenceRemovalResult(
            batch_id="cup",
            file_count=1,
            receipt_path=str(self.workspace / "manifests" / "receipt.json"),
            files=("style.jpg",),
        )

        with mock.patch.object(
            style_removal,
            "remove_style_references",
            return_value=result,
        ):
            handler.process_node(card)

        metadata = client.ops[-1][0]["metadata"]
        self.assertEqual({"styleReferenceRemoval"}, set(metadata))
        self.assertEqual("completed", metadata["styleReferenceRemoval"]["status"])
        self.assertEqual(
            ["style.jpg"],
            metadata["styleReferenceRemoval"]["receipt"]["files"],
        )

    def test_stale_command_fails_on_card_without_calling_removal(self) -> None:
        card = self.removal_card(requested_at=1_000)
        client = FakeCanvasClient(card)
        handler = style_removal.WorkflowStyleReferenceRemovalHandler(
            self.repository,
            client=client,
            clock_ms=lambda: 20_000,
            batch_lock_root=self.lock_root,
            recycle_executor=lambda _path: None,
        )

        with mock.patch.object(
            style_removal,
            "remove_style_references",
        ) as remove:
            handler.process_node(card)

        remove.assert_not_called()
        removal = client.ops[-1][0]["metadata"]["styleReferenceRemoval"]
        self.assertEqual("failed", removal["status"])
        self.assertIn("请求已过期", removal["errorMessage"])

    def test_business_rejection_keeps_existing_worker_running(self) -> None:
        card = self.removal_card()
        client = FakeCanvasClient(card)
        handler = style_removal.WorkflowStyleReferenceRemovalHandler(
            self.repository,
            client=client,
            clock_ms=lambda: 1_000,
            batch_lock_root=self.lock_root,
            recycle_executor=lambda _path: None,
        )
        statuses: list[str] = []
        service = style_intake.WorkflowStyleReferenceService(
            self.repository,
            client=client,
            clock_ms=lambda: 1_000,
            batch_lock_root=self.lock_root,
            removal_handler=handler,
            sleep=lambda _seconds: setattr(service, "stopping", True),
        )
        service.set_status_callback(statuses.append)

        with mock.patch.object(
            style_removal,
            "remove_style_references",
            side_effect=style_removal.StyleReferenceRemovalError(
                "没有可移除的风格参考图"
            ),
        ):
            service.serve_forever()

        self.assertEqual(["running"], statuses)
        removal = client.ops[-1][0]["metadata"]["styleReferenceRemoval"]
        self.assertEqual("failed", removal["status"])
        self.assertIn("没有可移除", removal["errorMessage"])


if __name__ == "__main__":
    unittest.main()
