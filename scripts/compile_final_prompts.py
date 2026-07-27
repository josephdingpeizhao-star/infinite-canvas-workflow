from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import validate_final_prompt_integrity as prompt_integrity


ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
MAX_COMFY_INT_SEED = 2_147_483_647
DEFAULT_MAIN_CANVAS = (1440, 1440)
DEFAULT_DETAIL_CANVAS = (1440, 1920)
COMMON_NEGATIVE_PROMPT = (
    "不要改形、改色、改材质、改变壶口/壶口边缘/壶身/壶底/壶柄或提梁结构；不要新增原图不存在的壶嘴、壶盖、"
    "出水口、滤网、密封圈、刻度、托盘或图案；不要生成多个销售件数承诺；不要乱码、错字或明显 AI 融化边缘。"
)


class ScriptError(Exception):
    pass


def validate_output_canvas_dimensions(output_type: str, width: int, height: int) -> None:
    if output_type == "main" and width != height:
        raise ScriptError(f"main output canvas must be 1:1; got {width}x{height}")
    if output_type == "detail" and width * 4 != height * 3:
        raise ScriptError(f"detail output canvas must be 3:4; got {width}x{height}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScriptError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_md(path: Path, title: str, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n```json\n{body}\n```\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def deterministic_seed(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (MAX_COMFY_INT_SEED + 1)


def first_item(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            raise ScriptError("expected non-empty list path")
        return str(value[0])
    return str(value)


def artifact_file(value: Any, default_name: str) -> Path:
    path = Path(first_item(value))
    if path.suffix.lower() == ".json":
        return path
    return path / default_name


def artifact_dir(value: Any) -> Path:
    path = Path(first_item(value))
    if path.suffix.lower() == ".json":
        return path.parent
    return path


def first_image(value: Any) -> str:
    path = Path(first_item(value))
    if path.is_file():
        return str(path)
    if not path.is_dir():
        raise ScriptError(f"style reference path does not exist: {path}")
    images = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise ScriptError(f"no style reference images found in: {path}")
    return str(images[0])


def merge_config(common: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(common)
    merged.update(overrides)
    return merged


def config_reference_lookup(doc: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    path_hash = file_sha256(path)
    for index, item in enumerate(doc["configs"]):
        refs[item["config_id"]] = {
            "config_id": item["config_id"],
            "output_type": item["output_type"],
            "source_path": str(path),
            "source_sha256": path_hash,
            "source_schema": "common_constraints + per_image_overrides",
            "common_constraints_ref": {
                "path": str(path),
                "json_pointer": "/common_constraints",
            },
            "per_image_overrides_ref": {
                "path": str(path),
                "json_pointer": f"/configs/{index}/per_image_overrides",
            },
            "resolved_variable_config_sha256": item["resolved_variable_config_sha256"],
        }
    return refs


def expanded_configs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    common = doc.get("common_constraints")
    if not isinstance(common, dict):
        raise ScriptError("variable config is missing common_constraints")
    result = []
    for item in doc.get("configs", []):
        result.append(
            {
                "config_id": item["config_id"],
                "output_type": item["output_type"],
                "variable_config": merge_config(common, item.get("per_image_overrides", {})),
                "resolved_variable_config_sha256": stable_json_sha256(
                    merge_config(common, item.get("per_image_overrides", {}))
                ),
            }
        )
    return result


def parse_bound_asset_id(bound_angle: str) -> str:
    match = re.search(r"对应白底图\s*([^，,；;\s]+)", bound_angle)
    if not match:
        raise ScriptError(f"cannot parse bound white-bg asset id from: {bound_angle}")
    return match.group(1)


def build_asset_lookup(angle_inventory: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for item in angle_inventory.get("image_assets", []):
        asset_id = item.get("asset_id")
        file_path = item.get("file_path")
        if asset_id and file_path:
            lookup[str(asset_id)] = str(file_path)
    if not lookup:
        raise ScriptError("angle inventory has no image_assets lookup")
    return lookup


def size_lock_from_config(configs: list[dict[str, Any]]) -> str:
    for item in configs:
        text = str(item["variable_config"].get("尺寸比例锁定", "")).strip()
        if not text:
            continue
        parts = re.split(r"[：:]", text, maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else text
    raise ScriptError("could not derive size lock from variable configs")


def render_prompt(output_type: str, vc: dict[str, Any], product_lock: str, size_lock: str) -> str:
    text = vc.get("中文营销文案", "无")
    structure_basis = vc.get(
        "产品结构关系依据",
        "壶口、壶口边缘、壶身、壶底支撑、壶柄或提梁连接点及所有可见结构关系",
    )
    style_text = vc.get(
        "风格精简描述",
        "暖奶油色生活化商业摄影，柔和侧上方自然光，米白桌面/布料，柔化绿植和白色/浅蓝花材形成前中后景层次；真实接触阴影，不退化为纯白棚拍。",
    )
    prompt = "\n".join(
        [
            f"生成一张淘宝天猫电商{'主图' if output_type == 'main' else '详情图'}，任务：{vc['页面任务']}。",
            f"产品锁定：{product_lock}",
            f"绑定白底参考图：{vc['绑定角度槽位']}。必须保持该图的产品角度、壶身透视、{structure_basis}和商品本体颜色。",
            f"真实尺寸锁定：{size_lock}",
            f"构图：{vc['构图方式']}；镜头距离：{vc['镜头距离']}。",
            f"风格：{style_text}",
            f"道具与背景：{vc.get('道具生成', vc.get('道具关系', ''))}",
            f"手持：{vc['手持交互声明']}",
            f"文字：{text}。如果为“无”，不要渲染任何文字；如有文字，仅渲染列出的简体中文，深灰/灰褐色，清晰无乱码，不遮挡产品。",
            "画面必须像真实商业摄影，产品材质表面、高光、阴影、接触关系和景深可信。",
        ]
    )
    if output_type == "detail" and str(vc.get("尺寸标注信息", "")).startswith("尺寸来源"):
        prompt += f"\n尺寸标注：{vc['尺寸标注信息']}；{vc['尺寸标注图规则']}"
    return prompt


def build_render_records(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for job in jobs:
        final_prompt_doc = load_json(Path(job["final_prompt_path"]))
        final_prompt = str(final_prompt_doc["final_prompt"])
        negative_prompt = str(final_prompt_doc.get("negative_prompt", ""))
        records.append(
            {
                "job_id": job["job_id"],
                "output_type": job["output_type"],
                "final_prompt_sha256": hashlib.sha256(final_prompt.encode("utf-8")).hexdigest(),
                "final_prompt_length": len(final_prompt),
                "negative_prompt_sha256": hashlib.sha256(negative_prompt.encode("utf-8")).hexdigest(),
                "negative_prompt_length": len(negative_prompt),
                "required_product_reference": job["required_product_reference"],
                "style_reference": job.get("style_reference", ""),
                "width": job.get("width"),
                "height": job.get("height"),
                "seed": deterministic_seed(str(job["job_id"])),
            }
        )
    return records


def compare_baseline(generated_records: list[dict[str, Any]], baseline_path: Path) -> dict[str, Any]:
    baseline_doc = load_json(baseline_path)
    baseline_records = baseline_doc.get("records") or baseline_doc.get("current_records")
    if not isinstance(baseline_records, list):
        raise ScriptError(f"baseline does not contain records: {baseline_path}")

    generated_by_id = {item["job_id"]: item for item in generated_records}
    baseline_by_id = {item["job_id"]: item for item in baseline_records}
    all_ids = sorted(set(generated_by_id) | set(baseline_by_id))
    comparisons = []
    for job_id in all_ids:
        generated = generated_by_id.get(job_id)
        baseline = baseline_by_id.get(job_id)
        if not generated or not baseline:
            comparisons.append({"job_id": job_id, "present_in_both": False})
            continue
        comparisons.append(
            {
                "job_id": job_id,
                "present_in_both": True,
                "final_prompt_match": generated["final_prompt_sha256"] == baseline.get("final_prompt_sha256"),
                "negative_prompt_match": generated["negative_prompt_sha256"] == baseline.get("negative_prompt_sha256"),
                "required_product_reference_match": generated["required_product_reference"]
                == baseline.get("required_product_reference"),
                "style_reference_match": generated["style_reference"] == baseline.get("style_reference"),
                "width_match": generated["width"] == baseline.get("width"),
                "height_match": generated["height"] == baseline.get("height"),
                "seed_match": generated["seed"] == baseline.get("seed"),
            }
        )
    return {
        "baseline": str(baseline_path),
        "record_count": len(generated_records),
        "baseline_record_count": len(baseline_records),
        "all_render_entry_fields_match": all(
            item.get("present_in_both")
            and item.get("final_prompt_match")
            and item.get("negative_prompt_match")
            and item.get("required_product_reference_match")
            and item.get("style_reference_match")
            and item.get("width_match")
            and item.get("height_match")
            and item.get("seed_match")
            for item in comparisons
        ),
        "comparisons": comparisons,
    }


def write_stage_reports(
    *,
    product_id: str,
    report_dir: Path,
    checked_at: str,
    final_index_path: Path,
    job_manifest_path: Path,
    prompt_count: int,
    job_count: int,
    recommended_concurrency: int,
    prompt_integrity_gate: dict[str, Any],
) -> None:
    stage_9 = {
        "product_id": product_id,
        "stage": 9,
        "stage_name": "Final Prompt Compilation",
        "status": "pass",
        "checked_at": checked_at,
        "outputs": [str(final_index_path)],
        "prompt_count": prompt_count,
        "uses_upstream_prompt_files_as_visual_requirements": False,
        "compiler": "scripts/compile_final_prompts.py",
        "image_generation_performed": False,
        "comfyui_execution_performed": False,
    }
    stage_10 = {
        "product_id": product_id,
        "stage": 10,
        "stage_name": "ComfyUI Render Job Preparation",
        "status": "blocked" if prompt_integrity_gate.get("render_blocked") else "pass",
        "checked_at": checked_at,
        "outputs": [str(job_manifest_path)],
        "job_count": job_count,
        "compiler": "scripts/compile_final_prompts.py",
        "prompt_integrity_gate": prompt_integrity_gate,
        "recommended_comfy_cloud_concurrency": recommended_concurrency,
        "notes": (
            "Prepared ComfyUI job manifest only. No ComfyUI call was made. "
            "Rendering remains blocked if prompt_integrity_gate.render_blocked is true."
        ),
        "image_generation_performed": False,
        "comfyui_execution_performed": False,
    }
    for name, payload in (
        (f"{product_id}_stage_9_final_prompt_compilation_report", stage_9),
        (f"{product_id}_stage_10_comfyui_render_job_preparation_report", stage_10),
    ):
        write_json(report_dir / f"{name}.json", payload)
        write_md(report_dir / f"{name}.md", name, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile final prompt artifacts and ComfyUI job manifests from generated upstream artifacts.")
    parser.add_argument("--batch-manifest", required=True)
    parser.add_argument("--output-final-prompts", default=None)
    parser.add_argument("--output-comfyui-jobs", default=None)
    parser.add_argument("--report-dir", default=str(ROOT / "reports"))
    parser.add_argument("--report-prefix", default=None)
    parser.add_argument("--compare-baseline-render-entry", default=None)
    parser.add_argument("--comparison-report", default=None)
    parser.add_argument("--write-stage-reports", action="store_true")
    parser.add_argument("--main-width", type=int, default=DEFAULT_MAIN_CANVAS[0])
    parser.add_argument("--main-height", type=int, default=DEFAULT_MAIN_CANVAS[1])
    parser.add_argument("--detail-width", type=int, default=DEFAULT_DETAIL_CANVAS[0])
    parser.add_argument("--detail-height", type=int, default=DEFAULT_DETAIL_CANVAS[1])
    parser.add_argument("--recommended-concurrency", type=int, default=3)
    parser.add_argument("--expected-handheld-count", type=int, default=None)
    parser.add_argument("--expected-handheld-scope", choices=prompt_integrity.HANDHELD_SCOPES, default="all")
    args = parser.parse_args()
    validate_output_canvas_dimensions("main", args.main_width, args.main_height)
    validate_output_canvas_dimensions("detail", args.detail_width, args.detail_height)

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest_path = Path(args.batch_manifest)
    manifest = load_json(manifest_path)
    product_id = str(manifest["product_id"])
    artifacts = manifest["artifacts"]
    inputs = manifest["inputs"]
    outputs = manifest["outputs"]

    identity_path = artifact_file(artifacts["product_identity_archive"], "product_identity_archive.json")
    style_path = artifact_file(artifacts["style_master"], "style_master.json")
    angle_path = artifact_file(artifacts["angle_inventory"], "angle_inventory.json")
    main_config_path = artifact_file(artifacts["main_variable_configs"], "main_variable_configs.json")
    detail_config_path = artifact_file(artifacts["detail_variable_configs"], "detail_variable_configs.json")
    final_prompts_dir = Path(args.output_final_prompts) if args.output_final_prompts else artifact_dir(artifacts["final_prompts"])
    comfyui_jobs_dir = Path(args.output_comfyui_jobs) if args.output_comfyui_jobs else artifact_dir(artifacts["comfyui_jobs"])
    report_dir = Path(args.report_dir)

    identity = load_json(identity_path)
    load_json(style_path)
    angle_inventory = load_json(angle_path)
    main_doc = load_json(main_config_path)
    detail_doc = load_json(detail_config_path)

    product_identity = identity["identity"]
    product_lock = product_identity["product_lock_description"]
    negative_prompt = product_identity.get("negative_prompt_constraints", COMMON_NEGATIVE_PROMPT)
    main_configs = expanded_configs(main_doc)
    detail_configs = expanded_configs(detail_doc)
    try:
        expected_ids = prompt_integrity.manifest_config_ids(manifest, ROOT)
    except prompt_integrity.ExecutorExecutionError as exc:
        raise ScriptError("batch manifest image counts are invalid") from exc
    observed_ids = tuple(
        str(item.get("config_id") or "")
        for item in main_configs + detail_configs
    )
    if observed_ids != expected_ids:
        raise ScriptError(
            "variable config ids do not match batch manifest image counts"
        )
    size_lock = size_lock_from_config(main_configs + detail_configs)
    main_refs = config_reference_lookup(main_doc, main_config_path)
    detail_refs = config_reference_lookup(detail_doc, detail_config_path)
    variable_config_refs = {**main_refs, **detail_refs}
    asset_lookup = build_asset_lookup(angle_inventory)
    style_reference = first_image(inputs["style_reference_images"])
    output_target_dir = first_item(outputs["renders"])

    final_prompt_docs = []
    jobs = []
    index_items = []
    for item in main_configs + detail_configs:
        config_id = item["config_id"]
        output_type = item["output_type"]
        vc = item["variable_config"]
        ref_asset = parse_bound_asset_id(str(vc["绑定角度槽位"]))
        reference = asset_lookup[ref_asset]
        prompt = render_prompt(output_type, vc, product_lock, size_lock)
        variable_config_path = main_config_path if output_type == "main" else detail_config_path
        final_doc = {
            "product_id": product_id,
            "artifact_type": "final_prompt",
            "upstream_artifacts": {
                "product_identity_archive": str(identity_path),
                "style_master": str(style_path),
                "angle_inventory": str(angle_path),
                "variable_config": str(variable_config_path),
                "realism_constraints": str(ROOT / "真实感约束.txt"),
                "prop_rules": str(ROOT / "道具生成规则模块.txt"),
                "platform_rules": str(ROOT / "淘宝天猫详情页链路与平台规范模块.txt"),
                "qc_checklist": str(ROOT / "电商图片通用质检清单.txt"),
            },
            "variable_config": variable_config_refs[config_id],
            "uses_upstream_prompt_files_as_visual_requirements": False,
            "final_prompt": prompt,
            "negative_prompt": negative_prompt,
            "notes": "Compiled from generated upstream artifacts and this-image variable config only. The render entry is final_prompt plus negative_prompt; variable_config is a resolvable reference.",
        }
        prompt_json = final_prompts_dir / f"{config_id}_final_prompt.json"
        write_json(prompt_json, final_doc)
        write_md(final_prompts_dir / f"{config_id}_final_prompt.md", f"{config_id} Final Prompt", final_doc)
        final_prompt_docs.append(final_doc)
        index_items.append(
            {
                "job_id": config_id,
                "output_type": output_type,
                "final_prompt_path": str(prompt_json),
                "bound_reference": reference,
            }
        )
        jobs.append(
            {
                "job_id": config_id,
                "output_type": output_type,
                "final_prompt_path": str(prompt_json),
                "required_product_reference": reference,
                "style_reference": style_reference,
                "output_target_dir": output_target_dir,
                "width": args.main_width if output_type == "main" else args.detail_width,
                "height": args.main_height if output_type == "main" else args.detail_height,
                "notes": "Prepared for ComfyUI/Comfy Cloud execution; not submitted by this artifact generation step.",
            }
        )

    final_index = {
        "product_id": product_id,
        "artifact_type": "final_prompt_index",
        "prompt_count": len(index_items),
        "uses_upstream_prompt_files_as_visual_requirements": False,
        "items": index_items,
    }
    final_index_path = final_prompts_dir / "final_prompt_index.json"
    write_json(final_index_path, final_index)
    write_md(final_prompts_dir / "final_prompt_index.md", "Final Prompt Index", final_index)

    comfy = {
        "product_id": product_id,
        "artifact_type": "comfyui_job",
        "generated_at": checked_at,
        "job_count": len(jobs),
        "execution_layer": "ComfyUI / Comfy Cloud",
        "execution_status": "prepared_not_submitted",
        "recommended_comfy_cloud_concurrency": args.recommended_concurrency,
        "concurrency_notes": "Default runner concurrency remains 1 for compatibility. Use --concurrency 3 or 4 after API quota and output path checks.",
        "jobs": jobs,
    }
    job_manifest_path = comfyui_jobs_dir / "comfyui_job_manifest.json"
    write_json(job_manifest_path, comfy)
    write_md(comfyui_jobs_dir / "comfyui_job_manifest.md", "ComfyUI Job Manifest", comfy)

    integrity_report = prompt_integrity.build_report(
        batch_manifest_path=manifest_path,
        identity_path=identity_path,
        final_prompt_index_path=final_index_path,
        job_manifest_path=job_manifest_path,
        compiler_path=Path(__file__).resolve(),
        expected_handheld_count=args.expected_handheld_count,
        expected_handheld_scope=args.expected_handheld_scope,
    )
    integrity_output_report = prompt_integrity.default_external_report(manifest)
    integrity_output_markdown = integrity_output_report.with_suffix(".md")
    (
        integrity_output_report,
        integrity_output_markdown,
        integrity_repo_report,
        integrity_repo_markdown,
    ) = prompt_integrity.write_report_files(
        report=integrity_report,
        output_report=integrity_output_report,
        output_markdown=integrity_output_markdown,
        repo_report_dir=report_dir,
        repo_report_prefix=f"{product_id}_final_prompt_integrity_report",
    )
    prompt_integrity_gate = {
        "required": True,
        "status": integrity_report["status"],
        "render_blocked": integrity_report["render_blocked"],
        "checked_at": integrity_report["checked_at"],
        "report_path": str(integrity_output_report),
        "markdown_path": str(integrity_output_markdown),
        "repo_report_path": str(integrity_repo_report) if integrity_repo_report else None,
        "repo_markdown_path": str(integrity_repo_markdown) if integrity_repo_markdown else None,
        "blocking_issue_count": integrity_report["blocking_issue_count"],
        "warning_count": integrity_report["warning_count"],
    }
    comfy["prompt_integrity_gate"] = prompt_integrity_gate
    if integrity_report["render_blocked"]:
        comfy["execution_status"] = "blocked_by_prompt_integrity_gate"
    write_json(job_manifest_path, comfy)
    write_md(comfyui_jobs_dir / "comfyui_job_manifest.md", "ComfyUI Job Manifest", comfy)

    generated_records = build_render_records(jobs)
    comparison = None
    if args.compare_baseline_render_entry:
        comparison = compare_baseline(generated_records, Path(args.compare_baseline_render_entry))
        comparison_report_path = Path(args.comparison_report) if args.comparison_report else report_dir / f"{product_id}_generic_final_prompt_compare.json"
        write_json(comparison_report_path, comparison)
        write_md(comparison_report_path.with_suffix(".md"), comparison_report_path.stem, comparison)

    report = {
        "product_id": product_id,
        "status": (
            "pass"
            if (not comparison or comparison["all_render_entry_fields_match"]) and not integrity_report["render_blocked"]
            else "fail"
        ),
        "checked_at": checked_at,
        "compiler": "scripts/compile_final_prompts.py",
        "prompt_integrity_gate": prompt_integrity_gate,
        "outputs": [
            str(final_index_path),
            str(job_manifest_path),
            str(integrity_output_report),
        ],
        "prompt_count": len(final_prompt_docs),
        "main_prompt_count": sum(1 for item in jobs if item["output_type"] == "main"),
        "detail_prompt_count": sum(1 for item in jobs if item["output_type"] == "detail"),
        "job_count": len(jobs),
        "image_generation_performed": False,
        "comfyui_execution_performed": False,
        "comparison": comparison,
    }
    report_prefix = args.report_prefix or f"{product_id}_generic_final_prompt_compilation_report"
    write_json(report_dir / f"{report_prefix}.json", report)
    write_md(report_dir / f"{report_prefix}.md", report_prefix, report)

    if args.write_stage_reports:
        write_stage_reports(
            product_id=product_id,
            report_dir=report_dir,
            checked_at=checked_at,
            final_index_path=final_index_path,
            job_manifest_path=job_manifest_path,
            prompt_count=len(final_prompt_docs),
            job_count=len(jobs),
            recommended_concurrency=args.recommended_concurrency,
            prompt_integrity_gate=prompt_integrity_gate,
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
