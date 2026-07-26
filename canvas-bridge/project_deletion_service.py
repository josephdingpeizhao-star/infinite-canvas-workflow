"""Fail-closed backend cleanup for project-owned registered batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import batch_creator
from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
from windows_recycle_bin import RecycleBinError, WindowsRecycleBinExecutor


MAX_BATCHES = 100
MAX_PROJECT_DELETION_REQUEST_ID_LENGTH = 8192
PREVIEW_TTL_SECONDS = 300.0
REPORT_SUFFIXES = (
    "_final_prompt_integrity_report.json",
    "_final_prompt_integrity_report.md",
    "_qc_report.json",
)
_RECYCLE_STAMP = re.compile(r"^\d{8}T\d{12}Z$")
_REQUEST_PREFIX = "pd1"
_INSTANCE_COMMIT_LENGTH = 32
_SNAPSHOT_COMMIT_LENGTH = 32
_REQUEST_PART_LENGTH = _INSTANCE_COMMIT_LENGTH + _SNAPSHOT_COMMIT_LENGTH
_LOWER_HEX = frozenset("0123456789abcdef")


def project_deletion_instance_commit(
    batch_id: str,
    manifest_identity_sha256: str,
) -> str:
    payload = (
        batch_id.encode("utf-8")
        + b"\0"
        + manifest_identity_sha256.encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()[:_INSTANCE_COMMIT_LENGTH]


def project_deletion_request_parts(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or len(value) > MAX_PROJECT_DELETION_REQUEST_ID_LENGTH
    ):
        return ()
    pieces = value.split(".")
    if (
        not pieces
        or pieces[0] != _REQUEST_PREFIX
        or not 1 <= len(pieces) - 1 <= MAX_BATCHES
    ):
        return ()
    parts = tuple(pieces[1:])
    if any(
        len(part) != _REQUEST_PART_LENGTH
        or any(character not in _LOWER_HEX for character in part)
        for part in parts
    ):
        return ()
    return parts


def valid_project_deletion_request_id(value: Any) -> bool:
    return bool(project_deletion_request_parts(value))


def project_deletion_request_has_instance(
    request_id: Any,
    instance_commit: str,
) -> bool:
    return (
        isinstance(instance_commit, str)
        and len(instance_commit) == _INSTANCE_COMMIT_LENGTH
        and all(character in _LOWER_HEX for character in instance_commit)
        and any(
            part.startswith(instance_commit)
            for part in project_deletion_request_parts(request_id)
        )
    )


class ProjectDeletionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "project_deletion_rejected",
        batch_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.batch_id = batch_id


@dataclass(frozen=True)
class _Target:
    path: Path
    kind: str
    batch_id: str
    request_id: str = ""


@dataclass(frozen=True)
class _Inventory:
    batch_id: str
    status: str
    closed: bool
    delivered: bool
    recycled: bool
    audited: bool
    request_id: str
    manifest_identity_sha256: str
    instance_commit: str
    targets: tuple[_Target, ...]


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


def _present(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


class ProjectDeletionService:
    def __init__(
        self,
        repository_root: Path,
        *,
        workspace_parent: Path,
        state_root: Path,
        audit_ledger: Any,
        recycle_executor: Callable[[Path], None] | None = None,
        lock_root: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.manifests_root = self.repository_root / "manifests"
        self.reports_root = self.repository_root / "reports"
        self.workspace_parent = Path(workspace_parent).resolve()
        self.state_root = batch_creator.require_state_root(Path(state_root))
        self.audit_ledger = audit_ledger
        self.recycle_executor = recycle_executor or WindowsRecycleBinExecutor()
        self.lock_root = lock_root
        self.clock = clock or time.monotonic
        self._tickets: dict[
            str,
            tuple[
                float,
                tuple[str, ...],
                tuple[tuple[Any, ...], ...],
            ],
        ] = {}
        self._ticket_lock = threading.Lock()
        self._require_static_roots()

    def _require_static_roots(self) -> None:
        for path, message in (
            (self.repository_root, "项目目录不可用，删除服务未启动。"),
            (self.manifests_root, "批次清单目录不可用，删除服务未启动。"),
            (self.workspace_parent, "批次工作区父目录不可用，删除服务未启动。"),
        ):
            try:
                safe = path.is_dir() and not _is_reparse(path)
            except OSError:
                safe = False
            if not safe:
                raise ProjectDeletionError(message, code="root_unavailable")
        if _present(self.reports_root) and (
            not self.reports_root.is_dir()
            or _is_reparse(self.reports_root)
        ):
            raise ProjectDeletionError(
                "批次报告目录不可用，删除服务未启动。",
                code="root_unavailable",
            )

    def _require_runtime_roots(self) -> None:
        self._require_static_roots()
        try:
            state_root = batch_creator.require_state_root(self.state_root)
        except batch_creator.BatchCreationError:
            raise ProjectDeletionError(
                "批次登记状态目录不可用，删除已停止。",
                code="root_unavailable",
            ) from None
        if not _same_path(state_root, self.state_root):
            raise ProjectDeletionError(
                "批次登记状态目录发生变化，删除已停止。",
                code="root_unavailable",
            )

    @staticmethod
    def _batch_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value in {".", ".."}
            or Path(value).name != value
            or any(char in value for char in ("/", "\\", "\0", "\r", "\n"))
            or len(value) > 120
        ):
            raise ProjectDeletionError("批次号无效。", code="batch_id_invalid")
        return value

    @classmethod
    def _batch_ids(cls, values: Any) -> tuple[str, ...]:
        if not isinstance(values, list) or not values or len(values) > MAX_BATCHES:
            raise ProjectDeletionError(
                "批次清单无效。",
                code="batch_list_invalid",
            )
        return tuple(sorted({cls._batch_id(value) for value in values}))

    @staticmethod
    def _safe_file(path: Path, parent: Path, *, batch_id: str) -> None:
        try:
            safe = (
                path.is_file()
                and not _is_reparse(path)
                and _same_path(path.parent, parent)
            )
        except OSError:
            safe = False
        if not safe:
            raise ProjectDeletionError(
                "批次文件边界无法安全确认，删除已停止。",
                code="unsafe_target",
                batch_id=batch_id,
            )

    @staticmethod
    def _read_json(path: Path, *, batch_id: str, code: str) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProjectDeletionError(
                "批次文件暂时无法安全读取，删除已停止。",
                code=code,
                batch_id=batch_id,
            ) from None
        if not isinstance(value, Mapping):
            raise ProjectDeletionError(
                "批次文件内容无法安全确认，删除已停止。",
                code=code,
                batch_id=batch_id,
            )
        return value

    @staticmethod
    def _file_identity_sha256(path: Path, *, batch_id: str) -> str:
        try:
            stat = path.stat(follow_symlinks=False)
            content = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    content.update(chunk)
        except OSError:
            raise ProjectDeletionError(
                "批次文件身份无法安全确认，删除已停止。",
                code="instance_unavailable",
                batch_id=batch_id,
            ) from None
        payload = json.dumps(
            [
                stat.st_mode,
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                content.hexdigest(),
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _manifest(
        self,
        batch_id: str,
    ) -> tuple[Path, Mapping[str, Any] | None, str]:
        path = self.manifests_root / f"{batch_id}.batch_manifest.json"
        if not _present(path):
            return path, None, ""
        self._safe_file(path, self.manifests_root, batch_id=batch_id)
        value = self._read_json(path, batch_id=batch_id, code="manifest_invalid")
        if value.get("product_id") != batch_id:
            raise ProjectDeletionError(
                "批次清单与批次号不一致，删除已停止。",
                code="manifest_invalid",
                batch_id=batch_id,
            )
        workspace = value.get("workspace")
        root_value = workspace.get("root") if isinstance(workspace, Mapping) else None
        expected = self.workspace_parent / batch_id
        if (
            not isinstance(root_value, str)
            or not Path(root_value).is_absolute()
            or not _same_path(Path(root_value), expected)
        ):
            raise ProjectDeletionError(
                "批次工作区超出批准目录，删除已停止。",
                code="workspace_outside_root",
                batch_id=batch_id,
            )
        return (
            path,
            value,
            self._file_identity_sha256(path, batch_id=batch_id),
        )

    def _events(
        self,
        batch_id: str,
    ) -> tuple[Path, list[Mapping[str, Any]], bool, bool, bool]:
        path = self.manifests_root / f"{batch_id}.events.jsonl"
        if not _present(path):
            return path, [], False, False, False
        self._safe_file(path, self.manifests_root, batch_id=batch_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise ProjectDeletionError(
                "批次账本暂时无法读取，删除已停止。",
                code="journal_invalid",
                batch_id=batch_id,
            ) from None
        events: list[Mapping[str, Any]] = []
        active_recycle = False
        closed = False
        delivered = False
        for line in lines:
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                raise ProjectDeletionError(
                    "批次账本内容损坏，删除已停止。",
                    code="journal_invalid",
                    batch_id=batch_id,
                ) from None
            if not isinstance(value, Mapping):
                raise ProjectDeletionError(
                    "批次账本内容损坏，删除已停止。",
                    code="journal_invalid",
                    batch_id=batch_id,
                )
            events.append(value)
            event = value.get("event")
            if event == "batch_recycled":
                active_recycle = True
            elif event == "batch_restored" and active_recycle:
                active_recycle = False
            elif event == "batch_acceptance_closed":
                closed = True
            elif event == "delivery_packaged":
                delivered = True
        return path, events, closed, delivered, active_recycle

    @staticmethod
    def _marker(
        path: Path,
        *,
        batch_id: str,
        expected_request_id: str = "",
    ) -> str:
        if not path.is_dir() or _is_reparse(path):
            raise ProjectDeletionError(
                "批次目录不是安全的普通目录，删除已停止。",
                code="workspace_invalid",
                batch_id=batch_id,
            )
        marker_path = path / ".canvas_batch"
        if not marker_path.is_file() or _is_reparse(marker_path):
            raise ProjectDeletionError(
                "批次目录安全标记缺失，删除已停止。",
                code="workspace_marker_invalid",
                batch_id=batch_id,
            )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            marker = None
        request_id = marker.get("request_id") if isinstance(marker, Mapping) else None
        if (
            not isinstance(marker, Mapping)
            or marker.get("type") != "canvas-batch-v1"
            or marker.get("product_id") != batch_id
            or not isinstance(request_id, str)
            or not request_id
            or (expected_request_id and request_id != expected_request_id)
        ):
            raise ProjectDeletionError(
                "批次目录安全标记不一致，删除已停止。",
                code="workspace_marker_invalid",
                batch_id=batch_id,
            )
        return request_id

    def _workspace(
        self,
        batch_id: str,
        *,
        lifecycle_recycled: bool,
    ) -> tuple[Path | None, str]:
        active = self.workspace_parent / batch_id
        active_present = _present(active)
        recycle_root = self.workspace_parent / "_回收站"
        recycled: list[Path] = []
        if _present(recycle_root):
            if not recycle_root.is_dir() or _is_reparse(recycle_root):
                raise ProjectDeletionError(
                    "批次回收目录无法安全确认，删除已停止。",
                    code="workspace_ambiguous",
                    batch_id=batch_id,
                )
            try:
                children = list(recycle_root.iterdir())
            except OSError:
                raise ProjectDeletionError(
                    "批次回收目录暂时无法核对，删除已停止。",
                    code="workspace_ambiguous",
                    batch_id=batch_id,
                ) from None
            for child in children:
                prefix = f"{batch_id}__"
                if not child.name.startswith(prefix):
                    continue
                stamp = child.name[len(prefix) :]
                if not _RECYCLE_STAMP.fullmatch(stamp):
                    raise ProjectDeletionError(
                        "发现无法确认归属的同名回收目录，删除已停止。",
                        code="workspace_ambiguous",
                        batch_id=batch_id,
                    )
                recycled.append(child)
        if active_present and recycled or len(recycled) > 1:
            raise ProjectDeletionError(
                "批次工作区位置不唯一，删除已停止。",
                code="workspace_ambiguous",
                batch_id=batch_id,
            )
        selected = active if active_present else (recycled[0] if recycled else None)
        if selected is None:
            return None, ""
        request_id = self._marker(selected, batch_id=batch_id)
        if (recycled and not lifecycle_recycled) or (
            active_present and lifecycle_recycled
        ):
            raise ProjectDeletionError(
                "批次工作区位置与账本状态不一致，删除已停止。",
                code="workspace_ambiguous",
                batch_id=batch_id,
            )
        return selected, request_id

    def _intake_targets(self, batch_id: str, request_id: str) -> list[_Target]:
        if not request_id:
            return []
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        targets: list[_Target] = []
        completed_root = self.state_root / "completed"
        completed = completed_root / f"{digest}.json"
        if _present(completed):
            if not completed_root.is_dir() or _is_reparse(completed_root):
                raise ProjectDeletionError(
                    "批次登记状态目录不安全，删除已停止。",
                    code="intake_state_invalid",
                    batch_id=batch_id,
                )
            self._safe_file(completed, completed_root, batch_id=batch_id)
            value = self._read_json(
                completed,
                batch_id=batch_id,
                code="intake_state_invalid",
            )
            if (
                value.get("request_id") != request_id
                or value.get("product_id") != batch_id
            ):
                raise ProjectDeletionError(
                    "批次登记状态与批次不一致，删除已停止。",
                    code="intake_state_invalid",
                    batch_id=batch_id,
                )
            targets.append(_Target(completed, "completed", batch_id, request_id))

        spool_root = self.state_root / "spool"
        spool = spool_root / digest
        if _present(spool):
            marker = spool / ".canvas_batch_intake_request"
            try:
                safe = (
                    spool_root.is_dir()
                    and not _is_reparse(spool_root)
                    and spool.is_dir()
                    and not _is_reparse(spool)
                    and marker.is_file()
                    and not _is_reparse(marker)
                    and marker.read_text(encoding="utf-8") == request_id + "\n"
                )
            except (OSError, UnicodeError):
                safe = False
            if not safe:
                raise ProjectDeletionError(
                    "批次上传暂存区无法安全确认，删除已停止。",
                    code="intake_state_invalid",
                    batch_id=batch_id,
                )
            targets.append(_Target(spool, "spool", batch_id, request_id))

        staging = (
            self.workspace_parent
            / f".{batch_id}.{digest[:12]}.batch-intake-staging"
        )
        if _present(staging):
            self._marker(
                staging,
                batch_id=batch_id,
                expected_request_id=request_id,
            )
            targets.append(_Target(staging, "staging", batch_id, request_id))
        return targets

    def _inventory(self, batch_id: str) -> _Inventory:
        self._require_runtime_roots()
        batch_id = self._batch_id(batch_id)
        manifest_path, manifest, manifest_identity_sha256 = self._manifest(
            batch_id
        )
        journal_path, _events, closed, delivered, recycled = self._events(batch_id)
        workspace, request_id = self._workspace(
            batch_id,
            lifecycle_recycled=recycled,
        )
        instance_commit = project_deletion_instance_commit(
            batch_id,
            manifest_identity_sha256 or ("0" * 64),
        )
        try:
            if manifest is not None:
                audited = bool(
                    self.audit_ledger.has_project_deletion(
                        batch_id,
                        instance_commit=instance_commit,
                    )
                )
            elif manifest is None and workspace is None:
                audited = bool(
                    self.audit_ledger.has_project_deletion(batch_id)
                )
            else:
                audited = False
        except Exception:
            raise ProjectDeletionError(
                "全局删除审计暂时无法安全读取，删除已停止。",
                code="audit_unavailable",
                batch_id=batch_id,
            ) from None
        if manifest is None and workspace is not None:
            raise ProjectDeletionError(
                "批次清单缺失，无法确认当前批次实例，删除已停止。",
                code="manifest_missing",
                batch_id=batch_id,
            )
        if manifest is None and not audited:
            raise ProjectDeletionError(
                "找不到这个批次。",
                code="batch_not_found",
                batch_id=batch_id,
            )
        if manifest is not None and workspace is None and not audited:
            raise ProjectDeletionError(
                "批次工作区不存在，删除已停止。",
                code="workspace_missing",
                batch_id=batch_id,
            )

        targets = self._intake_targets(batch_id, request_id)
        if workspace is not None:
            targets.append(_Target(workspace, "workspace", batch_id, request_id))

        layout = self.manifests_root / f"{batch_id}.canvas_layout.json"
        if _present(layout):
            self._safe_file(layout, self.manifests_root, batch_id=batch_id)
            targets.append(_Target(layout, "layout", batch_id))
        if self.reports_root.exists():
            for suffix in REPORT_SUFFIXES:
                report = self.reports_root / f"{batch_id}{suffix}"
                if _present(report):
                    self._safe_file(report, self.reports_root, batch_id=batch_id)
                    targets.append(_Target(report, "report", batch_id))
        if _present(journal_path):
            targets.append(_Target(journal_path, "journal", batch_id))
        if _present(manifest_path):
            targets.append(_Target(manifest_path, "manifest", batch_id))

        if manifest is None and workspace is None and audited and targets:
            raise ProjectDeletionError(
                "批次删除残留与已审计实例无法安全对应，删除已停止。",
                code="deletion_state_ambiguous",
                batch_id=batch_id,
            )
        if audited and (manifest is None or workspace is None):
            status = "deletion_pending" if targets else "deleted"
        elif recycled:
            status = "recycled"
        elif delivered:
            status = "delivered"
        elif closed:
            status = "closed"
        else:
            status = "in_production"
        return _Inventory(
            batch_id=batch_id,
            status=status,
            closed=closed,
            delivered=delivered,
            recycled=recycled,
            audited=audited,
            request_id=request_id,
            manifest_identity_sha256=manifest_identity_sha256,
            instance_commit=instance_commit,
            targets=tuple(targets),
        )

    @staticmethod
    def _inventory_signature(inventory: _Inventory) -> tuple[Any, ...]:
        target_signatures: list[tuple[Any, ...]] = []
        for target in inventory.targets:
            try:
                stat = target.path.stat(follow_symlinks=False)
            except OSError:
                raise ProjectDeletionError(
                    "批次文件在确认时发生变化，请重新查看删除清单。",
                    code="preview_changed",
                    batch_id=inventory.batch_id,
                ) from None
            target_signatures.append(
                (
                    target.kind,
                    target.request_id,
                    stat.st_mode,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        return (
            inventory.batch_id,
            inventory.status,
            inventory.closed,
            inventory.delivered,
            inventory.recycled,
            inventory.audited,
            inventory.request_id,
            inventory.manifest_identity_sha256,
            inventory.instance_commit,
            tuple(target_signatures),
        )

    @staticmethod
    def _request_id(
        inventories: Sequence[_Inventory],
        signatures: Sequence[tuple[Any, ...]],
    ) -> str:
        parts: list[str] = []
        for inventory, signature in zip(
            inventories,
            signatures,
            strict=True,
        ):
            snapshot_payload = json.dumps(
                signature,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            snapshot_commit = hashlib.sha256(snapshot_payload).hexdigest()[
                :_SNAPSHOT_COMMIT_LENGTH
            ]
            parts.append(inventory.instance_commit + snapshot_commit)
        request_id = _REQUEST_PREFIX + "." + ".".join(parts)
        if not valid_project_deletion_request_id(request_id):
            raise ProjectDeletionError(
                "删除请求编号无法生成。",
                code="request_id_invalid",
            )
        return request_id

    def preview(self, batch_ids: Any) -> dict[str, Any]:
        normalized = self._batch_ids(batch_ids)
        inventories = [self._inventory(batch_id) for batch_id in normalized]
        signatures = tuple(
            self._inventory_signature(inventory)
            for inventory in inventories
        )
        request_id = self._request_id(inventories, signatures)
        now = self.clock()
        with self._ticket_lock:
            self._tickets = {
                key: value for key, value in self._tickets.items() if value[0] >= now
            }
            self._tickets[request_id] = (
                now + PREVIEW_TTL_SECONDS,
                normalized,
                signatures,
            )
        return {
            "ok": True,
            "requestId": request_id,
            "batches": [
                {
                    "batchId": item.batch_id,
                    "status": item.status,
                    "closed": item.closed,
                    "delivered": item.delivered,
                    "recycled": item.recycled,
                    "requiresTypedConfirmation": item.closed or item.delivered,
                }
                for item in inventories
            ],
        }

    def _ticket(
        self,
        request_id: Any,
        batch_ids: Any,
    ) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
        normalized = self._batch_ids(batch_ids)
        if not valid_project_deletion_request_id(request_id):
            raise ProjectDeletionError(
                "删除请求编号无效。",
                code="preview_invalid",
            )
        now = self.clock()
        with self._ticket_lock:
            ticket = self._tickets.get(request_id)
        if ticket is None or ticket[0] < now or ticket[1] != normalized:
            raise ProjectDeletionError(
                "删除确认已经失效，请重新查看删除清单。",
                code="preview_invalid",
            )
        return normalized, ticket[2]

    def _validate_target(self, target: _Target) -> None:
        self._require_runtime_roots()
        path = target.path
        if not _present(path):
            return
        if target.kind in {"workspace", "staging"}:
            self._marker(
                path,
                batch_id=target.batch_id,
                expected_request_id=target.request_id,
            )
        elif target.kind == "spool":
            marker = path / ".canvas_batch_intake_request"
            try:
                safe = (
                    path.is_dir()
                    and not _is_reparse(path)
                    and marker.is_file()
                    and not _is_reparse(marker)
                    and marker.read_text(encoding="utf-8") == target.request_id + "\n"
                )
            except (OSError, UnicodeError):
                safe = False
            if not safe:
                raise ProjectDeletionError(
                    "批次上传暂存区在删除前发生变化，操作已停止。",
                    code="target_changed",
                    batch_id=target.batch_id,
                )
        elif target.kind == "completed":
            self._safe_file(
                path,
                self.state_root / "completed",
                batch_id=target.batch_id,
            )
            value = self._read_json(
                path,
                batch_id=target.batch_id,
                code="target_changed",
            )
            if (
                value.get("request_id") != target.request_id
                or value.get("product_id") != target.batch_id
            ):
                raise ProjectDeletionError(
                    "批次登记状态在删除前发生变化，操作已停止。",
                    code="target_changed",
                    batch_id=target.batch_id,
                )
        elif target.kind in {"layout", "journal", "manifest"}:
            self._safe_file(path, self.manifests_root, batch_id=target.batch_id)
        elif target.kind == "report":
            self._safe_file(path, self.reports_root, batch_id=target.batch_id)

    def _delete_one(
        self,
        batch_id: str,
        request_id: str,
        expected_signature: tuple[Any, ...],
    ) -> str:
        try:
            with BatchOperationLock(batch_id, lock_root=self.lock_root):
                inventory = self._inventory(batch_id)
                if not inventory.targets and inventory.audited:
                    return "already_deleted"
                if self._inventory_signature(inventory) != expected_signature:
                    raise ProjectDeletionError(
                        "批次状态或实例在确认后发生变化，请重新查看删除清单。",
                        code="preview_changed",
                        batch_id=batch_id,
                    )
                if not inventory.targets:
                    if inventory.audited:
                        return "already_deleted"
                    raise ProjectDeletionError(
                        "找不到这个批次。",
                        code="batch_not_found",
                        batch_id=batch_id,
                    )
                try:
                    request_audited = bool(
                        self.audit_ledger.has_project_deletion(
                            batch_id,
                            request_id=request_id,
                        )
                    )
                except Exception:
                    raise ProjectDeletionError(
                        "全局删除审计暂时无法安全读取，删除已停止。",
                        code="audit_unavailable",
                        batch_id=batch_id,
                    ) from None
                if not request_audited:
                    try:
                        self.audit_ledger.record_project_deletion(
                            batch_id,
                            request_id,
                        )
                    except Exception:
                        raise ProjectDeletionError(
                            "删除审计记录无法安全写入，未删除任何文件。",
                            code="audit_write_failed",
                            batch_id=batch_id,
                        ) from None
                for target in inventory.targets:
                    if not _present(target.path):
                        continue
                    self._validate_target(target)
                    try:
                        self.recycle_executor(target.path)
                    except (OSError, RuntimeError, RecycleBinError):
                        raise ProjectDeletionError(
                            "有文件未能进入 Windows 回收站，删除已停止。",
                            code="recycle_bin_failed",
                            batch_id=batch_id,
                        ) from None
                    if _present(target.path):
                        raise ProjectDeletionError(
                            "Windows 回收站没有确认接收文件，删除已停止。",
                            code="recycle_bin_failed",
                            batch_id=batch_id,
                        )
                return "deleted"
        except BatchOperationBusy:
            raise ProjectDeletionError(
                "本批次有任务正在运行，项目删除已停止。",
                code="batch_busy",
                batch_id=batch_id,
            ) from None
        except BatchOperationLockUnavailable:
            raise ProjectDeletionError(
                "批次独占保护暂时不可用，项目删除已安全停止。",
                code="lock_unavailable",
                batch_id=batch_id,
            ) from None

    def execute(self, request_id: Any, batch_ids: Any) -> dict[str, Any]:
        normalized, signatures = self._ticket(request_id, batch_ids)
        results: list[dict[str, str]] = []
        stopped = False
        for batch_id, expected_signature in zip(
            normalized,
            signatures,
            strict=True,
        ):
            if stopped:
                results.append(
                    {
                        "batchId": batch_id,
                        "status": "not_started",
                        "message": "前一批次删除失败，本批次尚未开始。",
                    }
                )
                continue
            try:
                status = self._delete_one(
                    batch_id,
                    request_id,
                    expected_signature,
                )
            except ProjectDeletionError as exc:
                results.append(
                    {
                        "batchId": batch_id,
                        "status": "failed",
                        "message": str(exc),
                    }
                )
                stopped = True
                continue
            results.append(
                {
                    "batchId": batch_id,
                    "status": status,
                    "message": (
                        "后端文件已删除。"
                        if status == "deleted"
                        else "后端文件此前已经删除。"
                    ),
                }
            )
        return {
            "ok": not stopped,
            "requestId": request_id,
            "status": "stopped" if stopped else "completed",
            "batches": results,
        }
