from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import batch_creator  # noqa: E402
import batch_intake_controller as intake_controller  # noqa: E402
import batch_type_gate  # noqa: E402
import workflow_batch_intake_service as intake_service_module  # noqa: E402
import workflow_production_service as production_service  # noqa: E402
from batch_intake_contract import (  # noqa: E402
    batch_intake_contract_sha256,
    canonical_contract_bytes,
    load_batch_intake_contract,
)
from executor_contract import ExecutionResult  # noqa: E402
from make_demo_workspace import build_manifest as build_demo_manifest  # noqa: E402


OLD_CONTRACT_HASH = (
    "ac9e633c814b2032eb5d72c436a773"
    "c03a7dc3f4500d3383580ee7b3f3c18de0"
)
NEW_CONTRACT_HASH = "a030df8d0aa9c96d9275d7c6f463fbc9d8f10af57e8c4539c2cb9d0d903456d3"
NOW_MS = 20_000
STEPS = (
    "identity",
    "style_master",
    "angle_inventory",
    "main_vc",
    "detail_vc",
    "final_prompts",
    "integrity",
    "renders",
    "qc",
)
FACTS = {
    "product_type": "杯子",
    "length_cm": None,
    "width_cm": None,
    "height_cm": 25,
    "main_image_count": 6,
    "detail_image_count": 8,
    "handheld_main": 2,
    "handheld_detail": 1,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}
SET_BATCH_BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)


def _image_node(node_id: str, name: str, index: int) -> dict[str, object]:
    payload = b"st01-offline-image-" + str(index).encode("ascii")
    return {
        "id": node_id,
        "type": "image",
        "title": name,
        "metadata": {
            "storageKey": f"image:st01-{index}",
            "sourceFile": {
                "name": name,
                "size": len(payload),
                "type": "image/png",
                "lastModified": 1_720_000_000_000 + index,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        },
    }


def _canvas_state(
    *,
    batch_type: str = "single",
    set_group_count: int = 0,
    component_count: int = 0,
) -> tuple[dict[str, object], dict[str, object]]:
    white = [_image_node("white-1", "产品原图.png", 1)] if batch_type == "single" else []
    set_group = [
        _image_node(f"set-group-{index}", f"套装合影{index}.png", 10 + index)
        for index in range(1, set_group_count + 1)
    ]
    components = [
        _image_node(f"component-{index}", f"单件{index}.png", 30 + index)
        for index in range(1, component_count + 1)
    ]
    set_group_ids = [str(node["id"]) for node in set_group]
    component_ids = [str(node["id"]) for node in components]
    source_nodes = [*white, *set_group, *components]
    source_ids = [str(node["id"]) for node in source_nodes]
    facts = copy.deepcopy(FACTS)
    if batch_type == "set":
        facts.update({"length_cm": None, "width_cm": None, "height_cm": None})
        facts.update({"handheld_main": 0, "handheld_detail": 0})
    info = {
        "id": "info-1",
        "type": "batch-info",
        "title": "批次信息卡",
        "metadata": {
            "content": (
                "# batch-intake\n"
                "# request-id: st01-request-0001\n"
                "# requested-at: 19000\n"
                "build: batch"
            ),
            "batchIntake": {
                "status": "queued",
                "requestId": "st01-request-0001",
                "requestedAt": 19_000,
                "category": "杯类",
                "contractHash": NEW_CONTRACT_HASH,
                "batch_type": batch_type,
                "facts": facts,
                "workflowNodeId": "workflow-1",
                "sourceImageNodeIds": source_ids,
                "setGroupImageNodeIds": set_group_ids,
                "componentWhiteBgImageNodeIds": component_ids,
            },
        },
    }
    workflow = {
        "id": "workflow-1",
        "type": "workflow",
        "title": "生图工作流",
        "metadata": {},
    }
    state = {
        "nodes": [info, workflow, *source_nodes],
        "connections": [
            {"id": "info-workflow", "fromNodeId": "info-1", "toNodeId": "workflow-1"},
            *[
                {
                    "id": f"{node['id']}-workflow",
                    "fromNodeId": node["id"],
                    "toNodeId": "workflow-1",
                }
                for node in source_nodes
            ],
        ],
    }
    return state, info


def _parse(state: dict[str, object], info: dict[str, object]) -> intake_controller.BatchIntakeRequest:
    return intake_controller.parse_queued_request(
        state,
        info,
        now_ms=NOW_MS,
        repository_root=ROOT,
    )


class St01ContractAndControllerTests(unittest.TestCase):
    def test_contract_v3_loads_and_canonical_hash_is_stable_and_new(self) -> None:
        contract = load_batch_intake_contract(ROOT)

        self.assertEqual(4, contract["schema_version"])
        self.assertEqual(
            ["category", "contractHash", "batch_type", "facts"],
            contract["payload"]["required"],
        )
        self.assertEqual(
            {"type": "string", "enum": ["single", "set"]},
            contract["payload"]["properties"]["batch_type"],
        )
        self.assertFalse(contract["payload"]["additionalProperties"])
        self.assertEqual(NEW_CONTRACT_HASH, batch_intake_contract_sha256(ROOT))
        self.assertEqual(
            NEW_CONTRACT_HASH,
            hashlib.sha256(canonical_contract_bytes(ROOT)).hexdigest(),
        )
        self.assertNotEqual(OLD_CONTRACT_HASH, NEW_CONTRACT_HASH)

    def test_valid_set_payload_classifies_every_selected_image_exactly_once(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)

        request = _parse(state, info)

        self.assertEqual("set", request.batch_type)
        self.assertEqual(
            ["set_group", "component_white_bg", "component_white_bg"],
            [source.image_category for source in request.source_images],
        )
        self.assertEqual(
            ["set_group", "component_white_bg", "component_white_bg"],
            [source["imageCategory"] for source in request.route_dict()["sourceImages"]],
        )
        self.assertEqual("set", request.route_dict()["batch_type"])

    def test_payload_batch_type_and_root_shape_fail_closed(self) -> None:
        cases: list[tuple[str, object]] = [
            ("missing", None),
            ("illegal", "bundle"),
            ("non_string", 1),
        ]
        for label, value in cases:
            with self.subTest(label=label):
                state, info = _canvas_state()
                batch = info["metadata"]["batchIntake"]
                if label == "missing":
                    batch.pop("batch_type")
                else:
                    batch["batch_type"] = value
                with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
                    _parse(state, info)
                self.assertEqual("contract_mismatch", caught.exception.code)

        state, info = _canvas_state()
        info["metadata"]["batchIntake"]["unexpectedPayloadField"] = True
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)
        self.assertEqual("contract_mismatch", caught.exception.code)

    def test_single_rejects_set_images_and_set_quantity_gates_fail_closed(self) -> None:
        invalid_states = [
            (
                _canvas_state(batch_type="single", set_group_count=1, component_count=0),
                "单品批次不能登记套装图片，请清空套装图片后再登记。",
            ),
            (
                _canvas_state(batch_type="single", set_group_count=0, component_count=2),
                "单品批次不能登记套装图片，请清空套装图片后再登记。",
            ),
            (
                _canvas_state(batch_type="set", set_group_count=0, component_count=2),
                "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            ),
            (
                _canvas_state(batch_type="set", set_group_count=1, component_count=0),
                "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            ),
            (
                _canvas_state(batch_type="set", set_group_count=1, component_count=1),
                "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            ),
            (
                _canvas_state(batch_type="set", set_group_count=4, component_count=2),
                "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            ),
            (
                _canvas_state(batch_type="set", set_group_count=3, component_count=9),
                "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            ),
        ]
        for index, ((state, info), expected_message) in enumerate(invalid_states):
            with self.subTest(index=index):
                with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
                    _parse(state, info)
                self.assertEqual("invalid_images", caught.exception.code)
                self.assertEqual(expected_message, caught.exception.user_message)

        for field in (
            "sourceImageNodeIds",
            "setGroupImageNodeIds",
            "componentWhiteBgImageNodeIds",
        ):
            with self.subTest(missing_field=field):
                state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
                info["metadata"]["batchIntake"].pop(field)
                with self.assertRaises(intake_controller.BatchIntakeGateError):
                    _parse(state, info)

    def test_set_minimum_and_upper_boundaries_are_accepted(self) -> None:
        for set_group_count, component_count in ((1, 2), (3, 8)):
            with self.subTest(
                set_group_count=set_group_count,
                component_count=component_count,
            ):
                state, info = _canvas_state(
                    batch_type="set",
                    set_group_count=set_group_count,
                    component_count=component_count,
                )

                request = _parse(state, info)

                self.assertEqual(
                    set_group_count,
                    sum(source.image_category == "set_group" for source in request.source_images),
                )
                self.assertEqual(
                    component_count,
                    sum(source.image_category == "component_white_bg" for source in request.source_images),
                )
                self.assertNotIn(
                    "white_bg",
                    [source.image_category for source in request.source_images],
                )

    def test_cross_category_duplicate_filename_is_rejected(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        set_group = next(node for node in state["nodes"] if node["id"] == "set-group-1")
        set_group["title"] = "单件1.png"
        set_group["metadata"]["sourceFile"]["name"] = "单件1.png"

        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)

        self.assertEqual("duplicate_image", caught.exception.code)

    def test_set_partition_must_cover_connected_images_exactly(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=3)
        batch = info["metadata"]["batchIntake"]
        batch["componentWhiteBgImageNodeIds"] = ["component-1", "component-2"]
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)
        self.assertEqual("invalid_images", caught.exception.code)
        self.assertEqual(
            "套装的合影与单件白底图必须恰好覆盖全部已连接原图，请重新勾选后再登记。",
            caught.exception.user_message,
        )

        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        batch = info["metadata"]["batchIntake"]
        batch["componentWhiteBgImageNodeIds"] = [
            "set-group-1",
            "component-1",
            "component-2",
        ]
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)
        self.assertEqual(
            "同一张图片不能同时用于多个商品图片类别，请重新选择后再登记。",
            caught.exception.user_message,
        )

        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        batch = info["metadata"]["batchIntake"]
        batch["setGroupImageNodeIds"] = ["not-on-canvas"]
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)
        self.assertEqual(
            "套装的合影与单件白底图必须恰好覆盖全部已连接原图，请重新勾选后再登记。",
            caught.exception.user_message,
        )

        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        batch = info["metadata"]["batchIntake"]
        batch["setGroupImageNodeIds"] = ["set-group-1", "not-on-canvas"]
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)
        self.assertEqual("invalid_images", caught.exception.code)
        self.assertEqual(
            "套装的合影与单件白底图必须恰好覆盖全部已连接原图，请重新勾选后再登记。",
            caught.exception.user_message,
        )

    def test_set_white_bg_mix_is_rejected_by_partition_gate(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        white = _image_node("white-mixed", "混入白底.png", 90)
        state["nodes"].append(white)
        state["connections"].append(
            {
                "id": "white-mixed-workflow",
                "fromNodeId": "white-mixed",
                "toNodeId": "workflow-1",
            }
        )
        info["metadata"]["batchIntake"]["sourceImageNodeIds"].append("white-mixed")

        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)

        self.assertEqual("invalid_images", caught.exception.code)
        self.assertEqual(
            "套装的合影与单件白底图必须恰好覆盖全部已连接原图，请重新勾选后再登记。",
            caught.exception.user_message,
        )

    def test_set_duplicate_hash_is_rejected_with_approved_copy(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        group = next(node for node in state["nodes"] if node["id"] == "set-group-1")
        component = next(node for node in state["nodes"] if node["id"] == "component-1")
        component["metadata"]["sourceFile"]["sha256"] = group["metadata"]["sourceFile"]["sha256"]

        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            _parse(state, info)

        self.assertEqual("duplicate_image", caught.exception.code)
        self.assertEqual(
            intake_controller.DUPLICATE_PRODUCT_IMAGE_MESSAGE,
            caught.exception.user_message,
        )


class St01CreatorTests(unittest.TestCase):
    @staticmethod
    def _make_repo_fixture(root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "canvas-bridge").mkdir()
        (root / "manifests").mkdir()
        shutil.copy2(ROOT / "scripts" / "build_batch_manifest.py", root / "scripts")
        shutil.copy2(ROOT / "canvas-bridge" / "category_recipes.py", root / "canvas-bridge")
        shutil.copy2(ROOT / "canvas-bridge" / "image_count_contract.py", root / "canvas-bridge")
        shutil.copytree(ROOT / "categories", root / "categories")
        shutil.copy2(ROOT / "manifests" / "batch_manifest.template.json", root / "manifests")
        shutil.copy2(ROOT / "manifests" / "asset_manifest.template.json", root / "manifests")

    @staticmethod
    def _facts() -> intake_controller.ConfirmedFacts:
        return intake_controller.ConfirmedFacts(
            product_type="杯子",
            height_cm=25,
            main_image_count=6,
            detail_image_count=8,
            handheld_main=0,
            handheld_detail=0,
            forbid_pouring_and_heating=True,
            missing_d_no_retake=True,
        )

    def test_set_build_writes_minimum_and_maximum_role_directories(self) -> None:
        for group_count, component_count in ((1, 2), (3, 8)):
            with self.subTest(group_count=group_count, component_count=component_count):
                with tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    repo = base / "repo"
                    test_root = base / "test-root"
                    state_root = base / "state"
                    upload_root = base / "uploads"
                    test_root.mkdir()
                    upload_root.mkdir()
                    (test_root / ".canvas_intake_test_root").write_text(
                        "canvas-intake-test-root-v1\n",
                        encoding="utf-8",
                    )
                    self._make_repo_fixture(repo)
                    batch_creator.prepare_state_root(state_root)
                    creator = batch_creator.BatchCreator(
                        repo_root=repo,
                        state_root=state_root,
                        test_root=test_root,
                        now=lambda: datetime(2026, 8, 9, 12, 0, 0),
                    )

                    specifications = [
                        *[
                            (
                                f"group-{index}",
                                f"套装合影{index}.png",
                                "set_group",
                                b"\x89PNG\r\n\x1a\ngroup-" + str(index).encode("ascii"),
                            )
                            for index in range(1, group_count + 1)
                        ],
                        *[
                            (
                                f"component-{index}",
                                f"单件{index}.png",
                                "component_white_bg",
                                b"\x89PNG\r\n\x1a\ncomponent-" + str(index).encode("ascii"),
                            )
                            for index in range(1, component_count + 1)
                        ],
                    ]
                    sources: list[intake_controller.SourceImage] = []
                    uploads: list[batch_creator.UploadedFile] = []
                    expected_bytes: dict[str, bytes] = {}
                    for index, (node_id, name, category, content) in enumerate(
                        specifications,
                        start=1,
                    ):
                        digest = hashlib.sha256(content).hexdigest()
                        source = intake_controller.SourceImage(
                            node_id=node_id,
                            storage_key=f"image:{node_id}",
                            name=name,
                            size=len(content),
                            mime_type="image/png",
                            last_modified=index,
                            expected_sha256=digest,
                            image_category=category,
                        )
                        upload_path = upload_root / f"{index}.upload"
                        upload_path.write_bytes(content)
                        sources.append(source)
                        uploads.append(
                            batch_creator.UploadedFile(
                                source_node_id=node_id,
                                path=upload_path,
                                name=name,
                                size=len(content),
                                mime_type="image/png",
                                sha256=digest,
                            )
                        )
                        expected_bytes[node_id] = content
                    request = intake_controller.BatchIntakeRequest(
                        request_id=f"st01-creator-{group_count}-{component_count}",
                        requested_at=19_000,
                        info_node_id="info-1",
                        workflow_node_id="workflow-1",
                        facts=self._facts(),
                        source_images=tuple(sources),
                        category="杯类",
                        contract_hash=NEW_CONTRACT_HASH,
                        batch_type="set",
                    )

                    result = creator.create(request, uploads)
                    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                    asset_manifest = json.loads(
                        (result.workspace_root / "manifests" / "asset_manifest.json").read_text(encoding="utf-8")
                    )
                    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))

                    self.assertEqual("set", manifest["batch_type"])
                    self.assertIs(True, manifest["user_declared_set_product"])
                    white_bg_directory = result.workspace_root / "inputs" / "white_bg"
                    self.assertTrue(white_bg_directory.is_dir())
                    self.assertEqual([], list(white_bg_directory.iterdir()))
                    self.assertEqual(
                        group_count,
                        len(list((result.workspace_root / "inputs" / "set_group").iterdir())),
                    )
                    self.assertEqual(
                        component_count,
                        len(list((result.workspace_root / "inputs" / "component_white_bg").iterdir())),
                    )
                    expected_roles = {
                        **{
                            f"inputs/set_group/套装合影{index}.png": (
                                "set_group_shot",
                                False,
                                True,
                            )
                            for index in range(1, group_count + 1)
                        },
                        **{
                            f"inputs/component_white_bg/单件{index}.png": (
                                "component_white_bg",
                                False,
                                False,
                            )
                            for index in range(1, component_count + 1)
                        },
                    }
                    self.assertEqual(
                        set(expected_roles),
                        {item["file_path"] for item in asset_manifest["assets"]},
                    )
                    for item in asset_manifest["assets"]:
                        role, single_white, set_group = expected_roles[item["file_path"]]
                        self.assertEqual(role, item["asset_role"])
                        self.assertIs(single_white, item["is_single_product_white_bg"])
                        self.assertIs(set_group, item["is_set_group_shot"])
                    receipt_assets = {item["source_node_id"]: item for item in receipt["assets"]}
                    for source in sources:
                        copied = result.workspace_root / receipt_assets[source.node_id]["relative_path"]
                        self.assertEqual(expected_bytes[source.node_id], copied.read_bytes())
                        self.assertEqual(source.expected_sha256, hashlib.sha256(copied.read_bytes()).hexdigest())
                        self.assertEqual(source.expected_sha256, receipt_assets[source.node_id]["destination_sha256"])

    def test_upload_service_accepts_valid_set_categories_and_rejects_a_loosened_category(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        request = _parse(state, info)
        intake_service = object.__new__(intake_service_module.WorkflowBatchIntakeService)
        intake_service._validate_request_sources(request)

        invalid_source = replace(request.source_images[-1], image_category="unapproved")
        with self.assertRaises(ValueError):
            intake_service._validate_request_sources(
                replace(request, source_images=(*request.source_images[:-1], invalid_source))
            )

    def test_creator_and_upload_service_reject_set_white_bg_sources(self) -> None:
        state, info = _canvas_state(batch_type="set", set_group_count=1, component_count=2)
        request = _parse(state, info)
        template = request.source_images[-1]
        white_source = replace(
            template,
            node_id="white-mixed",
            storage_key="image:white-mixed",
            name="混入白底.png",
            expected_sha256="f" * 64,
            image_category="white_bg",
        )
        mixed_request = replace(
            request,
            source_images=(*request.source_images, white_source),
        )

        with self.assertRaises(batch_creator.BatchCreationError) as creator_error:
            object.__new__(batch_creator.BatchCreator)._validate_uploads(
                mixed_request,
                (),
            )
        self.assertEqual("invalid_uploads", creator_error.exception.code)
        self.assertEqual(
            "原图清单不完整，未创建批次。",
            creator_error.exception.user_message,
        )

        intake_service = object.__new__(intake_service_module.WorkflowBatchIntakeService)
        with self.assertRaises(ValueError) as service_error:
            intake_service._validate_request_sources(mixed_request)
        self.assertEqual(
            "原始图片类别或数量不在可登记范围内。",
            str(service_error.exception),
        )

    def test_creator_rejects_cross_category_duplicate_filename(self) -> None:
        categories = (
            ("group", "a.jpg", "set_group"),
            ("component-1", "a.jpg", "component_white_bg"),
            ("component-2", "two.jpg", "component_white_bg"),
        )
        sources = tuple(
            intake_controller.SourceImage(
                node_id=node_id,
                storage_key=f"image:{node_id}",
                name=name,
                size=8,
                mime_type="image/jpeg",
                last_modified=index,
                expected_sha256=f"{index:064x}",
                image_category=category,
            )
            for index, (node_id, name, category) in enumerate(categories, start=1)
        )
        uploads = tuple(
            batch_creator.UploadedFile(
                source_node_id=source.node_id,
                path=ROOT / "unused-upload",
                name=source.name,
                size=source.size,
                mime_type=source.mime_type,
                sha256=source.expected_sha256,
            )
            for source in sources
        )
        request = intake_controller.BatchIntakeRequest(
            request_id="st01-duplicate-request",
            requested_at=19_000,
            info_node_id="info-1",
            workflow_node_id="workflow-1",
            facts=self._facts(),
            source_images=sources,
            category="杯类",
            contract_hash=NEW_CONTRACT_HASH,
            batch_type="set",
        )

        with self.assertRaises(batch_creator.BatchCreationError) as caught:
            object.__new__(batch_creator.BatchCreator)._validate_uploads(request, uploads)

        self.assertEqual("unsafe_filename", caught.exception.code)


class _OfflineExecutor:
    name = "st01-offline"

    def __init__(self, executed: list[str], on_execute=lambda: None) -> None:
        self.executed = executed
        self.on_execute = on_execute

    def execute(self, request: object) -> ExecutionResult:
        self.executed.append(request.step)
        self.on_execute()
        return ExecutionResult(detail="offline-ok", provider=self.name)


class _ForbiddenExecutor:
    name = "st01-forbidden"

    def execute(self, _request: object) -> ExecutionResult:
        raise AssertionError("set batch gate must stop before executor execution")


class _ProductionCanvasClient:
    def __init__(self, batch_id: str, step: str) -> None:
        self.machine = {
            "id": "machine",
            "type": "workflow",
            "position": {"x": 0, "y": 0},
            "width": 420,
            "height": 300,
            "metadata": {
                "content": (
                    "# workflow-production\n"
                    f"# request-id: req-{batch_id}\n"
                    f"run: {step}"
                ),
                "workflowProduction": {
                    "status": "queued",
                    "requestId": f"req-{batch_id}",
                    "batchId": batch_id,
                    "requestedAt": 1_000,
                    "producedCount": 0,
                },
            },
        }
        self.state = {
            "nodes": [
                self.machine,
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": batch_id, "imageCount": 1},
                        }
                    },
                },
                {
                    "id": "source",
                    "type": "image",
                    "metadata": {"content": "blob:source", "storageKey": "image:source"},
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "source-machine", "fromNodeId": "source", "toNodeId": "machine"},
            ],
        }

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        for op in ops:
            if op.get("type") != "update_node":
                raise AssertionError(op)
            if op.get("id") != "machine":
                raise AssertionError(op)
            self.machine["metadata"] = {
                **self.machine["metadata"],
                **op.get("metadata", {}),
            }
        return len(ops)


class St01StepGateTests(unittest.TestCase):
    _STEP_ROUTES = {
        "identity": ("needs_product_identity_archive", "product-identity-archive"),
        "style_master": ("needs_style_master", "style-master-extractor"),
        "angle_inventory": ("needs_angle_inventory", "angle-inventory"),
        "main_vc": ("needs_main_variable_configs", "main-variable-config"),
        "detail_vc": ("needs_detail_variable_configs", "detail-variable-config"),
        "final_prompts": ("needs_final_prompts", "final-prompt-compiler"),
        "qc": ("needs_qc_reports", "qc-inspector"),
    }

    @staticmethod
    def _prepare_repository(root: Path) -> Path:
        repository = root / "repo"
        (repository / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", repository / "categories")
        return repository

    @classmethod
    def _route_and_integrity(cls, step: str) -> tuple[dict[str, object], dict[str, object]]:
        if step in {"integrity", "renders"}:
            route = {
                "current_stage": "needs_generated_images_before_qc",
                "next_required_skill": None,
                "blocked_reasons": ["QC is post-generation only"],
                "available_artifacts": ["final_prompts"],
                "outputs": {
                    "renders": {"file_count": 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            }
            integrity = {
                "found": step == "renders",
                "status": "pass" if step == "renders" else "missing",
                "render_blocked": False,
            }
            return route, integrity
        stage, skill = cls._STEP_ROUTES[step]
        return (
            {
                "current_stage": stage,
                "next_required_skill": skill,
                "blocked_reasons": [],
                "available_artifacts": ["final_prompts"] if step == "qc" else [],
                "outputs": {
                    "renders": {"file_count": 2 if step == "qc" else 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            },
            {"found": False, "status": "missing", "render_blocked": False},
        )

    @staticmethod
    def _write_manifest(
        repository: Path,
        root: Path,
        batch_id: str,
        batch_type: str | None,
    ) -> Path:
        workspace = root / f"workspace-{batch_id}"
        (workspace / "inputs" / "style_refs").mkdir(parents=True)
        (workspace / "inputs" / "style_refs" / "style.jpg").write_bytes(b"style")
        (workspace / "outputs" / "renders").mkdir(parents=True)
        (workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": batch_id}),
            encoding="utf-8",
        )
        manifest: dict[str, object] = {
            "product_id": batch_id,
            "category": "杯类",
            "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
            "workspace": {"root": str(workspace)},
            "inputs": {"style_reference_images": [str(workspace / "inputs" / "style_refs")]},
            "user_confirmed_facts": {
                "main_image_count": 1,
                "detail_image_count": 1,
            },
            "drafts": {},
            "artifacts": {},
            "outputs": {"renders": [str(workspace / "outputs" / "renders")], "repaired": []},
        }
        if batch_type is not None:
            manifest["batch_type"] = batch_type
        manifest_path = repository / "manifests" / f"{batch_id}.batch_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return repository / "manifests" / f"{batch_id}.events.jsonl"

    @staticmethod
    def _event_names(journal: Path) -> list[str]:
        if not journal.exists():
            return []
        return [
            str(json.loads(line).get("event") or "")
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]

    def test_set_eight_step_matrix_stops_before_executor_with_exact_copy(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "identity",
                    "style_master",
                    "angle_inventory",
                    "main_vc",
                    "detail_vc",
                    "final_prompts",
                    "integrity",
                    "renders",
                }
            ),
            batch_type_gate.SET_READY_STEPS,
        )
        self.assertEqual(
            SET_BATCH_BLOCKED_MESSAGE,
            batch_type_gate.set_batch_blocked_message({"batch_type": "set"}, "qc"),
        )
        self.assertEqual(
            SET_BATCH_BLOCKED_MESSAGE,
            batch_type_gate.set_batch_blocked_message({"batch_type": "invalid"}, "identity"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._prepare_repository(root)
            blocked_steps = tuple(
                step
                for step in STEPS
                if step
                not in {
                    "identity",
                    "style_master",
                    "angle_inventory",
                    "main_vc",
                    "detail_vc",
                    "final_prompts",
                    "integrity",
                    "renders",
                }
            )
            for index, step in enumerate(blocked_steps, start=1):
                with self.subTest(step=step):
                    batch_id = f"set-{index}"
                    journal = self._write_manifest(repository, root, batch_id, "set")
                    route, integrity = self._route_and_integrity(step)
                    client = _ProductionCanvasClient(batch_id, step)
                    built: list[str] = []

                    def forbidden_builder(built_step: str, *_args: object) -> _ForbiddenExecutor:
                        built.append(built_step)
                        return _ForbiddenExecutor()

                    service = production_service.WorkflowProductionService(
                        repository,
                        client=client,
                        executor_builder=forbidden_builder,
                        route_reader=lambda _path, route=route: route,
                        integrity_reader=lambda _route, integrity=integrity: integrity,
                        artifact_reader=lambda _manifest: (),
                        render_artifact_reader=lambda _manifest: (),
                        repaired_artifact_reader=lambda _manifest: (),
                        clock_ms=lambda: 1_100,
                        environment={},
                        batch_lock_root=root / "locks",
                        persistence_timeout_ms=0,
                    )

                    service.poll_once()

                    production = client.machine["metadata"]["workflowProduction"]
                    events = self._event_names(journal)
                    checks = (
                        ("status", "failed", production["status"]),
                        ("message", SET_BATCH_BLOCKED_MESSAGE, production.get("errorMessage")),
                        ("executor_builds", [], built),
                        ("step_started", 0, events.count("step_started")),
                        ("step_auto_retry", 0, events.count("step_auto_retry")),
                        ("production_paused", 0, events.count("production_paused")),
                    )
                    for invariant, expected, actual in checks:
                        with self.subTest(invariant=invariant):
                            self.assertEqual(expected, actual)

    def test_single_and_missing_batch_type_nine_step_matrix_keep_executor_behavior(self) -> None:
        self.assertIsNone(batch_type_gate.set_batch_blocked_message({}, "identity"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._prepare_repository(root)
            for type_index, batch_type in enumerate(("single", None), start=1):
                for step_index, step in enumerate(STEPS, start=1):
                    with self.subTest(batch_type=batch_type, step=step):
                        batch_id = f"single-{type_index}-{step_index}"
                        journal = self._write_manifest(repository, root, batch_id, batch_type)
                        route, integrity = self._route_and_integrity(step)
                        client = _ProductionCanvasClient(batch_id, step)
                        built: list[str] = []
                        executed: list[str] = []
                        service: production_service.WorkflowProductionService

                        def build_executor(built_step: str, *_args: object) -> _OfflineExecutor:
                            built.append(built_step)
                            return _OfflineExecutor(
                                executed,
                                on_execute=lambda: setattr(service, "stopping", True),
                            )

                        service = production_service.WorkflowProductionService(
                            repository,
                            client=client,
                            executor_builder=build_executor,
                            route_reader=lambda _path, route=route: route,
                            integrity_reader=lambda _route, integrity=integrity: integrity,
                            artifact_reader=lambda _manifest: (),
                            render_artifact_reader=lambda _manifest: (),
                            repaired_artifact_reader=lambda _manifest: (),
                            clock_ms=lambda: 1_100,
                            environment={},
                            batch_lock_root=root / "locks",
                            persistence_timeout_ms=0,
                        )
                        with mock.patch.object(
                            service,
                            "_start_qc_heartbeat_worker",
                            return_value=None,
                        ):
                            service.poll_once()

                        production = client.machine["metadata"]["workflowProduction"]
                        events = self._event_names(journal)
                        self.assertNotIn("step_auto_retry", events)
                        if step in {"integrity", "renders"}:
                            self.assertEqual([], built)
                            self.assertEqual([], executed)
                            self.assertEqual("paused", production["status"])
                            self.assertEqual(
                                "上游准备完成，已停在出图前。等待批准下一闸门。",
                                production["message"],
                            )
                            self.assertNotIn("step_started", events)
                            self.assertEqual(1, events.count("production_paused"))
                        else:
                            self.assertEqual([step], built)
                            self.assertEqual([step], executed)
                            self.assertEqual("running", production["status"])
                            self.assertEqual(step, production["step"])
                            self.assertEqual(1, events.count("step_started"))
                            self.assertEqual(1, events.count("step_succeeded"))


class St01ManifestCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _manifest_command(workspace: Path) -> list[str]:
        return [
            sys.executable,
            "-B",
            str(ROOT / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            "st01_default_single",
            "--product-type",
            "杯子",
            "--category",
            "杯类",
            "--height-cm",
            "25",
            "--main-count",
            "1",
            "--detail-count",
            "1",
            "--handheld-main",
            "0",
            "--handheld-detail",
            "0",
            "--forbid-pouring-and-heating",
            "true",
            "--missing-d-no-retake",
            "true",
            "--workspace-root",
            str(workspace),
            "--dry-run",
        ]

    def test_build_manifest_omitted_parameter_is_byte_equal_to_explicit_single(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            command = self._manifest_command(workspace)
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            omitted = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                check=False,
                env=environment,
            )
            explicit = subprocess.run(
                [*command, "--batch-type", "single"],
                cwd=ROOT,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(0, omitted.returncode, omitted.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(0, explicit.returncode, explicit.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(omitted.stdout, explicit.stdout)
            manifest = json.loads(omitted.stdout.decode("utf-8"))["manifest_data"]
            self.assertEqual("single", manifest["batch_type"])
            self.assertIs(False, manifest["user_declared_set_product"])
            self.assertFalse(workspace.exists())

    def test_demo_workspace_manifest_remains_single_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = build_demo_manifest(Path(temporary) / "demo")

        self.assertEqual("single", manifest["batch_type"])
        self.assertIs(False, manifest["user_declared_set_product"])
        for step in STEPS:
            self.assertIsNone(batch_type_gate.set_batch_blocked_message(manifest, step))


if __name__ == "__main__":
    unittest.main()
