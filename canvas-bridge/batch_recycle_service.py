"""Ledger-first, all-or-nothing batch recycle and CLI-only restore."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import ic_client
import run_controller
from batch_recycle_canvas import clear_batch_canvas_nodes
from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
from batch_recycle_state import (
    LOCK_UNAVAILABLE_MESSAGE,
    RECYCLED_EVENT,
    RESTORED_EVENT,
    BatchLifecycle,
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
from manifest_relocation import relocate_manifest_if_moved


CANVAS_UNAVAILABLE_MESSAGE = (
    "批次已冻结，画布节点与目录尚未处理，请启动画布后重跑同一条回收命令。"
)
PERMISSION_MESSAGE = (
    "有程序正在打开该批次的文件（看图软件、资源管理器预览窗格、其他窗口），"
    "请全部关闭后重试。已中止，未做任何目录改动。"
)
DESTINATION_EXISTS_MESSAGE = "回收站中已存在同名目标，未做任何改动。"


class BatchRecycleError(RuntimeError):
    def __init__(self, message: str, *, code: str = "recycle_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BatchRecycleResult:
    batch_id: str
    status: str
    workspace_source: Path
    workspace_target: Path
    request_id: str
    deleted_canvas_nodes: int = 0
    resumed: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _target_stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    try:
        return bool(checker and checker())
    except OSError:
        return True


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
        str(second.resolve(strict=False))
    )


class BatchRecycleService:
    def __init__(
        self,
        repository_root: Path,
        *,
        client: Any = ic_client,
        lock_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.client = client
        self.lock_root = lock_root
        self.clock = clock or _utc_now
        self.request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)

    @staticmethod
    def _validate_batch_id(batch_id: str) -> str:
        if (
            not isinstance(batch_id, str)
            or not batch_id
            or Path(batch_id).name != batch_id
            or any(char in batch_id for char in ("/", "\\", "\0", "\r", "\n"))
        ):
            raise BatchRecycleError("批次号无效。", code="batch_id_invalid")
        return batch_id

    def _manifest_context(
        self,
        batch_id: str,
    ) -> tuple[dict[str, Any], Path, Path, Path]:
        batch_id = self._validate_batch_id(batch_id)
        manifest_path = (
            self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        )
        relocate_manifest_if_moved(self.repository_root, batch_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BatchRecycleError("找不到这个批次。", code="batch_not_found") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise BatchRecycleError(
                "批次清单暂时无法读取。", code="manifest_unavailable"
            ) from None
        if not isinstance(manifest, dict) or manifest.get("product_id") != batch_id:
            raise BatchRecycleError(
                "批次清单与批次号不一致。", code="manifest_mismatch"
            )
        workspace_value = (
            (manifest.get("workspace") or {}).get("root")
            if isinstance(manifest.get("workspace"), Mapping)
            else None
        )
        if not isinstance(workspace_value, str) or not workspace_value:
            raise BatchRecycleError(
                "批次工作区信息缺失。", code="workspace_invalid"
            )
        workspace = Path(workspace_value)
        if not workspace.is_absolute():
            raise BatchRecycleError(
                "批次工作区必须是绝对路径。", code="workspace_invalid"
            )
        journal = run_controller.journal_path(manifest_path, batch_id)
        # Keep the manifest's lexical absolute path until link/junction checks
        # finish. Resolving here would hide a workspace link and could make the
        # later atomic rename operate on its target instead of rejecting it.
        return manifest, manifest_path, workspace, journal

    @staticmethod
    def _validate_workspace_marker(workspace: Path, batch_id: str) -> None:
        try:
            if (
                not workspace.is_dir()
                or workspace.is_symlink()
                or _is_junction(workspace)
            ):
                raise OSError
            marker = json.loads(
                (workspace / ".canvas_batch").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise BatchRecycleError(
                "批次安全标记无效，未移动任何目录。",
                code="workspace_marker_invalid",
            ) from None
        if (
            not isinstance(marker, dict)
            or marker.get("type") != "canvas-batch-v1"
            or marker.get("product_id") != batch_id
        ):
            raise BatchRecycleError(
                "批次安全标记与批次号不一致，未移动任何目录。",
                code="workspace_marker_invalid",
            )

    @staticmethod
    def _lifecycle(journal: Path) -> BatchLifecycle:
        try:
            return read_batch_lifecycle(journal)
        except BatchLifecycleReadError as exc:
            raise BatchRecycleError(
                str(exc), code="journal_unavailable"
            ) from None

    @staticmethod
    def _event_paths(
        event: Mapping[str, Any],
        *,
        batch_id: str,
        manifest_workspace: Path,
    ) -> tuple[Path, Path]:
        source_value = event.get("workspace_source")
        target_value = event.get("workspace_target")
        if (
            not isinstance(source_value, str)
            or not source_value
            or not isinstance(target_value, str)
            or not target_value
        ):
            raise BatchRecycleError(
                "回收账本缺少目录定位信息，请保留现场并交由顾问核对。",
                code="recycle_event_invalid",
            )
        source = Path(source_value)
        target = Path(target_value)
        expected_root = manifest_workspace.parent / "_回收站"
        try:
            invalid_boundary = (
                not source.is_absolute()
                or not target.is_absolute()
                or manifest_workspace.is_symlink()
                or _is_junction(manifest_workspace)
                or source.is_symlink()
                or _is_junction(source)
                or target.is_symlink()
                or _is_junction(target)
                or target.parent.is_symlink()
                or _is_junction(target.parent)
                or not _same_path(source, manifest_workspace)
                or not _same_path(target.parent, expected_root)
                or not target.name.startswith(f"{batch_id}__")
            )
        except (OSError, RuntimeError):
            invalid_boundary = True
        if invalid_boundary:
            raise BatchRecycleError(
                "回收账本中的目录边界无效，请保留现场并交由顾问核对。",
                code="recycle_event_invalid",
            )
        # Boundary comparisons may resolve only after the event paths themselves
        # have passed link checks. The lexical paths are the operands for rename.
        return source, target

    @staticmethod
    def _translate_rename_error(exc: OSError, *, restoring: bool) -> BatchRecycleError:
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
            return BatchRecycleError(PERMISSION_MESSAGE, code="workspace_in_use")
        if isinstance(exc, FileExistsError):
            message = (
                "原工作区位置已存在，未做任何改动。"
                if restoring
                else DESTINATION_EXISTS_MESSAGE
            )
            return BatchRecycleError(message, code="destination_exists")
        error_code = getattr(exc, "winerror", None)
        if error_code is None:
            error_code = getattr(exc, "errno", None)
        return BatchRecycleError(
            f"目录改名失败（错误码 {error_code}），已中止，未做任何改动。",
            code="rename_failed",
        )

    def recycle(
        self,
        batch_id: str,
        *,
        source_entry: str = "cli",
    ) -> BatchRecycleResult:
        if source_entry not in {"cli", "workbench"}:
            raise BatchRecycleError("回收入口无效。", code="source_entry_invalid")
        _manifest, _manifest_path, manifest_workspace, journal = (
            self._manifest_context(batch_id)
        )
        request_id = self.request_id_factory()
        try:
            with BatchOperationLock(batch_id, lock_root=self.lock_root):
                lifecycle = self._lifecycle(journal)
                resumed = lifecycle.recycled
                if lifecycle.recycled:
                    event = lifecycle.active_recycled_event
                    if event is None:
                        raise BatchRecycleError(
                            "回收状态无法核对。", code="recycle_event_invalid"
                        )
                    source, target = self._event_paths(
                        event,
                        batch_id=batch_id,
                        manifest_workspace=manifest_workspace,
                    )
                    request_id = str(event.get("request_id") or request_id)
                    if not source.exists():
                        if target.is_dir():
                            self._validate_workspace_marker(target, batch_id)
                            return BatchRecycleResult(
                                batch_id=batch_id,
                                status="recycled",
                                workspace_source=source,
                                workspace_target=target,
                                request_id=request_id,
                                resumed=True,
                            )
                        raise BatchRecycleError(
                            "批次已冻结，但原工作区与回收目标都不存在；请保留现场并交由顾问核对。",
                            code="workspace_missing",
                        )
                else:
                    self._validate_workspace_marker(manifest_workspace, batch_id)
                    operated_at = self.clock()
                    source = manifest_workspace
                    target = (
                        source.parent
                        / "_回收站"
                        / f"{batch_id}__{_target_stamp(operated_at)}"
                    )
                    try:
                        run_controller.append_event(
                            journal,
                            RECYCLED_EVENT,
                            batch_id=batch_id,
                            request_id=request_id,
                            source_entry=source_entry,
                            operation_at_utc=_utc_text(operated_at),
                            workspace_source=str(source),
                            workspace_target=str(target),
                        )
                    except OSError:
                        raise BatchRecycleError(
                            "回收事件无法写入，操作已停止，目录未搬动。",
                            code="journal_write_failed",
                        ) from None

                try:
                    deleted = clear_batch_canvas_nodes(self.client, batch_id)
                except ic_client.CanvasAgentError:
                    raise BatchRecycleError(
                        CANVAS_UNAVAILABLE_MESSAGE,
                        code="canvas_unavailable",
                    ) from None
                except Exception:
                    raise BatchRecycleError(
                        "批次已冻结，画布节点清理失败，目录尚未处理；请保留现场后重跑同一条回收命令。",
                        code="canvas_cleanup_failed",
                    ) from None

                self._validate_workspace_marker(source, batch_id)
                try:
                    target.parent.mkdir(parents=False, exist_ok=True)
                    if (
                        not target.parent.is_dir()
                        or target.parent.is_symlink()
                        or _is_junction(target.parent)
                    ):
                        raise OSError
                except OSError:
                    raise BatchRecycleError(
                        "批次已冻结，但回收站目录无法使用；原工作区未搬动，请处理后重跑。",
                        code="recycle_root_unavailable",
                    ) from None
                try:
                    # Never replace this with shutil.move/copytree or Move-Item:
                    # their copy fallback can leave source plus a partial target.
                    os.rename(source, target)
                except OSError as exc:
                    raise self._translate_rename_error(
                        exc, restoring=False
                    ) from None
                return BatchRecycleResult(
                    batch_id=batch_id,
                    status="recycled",
                    workspace_source=source,
                    workspace_target=target,
                    request_id=request_id,
                    deleted_canvas_nodes=len(deleted),
                    resumed=resumed,
                )
        except BatchOperationBusy:
            raise BatchRecycleError(
                "本批次有任务正在运行，回收已拒绝，未写入任何事件。",
                code="batch_busy",
            ) from None
        except BatchOperationLockUnavailable:
            raise BatchRecycleError(
                LOCK_UNAVAILABLE_MESSAGE,
                code="lock_unavailable",
            ) from None

    def restore(self, batch_id: str) -> BatchRecycleResult:
        _manifest, _manifest_path, manifest_workspace, journal = (
            self._manifest_context(batch_id)
        )
        request_id = self.request_id_factory()
        try:
            with BatchOperationLock(batch_id, lock_root=self.lock_root):
                lifecycle = self._lifecycle(journal)
                event = (
                    lifecycle.active_recycled_event
                    if lifecycle.recycled
                    else lifecycle.last_recycled_event
                )
                if event is None:
                    raise BatchRecycleError(
                        "这个批次没有可还原的回收记录。",
                        code="restore_not_available",
                    )
                source, target = self._event_paths(
                    event,
                    batch_id=batch_id,
                    manifest_workspace=manifest_workspace,
                )
                recycle_request_id = str(event.get("request_id") or "")
                if not lifecycle.recycled:
                    if source.is_dir() and not target.exists():
                        return BatchRecycleResult(
                            batch_id=batch_id,
                            status="restored",
                            workspace_source=target,
                            workspace_target=source,
                            request_id=str(
                                (lifecycle.last_restored_event or {}).get(
                                    "request_id"
                                )
                                or request_id
                            ),
                            resumed=True,
                        )
                    raise BatchRecycleError(
                        "这个批次已经还原，当前目录状态需要人工核对。",
                        code="already_restored",
                    )

                directory_already_restored = not target.exists() and source.is_dir()
                if target.exists():
                    self._validate_workspace_marker(target, batch_id)
                    try:
                        # Restore is also a single same-volume rename; never
                        # introduce a file-by-file copy fallback here.
                        os.rename(target, source)
                    except OSError as exc:
                        raise self._translate_rename_error(
                            exc, restoring=True
                        ) from None
                elif source.is_dir():
                    self._validate_workspace_marker(source, batch_id)
                else:
                    raise BatchRecycleError(
                        "回收目录与原工作区都不存在，还原已停止，请保留现场并交由顾问核对。",
                        code="workspace_missing",
                    )

                operated_at = self.clock()
                try:
                    run_controller.append_event(
                        journal,
                        RESTORED_EVENT,
                        batch_id=batch_id,
                        request_id=request_id,
                        source_entry="cli",
                        operation_at_utc=_utc_text(operated_at),
                        workspace_source=str(target),
                        workspace_target=str(source),
                        recycle_request_id=recycle_request_id,
                    )
                except OSError:
                    raise BatchRecycleError(
                        "目录已还原，但还原事件写入失败；请勿开始生产，重跑同一条还原命令补记账本。",
                        code="restore_event_write_failed",
                    ) from None
                return BatchRecycleResult(
                    batch_id=batch_id,
                    status="restored",
                    workspace_source=target,
                    workspace_target=source,
                    request_id=request_id,
                    resumed=directory_already_restored,
                )
        except BatchOperationBusy:
            raise BatchRecycleError(
                "本批次有任务正在运行，还原已拒绝，未写入任何事件。",
                code="batch_busy",
            ) from None
        except BatchOperationLockUnavailable:
            raise BatchRecycleError(
                LOCK_UNAVAILABLE_MESSAGE,
                code="lock_unavailable",
            ) from None
