from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
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
from batch_identity import format_batch_id, parse_batch_stamp  # noqa: E402
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
    SourceImage,
)
from canvas_readonly_assistant import ReadonlyContextAssembler  # noqa: E402


FACTS = ConfirmedFacts(
    product_type="杯子",
    height_cm=12,
    main_image_count=6,
    detail_image_count=8,
    handheld_main=2,
    handheld_detail=1,
    forbid_pouring_and_heating=True,
    missing_d_no_retake=True,
)


class BatchIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.state_root = self.base / "state"
        self.test_root = self.base / "batches"
        self.upload_path = self.base / "source.png"
        self._make_repo_fixture()
        prepare_state_root(self.state_root)
        self.test_root.mkdir()
        (self.test_root / ".canvas_intake_test_root").write_text(
            "canvas-intake-test-root-v1\n",
            encoding="utf-8",
        )
        self.upload_path.write_bytes(b"bn01-original-png\x00\x01")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_repo_fixture(self) -> None:
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "manifests").mkdir()
        (self.repo / "canvas-bridge").mkdir()
        shutil.copy2(
            ROOT / "scripts" / "build_batch_manifest.py",
            self.repo / "scripts",
        )
        for name in ("category_recipes.py", "image_count_contract.py"):
            shutil.copy2(
                ROOT / "canvas-bridge" / name,
                self.repo / "canvas-bridge",
            )
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        for name in (
            "batch_manifest.template.json",
            "asset_manifest.template.json",
        ):
            shutil.copy2(
                ROOT / "manifests" / name,
                self.repo / "manifests",
            )

    def _request_and_upload(
        self,
        request_id: str,
    ) -> tuple[BatchIntakeRequest, UploadedFile]:
        payload = self.upload_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        source = SourceImage(
            node_id="image-1",
            storage_key="image:one",
            name="杯子正面.png",
            size=len(payload),
            mime_type="image/png",
            last_modified=1_722_112_000_000,
            expected_sha256=digest,
        )
        request = BatchIntakeRequest(
            request_id=request_id,
            requested_at=19_000,
            info_node_id="info-1",
            workflow_node_id="workflow-1",
            facts=FACTS,
            source_images=(source,),
        )
        upload = UploadedFile(
            source_node_id=source.node_id,
            path=self.upload_path,
            name=source.name,
            size=len(payload),
            mime_type=source.mime_type,
            sha256=digest,
        )
        return request, upload

    def _creator(self, now) -> BatchCreator:
        return BatchCreator(
            repo_root=self.repo,
            state_root=self.state_root,
            test_root=self.test_root,
            now=now,
        )

    def test_format_and_parse_support_new_and_historical_ids(self) -> None:
        moment = datetime(2026, 8, 1, 15, 45, 30)

        self.assertEqual("杯子_20260801_154530", format_batch_id("杯子", moment))
        cases = {
            "杯子_20260801_154530": ("20260801", "154530"),
            "杯子_20260722": ("20260722", ""),
            "餐具_水杯_20260801_000000": ("20260801", "000000"),
            "没有日期段": ("", ""),
            "杯子_20260801_badtime": ("", ""),
        }
        for batch_id, expected in cases.items():
            with self.subTest(batch_id=batch_id):
                self.assertEqual(expected, parse_batch_stamp(batch_id))

    def test_product_id_for_rejects_non_datetime_with_original_error(self) -> None:
        creator = self._creator(lambda: date(2026, 8, 1))
        request, _ = self._request_and_upload("bn01-invalid-moment")

        with self.assertRaises(BatchCreationError) as caught:
            creator.product_id_for(request)

        self.assertEqual("invalid_date", caught.exception.code)
        self.assertEqual("无法读取本机日期，已停止登记。", caught.exception.user_message)

    def test_same_day_different_moments_create_distinct_batches(self) -> None:
        moments = iter(
            (
                datetime(2026, 8, 1, 9, 0, 0),
                datetime(2026, 8, 1, 18, 30, 45),
            )
        )
        creator = self._creator(lambda: next(moments))
        first_request, first_upload = self._request_and_upload("bn01-morning")
        second_request, second_upload = self._request_and_upload("bn01-evening")

        first = creator.create(first_request, (first_upload,))
        second = creator.create(second_request, (second_upload,))

        self.assertEqual("杯子_20260801_090000", first.product_id)
        self.assertEqual("杯子_20260801_183045", second.product_id)
        self.assertNotEqual(first.workspace_root, second.workspace_root)
        self.assertTrue(first.workspace_root.is_dir())
        self.assertTrue(second.workspace_root.is_dir())
        self.assertTrue(first.manifest_path.is_file())
        self.assertTrue(second.manifest_path.is_file())

    def test_same_moment_second_create_keeps_batch_exists_guard(self) -> None:
        creator = self._creator(lambda: datetime(2026, 8, 1, 15, 45, 30))
        first_request, first_upload = self._request_and_upload("bn01-same-second-1")
        second_request, second_upload = self._request_and_upload("bn01-same-second-2")
        first = creator.create(first_request, (first_upload,))
        manifest_before = first.manifest_path.read_bytes()

        with self.assertRaises(BatchCreationError) as caught:
            creator.create(second_request, (second_upload,))

        self.assertEqual("batch_exists", caught.exception.code)
        self.assertEqual("这个批次已经存在，未覆盖任何文件。", caught.exception.user_message)
        self.assertEqual(manifest_before, first.manifest_path.read_bytes())

    def test_readonly_catalog_sorts_and_selects_historical_and_new_ids(self) -> None:
        batch_ids = (
            "杯子_20260729",
            "杯子_20260731",
            "杯子_20260731_000000",
            "餐具_水杯_20260731_154530",
            "杯子_20260801_010203",
        )
        for batch_id in reversed(batch_ids):
            path = self.repo / "manifests" / f"{batch_id}.batch_manifest.json"
            path.write_text(
                json.dumps({"product_id": batch_id}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        assembler = ReadonlyContextAssembler(self.repo)

        catalog = assembler._manifest_catalog()

        self.assertEqual(list(batch_ids), [item["batch_id"] for item in catalog])
        self.assertEqual(
            "杯子_20260729",
            assembler._select_batch("请看 7 月 29 日的批次", catalog)["batch_id"],
        )
        self.assertEqual(
            "餐具_水杯_20260731_154530",
            assembler._select_batch("请看 2026 年 7 月 31 日", catalog)["batch_id"],
        )
        self.assertEqual(
            "杯子_20260801_010203",
            assembler._select_batch("请看 8 月 1 日的批次", catalog)["batch_id"],
        )


if __name__ == "__main__":
    unittest.main()
