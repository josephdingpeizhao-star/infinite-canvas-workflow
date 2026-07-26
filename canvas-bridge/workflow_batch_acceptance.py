"""Irreversible, ledger-backed close-out for a fully received batch."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

import run_controller
from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
from workflow_production_projection import artifact_from_path


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)
CONFIG_ID_SET = frozenset(CONFIG_IDS)
SOURCES = frozenset({"renders", "repaired"})
ACCEPTANCE_EVENT = "batch_acceptance_closed"
ACCEPTANCE_STATEMENT = (
    "用户终审：桌面已收满 14 个不同图位，并确认全部收货，批次正式关账。"
)


class AcceptanceRejected(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _path_values(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item)


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        raise AcceptanceRejected(409, "批次账本暂时无法读取。") from None
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class BatchAcceptanceService:
    def __init__(
        self,
        repository_root: Path,
        *,
        batch_lock_root: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self._close_lock = threading.Lock()
        self.batch_lock_root = batch_lock_root

    def _manifest_journal(self, batch_id: str) -> tuple[Path, Path]:
        if (
            not batch_id
            or Path(batch_id).name != batch_id
            or any(char in batch_id for char in ("/", "\\", "\0"))
        ):
            raise AcceptanceRejected(400, "批次号无效。")
        manifest_path = (
            self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        )
        if not manifest_path.is_file():
            raise AcceptanceRejected(404, "找不到这个批次。")
        return manifest_path, run_controller.journal_path(manifest_path, batch_id)

    @staticmethod
    def _lifecycle(journal: Path):
        try:
            return read_batch_lifecycle(journal)
        except BatchLifecycleReadError:
            raise AcceptanceRejected(409, "批次账本暂时无法读取。") from None

    def _context(
        self,
        batch_id: str,
    ) -> tuple[dict[str, Any], Path, Path, Path]:
        if (
            not batch_id
            or Path(batch_id).name != batch_id
            or any(char in batch_id for char in ("/", "\\", "\0"))
        ):
            raise AcceptanceRejected(400, "批次号无效。")
        manifest_path = (
            self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AcceptanceRejected(404, "找不到这个批次。") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise AcceptanceRejected(409, "批次清单暂时无法读取。") from None
        if not isinstance(manifest, dict) or manifest.get("product_id") != batch_id:
            raise AcceptanceRejected(409, "批次清单与批次号不一致。")
        workspace_value = (
            (manifest.get("workspace") or {}).get("root")
            if isinstance(manifest.get("workspace"), Mapping)
            else None
        )
        if not isinstance(workspace_value, str) or not workspace_value:
            raise AcceptanceRejected(409, "批次工作区信息缺失。")
        workspace = Path(workspace_value).resolve()
        try:
            marker = json.loads(
                (workspace / ".canvas_batch").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise AcceptanceRejected(409, "批次安全标记无效。") from None
        if (
            not isinstance(marker, dict)
            or marker.get("type") != "canvas-batch-v1"
            or marker.get("product_id") != batch_id
        ):
            raise AcceptanceRejected(409, "批次安全标记与批次号不一致。")
        journal = run_controller.journal_path(manifest_path, batch_id)
        return manifest, manifest_path, workspace, journal

    @staticmethod
    def _closed_event(journal: Path) -> dict[str, Any] | None:
        return next(
            (
                event
                for event in _read_events(journal)
                if event.get("event") == ACCEPTANCE_EVENT
            ),
            None,
        )

    def status(self, batch_id: str) -> dict[str, Any]:
        _manifest_path, journal = self._manifest_journal(batch_id)
        lifecycle = self._lifecycle(journal)
        if lifecycle.recycled:
            return {
                "ok": True,
                "batchId": batch_id,
                "status": "recycled",
                "recycledAt": (
                    lifecycle.active_recycled_event or {}
                ).get("operation_at_utc")
                or (lifecycle.active_recycled_event or {}).get("ts"),
            }
        _manifest, _manifest_path, _workspace, journal = self._context(batch_id)
        event = self._closed_event(journal)
        payload: dict[str, Any] = {
            "ok": True,
            "batchId": batch_id,
            "status": "closed" if event else "open",
        }
        if event:
            payload["closedAt"] = event.get("ts")
            payload["finalReviewStatement"] = ACCEPTANCE_STATEMENT
        return payload

    @staticmethod
    def _validated_request(payload: Any) -> tuple[str, str, list[dict[str, str]]]:
        if not isinstance(payload, dict):
            raise AcceptanceRejected(400, "关账内容格式不正确。")
        request_id = payload.get("requestId")
        machine_id = payload.get("machineId")
        selections = payload.get("selections")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 160
            or any(char in request_id for char in ("\r", "\n", "\0"))
            or not isinstance(machine_id, str)
            or not 1 <= len(machine_id) <= 160
            or any(char in machine_id for char in ("\r", "\n", "\0"))
            or not isinstance(selections, list)
            or len(selections) != len(CONFIG_IDS)
        ):
            raise AcceptanceRejected(400, "必须一次提交 14 个完整图位。")
        normalized: list[dict[str, str]] = []
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) != {
                "configId",
                "source",
                "sha256",
            }:
                raise AcceptanceRejected(400, "收货图位字段不完整。")
            config_id = selection.get("configId")
            source = selection.get("source")
            sha256 = selection.get("sha256")
            if (
                config_id not in CONFIG_ID_SET
                or source not in SOURCES
                or not isinstance(sha256, str)
                or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)
            ):
                raise AcceptanceRejected(400, "收货图位字段无效。")
            normalized.append(
                {
                    "config_id": config_id,
                    "source": source,
                    "sha256": sha256,
                }
            )
        config_ids = [item["config_id"] for item in normalized]
        if len(set(config_ids)) != len(CONFIG_IDS) or set(config_ids) != CONFIG_ID_SET:
            raise AcceptanceRejected(400, "14 个图位必须齐全且不能重复。")
        normalized.sort(key=lambda item: CONFIG_IDS.index(item["config_id"]))
        return request_id, machine_id, normalized

    @staticmethod
    def _resolve_selection(
        manifest: Mapping[str, Any],
        workspace: Path,
        selection: Mapping[str, str],
    ) -> Path:
        outputs = (
            manifest.get("outputs")
            if isinstance(manifest.get("outputs"), Mapping)
            else {}
        )
        config_id = selection["config_id"]
        source = selection["source"]
        matches: dict[Path, Path] = {}
        for root in _path_values(outputs.get(source)):
            target = (
                root / f"{config_id}.png"
                if root.suffix.lower() != ".png"
                else root
            )
            if not _inside(target, workspace):
                raise AcceptanceRejected(409, "收货图片超出批次工作区。")
            if target.is_file() and target.stem == config_id:
                matches[target.resolve()] = target
        if len(matches) != 1:
            raise AcceptanceRejected(409, "收货图片在磁盘上不存在或不唯一。")
        path = next(iter(matches.values()))
        try:
            artifact = artifact_from_path(
                str(manifest.get("product_id") or ""),
                path,
                source=source,
            )
        except (OSError, ValueError):
            raise AcceptanceRejected(409, "收货图片实物无效。") from None
        if artifact.sha256 != selection["sha256"]:
            raise AcceptanceRejected(409, "收货图片与画布选择不一致。")
        return path

    def close(self, batch_id: str, payload: Any) -> dict[str, Any]:
        with self._close_lock:
            _early_manifest, early_journal = self._manifest_journal(batch_id)
            try:
                with existing_batch_operation(
                    batch_id,
                    lock_root=self.batch_lock_root,
                ):
                    lifecycle = self._lifecycle(early_journal)
                    if lifecycle.recycled:
                        raise AcceptanceRejected(
                            409, "本批次已回收，不能执行关账。"
                        )
                    request_id, machine_id, selections = self._validated_request(
                        payload
                    )
                    manifest, _manifest_path, workspace, journal = self._context(
                        batch_id
                    )
                    if self._closed_event(journal):
                        raise AcceptanceRejected(
                            409, "本批次已关账，不能重复关账。"
                        )
                    for selection in selections:
                        self._resolve_selection(manifest, workspace, selection)
                    run_controller.append_event(
                        journal,
                        ACCEPTANCE_EVENT,
                        request_id=request_id,
                        machine_id=machine_id,
                        selection_count=len(selections),
                        selections=selections,
                        final_review_statement=ACCEPTANCE_STATEMENT,
                    )
            except BatchOperationBusy:
                raise AcceptanceRejected(
                    409, "本批次有任务正在运行，暂不能关账。"
                ) from None
        return {
            "ok": True,
            "batchId": batch_id,
            "status": "closed",
            "selectionCount": len(selections),
            "finalReviewStatement": ACCEPTANCE_STATEMENT,
        }
