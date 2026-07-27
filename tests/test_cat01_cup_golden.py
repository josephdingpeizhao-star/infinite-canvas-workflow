from __future__ import annotations

"""CAT-01 杯类提示词金样。

生成方式（只允许在用户重新批准更新金样后执行）：

    PYTHONUTF8=1 python tests/test_cat01_cup_golden.py --record-golden

本金样首次生成于生产代码仍处于 dabf990c4703779b5505d584c7d7f027e027d6b2
的状态；当时只新增了本测试文件，尚未修改任何生产代码。记录过程只调用本地纯
提示词构造函数，不调用模型、不创建批次、不访问真实批次工作区。

正常测试会重新构造同一组提示词并逐字节比较。出现差异即为实现错误；不得通过
重录金样绕过，更新金样必须先硬停申报并取得用户批准。
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
for extra in (ROOT / "scripts", BRIDGE):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_downstream import (  # noqa: E402
    build_final_prompt_batch_prompt,
    build_variable_config_prompt,
    parse_user_confirmed_requirements,
    stable_json_sha256,
)
from codex_dev_executor import CodexDevExecutor  # noqa: E402
from codex_dev_qc import (  # noqa: E402
    QcAsset,
    QcBatch,
    QcPlan,
    _load_rule_documents,
    build_qc_batch_prompt,
)
from executor_contract import ExecutorContext  # noqa: E402


GOLDEN_ROOT = ROOT / "tests" / "fixtures" / "cat01_cup_golden"
SOURCE_HEAD = "dabf990c4703779b5505d584c7d7f027e027d6b2"
PRODUCT_ID = "杯子_CAT01_GOLDEN"
NOTES = (
    "用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | "
    "主图手持数量: 2 | 详情图手持数量: 1 | "
    "允许清水场景: 是 | 禁止倾倒与加热: 是 | D槽位不补拍: 是"
)
STRUCTURED_FACTS = {
    "product_type": "水壶",
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}
FINAL_BINDINGS = {
    "main": (
        ("img_001", "A"),
        ("img_002", "B"),
        ("img_003", "C"),
        ("img_001", "A"),
        ("img_002", "B"),
        ("img_001", "A"),
    ),
    "detail": (
        ("img_001", "A"),
        ("img_002", "B"),
        ("img_003", "C"),
        ("img_001", "A"),
        ("img_002", "B"),
        ("img_001", "A"),
        ("img_002", "B"),
        ("img_003", "C"),
    ),
}


def _identity() -> dict[str, Any]:
    return {
        "artifact_type": "product_identity_archive",
        "product_id": PRODUCT_ID,
        "identity": {
            "confirmed_facts": ["产品类型：水壶", "高度约 25 厘米"],
            "visible_inferences": ["可见壶口、壶身和底足"],
            "unknowns": ["容量无法确认"],
            "prohibited_inventions": ["不得虚构材质、容量或认证"],
            "product_lock_description": "保持产品结构、比例、颜色和图案不变。",
        },
    }


def _style_master() -> dict[str, Any]:
    return {
        "artifact_type": "style_master",
        "product_id": PRODUCT_ID,
        "style_master": {
            "visual_positioning": "产品为视觉主体。",
            "prop_rules": "道具克制，保留真实接触阴影。",
        },
    }


def _angle_inventory() -> dict[str, Any]:
    return {
        "artifact_type": "angle_inventory",
        "product_id": PRODUCT_ID,
        "angle_slots": [
            {
                "source_asset_id": "img_001",
                "angle_slot": "A",
                "admission_result": "合格，可进入对应槽位",
            },
            {
                "source_asset_id": "img_002",
                "angle_slot": "B",
                "admission_result": "合格，可进入对应槽位",
            },
            {
                "source_asset_id": "img_003",
                "angle_slot": "C",
                "admission_result": "合格，可进入对应槽位",
            },
            {
                "source_asset_id": "img_004",
                "angle_slot": "不适合归入现有槽位",
                "admission_result": "不适合入库，需重拍",
            },
        ],
        "missing_angle_slots": ["D"],
    }


def _formal_variable_config(mode: str, enabled_ids: set[str]) -> dict[str, Any]:
    common = {
        "产品类型": "水壶",
        "已确认高度": "约 25 厘米",
        "动作边界": "允许清水静置；禁止倾倒、加热、沸腾或热水动作",
    }
    configs: list[dict[str, Any]] = []
    for index, (asset_id, slot) in enumerate(FINAL_BINDINGS[mode], start=1):
        config_id = f"{mode}_{index:02d}"
        overrides = {
            "绑定角度槽位": f"{slot} 槽位，绑定源图 {asset_id}；本张仅调用这一张白底图。",
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：静态握持"
                if config_id in enabled_ids
                else "本张图不启用手持场景"
            ),
        }
        resolved = dict(common)
        resolved.update(overrides)
        configs.append(
            {
                "config_id": config_id,
                "output_type": mode,
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "CAT-01 杯类金样",
            }
        )
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": f"{mode}_variable_config",
        "config_count": len(configs),
        "upstream_artifacts": {},
        "common_constraints": common,
        "configs": configs,
        "notes": "CAT-01 杯类金样",
    }


def _qc_prompt() -> str:
    asset_ids = ("main_01", "main_02")
    assets = tuple(
        QcAsset(
            asset_id=config_id,
            config_id=config_id,
            output_type="main",
            render_path=Path(f"golden/renders/{config_id}.png"),
            reference_path=Path(f"golden/references/{config_id}.png"),
            final_prompt_path=Path(f"golden/final/{config_id}.json"),
            handheld=config_id == "main_01",
            width=1024,
            height=1024,
        )
        for config_id in asset_ids
    )
    plan = QcPlan(
        product_id=PRODUCT_ID,
        output_path=Path("golden/qc_report.json"),
        assets=assets,
        batches=(QcBatch(index=1, assets=assets),),
        rule_documents=_load_rule_documents(ROOT),
        documents={
            "product_identity_archive": _identity(),
            "style_master": _style_master(),
            "angle_inventory": _angle_inventory(),
            "main_variable_configs": {
                "configs": [{"config_id": f"main_{index:02d}"} for index in range(1, 7)]
            },
            "final_prompts": {
                config_id: {"config_id": config_id, "final_prompt": "正式提示词"}
                for config_id in asset_ids
            },
            "final_prompt_index": {"prompt_ids": list(asset_ids)},
        },
    )
    return build_qc_batch_prompt(plan, plan.batches[0])


def collect_golden() -> dict[str, str]:
    notes_requirements = parse_user_confirmed_requirements({"notes": NOTES})
    structured_requirements = parse_user_confirmed_requirements(
        {"user_confirmed_facts": STRUCTURED_FACTS}
    )
    if notes_requirements != structured_requirements:
        raise AssertionError("金样的 notes 与结构化七字段未解析为同一组杯类事实")

    executor = CodexDevExecutor(
        ExecutorContext(manifest={"product_id": PRODUCT_ID, "notes": NOTES}),
        transport=object(),
        repository_root=ROOT,
    )
    identity_skill, identity_reference = executor._load_required_rules()
    style_skill, style_reference = executor._load_style_master_rules()
    angle_skill, angle_reference = executor._load_angle_inventory_rules()
    identity = _identity()
    style_master = _style_master()
    angle_inventory = _angle_inventory()
    main_config = _formal_variable_config("main", {"main_01", "main_02"})
    detail_config = _formal_variable_config("detail", {"detail_01"})

    def legacy_seven_fields(requirements: Any) -> dict[str, Any]:
        # The pre-migration fixture intentionally records the original public
        # seven-field result, not loader-only recipe context added by CAT-01.
        return {
            key: getattr(requirements, key)
            for key in (
                "product_type",
                "height_cm",
                "handheld_main",
                "handheld_detail",
                "allow_clear_water",
                "forbid_pouring_and_heating",
                "missing_d_no_retake",
            )
        }

    values = {
        "identity_prompt.txt": executor._build_prompt(
            PRODUCT_ID,
            ("正面.png", "侧面.png"),
            identity_skill,
            identity_reference,
        ),
        "style_prompt.txt": executor._build_style_master_prompt(
            PRODUCT_ID,
            ("风格参考.png",),
            identity,
            style_skill,
            style_reference,
        ),
        "angle_prompt.txt": executor._build_angle_inventory_prompt(
            PRODUCT_ID,
            (
                {"source_asset_id": "img_001", "filename": "正面.png"},
                {"source_asset_id": "img_002", "filename": "侧面.png"},
            ),
            (),
            identity,
            angle_skill,
            angle_reference,
        ),
        "main_vc_notes_prompt.txt": build_variable_config_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=notes_requirements,
        ),
        "main_vc_structured_prompt.txt": build_variable_config_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=structured_requirements,
        ),
        "detail_vc_notes_prompt.txt": build_variable_config_prompt(
            mode="detail",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=notes_requirements,
            main_variable_config=main_config,
        ),
        "detail_vc_structured_prompt.txt": build_variable_config_prompt(
            mode="detail",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=structured_requirements,
            main_variable_config=main_config,
        ),
        "final_main_notes_prompt.txt": build_final_prompt_batch_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=main_config,
            requirements=notes_requirements,
        ),
        "final_main_structured_prompt.txt": build_final_prompt_batch_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=main_config,
            requirements=structured_requirements,
        ),
        "final_detail_notes_prompt.txt": build_final_prompt_batch_prompt(
            mode="detail",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=detail_config,
            requirements=notes_requirements,
        ),
        "final_detail_structured_prompt.txt": build_final_prompt_batch_prompt(
            mode="detail",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=detail_config,
            requirements=structured_requirements,
        ),
        "qc_batch_prompt.txt": _qc_prompt(),
        "requirements_notes.json": json.dumps(
            legacy_seven_fields(notes_requirements),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        "requirements_structured.json": json.dumps(
            legacy_seven_fields(structured_requirements),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
    }
    return values


def record_golden() -> None:
    values = collect_golden()
    GOLDEN_ROOT.mkdir(parents=True, exist_ok=True)
    records: dict[str, Any] = {}
    for name, text in sorted(values.items()):
        payload = text.encode("utf-8")
        (GOLDEN_ROOT / name).write_bytes(payload)
        records[name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    index = {
        "source_head": SOURCE_HEAD,
        "encoding": "utf-8",
        "files": records,
    }
    (GOLDEN_ROOT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


class Cat01CupGoldenTest(unittest.TestCase):
    def test_current_cup_prompts_match_pre_migration_golden_byte_for_byte(self) -> None:
        expected_index = json.loads(
            (GOLDEN_ROOT / "index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SOURCE_HEAD, expected_index["source_head"])
        actual = collect_golden()
        self.assertEqual(set(expected_index["files"]), set(actual))
        for name, text in sorted(actual.items()):
            with self.subTest(name=name):
                actual_bytes = text.encode("utf-8")
                self.assertEqual((GOLDEN_ROOT / name).read_bytes(), actual_bytes)
                self.assertEqual(
                    expected_index["files"][name],
                    {
                        "bytes": len(actual_bytes),
                        "sha256": hashlib.sha256(actual_bytes).hexdigest(),
                    },
                )

    def test_notes_and_structured_seven_field_paths_are_identical(self) -> None:
        actual = collect_golden()
        for notes_name, structured_name in (
            ("main_vc_notes_prompt.txt", "main_vc_structured_prompt.txt"),
            ("detail_vc_notes_prompt.txt", "detail_vc_structured_prompt.txt"),
            ("final_main_notes_prompt.txt", "final_main_structured_prompt.txt"),
            ("final_detail_notes_prompt.txt", "final_detail_structured_prompt.txt"),
            ("requirements_notes.json", "requirements_structured.json"),
        ):
            with self.subTest(notes_name=notes_name):
                self.assertEqual(actual[notes_name], actual[structured_name])


if __name__ == "__main__":
    if sys.argv[1:] == ["--record-golden"]:
        record_golden()
    else:
        unittest.main()
