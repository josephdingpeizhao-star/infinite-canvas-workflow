from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import BatchCreator, prepare_state_root  # noqa: E402
from batch_intake_contract import (  # noqa: E402
    batch_intake_contract_sha256,
    load_batch_intake_contract,
)
from batch_intake_controller import BatchIntakeRequest, ConfirmedFacts  # noqa: E402
import batch_type_gate  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    SET_ARRANGEMENT_BASIS_LITERAL,
    SET_DETAIL_REQUIRED_OVERRIDE_FIELDS,
    SET_HANDHELD_SUMMARY_EXPLANATION_FIELD,
    SET_MAIN_REQUIRED_OVERRIDE_FIELDS,
    SET_PRODUCT_COLOR_BASIS_LITERAL,
    SET_SIZE_ANNOTATION_SUBFIELDS,
    build_set_variable_config_prompt,
    detail_variable_config_chunk_count,
    parse_detail_variable_config_chunk,
    parse_set_variable_config_response,
    parse_user_confirmed_requirements,
)
from codex_dev_executor import (  # noqa: E402
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
)
from content_correction import ContentPredicateViolation  # noqa: E402
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
)
from image_count_contract import (  # noqa: E402
    detail_module_assignment_lines,
    detail_module_groups,
    pair_config_ids,
)


CONTRACT_SHA256 = "a030df8d0aa9c96d9275d7c6f463fbc9d8f10af57e8c4539c2cb9d0d903456d3"
OLD_CONTRACT_SHA256 = (
    "266f01acb929669c4a4057624308f1bc"
    "97f44c497e91489c22e1771012cf5e7c"
)


def _facts(*, height_cm: int | None, length_cm: int | None = None, width_cm: int | None = None) -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": length_cm,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "main_image_count": 6,
        "detail_image_count": 8,
        "handheld_main": 2,
        "handheld_detail": 1,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


class SetBatchIntakeDimensionTests(unittest.TestCase):
    def _manifest(self, batch_type: str, facts: dict[str, object]) -> dict[str, object]:
        return {
            "category": "杯类",
            "batch_type": batch_type,
            "user_confirmed_facts": facts,
        }

    def _run_manifest_builder(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_batch_manifest.py"),
                "--product-id",
                "set_dimension_contract_test",
                "--product-type",
                "杯子",
                "--category",
                "杯类",
                "--main-count",
                "6",
                "--detail-count",
                "8",
                "--handheld-main",
                "2",
                "--handheld-detail",
                "1",
                "--forbid-pouring-and-heating",
                "true",
                "--missing-d-no-retake",
                "true",
                "--dry-run",
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_set_batch_accepts_three_missing_dimensions_through_contract_and_cli(self) -> None:
        requirements = parse_user_confirmed_requirements(
            self._manifest("set", _facts(height_cm=None)),
            ROOT,
        )
        self.assertIsNone(requirements.length_cm)
        self.assertIsNone(requirements.width_cm)
        self.assertIsNone(requirements.height_cm)

        completed = self._run_manifest_builder("--batch-type", "set")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        planned = json.loads(completed.stdout)
        self.assertEqual(
            {"length_cm": None, "width_cm": None, "height_cm": None},
            {
                key: planned["manifest_data"]["user_confirmed_facts"][key]
                for key in ("length_cm", "width_cm", "height_cm")
            },
        )

    def test_single_batch_still_rejects_missing_required_height(self) -> None:
        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "codex-dev 缺少有效的用户确认商品信息",
        ):
            parse_user_confirmed_requirements(
                self._manifest("single", _facts(height_cm=None)),
                ROOT,
            )

        completed = self._run_manifest_builder("--batch-type", "single")
        self.assertEqual(2, completed.returncode)
        self.assertIn("selected category is missing a required dimension", completed.stdout)

    def test_set_batch_keeps_supplied_dimensions_and_batch_creator_omits_none(self) -> None:
        supplied = _facts(height_cm=12, length_cm=10, width_cm=11)
        requirements = parse_user_confirmed_requirements(
            self._manifest("set", supplied),
            ROOT,
        )
        self.assertEqual((10, 11, 12), (requirements.length_cm, requirements.width_cm, requirements.height_cm))

        completed = self._run_manifest_builder(
            "--batch-type",
            "set",
            "--length-cm",
            "10",
            "--width-cm",
            "11",
            "--height-cm",
            "12",
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        planned = json.loads(completed.stdout)
        self.assertEqual(
            {"length_cm": 10, "width_cm": 11, "height_cm": 12},
            {
                key: planned["manifest_data"]["user_confirmed_facts"][key]
                for key in ("length_cm", "width_cm", "height_cm")
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_root = base / "state"
            test_root = base / "workspace"
            test_root.mkdir()
            (test_root / ".canvas_intake_test_root").write_text(
                "canvas-intake-test-root-v1\n",
                encoding="utf-8",
            )
            prepare_state_root(state_root)
            creator = BatchCreator(
                repo_root=ROOT,
                state_root=state_root,
                test_root=test_root,
            )
            facts = ConfirmedFacts(
                product_type="杯子",
                height_cm=None,
                main_image_count=6,
                detail_image_count=8,
                handheld_main=2,
                handheld_detail=1,
                forbid_pouring_and_heating=True,
                missing_d_no_retake=True,
            )
            request = BatchIntakeRequest(
                request_id="st03b-set-dimensions",
                requested_at=1,
                info_node_id="info",
                workflow_node_id="workflow",
                facts=facts,
                source_images=(),
                category="杯类",
                contract_hash=CONTRACT_SHA256,
                batch_type="set",
            )
            manifest, _ = creator._dry_run_plan(
                request,
                "set_dimension_contract_test",
                test_root / "set_dimension_contract_test",
            )
        self.assertIsNone(manifest["user_confirmed_facts"]["height_cm"])

    def test_contract_v4_hash_matches_source_loader_fork_and_active_dist(self) -> None:
        fork = ROOT.parent / "infinite-canvas"
        if not fork.is_dir():
            self.skipTest(f"fork directory is unavailable: {fork}")
        dist = fork / "web" / "dist"
        if not dist.is_dir():
            self.skipTest(f"fork web/dist is unavailable: {dist}")

        contract = load_batch_intake_contract(ROOT)
        self.assertEqual(4, contract["schema_version"])
        self.assertEqual(CONTRACT_SHA256, batch_intake_contract_sha256(ROOT))
        self.assertEqual(
            ["integer", "null"],
            contract["payload"]["properties"]["facts"]["properties"]["height_cm"]["type"],
        )

        for relative_path in (
            "web/src/lib/canvas/canvas-batch-intake.ts",
            "web/tests/st01-set-batch-declaration.test.ts",
            "web/tests/cfg01-clear-water-retirement.test.ts",
        ):
            self.assertIn(CONTRACT_SHA256, (fork / relative_path).read_text(encoding="utf-8"))
        dist_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in dist.rglob("*")
            if path.is_file()
        )
        self.assertIn(CONTRACT_SHA256, dist_text)

        active_files = [
            ROOT / "tests" / "test_cat02_bowl_category.py",
            ROOT / "tests" / "test_cfg01_clear_water_retirement.py",
            ROOT / "tests" / "test_st01_set_batch_declaration.py",
            fork / "web" / "src" / "lib" / "canvas" / "canvas-batch-intake.ts",
            fork / "web" / "tests" / "st01-set-batch-declaration.test.ts",
            fork / "web" / "tests" / "cfg01-clear-water-retirement.test.ts",
            *[path for path in dist.rglob("*") if path.is_file()],
        ]
        self.assertEqual(
            [],
            [str(path) for path in active_files if OLD_CONTRACT_SHA256 in path.read_text(encoding="utf-8", errors="ignore")],
        )


PRODUCT_ID = "st03b-set-product"
BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)


def set_manifest_facts(
    *,
    main_count: int = 2,
    detail_count: int = 7,
    handheld_main: int = 1,
    handheld_detail: int = 1,
) -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "main_image_count": main_count,
        "detail_image_count": detail_count,
        "handheld_main": handheld_main,
        "handheld_detail": handheld_detail,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


def set_requirements(
    *,
    main_count: int = 2,
    detail_count: int = 7,
    handheld_main: int = 1,
    handheld_detail: int = 1,
):
    return parse_user_confirmed_requirements(
        {
            "category": "杯类",
            "batch_type": "set",
            "user_confirmed_facts": set_manifest_facts(
                main_count=main_count,
                detail_count=detail_count,
                handheld_main=handheld_main,
                handheld_detail=handheld_detail,
            ),
        },
        ROOT,
    )


def valid_set_identity() -> dict[str, object]:
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": "set_product_identity",
        "user_declared_set_product": True,
        "set_identity": {
            "set_name": "双件测试套装",
            "set_piece_count": 2,
            "set_lock_description": "保持两件组成、主次与相对比例不变。",
            "套装真实尺寸与尺寸置信度": {
                "套装整体尺寸来源": "用户实测",
                "套装整体尺寸置信度": "高",
                "套装整体高度": "20 厘米",
            },
        },
        "components": [
            {
                "component_index": 1,
                "component_name": "主体杯",
                "identity_archive_file": "component_01_product_identity_archive.json",
            },
            {
                "component_index": 2,
                "component_name": "配套碟",
                "identity_archive_file": "component_02_product_identity_archive.json",
            },
        ],
        "notes": "",
    }


def valid_component_identity(index: int) -> dict[str, object]:
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": "product_identity_archive",
        "identity": {
            "component_name": "主体杯" if index == 1 else "配套碟",
            "产品真实尺寸": {
                "尺寸来源": "用户实测",
                "尺寸置信度": "高",
                "高度": f"{11 + index} 厘米",
            },
            "confirmed_facts": ["组成身份已确认"],
            "visible_inferences": ["只使用白底图可见关系"],
            "unknowns": ["容量无法确认"],
            "prohibited_inventions": ["禁止虚构未确认尺寸"],
            "product_lock_description": "保持单件可见结构与颜色不变。",
        },
        "missing_information": ["容量无法确认"],
        "blocked_reasons": [],
        "notes": "",
    }


def valid_style_master() -> dict[str, object]:
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": "style_master",
        "style_master": {
            "prop_rules": "只使用克制的非商品道具。",
            "concise_style_master": "暖米色背景与柔和自然光。",
        },
        "missing_information": [],
        "notes": "",
    }


def valid_set_layout_inventory(*, is_set_group: bool = True) -> dict[str, object]:
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": "set_angle_layout_inventory",
        "layouts": [
            {
                "layout_id": "layout_001",
                "image_index": 1,
                "file_name": "group.png",
                "is_set_group": is_set_group,
                "overall_camera": "A",
                "layout_slot": "编排槽位一",
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
            {
                "layout_id": "layout_002",
                "image_index": 2,
                "file_name": "component.png",
                "is_set_group": False,
                "overall_camera": "B",
                "layout_slot": "单件白底图，不涉及编排",
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
        ],
        "notes": "",
    }


def _size_annotation_text() -> str:
    return "；".join(f"{field}：以档案已确认字段为准" for field in SET_SIZE_ANNOTATION_SUBFIELDS)


def valid_set_variable_response(
    mode: str,
    *,
    count: int,
    handheld_target: int,
    enabled_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    required_fields = (
        SET_MAIN_REQUIRED_OVERRIDE_FIELDS
        if mode == "main"
        else SET_DETAIL_REQUIRED_OVERRIDE_FIELDS
    )
    configs: list[dict[str, object]] = []
    module_lines = detail_module_assignment_lines(count) if mode == "detail" else ()
    module_groups = detail_module_groups(count) if mode == "detail" else ()
    for index in range(1, count + 1):
        config_id = f"{mode}_{index:02d}"
        overrides = {field: "按档案与合格白底图执行。" for field in required_fields}
        overrides.update(
            {
                "绑定角度槽位": "整体机位 A：正面微俯视机位。对应白底图：图1，group.png。",
                "套装编排槽位": "编排槽位一：并列陈列。对应套装合影白底图：图1，group.png。",
                "套装编排依据": SET_ARRANGEMENT_BASIS_LITERAL,
                "套装产品颜色依据": SET_PRODUCT_COLOR_BASIS_LITERAL,
                "套装组成调用": (
                    "全员出镜，档案件数完整。"
                    if mode == "main" and index <= min(2, count)
                    else "按档案调用全部组成单件。"
                ),
                "套装尺寸比例锁定": "只按档案尺寸置信度保持真实比例。",
                "尺寸比例锁定": "只按档案保持现实比例。",
                "输出画布比例": "1:1" if mode == "main" else "3:4",
                "手持交互声明": "本张图不启用手持场景",
                "动态手持样式参考图调用": "无",
            }
        )
        if config_id in enabled_ids:
            overrides["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：静态握持。"
                "持握套装中某一主体单件，其余单件作为静物陈列。"
            )
            overrides["动态手持样式参考图调用"] = "无，仅动态拿起场景可调用"
        if mode == "detail":
            overrides["标准模块归属"] = module_lines[index - 1]
            if 5 in module_groups[index - 1]:
                overrides["尺寸标注信息"] = "仅调用档案已确认尺寸字段。"
                overrides["尺寸标注图规则"] = "仅按档案已确认字段标注。"
                overrides["套装尺寸标注信息"] = _size_annotation_text()
            else:
                overrides["尺寸标注信息"] = "非尺寸标注图，不启用"
                overrides["尺寸标注图规则"] = "非尺寸标注图，不启用"
                overrides["套装尺寸标注信息"] = "非尺寸标注图，不启用"
        configs.append(
            {
                "config_id": config_id,
                "per_image_overrides": overrides,
                "notes": "",
            }
        )
    enabled = len(enabled_ids)
    scope = "主图" if mode == "main" else "详情图"
    return {
        "common_constraints": {"批次规则": "只使用档案与合格白底图。"},
        "configs": configs,
        "handheld_count_summary": {
            f"用户要求{scope}手持数量": handheld_target,
            "实际启用手持数量": enabled,
            "未启用手持数量": count - enabled,
            "启用手持配置": list(enabled_ids),
            "是否完全满足用户数量": "是" if enabled == handheld_target else "否",
            SET_HANDHELD_SUMMARY_EXPLANATION_FIELD: (
                f"实际启用 {enabled} 项，已按目标启用。"
                if enabled == handheld_target
                else f"实际启用 {enabled} 项；原因：档案置信度前提不足。"
            ),
        },
        "notes": "套装变量配置离线测试样本",
    }


def set_detail_chunks(response: dict[str, object]) -> list[dict[str, object]]:
    configs = response["configs"]
    batches = pair_config_ids("detail", len(configs))
    chunks: list[dict[str, object]] = []
    offset = 0
    for chunk_index, batch in enumerate(batches, start=1):
        chunk: dict[str, object] = {
            "chunk_index": chunk_index,
            "chunk_count": len(batches),
            "configs": copy.deepcopy(configs[offset : offset + len(batch)]),
        }
        offset += len(batch)
        if chunk_index == 1:
            chunk["common_constraints"] = copy.deepcopy(response["common_constraints"])
            chunk["notes"] = response["notes"]
        if chunk_index == len(batches):
            chunk["handheld_count_summary"] = copy.deepcopy(response["handheld_count_summary"])
        chunks.append(chunk)
    return chunks


class SequenceTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.results = [
            CodexTurnResult(
                text=json.dumps(response, ensure_ascii=False),
                thread_id="st03b-thread",
            )
            for response in responses
        ]
        self.calls: list[tuple[str, tuple[CodexAttachment, ...]]] = []
        self.continuation_calls: list[tuple[str, str, tuple[CodexAttachment, ...]]] = []

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        if not self.results:
            raise AssertionError("unexpected transport call")
        return self.results.pop(0)

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.continuation_calls.append((thread_id, prompt, attachments))
        if not self.results:
            raise AssertionError("unexpected continuation")
        return self.results.pop(0)


class SetVariableConfigFixture(unittest.TestCase):
    def make_upstream_paths(
        self,
        root: Path,
        *,
        mode: str,
    ) -> dict[str, Path]:
        style_path = root / "style_master.json"
        style_path.write_text(json.dumps(valid_style_master(), ensure_ascii=False), encoding="utf-8")
        paths = {
            "set_product_identity": root / "set_product_identity.json",
            "component_identity_archive_01": root / "component_01_product_identity_archive.json",
            "component_identity_archive_02": root / "component_02_product_identity_archive.json",
            "style_master": style_path,
            "set_angle_layout_inventory": root / "set_angle_layout_inventory.json",
        }
        if mode == "detail":
            paths["main_variable_configs"] = root / "main_variable_configs.json"
        return paths

    def parse(
        self,
        response: dict[str, object],
        *,
        mode: str = "main",
        layout: dict[str, object] | None = None,
        set_identity: dict[str, object] | None = None,
        component_identities: tuple[dict[str, object], ...] | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            return parse_set_variable_config_response(
                json.dumps(response, ensure_ascii=False),
                mode=mode,
                product_id=PRODUCT_ID,
                requirements=set_requirements(),
                set_identity=set_identity or valid_set_identity(),
                component_identities=component_identities
                or (valid_component_identity(1), valid_component_identity(2)),
                set_angle_layout_inventory=layout or valid_set_layout_inventory(),
                upstream_paths=self.make_upstream_paths(root, mode=mode),
            )

    def make_executor(
        self,
        root: Path,
        responses: list[dict[str, object]],
        *,
        write_set_identity: bool = True,
        missing_component: int | None = None,
        layout_type: str = "set_angle_layout_inventory",
    ) -> tuple[CodexDevExecutor, SequenceTransport, dict[str, object], dict[str, Path]]:
        workspace = root / "workspace"
        artifacts_root = workspace / "artifacts"
        paths = {
            "identity": artifacts_root / "identity",
            "style": artifacts_root / "style_master",
            "layout": artifacts_root / "angle_inventory",
            "main": artifacts_root / "main_vc",
            "detail": artifacts_root / "detail_vc",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        if write_set_identity:
            (paths["identity"] / "set_product_identity.json").write_text(
                json.dumps(valid_set_identity(), ensure_ascii=False),
                encoding="utf-8",
            )
        for index in (1, 2):
            if index == missing_component:
                continue
            (paths["identity"] / f"component_{index:02d}_product_identity_archive.json").write_text(
                json.dumps(valid_component_identity(index), ensure_ascii=False),
                encoding="utf-8",
            )
        (paths["style"] / "style_master.json").write_text(
            json.dumps(valid_style_master(), ensure_ascii=False),
            encoding="utf-8",
        )
        layout = valid_set_layout_inventory()
        layout["artifact_type"] = layout_type
        (paths["layout"] / "set_angle_layout_inventory.json").write_text(
            json.dumps(layout, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest: dict[str, object] = {
            "product_id": PRODUCT_ID,
            "batch_type": "set",
            "user_declared_set_product": True,
            "category": "杯类",
            "user_confirmed_facts": set_manifest_facts(),
            "workspace": {
                "root": str(workspace),
                "artifacts_root": str(artifacts_root),
            },
            "artifacts": {
                "product_identity_archive": str(paths["identity"]),
                "set_product_identity": str(paths["identity"]),
                "style_master": str(paths["style"]),
                "set_angle_layout_inventory": str(paths["layout"]),
                "main_variable_configs": str(paths["main"]),
                "detail_variable_configs": str(paths["detail"]),
            },
        }
        manifest_path = root / "manifests" / f"{PRODUCT_ID}.batch_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        transport = SequenceTransport(responses)
        executor = CodexDevExecutor(
            ExecutorContext(
                manifest=manifest,
                manifest_path=manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            ),
            transport=transport,
            repository_root=ROOT,
        )
        return executor, transport, manifest, paths


class St03bGateTests(unittest.TestCase):
    def test_set_has_five_open_steps_four_closed_and_single_has_all_nine(self) -> None:
        steps = (
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
        ready = {"identity", "style_master", "angle_inventory", "main_vc", "detail_vc"}
        self.assertEqual(frozenset(ready), batch_type_gate.SET_READY_STEPS)
        for step in steps:
            with self.subTest(batch_type="set", step=step):
                self.assertEqual(
                    None if step in ready else BLOCKED_MESSAGE,
                    batch_type_gate.set_batch_blocked_message({"batch_type": "set"}, step),
                )
            with self.subTest(batch_type="single", step=step):
                self.assertIsNone(
                    batch_type_gate.set_batch_blocked_message({"batch_type": "single"}, step)
                )


class St03bSetContractTests(SetVariableConfigFixture):
    def test_single_detail_chunk_rejects_set_key_before_final_assembly(self) -> None:
        facts = set_manifest_facts(detail_count=2)
        facts.update({"length_cm": 8, "width_cm": 8, "height_cm": 10})
        requirements = parse_user_confirmed_requirements(
            {
                "category": "杯类",
                "batch_type": "single",
                "user_confirmed_facts": facts,
            },
            ROOT,
        )
        chunk = {
            "chunk_index": 1,
            "chunk_count": 1,
            "configs": [],
            "common_constraints": {},
            "notes": "分段层测试",
            "handheld_count_summary": {},
            "套装编排依据": SET_ARRANGEMENT_BASIS_LITERAL,
        }

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "详情图变量配置分段包含越界字段",
        ):
            parse_detail_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False),
                1,
                requirements=requirements,
                angle_inventory={},
                prior_chunks=[],
            )

    def test_legal_main_and_detail_samples_cover_a_through_h(self) -> None:
        main = valid_set_variable_response("main", count=2, handheld_target=1)
        main_artifact = self.parse(main)
        self.assertEqual("main_variable_config", main_artifact["artifact_type"])
        self.assertEqual(2, main_artifact["config_count"])

        detail = valid_set_variable_response("detail", count=7, handheld_target=1)
        detail_artifact = self.parse(detail, mode="detail")
        self.assertEqual("detail_variable_config", detail_artifact["artifact_type"])
        self.assertEqual(7, detail_artifact["config_count"])
        self.assertEqual(
            ((1,), (2,), (3,), (4,), (5,), (6, 7), (8,)),
            detail_module_groups(7),
        )

    def test_a_angle_binding_rejects_wrong_image(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        response["configs"][0]["per_image_overrides"]["绑定角度槽位"] = (
            "整体机位 A：正面微俯视机位。对应白底图：图2，component.png。"
        )
        with self.assertRaisesRegex(ContentPredicateViolation, "套装整体机位绑定异常"):
            self.parse(response)

    def test_b_layout_binding_requires_set_group(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        with self.assertRaisesRegex(ExecutorExecutionError, "套装角度与编排合格条目"):
            self.parse(response, layout=valid_set_layout_inventory(is_set_group=False))

    def test_c_fixed_arrangement_and_color_literals_are_exact(self) -> None:
        for field in ("套装编排依据", "套装产品颜色依据"):
            with self.subTest(field=field):
                response = valid_set_variable_response("main", count=2, handheld_target=1)
                response["configs"][0]["per_image_overrides"][field] += "改"
                with self.assertRaises(ContentPredicateViolation):
                    self.parse(response)

    def test_d_main_requires_minimum_two_all_appear_declarations(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        response["configs"][1]["per_image_overrides"]["套装组成调用"] = "只调用部分组成。"
        with self.assertRaisesRegex(ContentPredicateViolation, "全员出镜数量异常"):
            self.parse(response)

    def test_e_set_size_lock_requires_confidence_word(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        response["configs"][0]["per_image_overrides"]["套装尺寸比例锁定"] = "只按档案尺寸执行。"
        with self.assertRaisesRegex(ContentPredicateViolation, "套装尺寸比例锁定异常"):
            self.parse(response)

    def test_f_detail_size_annotation_is_structured_scoped_and_archive_sourced(self) -> None:
        base = valid_set_variable_response("detail", count=7, handheld_target=1)
        module05_index = next(index for index, group in enumerate(detail_module_groups(7)) if 5 in group)

        missing_subfield = copy.deepcopy(base)
        missing_subfield["configs"][module05_index]["per_image_overrides"]["套装尺寸标注信息"] = (
            _size_annotation_text().replace("单位规则", "单位要求")
        )
        with self.assertRaisesRegex(ContentPredicateViolation, "套装尺寸标注结构异常"):
            self.parse(missing_subfield, mode="detail")

        wrong_scope = copy.deepcopy(base)
        wrong_scope["configs"][0]["per_image_overrides"]["套装尺寸标注信息"] = "启用尺寸标注"
        with self.assertRaisesRegex(ContentPredicateViolation, "套装尺寸标注范围异常"):
            self.parse(wrong_scope, mode="detail")

        unsupported = copy.deepcopy(base)
        unsupported["configs"][module05_index]["per_image_overrides"]["套装尺寸标注信息"] += (
            "；画面中允许出现的尺寸文字：高度 99 厘米"
        )
        with self.assertRaises(ContentPredicateViolation):
            self.parse(unsupported, mode="detail")

        confirmed = copy.deepcopy(base)
        confirmed["configs"][module05_index]["per_image_overrides"]["套装尺寸标注信息"] += (
            "；画面中允许出现的尺寸文字：套装整体高度 20 厘米"
        )
        self.parse(confirmed, mode="detail")

    def test_g_handheld_allows_less_with_explanation_rejects_over_target(self) -> None:
        less = valid_set_variable_response("main", count=2, handheld_target=1)
        self.parse(less)
        del less["handheld_count_summary"][SET_HANDHELD_SUMMARY_EXPLANATION_FIELD]
        with self.assertRaises(ExecutorExecutionError):
            self.parse(less)

        over = valid_set_variable_response(
            "main",
            count=2,
            handheld_target=1,
            enabled_ids=("main_01", "main_02"),
        )
        with self.assertRaisesRegex(ContentPredicateViolation, "手持数量异常"):
            self.parse(over)

        exact = valid_set_variable_response(
            "main", count=2, handheld_target=1, enabled_ids=("main_01",)
        )
        self.parse(exact)

    def test_h_config_count_must_equal_free_count_contract(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        response["configs"].pop()
        with self.assertRaisesRegex(ExecutorExecutionError, "数量或结构异常"):
            self.parse(response)

        missing_top_key = valid_set_variable_response("main", count=2, handheld_target=1)
        missing_top_key.pop("notes")
        with self.assertRaisesRegex(ExecutorExecutionError, "顶层字段异常"):
            self.parse(missing_top_key)

    def test_unregistered_set_key_and_forbidden_upstream_key_stay_closed(self) -> None:
        for key in ("套装未登记栏目", "set_angle_layout_inventory"):
            with self.subTest(key=key):
                response = valid_set_variable_response("main", count=2, handheld_target=1)
                response["configs"][0]["per_image_overrides"][key] = "越界"
                with self.assertRaisesRegex(ExecutorExecutionError, "包含越界字段"):
                    self.parse(response)

    def test_dimension_allowlist_is_empty_when_archives_have_no_confirmed_dimensions(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        response["configs"][0]["per_image_overrides"]["展示重点"] = "套装整体高度 20 厘米"
        set_identity = valid_set_identity()
        set_identity["set_identity"].pop("套装真实尺寸与尺寸置信度")
        components = (valid_component_identity(1), valid_component_identity(2))
        for component in components:
            component["identity"].pop("产品真实尺寸")
        with self.assertRaises(ContentPredicateViolation):
            self.parse(
                response,
                set_identity=set_identity,
                component_identities=components,
            )

    def test_dimension_allowlist_uses_the_nearest_archive_field_label(self) -> None:
        set_identity = valid_set_identity()
        set_identity["set_identity"]["套装真实尺寸与尺寸置信度"]["套装整体占地尺寸"] = (
            "长 30 厘米，宽 18 厘米"
        )
        legal = valid_set_variable_response("main", count=2, handheld_target=1)
        legal["configs"][0]["per_image_overrides"]["展示重点"] = "套装整体长 30 厘米"
        self.parse(legal, set_identity=set_identity)

        wrong_field = valid_set_variable_response("main", count=2, handheld_target=1)
        wrong_field["configs"][0]["per_image_overrides"]["展示重点"] = "套装整体长 18 厘米"
        with self.assertRaises(ContentPredicateViolation):
            self.parse(wrong_field, set_identity=set_identity)


class St03bPromptAndProvenanceTests(unittest.TestCase):
    def test_prompt_injects_four_set_teachings_archives_layout_and_archive_dimension_semantics(self) -> None:
        prompt = build_set_variable_config_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            set_identity=valid_set_identity(),
            component_identities=(valid_component_identity(1), valid_component_identity(2)),
            style_master=valid_style_master(),
            set_angle_layout_inventory=valid_set_layout_inventory(),
            requirements=set_requirements(),
            set_skill_text="SET_SKILL_MARKER",
            set_variable_config_supplement="SET_VARIABLE_SUPPLEMENT_MARKER",
            set_workflow_supplement="SET_WORKFLOW_SUPPLEMENT_MARKER",
            set_layout_rules="SET_LAYOUT_RULES_MARKER",
        )
        for marker in (
            "SET_SKILL_MARKER",
            "SET_VARIABLE_SUPPLEMENT_MARKER",
            "SET_WORKFLOW_SUPPLEMENT_MARKER",
            "SET_LAYOUT_RULES_MARKER",
            "双件测试套装",
            "主体杯",
            "group.png",
            "以《套装产品身份档案》及各单件《产品身份档案》的真实尺寸字段为准",
        ):
            self.assertIn(marker, prompt)
        self.assertNotIn("也不处理套装", prompt)
        self.assertNotIn("高度约 None", prompt)

    def test_supplement_is_byte_identical_to_root_source(self) -> None:
        source = (ROOT / "套装变量配置补充模块.txt").read_bytes()
        copied = (ROOT / "categories/_shared/prompts/set_variable_config_supplement.md").read_bytes()
        self.assertEqual(source, copied)
        self.assertEqual(15893, len(copied))
        self.assertEqual(
            "d770b41bab8d5e78785645d32b79eb077a2d1616c67ea2256ff1b1d9ed617e5a",
            hashlib.sha256(copied).hexdigest(),
        )


class St03bExecutorTests(SetVariableConfigFixture):
    def test_set_main_full_chain_writes_unchanged_envelope_and_refuses_overwrite(self) -> None:
        response = valid_set_variable_response("main", count=2, handheld_target=1)
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, _manifest, paths = self.make_executor(Path(temporary), [response])
            result = executor.execute(ExecutionRequest(step="main_vc"))
            output = paths["main"] / "main_variable_configs.json"
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("main_variable_config", artifact["artifact_type"])
            self.assertEqual(
                {
                    "set_product_identity",
                    "component_identity_archive_01",
                    "component_identity_archive_02",
                    "style_master",
                    "set_angle_layout_inventory",
                },
                set(artifact["upstream_artifacts"]),
            )
            self.assertEqual((output,), result.outputs)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual((), transport.calls[0][1])
            with self.assertRaisesRegex(ExecutorExecutionError, "已存在"):
                executor.execute(ExecutionRequest(step="main_vc"))

    def test_set_detail_full_chain_reuses_chunks_and_assembles_seven_configs(self) -> None:
        response = valid_set_variable_response("detail", count=7, handheld_target=1)
        chunks = set_detail_chunks(response)
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, _manifest, paths = self.make_executor(Path(temporary), chunks)
            (paths["main"] / "main_variable_configs.json").write_text(
                json.dumps(
                    {
                        "product_id": PRODUCT_ID,
                        "artifact_type": "main_variable_config",
                        "config_count": 2,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = executor.execute(ExecutionRequest(step="detail_vc"))
            output = paths["detail"] / "detail_variable_configs.json"
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("detail_variable_config", artifact["artifact_type"])
            self.assertEqual(7, artifact["config_count"])
            self.assertEqual(detail_variable_config_chunk_count(set_requirements()), len(chunks))
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(len(chunks) - 1, len(transport.continuation_calls))
            self.assertIn("common_constraints", transport.calls[0][0])
            self.assertIn("handheld_count_summary", transport.continuation_calls[-1][1])
            self.assertEqual((output,), result.outputs)

    def test_set_upstreams_fail_closed_before_transport(self) -> None:
        cases = (
            ({"missing_component": 2}, "codex-dev 无法读取有效的第 2 件产品身份档案"),
            ({"layout_type": "wrong_type"}, "codex-dev 无法读取有效的套装角度与编排入库表"),
            ({"write_set_identity": False}, "codex-dev 无法读取有效的套装产品身份档案"),
        )
        for options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                executor, transport, _manifest, _paths = self.make_executor(
                    Path(temporary),
                    [valid_set_variable_response("main", count=2, handheld_target=1)],
                    **options,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="main_vc"))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual([], transport.calls)

    def test_single_branch_does_not_call_set_loaders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts_root = root / "artifacts"
            style_dir = artifacts_root / "style"
            style_dir.mkdir(parents=True)
            style = valid_style_master()
            style["product_id"] = "single-product"
            (style_dir / "style_master.json").write_text(
                json.dumps(style, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest = {
                "product_id": "single-product",
                "batch_type": "single",
                "user_declared_set_product": False,
                "category": "杯类",
                "user_confirmed_facts": {
                    **set_manifest_facts(),
                    "height_cm": 12,
                },
                "workspace": {"artifacts_root": str(artifacts_root)},
                "artifacts": {
                    "product_identity_archive": str(artifacts_root / "missing-identity"),
                    "style_master": str(style_dir),
                    "angle_inventory": str(artifacts_root / "missing-angle"),
                    "main_variable_configs": str(artifacts_root / "main"),
                },
            }
            executor = CodexDevExecutor(
                ExecutorContext(
                    manifest=manifest,
                    manifest_path=root / "single.batch_manifest.json",
                    environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
                ),
                transport=SequenceTransport([]),
                repository_root=ROOT,
            )
            with mock.patch.object(
                executor,
                "_load_set_product_identity",
                side_effect=AssertionError("single branch touched set loader"),
            ) as set_loader, mock.patch.object(
                executor,
                "_load_set_component_identity_archives",
                side_effect=AssertionError("single branch touched component loader"),
            ) as component_loader:
                with self.assertRaisesRegex(ExecutorExecutionError, "产品身份档案"):
                    executor.execute(ExecutionRequest(step="main_vc"))
            set_loader.assert_not_called()
            component_loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
