"""Fail-closed relocation for portable batch manifests pinned to an old root."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
import run_controller


@dataclass(frozen=True)
class _RelocationCandidate:
    manifest_path: Path
    manifest: dict[str, Any]
    old_install_root: Path
    new_install_root: Path
    needs_relocation: bool


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())
    except OSError:
        return True


def _same_path(first: Path, second: Path) -> bool:
    try:
        return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
            str(second.resolve(strict=False))
        )
    except (OSError, RuntimeError):
        return False


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _candidate(
    repository_root: Path,
    batch_id: str,
) -> _RelocationCandidate | None:
    manifest_path = repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
    manifest = _read_json_object(manifest_path)
    if manifest is None or manifest.get("product_id") != batch_id:
        return None

    workspace = manifest.get("workspace")
    root_value = workspace.get("root") if isinstance(workspace, dict) else None
    if not isinstance(root_value, str) or not root_value:
        return None
    old_workspace = Path(root_value)
    if (
        not old_workspace.is_absolute()
        or old_workspace.name != batch_id
        or not old_workspace.parent.name
    ):
        return None

    parent_name = old_workspace.parent.name
    old_install_root = old_workspace.parent.parent
    try:
        new_install_root = repository_root.resolve(strict=False).parent
    except (OSError, RuntimeError):
        return None
    new_workspace = new_install_root / parent_name / batch_id
    if _same_path(old_workspace, new_workspace):
        return _RelocationCandidate(
            manifest_path=manifest_path,
            manifest=manifest,
            old_install_root=old_install_root,
            new_install_root=new_install_root,
            needs_relocation=False,
        )

    try:
        workspace_safe = new_workspace.is_dir() and not _is_reparse(new_workspace)
    except OSError:
        workspace_safe = False
    if not workspace_safe:
        return None
    marker_path = new_workspace / ".canvas_batch"
    if _is_reparse(marker_path):
        return None
    marker = _read_json_object(marker_path)
    if (
        marker is None
        or marker.get("type") != "canvas-batch-v1"
        or marker.get("product_id") != batch_id
    ):
        return None
    return _RelocationCandidate(
        manifest_path=manifest_path,
        manifest=manifest,
        old_install_root=old_install_root,
        new_install_root=new_install_root,
        needs_relocation=True,
    )


def _replace_install_root(
    value: str,
    *,
    old_install_root: str,
    new_install_root: str,
) -> tuple[str, bool]:
    normalized_value = os.path.normcase(value)
    normalized_old = os.path.normcase(old_install_root)
    if not normalized_value.startswith(normalized_old):
        return value, False
    suffix = value[len(old_install_root) :]
    if (
        suffix
        and suffix[0] not in {"/", "\\"}
        and old_install_root[-1:] not in {"/", "\\"}
    ):
        return value, False
    return new_install_root + suffix, True


def _relocate_string_values(
    value: Any,
    *,
    old_install_root: str,
    new_install_root: str,
) -> tuple[Any, int]:
    if isinstance(value, str):
        relocated, changed = _replace_install_root(
            value,
            old_install_root=old_install_root,
            new_install_root=new_install_root,
        )
        return relocated, int(changed)
    if isinstance(value, list):
        relocated_items: list[Any] = []
        replaced_count = 0
        for item in value:
            relocated, count = _relocate_string_values(
                item,
                old_install_root=old_install_root,
                new_install_root=new_install_root,
            )
            relocated_items.append(relocated)
            replaced_count += count
        return relocated_items, replaced_count
    if isinstance(value, dict):
        relocated_object: dict[str, Any] = {}
        replaced_count = 0
        for key, item in value.items():
            relocated, count = _relocate_string_values(
                item,
                old_install_root=old_install_root,
                new_install_root=new_install_root,
            )
            relocated_object[key] = relocated
            replaced_count += count
        return relocated_object, replaced_count
    return value, 0


def _atomic_write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            file_descriptor = -1
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _relocate_manifest_if_moved(repository_root: Path, batch_id: str) -> int:
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or Path(batch_id).name != batch_id
        or any(char in batch_id for char in ("/", "\\", "\0", "\r", "\n"))
    ):
        return 0
    root = Path(repository_root)
    initial = _candidate(root, batch_id)
    if initial is None or not initial.needs_relocation:
        return 0

    try:
        with BatchOperationLock(batch_id):
            candidate = _candidate(root, batch_id)
            if candidate is None or not candidate.needs_relocation:
                return 0
            old_install_root = str(candidate.old_install_root)
            new_install_root = str(candidate.new_install_root)
            relocated, replaced_count = _relocate_string_values(
                candidate.manifest,
                old_install_root=old_install_root,
                new_install_root=new_install_root,
            )
            if not isinstance(relocated, dict) or replaced_count <= 0:
                return 0
            _atomic_write_manifest(candidate.manifest_path, relocated)
            try:
                run_controller.append_event(
                    run_controller.journal_path(candidate.manifest_path, batch_id),
                    "workspace_relocated",
                    old_install_root=old_install_root,
                    new_install_root=new_install_root,
                    replaced_count=replaced_count,
                )
            except Exception:
                pass
            return replaced_count
    except (BatchOperationBusy, BatchOperationLockUnavailable):
        return 0


def relocate_manifest_if_moved(repository_root: Path, batch_id: str) -> int:
    """Repair a verified moved manifest, returning the number of replaced values.

    Every failure is deliberately silent so existing callers retain their original
    error contracts and may retry relocation the next time the batch is touched.
    """

    try:
        return _relocate_manifest_if_moved(repository_root, batch_id)
    except Exception:
        return 0
