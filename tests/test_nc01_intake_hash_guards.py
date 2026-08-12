from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import batch_intake_controller as batch_controller  # noqa: E402
import workflow_style_reference_intake as style_intake  # noqa: E402
from batch_creator import (  # noqa: E402
    BatchCreator,
    UploadedFile,
    prepare_state_root,
)
from workflow_batch_intake_service import WorkflowBatchIntakeService  # noqa: E402


DUPLICATE_PRODUCT_IMAGE_MESSAGE = (
    "同一张图被重复加入本次产品原图登记，不能建批。"
    "请删除重复项，只保留一张；产品原图连工作流机器，风格参考图连信息卡。"
)
CROSS_ROLE_IMAGE_MESSAGE = (
    "这张图已经是本批的产品原图，不能再登记为风格参考。"
    "若是接反了：产品原图连工作流机器，风格参考图连信息卡。"
)
UNSAFE_PRODUCT_EVIDENCE_MESSAGE = (
    "无法安全核对本批已登记的产品原图，风格补登已停止。"
    "请保留现场并交由顾问核对，系统不会自动重试。"
)
REQUEST_ID = "request-001"
NOW_MS = 2_000
PRODUCT_A = b"\xff\xd8\xffproduct-a"
PRODUCT_B = b"\xff\xd8\xffproduct-b"
STYLE = b"\xff\xd8\xffstyle-reference"


def _facts() -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": None,
        "width_cm": None,
        "height_cm": 8,
        "main_image_count": 6,
        "detail_image_count": 8,
        "handheld_main": 2,
        "handheld_detail": 1,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


def _image(node_id: str, name: str, sha256: str) -> dict[str, object]:
    return {
        "id": node_id,
        "type": "image",
        "title": name,
        "metadata": {
            "storageKey": f"image:{node_id}",
            "sourceFile": {
                "name": name,
                "size": 100,
                "type": "image/jpeg",
                "lastModified": 1_000,
                "sha256": sha256,
            },
        },
    }


def _duplicate_hash_canvas_state() -> tuple[dict[str, object], dict[str, object]]:
    sha256 = hashlib.sha256(PRODUCT_A).hexdigest()
    card = {
        "id": "card",
        "type": "batch-info",
        "metadata": {
            "content": (
                "# batch-intake\n"
                f"# request-id: {REQUEST_ID}\n"
                "# requested-at: 1000\n"
                "build: batch"
            ),
            "batchIntake": {
                "status": "queued",
                "requestId": REQUEST_ID,
                "requestedAt": 1_000,
                "category": "杯类",
                "contractHash": batch_controller.batch_intake_contract_sha256(ROOT),
                "batch_type": "single",
                "facts": _facts(),
            },
        },
    }
    workflow = {"id": "machine", "type": "workflow", "metadata": {}}
    first = _image("first", "正面.jpg", sha256)
    second = _image("second", "背面.jpg", sha256)
    state = {
        "nodes": [card, workflow, first, second],
        "connections": [
            {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
            {"id": "first-machine", "fromNodeId": "first", "toNodeId": "machine"},
            {"id": "second-machine", "fromNodeId": "second", "toNodeId": "machine"},
        ],
    }
    return state, card


class _CanvasClient:
    def __init__(self, state: dict[str, object]):
        self.state = state
        self.ops: list[list[dict[str, object]]] = []

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> None:
        self.ops.append(ops)


class Nc01BatchHashGateTests(unittest.TestCase):
    def test_shared_human_copy_is_exact_and_duplicate_hash_is_rejected(self) -> None:
        self.assertEqual(
            DUPLICATE_PRODUCT_IMAGE_MESSAGE,
            batch_controller.DUPLICATE_PRODUCT_IMAGE_MESSAGE,
        )
        state, card = _duplicate_hash_canvas_state()
        with self.assertRaises(batch_controller.BatchIntakeGateError) as caught:
            batch_controller.parse_queued_request(state, card, now_ms=NOW_MS)
        self.assertEqual("duplicate_image", caught.exception.code)
        self.assertEqual(DUPLICATE_PRODUCT_IMAGE_MESSAGE, str(caught.exception))

    def test_duplicate_hash_gate_runs_before_spool_or_creator(self) -> None:
        state, _card = _duplicate_hash_canvas_state()
        client = _CanvasClient(state)
        with tempfile.TemporaryDirectory() as raw:
            state_root = prepare_state_root(Path(raw) / "state")
            creator = mock.Mock()
            service = WorkflowBatchIntakeService(
                ROOT,
                state_root,
                client=client,
                creator=creator,
                clock_ms=lambda: NOW_MS,
                upload_port=0,
            )
            with mock.patch.object(service, "_begin_spool") as begin_spool:
                service.poll_once()
            begin_spool.assert_not_called()
            creator.product_id_for.assert_not_called()
            creator.create.assert_not_called()
            self.assertEqual({}, service.sessions)
            self.assertEqual(DUPLICATE_PRODUCT_IMAGE_MESSAGE, client.ops[-1][0]["metadata"]["batchIntake"]["errorMessage"])


class Nc01StyleEvidenceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        self.repo_manifests = self.repo / "manifests"
        self.workspace_manifests = self.workspace / "manifests"
        self.white_bg = self.workspace / "inputs" / "white_bg"
        self.style_root = self.workspace / "inputs" / "style_refs"
        self.repo_manifests.mkdir(parents=True)
        self.workspace_manifests.mkdir(parents=True)
        self.white_bg.mkdir(parents=True)
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}) + "\n",
            encoding="utf-8",
        )
        self.manifest_path = self.repo_manifests / "cup.batch_manifest.json"
        self.asset_manifest = self.workspace_manifests / "asset_manifest.json"
        self.product_path = self.white_bg / "product.jpg"
        self.product_path.write_bytes(PRODUCT_A)
        self._write_manifest()
        self._write_asset_manifest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_manifest(self) -> None:
        manifest = {
            "product_id": "cup",
            "workspace": {"root": str(self.workspace)},
            "inputs": {"style_reference_images": [str(self.style_root)]},
            "artifacts": {"asset_manifest": str(self.asset_manifest)},
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_asset_manifest(self, *, role: object = "white_bg") -> None:
        value = {
            "assets": [
                {
                    "asset_id": "white_bg_001",
                    "file_path": "inputs/white_bg/product.jpg",
                    "asset_role": role,
                    "is_single_product_white_bg": True,
                    "is_set_group_shot": False,
                    "is_style_reference": False,
                    "bound_angle_slot": "",
                    "component_id": "",
                    "notes": "",
                }
            ]
        }
        self.asset_manifest.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _upload(data: bytes = STYLE) -> style_intake.StyleReferenceUpload:
        return style_intake.StyleReferenceUpload(
            node_id="style-node",
            name="style.jpg",
            mime_type="image/jpeg",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    def _assert_unsafe_without_writes(self) -> None:
        with self.assertRaises(style_intake.StyleReferenceIntakeError) as caught:
            style_intake.publish_style_references(
                self.manifest_path,
                "style-request-001",
                (self._upload(),),
            )
        self.assertEqual(UNSAFE_PRODUCT_EVIDENCE_MESSAGE, str(caught.exception))
        self.assertFalse(self.style_root.exists())
        self.assertEqual([], list(self.workspace_manifests.glob("style_reference_intake_receipt.*.json")))

    def test_shared_cross_role_and_fail_closed_copy_are_exact(self) -> None:
        self.assertEqual(CROSS_ROLE_IMAGE_MESSAGE, style_intake.CROSS_ROLE_IMAGE_MESSAGE)
        self.assertEqual(UNSAFE_PRODUCT_EVIDENCE_MESSAGE, style_intake.UNSAFE_PRODUCT_EVIDENCE_MESSAGE)

    def test_same_product_hash_is_rejected_before_any_style_write(self) -> None:
        original_asset = self.asset_manifest.read_bytes()
        original_product = self.product_path.read_bytes()
        with self.assertRaises(style_intake.StyleReferenceIntakeError) as caught:
            style_intake.publish_style_references(
                self.manifest_path,
                "style-request-001",
                (self._upload(PRODUCT_A),),
            )
        self.assertEqual(CROSS_ROLE_IMAGE_MESSAGE, str(caught.exception))
        self.assertFalse(self.style_root.exists())
        self.assertEqual(original_asset, self.asset_manifest.read_bytes())
        self.assertEqual(original_product, self.product_path.read_bytes())
        self.assertEqual([], list(self.workspace_manifests.glob("style_reference_intake_receipt.*.json")))

    def test_missing_asset_manifest_fails_closed(self) -> None:
        self.asset_manifest.unlink()
        self._assert_unsafe_without_writes()

    def test_unreadable_asset_manifest_fails_closed(self) -> None:
        self.asset_manifest.write_bytes(b"\xff\xfe\x00")
        self._assert_unsafe_without_writes()

    def test_abnormal_asset_role_fails_closed(self) -> None:
        self._write_asset_manifest(role="style_reference")
        self._assert_unsafe_without_writes()

    def test_missing_white_background_file_fails_closed(self) -> None:
        self.product_path.unlink()
        self._assert_unsafe_without_writes()

    def test_product_hash_io_error_fails_closed(self) -> None:
        with mock.patch.object(style_intake, "_sha256_file", side_effect=OSError("private path")):
            self._assert_unsafe_without_writes()

    def test_distinct_style_hash_publishes_without_rewriting_product_evidence(self) -> None:
        original_asset = self.asset_manifest.read_bytes()
        original_product = self.product_path.read_bytes()
        result = style_intake.publish_style_references(
            self.manifest_path,
            "style-request-001",
            (self._upload(),),
        )
        self.assertEqual(1, result.file_count)
        self.assertEqual(STYLE, (self.style_root / "style.jpg").read_bytes())
        self.assertEqual(original_asset, self.asset_manifest.read_bytes())
        self.assertEqual(original_product, self.product_path.read_bytes())


class Nc01IsolatedNormalFlowTests(unittest.TestCase):
    def test_two_product_images_and_one_distinct_style_reference_complete_in_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            scripts = repo / "scripts"
            manifests = repo / "manifests"
            scripts.mkdir(parents=True)
            manifests.mkdir()
            shutil.copy2(ROOT / "scripts" / "build_batch_manifest.py", scripts)
            (repo / "canvas-bridge").mkdir()
            shutil.copy2(
                ROOT / "canvas-bridge" / "category_recipes.py",
                repo / "canvas-bridge",
            )
            shutil.copy2(
                ROOT / "canvas-bridge" / "image_count_contract.py",
                repo / "canvas-bridge",
            )
            shutil.copytree(ROOT / "categories", repo / "categories")
            shutil.copy2(ROOT / "manifests" / "batch_manifest.template.json", manifests)
            shutil.copy2(ROOT / "manifests" / "asset_manifest.template.json", manifests)
            state_root = prepare_state_root(root / "state")
            test_root = root / "isolated-batches"
            test_root.mkdir()
            (test_root / ".canvas_intake_test_root").write_text(
                "canvas-intake-test-root-v1\n",
                encoding="utf-8",
            )
            creator = BatchCreator(
                repo,
                state_root,
                test_root=test_root,
                now=lambda: datetime(2026, 7, 25, 12, 34, 56),
            )
            sources = []
            uploads = []
            for index, data in enumerate((PRODUCT_A, PRODUCT_B), start=1):
                node_id = f"product-{index}"
                name = f"product-{index}.jpg"
                sha256 = hashlib.sha256(data).hexdigest()
                source_path = root / f"upload-{index}.jpg"
                source_path.write_bytes(data)
                sources.append(
                    batch_controller.SourceImage(
                        node_id=node_id,
                        storage_key=f"image:{node_id}",
                        name=name,
                        size=len(data),
                        mime_type="image/jpeg",
                        last_modified=1_000 + index,
                        expected_sha256=sha256,
                    )
                )
                uploads.append(
                    UploadedFile(
                        source_node_id=node_id,
                        path=source_path,
                        name=name,
                        size=len(data),
                        mime_type="image/jpeg",
                        sha256=sha256,
                    )
                )
            request = batch_controller.BatchIntakeRequest(
                request_id="normal-flow-001",
                requested_at=1_000,
                info_node_id="card",
                workflow_node_id="machine",
                facts=batch_controller.ConfirmedFacts(
                    product_type="杯子",
                    height_cm=8,
                    main_image_count=6,
                    detail_image_count=8,
                    handheld_main=2,
                    handheld_detail=1,
                    forbid_pouring_and_heating=True,
                    missing_d_no_retake=True,
                ),
                source_images=tuple(sources),
            )
            created = creator.create(request, tuple(uploads))
            supplemented = style_intake.publish_style_references(
                created.manifest_path,
                "style-normal-001",
                (Nc01StyleEvidenceGateTests._upload(),),
            )
            self.assertEqual(2, created.image_count)
            self.assertEqual(1, supplemented.file_count)
            self.assertEqual(
                ["product-1.jpg", "product-2.jpg"],
                sorted(path.name for path in (created.workspace_root / "inputs" / "white_bg").iterdir()),
            )
            self.assertEqual(
                ["style.jpg"],
                sorted(path.name for path in (created.workspace_root / "inputs" / "style_refs").iterdir()),
            )


class St06SetStyleEvidenceGateTests(unittest.TestCase):
    GROUP = b"\xff\xd8\xffset-group"
    COMPONENT_A = b"\xff\xd8\xffcomponent-a"
    COMPONENT_B = b"\xff\xd8\xffcomponent-b"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        self.repo_manifests = self.repo / "manifests"
        self.workspace_manifests = self.workspace / "manifests"
        self.set_group = self.workspace / "inputs" / "set_group"
        self.component_white_bg = self.workspace / "inputs" / "component_white_bg"
        self.style_root = self.workspace / "inputs" / "style_refs"
        self.lock_root = self.root / "locks"
        self.repo_manifests.mkdir(parents=True)
        self.workspace_manifests.mkdir(parents=True)
        (self.workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "set-cup"}) + "\n",
            encoding="utf-8",
        )
        self.manifest_path = self.repo_manifests / "set-cup.batch_manifest.json"
        self.asset_manifest = self.workspace_manifests / "asset_manifest.json"
        self._write_manifest(batch_type="set")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_manifest(self, *, batch_type: str) -> None:
        manifest = {
            "product_id": "set-cup",
            "batch_type": batch_type,
            "workspace": {"root": str(self.workspace)},
            "inputs": {"style_reference_images": [str(self.style_root)]},
            "artifacts": {"asset_manifest": str(self.asset_manifest)},
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _asset(
        asset_id: str,
        file_path: str,
        role: str,
        *,
        is_single_product_white_bg: bool,
        is_set_group_shot: bool,
    ) -> dict[str, object]:
        return {
            "asset_id": asset_id,
            "file_path": file_path,
            "asset_role": role,
            "is_single_product_white_bg": is_single_product_white_bg,
            "is_set_group_shot": is_set_group_shot,
            "is_style_reference": False,
            "bound_angle_slot": "",
            "component_id": "",
            "notes": "",
        }

    def _set_assets(self) -> list[dict[str, object]]:
        return [
            self._asset(
                "set_group_001",
                "inputs/set_group/group.jpg",
                "set_group_shot",
                is_single_product_white_bg=False,
                is_set_group_shot=True,
            ),
            self._asset(
                "component_white_bg_001",
                "inputs/component_white_bg/component-a.jpg",
                "component_white_bg",
                is_single_product_white_bg=False,
                is_set_group_shot=False,
            ),
            self._asset(
                "component_white_bg_002",
                "inputs/component_white_bg/component-b.jpg",
                "component_white_bg",
                is_single_product_white_bg=False,
                is_set_group_shot=False,
            ),
        ]

    def _write_asset_manifest(self, assets: list[dict[str, object]]) -> None:
        self.asset_manifest.write_text(
            json.dumps({"assets": assets}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_product(self, relative_path: str, data: bytes) -> Path:
        target = self.workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def _install_set_assets(self) -> tuple[list[dict[str, object]], dict[Path, bytes]]:
        assets = self._set_assets()
        products = {
            self._write_product("inputs/set_group/group.jpg", self.GROUP): self.GROUP,
            self._write_product(
                "inputs/component_white_bg/component-a.jpg",
                self.COMPONENT_A,
            ): self.COMPONENT_A,
            self._write_product(
                "inputs/component_white_bg/component-b.jpg",
                self.COMPONENT_B,
            ): self.COMPONENT_B,
        }
        self._write_asset_manifest(assets)
        return assets, products

    @staticmethod
    def _upload(data: bytes = STYLE) -> style_intake.StyleReferenceUpload:
        return style_intake.StyleReferenceUpload(
            node_id="style-node",
            name="style.jpg",
            mime_type="image/jpeg",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    def _publish(
        self,
        data: bytes = STYLE,
        *,
        request_id: str,
    ) -> style_intake.StyleReferencePublishResult:
        return style_intake.publish_style_references(
            self.manifest_path,
            request_id,
            (self._upload(data),),
            batch_lock_root=self.lock_root,
        )

    def _receipt_paths(self) -> list[Path]:
        return list(self.workspace_manifests.glob("style_reference_intake_receipt.*.json"))

    def _assert_product_bytes(self, products: dict[Path, bytes]) -> None:
        for path, expected in products.items():
            self.assertEqual(expected, path.read_bytes())

    def _assert_cross_role_without_writes(
        self,
        data: bytes,
        *,
        request_id: str,
        products: dict[Path, bytes],
    ) -> None:
        original_asset_manifest = self.asset_manifest.read_bytes()
        with self.assertRaises(style_intake.StyleReferenceIntakeError) as caught:
            self._publish(data, request_id=request_id)
        self.assertEqual(CROSS_ROLE_IMAGE_MESSAGE, str(caught.exception))
        self.assertFalse(self.style_root.exists())
        self.assertEqual([], self._receipt_paths())
        self.assertEqual(original_asset_manifest, self.asset_manifest.read_bytes())
        self._assert_product_bytes(products)

    def _assert_unsafe_without_writes(self, *, request_id: str) -> None:
        with self.assertRaises(style_intake.StyleReferenceIntakeError) as caught:
            self._publish(request_id=request_id)
        self.assertEqual(UNSAFE_PRODUCT_EVIDENCE_MESSAGE, str(caught.exception))
        self.assertFalse(self.style_root.exists())
        self.assertEqual([], self._receipt_paths())

    def test_g1_set_group_and_components_publish_distinct_style(self) -> None:
        _assets, products = self._install_set_assets()
        original_asset_manifest = self.asset_manifest.read_bytes()
        result = self._publish(request_id="st06-g1")

        receipt_path = self.workspace_manifests / "style_reference_intake_receipt.st06-g1.json"
        self.assertEqual("set-cup", result.batch_id)
        self.assertEqual(1, result.file_count)
        self.assertEqual(str(receipt_path), result.receipt_path)
        self.assertEqual(("style.jpg",), result.files)
        self.assertEqual(STYLE, (self.style_root / "style.jpg").read_bytes())
        self.assertEqual(
            {
                "receipt_type": "style_reference_intake_v1",
                "product_id": "set-cup",
                "request_id": "st06-g1",
                "file_count": 1,
                "files": [
                    {
                        "node_id": "style-node",
                        "name": "style.jpg",
                        "size": len(STYLE),
                        "mime_type": "image/jpeg",
                        "sha256": hashlib.sha256(STYLE).hexdigest(),
                    }
                ],
            },
            json.loads(receipt_path.read_text(encoding="utf-8")),
        )
        self.assertEqual(original_asset_manifest, self.asset_manifest.read_bytes())
        self._assert_product_bytes(products)

    def test_g2_group_hash_is_rejected_before_any_style_write(self) -> None:
        _assets, products = self._install_set_assets()
        self._assert_cross_role_without_writes(
            self.GROUP,
            request_id="st06-g2",
            products=products,
        )

    def test_g3_component_hash_is_rejected_before_any_style_write(self) -> None:
        _assets, products = self._install_set_assets()
        self._assert_cross_role_without_writes(
            self.COMPONENT_B,
            request_id="st06-g3",
            products=products,
        )

    def test_g4_group_role_rejects_component_directory(self) -> None:
        assets, _products = self._install_set_assets()
        assets[0]["file_path"] = "inputs/component_white_bg/component-a.jpg"
        self._write_asset_manifest(assets)
        self._assert_unsafe_without_writes(request_id="st06-g4")

    def test_g5_role_flags_must_match_exactly(self) -> None:
        cases = (
            ("group-flag", 0, "is_set_group_shot", False),
            ("component-flag", 1, "is_single_product_white_bg", True),
        )
        for case_name, asset_index, field, wrong_value in cases:
            with self.subTest(case=case_name):
                assets, _products = self._install_set_assets()
                assets[asset_index][field] = wrong_value
                self._write_asset_manifest(assets)
                self._assert_unsafe_without_writes(request_id=f"st06-g5-{case_name}")

    def test_g6_mixed_roles_collect_both_hashes_and_block_duplicate(self) -> None:
        white_data = b"\xff\xd8\xfflegacy-white"
        group_path = self._write_product("inputs/set_group/group.jpg", self.GROUP)
        white_path = self._write_product("inputs/white_bg/white.jpg", white_data)
        assets = [
            self._asset(
                "white_bg_001",
                "inputs/white_bg/white.jpg",
                "white_bg",
                is_single_product_white_bg=True,
                is_set_group_shot=False,
            ),
            self._asset(
                "set_group_001",
                "inputs/set_group/group.jpg",
                "set_group_shot",
                is_single_product_white_bg=False,
                is_set_group_shot=True,
            ),
        ]
        self._write_asset_manifest(assets)

        self.assertEqual(
            frozenset(
                {
                    hashlib.sha256(white_data).hexdigest(),
                    hashlib.sha256(self.GROUP).hexdigest(),
                }
            ),
            style_intake._registered_product_sha256s(self.manifest_path),
        )
        self._assert_cross_role_without_writes(
            white_data,
            request_id="st06-g6",
            products={white_path: white_data, group_path: self.GROUP},
        )

    def test_g7_legacy_white_bg_only_does_not_require_set_directories(self) -> None:
        self._write_manifest(batch_type="single")
        white_data = b"\xff\xd8\xfflegacy-only-white"
        white_path = self._write_product("inputs/white_bg/white.jpg", white_data)
        self._write_asset_manifest(
            [
                self._asset(
                    "white_bg_001",
                    "inputs/white_bg/white.jpg",
                    "white_bg",
                    is_single_product_white_bg=True,
                    is_set_group_shot=False,
                )
            ]
        )
        self.assertFalse(self.set_group.exists())
        self.assertFalse(self.component_white_bg.exists())

        result = self._publish(request_id="st06-g7")

        self.assertEqual(1, result.file_count)
        self.assertEqual(STYLE, (self.style_root / "style.jpg").read_bytes())
        self.assertEqual(white_data, white_path.read_bytes())
        self.assertEqual(1, len(self._receipt_paths()))
        self.assertFalse(self.set_group.exists())
        self.assertFalse(self.component_white_bg.exists())

    def test_g8_mixed_valid_and_unknown_role_fails_closed(self) -> None:
        valid_asset = self._asset(
            "set_group_001",
            "inputs/set_group/group.jpg",
            "set_group_shot",
            is_single_product_white_bg=False,
            is_set_group_shot=True,
        )
        unknown_asset = self._asset(
            "unknown_001",
            "inputs/set_group/unknown.jpg",
            "style_reference",
            is_single_product_white_bg=False,
            is_set_group_shot=True,
        )
        self._write_product("inputs/set_group/group.jpg", self.GROUP)
        self._write_product("inputs/set_group/unknown.jpg", STYLE)
        self._write_asset_manifest([valid_asset, unknown_asset])

        self._assert_unsafe_without_writes(request_id="st06-g8")

    def test_g9_nested_product_file_fails_closed(self) -> None:
        nested_asset = self._asset(
            "set_group_001",
            "inputs/set_group/nested/group.jpg",
            "set_group_shot",
            is_single_product_white_bg=False,
            is_set_group_shot=True,
        )
        self._write_product("inputs/set_group/nested/group.jpg", self.GROUP)
        self._write_asset_manifest([nested_asset])

        self._assert_unsafe_without_writes(request_id="st06-g9")

    def test_g10_integer_one_is_not_true_flag_fails_closed(self) -> None:
        white_asset = self._asset(
            "white_bg_001",
            "inputs/white_bg/white.jpg",
            "white_bg",
            is_single_product_white_bg=True,
            is_set_group_shot=False,
        )
        white_asset["is_single_product_white_bg"] = 1
        self._write_product("inputs/white_bg/white.jpg", PRODUCT_A)
        self._write_asset_manifest([white_asset])

        self._assert_unsafe_without_writes(request_id="st06-g10")

    def test_g11_missing_style_reference_flag_fails_closed(self) -> None:
        group_asset = self._asset(
            "set_group_001",
            "inputs/set_group/group.jpg",
            "set_group_shot",
            is_single_product_white_bg=False,
            is_set_group_shot=True,
        )
        del group_asset["is_style_reference"]
        self._write_product("inputs/set_group/group.jpg", self.GROUP)
        self._write_asset_manifest([group_asset])

        self._assert_unsafe_without_writes(request_id="st06-g11")


if __name__ == "__main__":
    unittest.main()
