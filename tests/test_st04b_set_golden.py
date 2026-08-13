from __future__ import annotations

"""ST-04b 真实套装批次金样。

生成方式（只允许在用户重新批准更新金样后执行）：

    PYTHONUTF8=1 python -B tests/test_st04b_set_golden.py --record-golden

仓内 inputs/ 保留真实批次产物的原始字节，包括原有绝对路径与混合行尾。
完整性校验和渲染装配仅在临时目录使用白名单式路径重定位镜像；往返测试必须证明
重定位只改变获批路径字段。更新金样必须先硬停申报并取得用户批准。
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
for extra in (ROOT / "scripts", BRIDGE):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_downstream import (  # noqa: E402
    build_set_final_prompt_batch_prompt,
    build_set_final_prompt_repair_prompt,
    build_set_variable_config_prompt,
    parse_user_confirmed_requirements,
)
from codex_dev_executor import CodexDevExecutor  # noqa: E402
from executor_contract import ExecutorContext  # noqa: E402
from render_task_assembler import assemble_render_tasks  # noqa: E402
import validate_final_prompt_integrity as integrity_validator  # noqa: E402


GOLDEN_ROOT = ROOT / "tests" / "fixtures" / "st04b_set_golden"
INPUT_ROOT = GOLDEN_ROOT / "inputs"
FIXTURE_ATTRIBUTES = ROOT / "tests" / "fixtures" / ".gitattributes"
SOURCE_HEAD = "a050201dbea63a664e2f7712607d56b0f24b7833"
PRODUCT_ID = "杯子_20260812_013323"
SOURCE_BATCH_ROOT_TEXT = (
    r"D:\onedrive\OneDrive\Desktop\无限画布工作流\杯类\杯子_20260812_013323"
)
GROUP_FILENAMES = (
    "1S0A1884.JPG",
    "1S0A1887.JPG",
    "1S0A1888.JPG",
)
COMPONENT_FILENAMES = (
    "1S0A1890.JPG",
    "1S0A1898.JPG",
    "1S0A1908.JPG",
    "1S0A1913.JPG",
    "1S0A1921.JPG",
)
USER_CONFIRMED_FACTS = {
    "product_type": "杯子",
    "length_cm": 5,
    "width_cm": 6,
    "height_cm": 6,
    "main_image_count": 3,
    "detail_image_count": 3,
    "handheld_main": 0,
    "handheld_detail": 0,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}

FROZEN_INPUTS = (
    "inputs/identity/set_product_identity.json",
    "inputs/identity/component_01_product_identity_archive.json",
    "inputs/identity/component_02_product_identity_archive.json",
    "inputs/identity/component_03_product_identity_archive.json",
    "inputs/identity/component_04_product_identity_archive.json",
    "inputs/identity/component_05_product_identity_archive.json",
    "inputs/style_master/style_master.json",
    "inputs/angle_inventory/set_angle_layout_inventory.json",
    "inputs/variable_configs/main_variable_configs.json",
    "inputs/variable_configs/detail_variable_configs.json",
    "inputs/final_prompts/main_01_final_prompt.json",
    "inputs/final_prompts/main_02_final_prompt.json",
    "inputs/final_prompts/main_03_final_prompt.json",
    "inputs/final_prompts/detail_01_final_prompt.json",
    "inputs/final_prompts/detail_02_final_prompt.json",
    "inputs/final_prompts/detail_03_final_prompt.json",
    "inputs/final_prompts/final_prompt_index.json",
)
PROMPT_GOLDENS = (
    "component_01_identity_prompt.txt",
    "component_02_identity_prompt.txt",
    "component_03_identity_prompt.txt",
    "component_04_identity_prompt.txt",
    "component_05_identity_prompt.txt",
    "set_identity_prompt.txt",
    "set_angle_layout_prompt.txt",
    "main_variable_config_prompt.txt",
    "detail_variable_config_prompt.txt",
    "main_final_prompt_batch_prompt.txt",
    "detail_final_prompt_batch_prompt.txt",
    "main_final_prompt_repair_prompt.txt",
    "detail_final_prompt_repair_prompt.txt",
)
GENERATED_GOLDENS = (*PROMPT_GOLDENS, "integrity_report.json", "render_reference_sequences.json")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _base_manifest() -> dict[str, Any]:
    return {
        "product_id": PRODUCT_ID,
        "batch_type": "set",
        "user_declared_set_product": True,
        "category": "杯类",
        "requested_outputs": ["main", "detail", "final_prompts"],
        "user_confirmed_facts": dict(USER_CONFIRMED_FACTS),
        "notes": "",
    }


def _component_identities() -> list[dict[str, Any]]:
    return [
        _read_json(
            INPUT_ROOT
            / "identity"
            / f"component_{index:02d}_product_identity_archive.json"
        )
        for index in range(1, 6)
    ]


def collect_prompt_goldens() -> dict[str, str]:
    manifest = _base_manifest()
    requirements = parse_user_confirmed_requirements(manifest, ROOT)
    executor = CodexDevExecutor(
        ExecutorContext(manifest=manifest),
        transport=object(),
        repository_root=ROOT,
    )
    component_skill, component_reference = executor._load_required_rules()
    set_skill, set_identity_reference, set_workflow_reference = (
        executor._load_set_identity_rules()
    )
    angle_skill, angle_inventory_reference, set_layout_reference = (
        executor._load_set_angle_layout_rules()
    )
    variable_rules = executor._load_set_variable_config_rules()
    final_rules = executor._load_set_final_prompt_rules()

    set_identity = _read_json(INPUT_ROOT / "identity" / "set_product_identity.json")
    component_identities = _component_identities()
    style_master = _read_json(INPUT_ROOT / "style_master" / "style_master.json")
    set_layout = _read_json(
        INPUT_ROOT / "angle_inventory" / "set_angle_layout_inventory.json"
    )
    main_config = _read_json(
        INPUT_ROOT / "variable_configs" / "main_variable_configs.json"
    )
    detail_config = _read_json(
        INPUT_ROOT / "variable_configs" / "detail_variable_configs.json"
    )

    values = {
        f"component_{index:02d}_identity_prompt.txt": (
            executor._build_component_identity_prompt(
                PRODUCT_ID,
                index,
                len(COMPONENT_FILENAMES),
                filename,
                component_skill,
                component_reference,
            )
        )
        for index, filename in enumerate(COMPONENT_FILENAMES, start=1)
    }
    values.update(
        {
            "set_identity_prompt.txt": executor._build_set_identity_prompt(
                PRODUCT_ID,
                GROUP_FILENAMES,
                COMPONENT_FILENAMES,
                component_identities,
                set_skill,
                set_identity_reference,
                set_workflow_reference,
            ),
            "set_angle_layout_prompt.txt": executor._build_set_angle_layout_prompt(
                PRODUCT_ID,
                GROUP_FILENAMES,
                COMPONENT_FILENAMES,
                set_identity,
                angle_skill,
                angle_inventory_reference,
                set_layout_reference,
            ),
            "main_variable_config_prompt.txt": build_set_variable_config_prompt(
                mode="main",
                product_id=PRODUCT_ID,
                repository_root=ROOT,
                set_identity=set_identity,
                component_identities=component_identities,
                style_master=style_master,
                set_angle_layout_inventory=set_layout,
                requirements=requirements,
                set_skill_text=variable_rules[0],
                set_variable_config_supplement=variable_rules[1],
                set_workflow_supplement=variable_rules[2],
                set_layout_rules=variable_rules[3],
            ),
            "detail_variable_config_prompt.txt": build_set_variable_config_prompt(
                mode="detail",
                product_id=PRODUCT_ID,
                repository_root=ROOT,
                set_identity=set_identity,
                component_identities=component_identities,
                style_master=style_master,
                set_angle_layout_inventory=set_layout,
                requirements=requirements,
                set_skill_text=variable_rules[0],
                set_variable_config_supplement=variable_rules[1],
                set_workflow_supplement=variable_rules[2],
                set_layout_rules=variable_rules[3],
                main_variable_config=main_config,
            ),
            "main_final_prompt_batch_prompt.txt": build_set_final_prompt_batch_prompt(
                mode="main",
                product_id=PRODUCT_ID,
                repository_root=ROOT,
                set_identity=set_identity,
                component_identities=component_identities,
                style_master=style_master,
                set_angle_layout_inventory=set_layout,
                variable_config=main_config,
                requirements=requirements,
                final_prompt_skill_text=final_rules[0],
                set_workflow_supplement=final_rules[1],
                set_layout_rules=final_rules[2],
            ),
            "detail_final_prompt_batch_prompt.txt": build_set_final_prompt_batch_prompt(
                mode="detail",
                product_id=PRODUCT_ID,
                repository_root=ROOT,
                set_identity=set_identity,
                component_identities=component_identities,
                style_master=style_master,
                set_angle_layout_inventory=set_layout,
                variable_config=detail_config,
                requirements=requirements,
                final_prompt_skill_text=final_rules[0],
                set_workflow_supplement=final_rules[1],
                set_layout_rules=final_rules[2],
            ),
            "main_final_prompt_repair_prompt.txt": (
                build_set_final_prompt_repair_prompt(mode="main")
            ),
            "detail_final_prompt_repair_prompt.txt": (
                build_set_final_prompt_repair_prompt(mode="detail")
            ),
        }
    )
    if set(values) != set(PROMPT_GOLDENS):
        raise AssertionError("ST-04b prompt golden file set is incomplete")
    return values


def _repository_root_hit_count(values: Mapping[str, str]) -> int:
    root_literals = {str(ROOT), str(ROOT).replace("\\", "/")}
    return sum(text.count(literal) for text in values.values() for literal in root_literals)


def _approved_path_values(relative: str, document: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    if relative == "inputs/final_prompts/final_prompt_index.json":
        items = document.get("items")
        if not isinstance(items, list):
            raise AssertionError("final prompt index items are invalid")
        for item in items:
            value = item.get("final_prompt_path") if isinstance(item, Mapping) else None
            if not isinstance(value, str):
                raise AssertionError("final prompt index path is invalid")
            values.append(value)
        return values

    if relative.startswith("inputs/final_prompts/") and relative.endswith(
        "_final_prompt.json"
    ):
        upstreams = document.get("upstream_artifacts")
        variable = document.get("variable_config")
        if not isinstance(upstreams, Mapping) or not isinstance(variable, Mapping):
            raise AssertionError("final prompt path containers are invalid")
        for value in upstreams.values():
            if not isinstance(value, str):
                raise AssertionError("final prompt upstream path is invalid")
            values.append(value)
        source_path = variable.get("source_path")
        common_ref = variable.get("common_constraints_ref")
        override_ref = variable.get("per_image_overrides_ref")
        for value in (
            source_path,
            common_ref.get("path") if isinstance(common_ref, Mapping) else None,
            override_ref.get("path") if isinstance(override_ref, Mapping) else None,
        ):
            if not isinstance(value, str):
                raise AssertionError("final prompt variable path is invalid")
            values.append(value)
    return values


def _replace_exact_json_path_literals(
    payload: bytes,
    replacements: Mapping[str, str],
    expected_values: list[str],
) -> bytes:
    counts = Counter(expected_values)
    result = payload
    for old_value in sorted(counts, key=len, reverse=True):
        new_value = replacements[old_value]
        old_literal = json.dumps(old_value, ensure_ascii=False)[1:-1].encode("utf-8")
        new_literal = json.dumps(new_value, ensure_ascii=False)[1:-1].encode("utf-8")
        if result.count(old_literal) != counts[old_value]:
            raise AssertionError(
                f"path literal appears outside or is missing from the approved fields: {old_value}"
            )
        result = result.replace(old_literal, new_literal)
    return result


def _runtime_destination(runtime_root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.parts[0] != "inputs":
        raise AssertionError(f"unexpected frozen path: {relative}")
    return runtime_root / "artifacts" / Path(*relative_path.parts[1:])


def _runtime_manifest(runtime_root: Path) -> dict[str, Any]:
    artifacts = runtime_root / "artifacts"
    inputs = runtime_root / "inputs"
    outputs = runtime_root / "outputs"
    manifest = _base_manifest()
    manifest.update(
        {
            "workspace": {
                "artifacts_root": str(artifacts),
                "outputs_root": str(outputs),
            },
            "inputs": {
                "white_bg_images": [],
                "set_group_images": [str(inputs / "set_group")],
                "component_white_bg_images": [
                    str(inputs / "component_white_bg")
                ],
            },
            "artifacts": {
                "product_identity_archive": str(artifacts / "identity"),
                "set_product_identity": str(artifacts / "identity"),
                "style_master": str(artifacts / "style_master"),
                "set_angle_layout_inventory": str(artifacts / "angle_inventory"),
                "main_variable_configs": [str(artifacts / "variable_configs")],
                "detail_variable_configs": [str(artifacts / "variable_configs")],
                "final_prompts": [str(artifacts / "final_prompts")],
            },
            "outputs": {"renders": str(outputs / "renders")},
        }
    )
    return manifest


def _materialize_runtime_mirror(
    temporary_root: Path,
    *,
    include_placeholder_images: bool,
) -> tuple[Path, dict[str, tuple[dict[str, str], list[str]]]]:
    runtime_root = temporary_root / "runtime"
    roundtrip_rules: dict[str, tuple[dict[str, str], list[str]]] = {}
    for relative in FROZEN_INPUTS:
        source = GOLDEN_ROOT / relative
        destination = _runtime_destination(runtime_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        document = _read_json(source)
        old_values = _approved_path_values(relative, document)
        if not old_values:
            continue
        if any(not value.startswith(SOURCE_BATCH_ROOT_TEXT) for value in old_values):
            raise AssertionError(f"unapproved source path prefix in {relative}")
        replacements = {
            value: str(runtime_root) + value[len(SOURCE_BATCH_ROOT_TEXT) :]
            for value in set(old_values)
        }
        destination.write_bytes(
            _replace_exact_json_path_literals(
                source.read_bytes(),
                replacements,
                old_values,
            )
        )
        roundtrip_rules[relative] = (replacements, old_values)

    if include_placeholder_images:
        for directory, filenames in (
            (runtime_root / "inputs" / "set_group", GROUP_FILENAMES),
            (
                runtime_root / "inputs" / "component_white_bg",
                COMPONENT_FILENAMES,
            ),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            for filename in filenames:
                (directory / filename).write_bytes(b"x")

    manifest_path = runtime_root / "manifests" / "batch_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(_json_bytes(_runtime_manifest(runtime_root)))
    return manifest_path, roundtrip_rules


def _normalized_report_bytes(report: dict[str, Any], temporary_root: Path) -> bytes:
    root_text = str(temporary_root)

    def normalize(value: object, *, key: str | None = None) -> object:
        if key == "checked_at":
            return "<TIMESTAMP>"
        if isinstance(value, dict):
            return {
                item_key: normalize(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return value.replace(root_text, "<TMP>")
        return value

    return _json_bytes(normalize(report))


def _reference_sequence_bytes(manifest_path: Path) -> bytes:
    manifest = _read_json(manifest_path)
    final_index_path = (
        Path(manifest["artifacts"]["final_prompts"][0]) / "final_prompt_index.json"
    )
    plan = assemble_render_tasks(manifest, final_index_path)
    sequences = {
        task.output_path.stem: [path.name for path in task.reference_images]
        for task in plan.tasks
    }
    return _json_bytes(
        {
            "product_id": PRODUCT_ID,
            "planned": list(plan.planned),
            "skipped": list(plan.skipped),
            "reference_sequences": sequences,
        }
    )


def record_golden() -> None:
    prompts = collect_prompt_goldens()
    root_hits = _repository_root_hit_count(prompts)
    if root_hits:
        raise AssertionError(
            f"repository root absolute path leaked into prompt goldens: {root_hits}"
        )
    generated = {name: text.encode("utf-8") for name, text in prompts.items()}
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        manifest_path, _roundtrip_rules = _materialize_runtime_mirror(
            temporary_root,
            include_placeholder_images=True,
        )
        report = integrity_validator.build_prompts_only_report(
            batch_manifest_path=manifest_path
        )
        if report["status"] != "pass" or report["blocking_issue_count"] != 0:
            raise AssertionError(report["blocking_issues"])
        generated["integrity_report.json"] = _normalized_report_bytes(
            report,
            temporary_root,
        )
        generated["render_reference_sequences.json"] = _reference_sequence_bytes(
            manifest_path
        )

    for name, payload in generated.items():
        (GOLDEN_ROOT / name).write_bytes(payload)

    records: dict[str, dict[str, Any]] = {}
    for relative in sorted((*FROZEN_INPUTS, *GENERATED_GOLDENS)):
        payload = (GOLDEN_ROOT / relative).read_bytes()
        records[relative] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    index = {
        "source_head": SOURCE_HEAD,
        "encoding": "utf-8; frozen input line endings preserved byte-for-byte",
        "provenance": {
            "batch_id": PRODUCT_ID,
            "source": (
                "17 JSON files copied byte-for-byte from the accepted real batch artifacts; "
                "photos, Markdown mirrors, qc_reports, outputs, and batch inputs are excluded."
            ),
            "accepted_render_result": (
                "renders 6/6 completed on 2026-08-13 at 18:41 and the user accepted "
                "the batch as 正常无误."
            ),
        },
        "files": records,
    }
    (GOLDEN_ROOT / "index.json").write_bytes(
        (json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
    )


class St04bSetGoldenTest(unittest.TestCase):
    def test_fixture_gitattributes_disables_line_ending_conversion(self) -> None:
        self.assertEqual(
            ["# 金样夹具必须逐字节冻结，禁止任何行尾转换", "** -text"],
            FIXTURE_ATTRIBUTES.read_text(encoding="utf-8").splitlines(),
        )

    def test_frozen_and_generated_files_match_index_byte_for_byte(self) -> None:
        expected_index = _read_json(GOLDEN_ROOT / "index.json")
        self.assertEqual(SOURCE_HEAD, expected_index["source_head"])
        self.assertEqual(PRODUCT_ID, expected_index["provenance"]["batch_id"])
        expected_files = set((*FROZEN_INPUTS, *GENERATED_GOLDENS))
        self.assertEqual(expected_files, set(expected_index["files"]))
        actual_files = {
            path.relative_to(GOLDEN_ROOT).as_posix()
            for path in GOLDEN_ROOT.rglob("*")
            if path.is_file() and path.name != "index.json"
        }
        self.assertEqual(expected_files, actual_files)
        for relative in sorted(expected_files):
            with self.subTest(relative=relative):
                payload = (GOLDEN_ROOT / relative).read_bytes()
                self.assertEqual(
                    expected_index["files"][relative],
                    {
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    },
                )

    def test_current_set_teaching_prompts_match_goldens_byte_for_byte(self) -> None:
        actual = collect_prompt_goldens()
        self.assertEqual(set(PROMPT_GOLDENS), set(actual))
        for name, text in sorted(actual.items()):
            with self.subTest(name=name):
                self.assertEqual((GOLDEN_ROOT / name).read_bytes(), text.encode("utf-8"))

    def test_prompt_goldens_do_not_embed_repository_root_absolute_path(self) -> None:
        self.assertEqual(0, _repository_root_hit_count(collect_prompt_goldens()))

    def test_runtime_path_rebasing_round_trips_to_frozen_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            manifest_path, roundtrip_rules = _materialize_runtime_mirror(
                temporary_root,
                include_placeholder_images=False,
            )
            runtime_root = manifest_path.parents[1]
            for relative in FROZEN_INPUTS:
                with self.subTest(relative=relative):
                    frozen_payload = (GOLDEN_ROOT / relative).read_bytes()
                    runtime_payload = _runtime_destination(runtime_root, relative).read_bytes()
                    if relative in roundtrip_rules:
                        replacements, old_values = roundtrip_rules[relative]
                        inverse = {new: old for old, new in replacements.items()}
                        runtime_payload = _replace_exact_json_path_literals(
                            runtime_payload,
                            inverse,
                            [replacements[value] for value in old_values],
                        )
                    self.assertEqual(frozen_payload, runtime_payload)

    def test_real_set_integrity_report_matches_normalized_golden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            manifest_path, _roundtrip_rules = _materialize_runtime_mirror(
                temporary_root,
                include_placeholder_images=False,
            )
            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=manifest_path
            )
            self.assertEqual("pass", report["status"], report["blocking_issues"])
            self.assertFalse(report["render_blocked"])
            self.assertEqual(0, report["blocking_issue_count"])
            skipped = {item["check"]: item["reason"] for item in report["skipped_checks"]}
            self.assertEqual(
                "套装编译链不产出已确认高度字面，故本检查按设计跳过。",
                skipped["confirmed_height_literal"],
            )
            ratio_result = next(
                item
                for item in report["results"]
                if item["check_item"] == "ratio_and_confirmed_height_literals"
            )
            self.assertEqual(
                "invalid_ratios=0, invalid_heights=skipped.",
                ratio_result["notes"],
            )
            self.assertEqual(
                (GOLDEN_ROOT / "integrity_report.json").read_bytes(),
                _normalized_report_bytes(report, temporary_root),
            )

    def test_reference_assembly_matches_golden_and_set_axioms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            manifest_path, _roundtrip_rules = _materialize_runtime_mirror(
                temporary_root,
                include_placeholder_images=True,
            )
            actual_bytes = _reference_sequence_bytes(manifest_path)
            self.assertEqual(
                (GOLDEN_ROOT / "render_reference_sequences.json").read_bytes(),
                actual_bytes,
            )
            actual = json.loads(actual_bytes)
            index = _read_json(
                _runtime_destination(
                    manifest_path.parents[1],
                    "inputs/final_prompts/final_prompt_index.json",
                )
            )
            bound_by_id = {
                item["config_id"]: item["bound_reference"] for item in index["items"]
            }
            layout = _read_json(
                INPUT_ROOT / "angle_inventory" / "set_angle_layout_inventory.json"
            )
            ordered_components = tuple(
                item["file_name"]
                for item in sorted(layout["layouts"], key=lambda item: item["image_index"])
                if item["is_set_group"] is False
            )
            self.assertEqual(COMPONENT_FILENAMES, ordered_components)
            for config_id, sequence in actual["reference_sequences"].items():
                with self.subTest(config_id=config_id):
                    self.assertEqual(bound_by_id[config_id], sequence[0])
                    self.assertEqual(list(ordered_components), sequence[1:])
                    self.assertEqual(len(sequence), len(dict.fromkeys(sequence)))


if __name__ == "__main__":
    if sys.argv[1:] == ["--record-golden"]:
        record_golden()
    else:
        unittest.main()
