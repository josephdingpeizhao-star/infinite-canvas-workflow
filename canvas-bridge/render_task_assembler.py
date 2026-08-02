"""Build provider-neutral image tasks from an accepted final prompt index."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from codex_dev_downstream import manifest_config_ids
from executor_contract import ImageGenerationTask
from executor_contract import ExecutorExecutionError
from white_bg_recovery import (
    WhiteBgRecoveryError,
    WhiteBgScan,
    scan_white_bg_recovery,
)


NEGATIVE_PROMPT_SEPARATOR = "\n\n--- negative_prompt（以下内容必须避免）---\n"
ASPECT_TO_IMAGE_SIZE = {"1:1": "1024x1024", "3:4": "1024x1536"}
OUTPUT_TYPE_TO_ASPECT = {"main": "1:1", "detail": "3:4"}
SUPPORTED_REFERENCE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class RenderTaskAssemblyError(ValueError):
    """The complete render batch could not be assembled safely."""


def _white_bg_failure(message: str, scan: WhiteBgScan) -> RenderTaskAssemblyError:
    failure = RenderTaskAssemblyError(message)
    if scan.kind == "inputs_unavailable":
        failure.code = "render_inputs_unavailable"
        return failure
    if scan.kind == "missing_reference":
        failure.code = "render_input_missing"
        failure.missing_files = scan.missing_files
        failure.missing_count = scan.missing_count
        failure.remaining_count = scan.remaining_count
    return failure


def _scan_reference_failure(
    manifest: Mapping[str, Any],
    filename: str,
) -> WhiteBgScan:
    """Prefer every recorded binding while retaining a safe legacy fallback."""

    try:
        return scan_white_bg_recovery(manifest)
    except WhiteBgRecoveryError:
        return scan_white_bg_recovery(manifest, bound_references=(filename,))


@dataclass(frozen=True)
class RenderTaskPlan:
    tasks: tuple[ImageGenerationTask, ...]
    planned: tuple[str, ...]
    skipped: tuple[str, ...]


def _first_path(value: Any, label: str) -> Path:
    if isinstance(value, list):
        if not value:
            raise RenderTaskAssemblyError(f"{label} 未声明路径")
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise RenderTaskAssemblyError(f"{label} 未声明路径")
    return Path(value)


def _all_paths(value: Any, label: str) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    paths = tuple(Path(item) for item in values if isinstance(item, str) and item.strip())
    if not paths:
        raise RenderTaskAssemblyError(f"{label} 未声明路径")
    return paths


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RenderTaskAssemblyError(f"{label} 无法读取") from exc
    if "\ufffd" in text:
        raise RenderTaskAssemblyError(f"{label} 包含损坏字符")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RenderTaskAssemblyError(f"{label} 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise RenderTaskAssemblyError(f"{label} 顶层结构无效")
    return value


def resolve_final_prompt_index_path(manifest: Mapping[str, Any]) -> Path:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise RenderTaskAssemblyError("manifest.artifacts 缺失")
    final_dir = _first_path(artifacts.get("final_prompts"), "final_prompts")
    if final_dir.suffix.lower() == ".json":
        final_dir = final_dir.parent
    return final_dir / "final_prompt_index.json"


def _reference_image(manifest: Mapping[str, Any], filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise RenderTaskAssemblyError("索引中的绑定参考图文件名无效")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise RenderTaskAssemblyError("manifest.inputs 缺失")
    roots = _all_paths(inputs.get("white_bg_images"), "white_bg_images")
    matches: dict[Path, Path] = {}
    for root in roots:
        if root.is_file():
            candidates = (root,) if root.name == filename else ()
            boundary = root.parent
        elif root.is_dir():
            try:
                candidates = tuple(
                    item for item in root.rglob("*") if item.is_file() and item.name == filename
                )
            except OSError as exc:
                scan = WhiteBgScan("inputs_unavailable", (), 0, 0)
                raise _white_bg_failure("白底图目录无法读取", scan) from exc
            boundary = root
        else:
            scan = WhiteBgScan("inputs_unavailable", (), 0, 0)
            raise _white_bg_failure("白底图路径不存在", scan)
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if not _is_within(candidate, boundary):
                raise RenderTaskAssemblyError("绑定参考图越出白底图目录")
            matches[resolved] = candidate
    if not matches:
        scan = _scan_reference_failure(manifest, filename)
        if scan.kind in {"missing_reference", "inputs_unavailable"}:
            raise _white_bg_failure("绑定参考图不在白底图目录中", scan)
        raise RenderTaskAssemblyError("绑定参考图必须在白底图目录中唯一匹配")
    if len(matches) != 1:
        raise RenderTaskAssemblyError("绑定参考图必须在白底图目录中唯一匹配")
    reference = next(iter(matches.values()))
    if reference.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
        raise RenderTaskAssemblyError("绑定参考图格式不受支持")
    try:
        with reference.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        scan = _scan_reference_failure(manifest, filename)
        if scan.kind == "available":
            scan = WhiteBgScan(
                kind="missing_reference",
                missing_files=(filename,),
                missing_count=1,
                remaining_count=0,
            )
        raise _white_bg_failure("绑定参考图无法读取", scan) from exc
    return reference


def assemble_render_tasks(
    manifest: Mapping[str, Any],
    final_prompt_index_path: Path,
) -> RenderTaskPlan:
    product_id = str(manifest.get("product_id") or "")
    try:
        expected_ids = manifest_config_ids(
            manifest,
            Path(__file__).resolve().parent.parent,
        )
    except ExecutorExecutionError:
        raise RenderTaskAssemblyError("manifest 图片张数无效") from None
    workspace = manifest.get("workspace")
    outputs = manifest.get("outputs")
    artifacts = manifest.get("artifacts")
    if not isinstance(workspace, Mapping) or not isinstance(outputs, Mapping) or not isinstance(artifacts, Mapping):
        raise RenderTaskAssemblyError("manifest 缺少 workspace、outputs 或 artifacts")
    outputs_root = _first_path(workspace.get("outputs_root"), "workspace.outputs_root")
    renders_dir = _first_path(outputs.get("renders"), "outputs.renders")
    final_dir = _first_path(artifacts.get("final_prompts"), "artifacts.final_prompts")
    if final_dir.suffix.lower() == ".json":
        final_dir = final_dir.parent
    if not _is_within(renders_dir, outputs_root):
        raise RenderTaskAssemblyError("renders 目录必须位于 workspace.outputs_root 内")
    if renders_dir.exists() and not renders_dir.is_dir():
        raise RenderTaskAssemblyError("renders 路径不是目录")
    if not _is_within(final_prompt_index_path, final_dir):
        raise RenderTaskAssemblyError("最终提示词索引不在声明的提示词目录内")

    index = _read_json(final_prompt_index_path, "最终提示词索引")
    items = index.get("items")
    if (
        index.get("artifact_type") != "final_prompt_index"
        or index.get("product_id") != product_id
        or index.get("uses_upstream_prompt_files_as_visual_requirements") is not False
        or not isinstance(items, list)
        or index.get("prompt_count") != len(items)
        or len(items) != len(expected_ids)
    ):
        raise RenderTaskAssemblyError("最终提示词索引契约无效")

    tasks: list[ImageGenerationTask] = []
    planned: list[str] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()
    observed_ids: list[str] = []
    seen_prompts: set[Path] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise RenderTaskAssemblyError("最终提示词索引项结构无效")
        config_id = str(item.get("config_id") or "")
        output_type = str(item.get("output_type") or "")
        if not re.fullmatch(r"(?:main|detail)_[0-9]{2}", config_id):
            raise RenderTaskAssemblyError("最终提示词索引 config_id 无效")
        if config_id in seen_ids:
            raise RenderTaskAssemblyError("最终提示词索引包含重复 config_id")
        seen_ids.add(config_id)
        observed_ids.append(config_id)
        if output_type not in OUTPUT_TYPE_TO_ASPECT or not config_id.startswith(f"{output_type}_"):
            raise RenderTaskAssemblyError("最终提示词索引输出类型无效")

        prompt_value = item.get("final_prompt_path")
        if not isinstance(prompt_value, str) or not prompt_value:
            raise RenderTaskAssemblyError("最终提示词索引缺少提示词路径")
        prompt_path = Path(prompt_value)
        resolved_prompt = prompt_path.resolve(strict=False)
        if (
            prompt_path.suffix.lower() != ".json"
            or not _is_within(prompt_path, final_dir)
            or resolved_prompt in seen_prompts
        ):
            raise RenderTaskAssemblyError("最终提示词路径越界或重复")
        seen_prompts.add(resolved_prompt)
        document = _read_json(prompt_path, f"最终提示词 {config_id}")
        variable = document.get("variable_config")
        if (
            document.get("product_id") != product_id
            or document.get("artifact_type") != "final_prompt"
            or document.get("uses_upstream_prompt_files_as_visual_requirements") is not False
            or not isinstance(variable, Mapping)
            or variable.get("config_id") != config_id
            or variable.get("output_type") != output_type
        ):
            raise RenderTaskAssemblyError("最终提示词与索引不一致")
        positive = document.get("final_prompt")
        negative = document.get("negative_prompt")
        if not isinstance(positive, str) or not positive.strip() or not isinstance(negative, str) or not negative.strip():
            raise RenderTaskAssemblyError("最终提示词正文或 negative_prompt 为空")
        reference = _reference_image(manifest, str(item.get("bound_reference") or ""))
        output_path = renders_dir / f"{config_id}.png"
        if not _is_within(output_path, renders_dir):
            raise RenderTaskAssemblyError("渲染输出路径越界")
        if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
            raise RenderTaskAssemblyError("渲染输出路径已被非普通文件占用")
        if output_path.is_file():
            skipped.append(config_id)
            continue
        aspect = OUTPUT_TYPE_TO_ASPECT[output_type]
        tasks.append(
            ImageGenerationTask(
                prompt=positive + NEGATIVE_PROMPT_SEPARATOR + negative,
                output_path=output_path,
                reference_images=(reference,),
                size=ASPECT_TO_IMAGE_SIZE[aspect],
                output_format="png",
            )
        )
        planned.append(config_id)

    if tuple(observed_ids) != expected_ids:
        raise RenderTaskAssemblyError("最终提示词索引与批次登记张数不一致")

    try:
        renders_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RenderTaskAssemblyError("无法创建 renders 目录") from exc
    return RenderTaskPlan(tasks=tuple(tasks), planned=tuple(planned), skipped=tuple(skipped))
