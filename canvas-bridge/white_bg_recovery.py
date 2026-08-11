"""Fail-closed white-background input recovery helpers for RB-01."""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SUPPORTED_WHITE_BG_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_SAFE_FILENAME_PATTERN = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9 _().（）\[\]-]{0,79}"
    r"(?:\.[\u3400-\u9fffA-Za-z0-9]{1,10})?"
)
_SENSITIVE_FILENAME_PATTERN = re.compile(
    r"(?:bearer|token|api[ _-]?key|authorization|password|secret|sk-|令牌|密钥|凭据)",
    flags=re.IGNORECASE,
)
_ARCHIVE_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")
_ARCHIVE_ARTIFACT_KEYS = (
    "angle_inventory",
    "main_variable_configs",
    "detail_variable_configs",
    "final_prompts",
    "comfyui_jobs",
    "qc_reports",
)
_REPO_INTEGRITY_KEY = "repo_final_prompt_integrity_report"
_MAX_ARCHIVE_ATTEMPTS = 8
_SET_ANGLE_LAYOUT_FILENAME = "set_angle_layout_inventory.json"


class WhiteBgRecoveryError(ValueError):
    """Recovery state or archival inputs are unsafe or inconsistent."""


@dataclass(frozen=True)
class WhiteBgScan:
    kind: str
    missing_files: tuple[str, ...]
    missing_count: int
    remaining_count: int


@dataclass(frozen=True)
class RecoveryEligibility:
    eligible: bool
    code: str
    message: str


@dataclass(frozen=True)
class RecoveryArchiveResult:
    superseded: tuple[str, ...]
    superseded_dir: str
    archive_id: str


def sanitize_filename(value: object) -> str | None:
    """Return a display-safe basename, or ``None`` for a count-only fallback."""

    if type(value) is not str:
        return None
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFC", basename).strip()
    if (
        not basename
        or len(basename) > 80
        or basename in {".", ".."}
        or _SAFE_FILENAME_PATTERN.fullmatch(basename) is None
        or _SENSITIVE_FILENAME_PATTERN.search(basename)
    ):
        return None
    return basename


def sanitize_filenames(values: Iterable[object]) -> tuple[str, ...]:
    """Sanitize a whole filename set; one unsafe token makes it count-only."""

    sanitized: list[str] = []
    seen: set[str] = set()
    for value in values:
        filename = sanitize_filename(value)
        if filename is None:
            return ()
        if filename not in seen:
            seen.add(filename)
            sanitized.append(filename)
    return tuple(sanitized)


def _path_values(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item.strip())


def _reference_input_roots(manifest: Mapping[str, Any]) -> tuple[Path, ...]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return ()
    if manifest.get("batch_type", "single") != "set":
        return _path_values(inputs.get("white_bg_images"))

    white_bg_roots = _path_values(inputs.get("white_bg_images"))
    group_roots = _path_values(inputs.get("set_group_images"))
    component_roots = _path_values(inputs.get("component_white_bg_images"))
    if not group_roots or not component_roots:
        return ()
    return (*white_bg_roots, *group_roots, *component_roots)


def _readable_supported_images(manifest: Mapping[str, Any]) -> tuple[Path, ...]:
    roots = _reference_input_roots(manifest)
    if not roots:
        return ()

    images: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix.lower() not in SUPPORTED_WHITE_BG_SUFFIXES:
                return ()
            candidates = (root,)
        elif root.is_dir():
            try:
                candidates = tuple(
                    item
                    for item in sorted(root.iterdir(), key=lambda item: item.name.lower())
                    if item.is_file()
                    and item.suffix.lower() in SUPPORTED_WHITE_BG_SUFFIXES
                )
            except OSError:
                return ()
        else:
            return ()
        for candidate in candidates:
            try:
                with candidate.open("rb") as handle:
                    handle.read(1)
            except OSError:
                continue
            images.append(candidate)
    return tuple(images)


def _bound_references_from_index(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    artifacts = manifest.get("artifacts")
    final_paths = _path_values(
        artifacts.get("final_prompts") if isinstance(artifacts, Mapping) else None
    )
    if not final_paths:
        raise WhiteBgRecoveryError("最终提示词索引位置不可用")
    final_path = final_paths[0]
    final_dir = final_path.parent if final_path.suffix.lower() == ".json" else final_path
    index_path = final_dir / "final_prompt_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WhiteBgRecoveryError("最终提示词索引无法读取") from exc
    items = index.get("items") if isinstance(index, Mapping) else None
    if not isinstance(items, list) or not items:
        raise WhiteBgRecoveryError("最终提示词索引结构无效")
    references: list[str] = []
    for item in items:
        value = item.get("bound_reference") if isinstance(item, Mapping) else None
        if type(value) is not str or not value or Path(value).name != value:
            raise WhiteBgRecoveryError("最终提示词绑定参考图无效")
        references.append(value)
    return tuple(references)


def _set_component_reference_filenames(
    manifest: Mapping[str, Any],
) -> tuple[str, ...]:
    artifacts = manifest.get("artifacts")
    paths = _path_values(
        artifacts.get("set_angle_layout_inventory")
        if isinstance(artifacts, Mapping)
        else None
    )
    if not paths:
        raise WhiteBgRecoveryError("套装角度与编排入库表位置不可用")
    declared = paths[0]
    inventory_path = (
        declared
        if declared.suffix.lower() == ".json"
        else declared / _SET_ANGLE_LAYOUT_FILENAME
    )
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WhiteBgRecoveryError("套装角度与编排入库表无法读取") from exc

    product_id = manifest.get("product_id")
    layouts = inventory.get("layouts") if isinstance(inventory, Mapping) else None
    if (
        not isinstance(inventory, Mapping)
        or inventory.get("artifact_type") != "set_angle_layout_inventory"
        or type(product_id) is not str
        or not product_id
        or inventory.get("product_id") != product_id
        or not isinstance(layouts, list)
    ):
        raise WhiteBgRecoveryError("套装角度与编排入库表契约无效")

    components: list[tuple[int, str]] = []
    seen_indexes: set[int] = set()
    for layout in layouts:
        if not isinstance(layout, Mapping):
            raise WhiteBgRecoveryError("套装角度与编排入库表 layouts 结构无效")
        image_index = layout.get("image_index")
        file_name = layout.get("file_name")
        is_set_group = layout.get("is_set_group")
        if (
            type(image_index) is not int
            or image_index <= 0
            or image_index in seen_indexes
            or type(file_name) is not str
            or not file_name
            or Path(file_name).name != file_name
            or type(is_set_group) is not bool
        ):
            raise WhiteBgRecoveryError("套装角度与编排入库表 layouts 结构无效")
        seen_indexes.add(image_index)
        if is_set_group is False:
            components.append((image_index, file_name))
    if not components:
        raise WhiteBgRecoveryError("套装角度与编排入库表未登记组成单件白底图")

    ordered = (file_name for _index, file_name in sorted(components))
    return tuple(dict.fromkeys(ordered))


def set_reference_filenames(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every required set reference in deterministic upload order."""

    if manifest.get("batch_type", "single") != "set":
        raise WhiteBgRecoveryError("当前批次不是套装批次")
    group_references = tuple(dict.fromkeys(_bound_references_from_index(manifest)))
    component_references = _set_component_reference_filenames(manifest)
    return (*group_references, *component_references)


def scan_white_bg_recovery(
    manifest: Mapping[str, Any],
    *,
    bound_references: Iterable[str] | None = None,
) -> WhiteBgScan:
    """Classify current disk state against the recorded prompt bindings."""

    remaining = _readable_supported_images(manifest)
    if not remaining:
        return WhiteBgScan(
            kind="inputs_unavailable",
            missing_files=(),
            missing_count=0,
            remaining_count=0,
        )
    references = (
        tuple(bound_references)
        if bound_references is not None
        else (
            set_reference_filenames(manifest)
            if manifest.get("batch_type", "single") == "set"
            else _bound_references_from_index(manifest)
        )
    )
    if not references or any(type(value) is not str or not value for value in references):
        raise WhiteBgRecoveryError("绑定参考图清单无效")
    by_name: dict[str, list[Path]] = {}
    for path in remaining:
        by_name.setdefault(path.name, []).append(path)
    if any(len(by_name.get(filename, ())) > 1 for filename in references):
        raise WhiteBgRecoveryError("绑定参考图在白底图目录中不是唯一文件")
    missing_raw = tuple(dict.fromkeys(
        filename for filename in references if not by_name.get(filename)
    ))
    if not missing_raw:
        return WhiteBgScan(
            kind="available",
            missing_files=(),
            missing_count=0,
            remaining_count=len(remaining),
        )
    return WhiteBgScan(
        kind="missing_reference",
        missing_files=sanitize_filenames(missing_raw),
        missing_count=len(missing_raw),
        remaining_count=len(remaining),
    )


def allows_rebind_recompute(batch_type: object) -> bool:
    """Only an explicitly declared single-product batch may discard an angle."""

    return type(batch_type) is str and batch_type == "single"


def evaluate_rebind_eligibility(
    scan: WhiteBgScan,
    ready_count: int,
    *,
    batch_type: object = "single",
) -> RecoveryEligibility:
    """Apply the side-effect-free RB-01 endpoint qualification rules."""

    if not allows_rebind_recompute(batch_type):
        return RecoveryEligibility(
            eligible=False,
            code="recompute_unsupported_for_set",
            message=(
                "套装每张白底图都是所有图片的必需参照，不能剔除后重排，"
                "请恢复缺失的白底图后重新开始。"
            ),
        )
    if type(ready_count) is not int or ready_count < 0:
        raise WhiteBgRecoveryError("成图计数无效")
    if ready_count:
        return RecoveryEligibility(
            eligible=False,
            code="render_outputs_exist",
            message=(
                f"本批已有 {ready_count} 张成图，不能整体重排。"
                "请恢复缺失文件后重新开始。"
            ),
        )
    if scan.kind == "available":
        return RecoveryEligibility(
            eligible=False,
            code="missing_files_restored",
            message="白底图已齐全，直接重新开始即可。",
        )
    if scan.kind == "inputs_unavailable":
        return RecoveryEligibility(
            eligible=False,
            code="inputs_unavailable",
            message="白底图目录整体无法访问，本次已停止。请恢复 inputs/white_bg 后再重新开始。",
        )
    if (
        scan.kind != "missing_reference"
        or type(scan.missing_count) is not int
        or scan.missing_count < 1
        or type(scan.remaining_count) is not int
        or scan.remaining_count < 1
    ):
        raise WhiteBgRecoveryError("白底图恢复分类无效")
    return RecoveryEligibility(
        eligible=True,
        code="eligible",
        message="可以剔除缺失图并重新分配。",
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _default_archive_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _remove_empty_tree(root: Path) -> None:
    if not root.exists():
        return
    try:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
    except OSError:
        directories = []
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def archive_recompute_artifacts(
    manifest: Mapping[str, Any],
    repository_root: Path,
    *,
    archive_id_factory: Callable[[], str] | None = None,
) -> RecoveryArchiveResult:
    """Atomically supersede derived artifacts, rolling back every OSError."""

    product_id = manifest.get("product_id")
    if (
        type(product_id) is not str
        or not product_id
        or Path(product_id).name != product_id
        or any(char in product_id for char in ("/", "\\", "\0"))
    ):
        raise WhiteBgRecoveryError("批次号无效")
    workspace = manifest.get("workspace")
    workspace_root_value = (
        workspace.get("root") if isinstance(workspace, Mapping) else None
    )
    artifacts_root_value = (
        workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
    )
    if (
        type(workspace_root_value) is not str
        or not workspace_root_value
        or type(artifacts_root_value) is not str
        or not artifacts_root_value
    ):
        raise WhiteBgRecoveryError("批次派生产物目录无效")
    workspace_root = Path(workspace_root_value).resolve(strict=False)
    artifacts_root = Path(artifacts_root_value).resolve(strict=False)
    if artifacts_root == workspace_root or not _inside(artifacts_root, workspace_root):
        raise WhiteBgRecoveryError("批次派生产物目录越出工作区")
    repository_root = repository_root.resolve(strict=False)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise WhiteBgRecoveryError("批次派生产物声明无效")

    source_keys: dict[Path, list[str]] = {}
    source_paths: dict[Path, Path] = {}
    for key in _ARCHIVE_ARTIFACT_KEYS:
        for source in _path_values(artifacts.get(key)):
            resolved = source.resolve(strict=False)
            if source.is_symlink():
                raise WhiteBgRecoveryError(f"{key} 不能通过符号链接归档")
            if not source.exists():
                continue
            if not _inside(source, artifacts_root) or resolved == artifacts_root:
                raise WhiteBgRecoveryError(f"{key} 越出批次派生产物目录")
            source_paths.setdefault(resolved, source)
            source_keys.setdefault(resolved, []).append(key)

    resolved_sources = tuple(source_paths)
    if any(
        left != right and (_inside(left, right) or _inside(right, left))
        for index, left in enumerate(resolved_sources)
        for right in resolved_sources[index + 1 :]
    ):
        raise WhiteBgRecoveryError("派生产物归档范围发生嵌套")

    repo_report = repository_root / "reports" / f"{product_id}_final_prompt_integrity_report.json"
    if repo_report.is_symlink():
        raise WhiteBgRecoveryError("仓库完整性报告不能通过符号链接归档")
    repo_report_exists = repo_report.is_file()
    batch_superseded_root = artifacts_root / "_superseded"
    repo_superseded_root = repository_root / "reports" / "_superseded"
    if batch_superseded_root.is_symlink() or (
        batch_superseded_root.exists() and not batch_superseded_root.is_dir()
    ):
        raise WhiteBgRecoveryError("批次归档目录不可用")
    if repo_superseded_root.is_symlink() or (
        repo_superseded_root.exists() and not repo_superseded_root.is_dir()
    ):
        raise WhiteBgRecoveryError("仓库报告归档目录不可用")
    if repo_report_exists and not _inside(repo_report, repository_root):
        raise WhiteBgRecoveryError("仓库完整性报告越出仓库目录")
    batch_superseded_existed = batch_superseded_root.exists()
    repo_superseded_existed = repo_superseded_root.exists()
    id_factory = archive_id_factory or _default_archive_id

    for _attempt in range(_MAX_ARCHIVE_ATTEMPTS):
        archive_id = id_factory()
        if type(archive_id) is not str or _ARCHIVE_ID_PATTERN.fullmatch(archive_id) is None:
            raise WhiteBgRecoveryError("归档编号无效")
        batch_archive = batch_superseded_root / archive_id
        repo_archive = repo_superseded_root / archive_id
        if (
            batch_archive.exists()
            or batch_archive.is_symlink()
            or (
                repo_report_exists
                and (repo_archive.exists() or repo_archive.is_symlink())
            )
        ):
            continue

        moved: list[tuple[Path, Path]] = []
        batch_archive_created = False
        repo_archive_created = False
        try:
            batch_archive.mkdir(parents=True, exist_ok=False)
            batch_archive_created = True
            if repo_report_exists:
                repo_archive.mkdir(parents=True, exist_ok=False)
                repo_archive_created = True
            for resolved, source in source_paths.items():
                relative = resolved.relative_to(artifacts_root)
                destination = batch_archive / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, destination)
                moved.append((source, destination))
            if repo_report_exists:
                destination = repo_archive / repo_report.name
                os.rename(repo_report, destination)
                moved.append((repo_report, destination))
        except OSError as exc:
            rollback_error: OSError | None = None
            for source, destination in reversed(moved):
                try:
                    os.rename(destination, source)
                except OSError as rollback_exc:
                    rollback_error = rollback_error or rollback_exc
            if batch_archive_created:
                _remove_empty_tree(batch_archive)
            if repo_archive_created:
                _remove_empty_tree(repo_archive)
            if not batch_superseded_existed:
                try:
                    batch_superseded_root.rmdir()
                except OSError:
                    pass
            if not repo_superseded_existed:
                try:
                    repo_superseded_root.rmdir()
                except OSError:
                    pass
            if rollback_error is not None:
                raise WhiteBgRecoveryError("派生产物归档失败且无法完整回滚") from rollback_error
            if isinstance(exc, FileExistsError):
                continue
            raise WhiteBgRecoveryError("派生产物归档失败，已恢复原状") from exc

        superseded = [
            key
            for key in _ARCHIVE_ARTIFACT_KEYS
            if any(key in keys for keys in source_keys.values())
        ]
        if repo_report_exists:
            superseded.append(_REPO_INTEGRITY_KEY)
        return RecoveryArchiveResult(
            superseded=tuple(superseded),
            superseded_dir=(Path("artifacts") / "_superseded" / archive_id).as_posix(),
            archive_id=archive_id,
        )
    raise WhiteBgRecoveryError("无法分配不冲突的归档目录")


def rollback_recompute_archive(
    manifest: Mapping[str, Any],
    repository_root: Path,
    archived: RecoveryArchiveResult,
) -> None:
    """Restore one verified recovery archive without overwriting live sources."""

    if not isinstance(archived, RecoveryArchiveResult):
        raise WhiteBgRecoveryError("归档回滚凭据无效")
    archive_id = archived.archive_id
    if type(archive_id) is not str or _ARCHIVE_ID_PATTERN.fullmatch(archive_id) is None:
        raise WhiteBgRecoveryError("归档回滚编号无效")
    expected_relative = (
        Path("artifacts") / "_superseded" / archive_id
    ).as_posix()
    if archived.superseded_dir != expected_relative:
        raise WhiteBgRecoveryError("归档回滚目录声明无效")
    superseded = archived.superseded
    allowed_keys = frozenset((*_ARCHIVE_ARTIFACT_KEYS, _REPO_INTEGRITY_KEY))
    if (
        not isinstance(superseded, tuple)
        or not superseded
        or any(type(key) is not str or key not in allowed_keys for key in superseded)
        or len(superseded) != len(set(superseded))
    ):
        raise WhiteBgRecoveryError("归档回滚产物清单无效")

    product_id = manifest.get("product_id")
    if (
        type(product_id) is not str
        or not product_id
        or Path(product_id).name != product_id
        or any(char in product_id for char in ("/", "\\", "\0"))
    ):
        raise WhiteBgRecoveryError("批次号无效")
    workspace = manifest.get("workspace")
    workspace_root_value = (
        workspace.get("root") if isinstance(workspace, Mapping) else None
    )
    artifacts_root_value = (
        workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
    )
    artifacts = manifest.get("artifacts")
    if (
        type(workspace_root_value) is not str
        or not workspace_root_value
        or type(artifacts_root_value) is not str
        or not artifacts_root_value
        or not isinstance(artifacts, Mapping)
    ):
        raise WhiteBgRecoveryError("批次派生产物目录无效")

    try:
        workspace_root = Path(workspace_root_value).resolve(strict=False)
        artifacts_root = Path(artifacts_root_value).resolve(strict=False)
        if artifacts_root == workspace_root or not _inside(artifacts_root, workspace_root):
            raise WhiteBgRecoveryError("批次派生产物目录越出工作区")
        repository_root = repository_root.resolve(strict=False)
        batch_archive = artifacts_root / "_superseded" / archive_id
        reports_root = repository_root / "reports"
        repo_archive = reports_root / "_superseded" / archive_id
        if (
            batch_archive.is_symlink()
            or not batch_archive.is_dir()
            or not _inside(batch_archive, artifacts_root / "_superseded")
        ):
            raise WhiteBgRecoveryError("批次归档回滚目录不可用")
        if _REPO_INTEGRITY_KEY in superseded and (
            repo_archive.is_symlink()
            or not repo_archive.is_dir()
            or not _inside(repo_archive, reports_root / "_superseded")
        ):
            raise WhiteBgRecoveryError("仓库报告归档回滚目录不可用")

        moves: dict[Path, Path] = {}
        for key in superseded:
            if key == _REPO_INTEGRITY_KEY:
                source = reports_root / f"{product_id}_final_prompt_integrity_report.json"
                destination = repo_archive / source.name
                if source.exists() or source.is_symlink():
                    raise WhiteBgRecoveryError("仓库完整性报告已存在，拒绝覆盖")
                if destination.is_symlink() or not destination.is_file():
                    raise WhiteBgRecoveryError("仓库完整性报告归档缺失")
                if not _inside(source, repository_root) or not _inside(destination, repo_archive):
                    raise WhiteBgRecoveryError("仓库完整性报告归档越界")
                moves[destination] = source
                continue

            declared = _path_values(artifacts.get(key))
            if not declared:
                raise WhiteBgRecoveryError(f"{key} 归档回滚声明缺失")
            found_destination = False
            for declared_source in declared:
                source = declared_source.resolve(strict=False)
                if (
                    declared_source.is_symlink()
                    or not _inside(source, artifacts_root)
                    or source == artifacts_root
                ):
                    raise WhiteBgRecoveryError(f"{key} 归档回滚范围无效")
                destination = batch_archive / source.relative_to(artifacts_root)
                if source.exists() or source.is_symlink():
                    raise WhiteBgRecoveryError(f"{key} 原位置已存在，拒绝覆盖")
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not _inside(destination, batch_archive):
                        raise WhiteBgRecoveryError(f"{key} 归档回滚来源无效")
                    previous = moves.get(destination)
                    if previous is not None and previous != source:
                        raise WhiteBgRecoveryError("归档回滚来源映射冲突")
                    moves[destination] = source
                    found_destination = True
            if not found_destination:
                raise WhiteBgRecoveryError(f"{key} 归档回滚来源缺失")
    except WhiteBgRecoveryError:
        raise
    except OSError as exc:
        raise WhiteBgRecoveryError("归档回滚校验失败") from exc

    restored: list[tuple[Path, Path]] = []
    try:
        for destination, source in reversed(tuple(moves.items())):
            if not source.parent.is_dir():
                raise OSError(f"restore parent unavailable: {source.parent.name}")
            os.rename(destination, source)
            restored.append((source, destination))
    except OSError as exc:
        replay_error: OSError | None = None
        for source, destination in reversed(restored):
            try:
                os.rename(source, destination)
            except OSError as replay_exc:
                replay_error = replay_error or replay_exc
        if replay_error is not None:
            raise WhiteBgRecoveryError("归档回滚失败且无法恢复归档状态") from replay_error
        raise WhiteBgRecoveryError("归档回滚失败，归档状态已保持") from exc

    _remove_empty_tree(batch_archive)
    if _REPO_INTEGRITY_KEY in superseded:
        _remove_empty_tree(repo_archive)
