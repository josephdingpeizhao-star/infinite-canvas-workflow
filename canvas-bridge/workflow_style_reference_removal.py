"""Remove registered style references through the existing intake worker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
from batch_recycle_state import BatchLifecycleReadError, read_batch_lifecycle
import ic_client
import run_controller
import state_reader
from windows_recycle_bin import RecycleBinError, WindowsRecycleBinExecutor
from workflow_style_reference_intake import (
    REQUEST_ID_RE,
    StyleReferenceBatchContext,
    StyleReferenceIntakeError,
    _inside,
    _sha256_file,
    open_new_json_receipt,
    resolve_style_reference_batch_context,
    resolve_style_reference_manifest_path,
    style_reference_files,
    validate_registered_style_reference_request,
    write_json_receipt,
)


STYLE_REFERENCE_REMOVED_EVENT = "style_reference_removed"
REMOVAL_RECEIPT_TYPE = "style_reference_removal_v1"
RENDERS_PRESENT_MESSAGE = (
    "本批已出图，移除会让成品来源断链；如需更换风格请走返修或新批次"
)
CLOSED_BATCH_MESSAGE = (
    "本批已关账，不能移除风格参考图；如需更换风格请新建批次。"
)
EMPTY_STYLE_REFERENCE_MESSAGE = "没有可移除的风格参考图"
BUSY_REMOVAL_MESSAGE = "本批有操作正在进行，风格参考图未移除。请等待当前操作结束后再试。"
LOCK_UNAVAILABLE_MESSAGE = (
    "批次独占保护暂时不可用，风格参考图未移除。请稍后重试；系统不会绕过保护。"
)


class StyleReferenceRemovalError(StyleReferenceIntakeError):
    """One removal request stopped safely and must be shown on its card."""


@dataclass
class StyleReferenceRemovalFile:
    name: str
    size: int
    sha256: str
    path: Path
    removed: bool = False

    def receipt_value(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "removed": self.removed,
        }


@dataclass(frozen=True)
class StyleReferenceRemovalResult:
    batch_id: str
    file_count: int
    receipt_path: str
    files: tuple[str, ...]


def _output_count(route: Mapping[str, Any], output_name: str) -> int:
    outputs = route.get("outputs")
    summary = outputs.get(output_name) if isinstance(outputs, Mapping) else None
    count = summary.get("file_count") if isinstance(summary, Mapping) else None
    if type(count) is not int or count < 0:
        raise StyleReferenceRemovalError(
            "无法安全核对本批是否已经出图，风格参考图未移除。请保留现场并交由顾问核对。"
        )
    return count


def _read_route(
    context: StyleReferenceBatchContext,
    route_reader: Callable[[Path], Mapping[str, Any]],
) -> Mapping[str, Any]:
    try:
        route = route_reader(context.manifest_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise StyleReferenceRemovalError(
            "无法安全核对本批是否已经出图，风格参考图未移除。请保留现场并交由顾问核对。"
        ) from None
    if not isinstance(route, Mapping):
        raise StyleReferenceRemovalError(
            "无法安全核对本批是否已经出图，风格参考图未移除。请保留现场并交由顾问核对。"
        )
    return route


def _hash_removal_files(
    context: StyleReferenceBatchContext,
) -> list[StyleReferenceRemovalFile]:
    values: list[StyleReferenceRemovalFile] = []
    for path in style_reference_files(context):
        try:
            size = path.stat().st_size
            sha256 = _sha256_file(path)
        except OSError:
            raise StyleReferenceRemovalError(
                "无法完整核对风格参考图，未开始移除。请保留现场并交由顾问核对。"
            ) from None
        values.append(
            StyleReferenceRemovalFile(
                name=path.name,
                size=size,
                sha256=sha256,
                path=path,
            )
        )
    return values


def _assert_file_unchanged(
    context: StyleReferenceBatchContext,
    value: StyleReferenceRemovalFile,
) -> None:
    try:
        resolved = value.path.resolve(strict=True)
        current_size = resolved.stat().st_size
        current_sha256 = _sha256_file(resolved)
    except (OSError, RuntimeError):
        raise StyleReferenceRemovalError(
            f"风格参考图“{value.name}”在移除前发生变化，本次操作已停止。"
            "请保留现场并交由顾问核对。"
        ) from None
    if (
        resolved != value.path
        or resolved.parent != context.style_root.resolve(strict=True)
        or not _inside(resolved, context.workspace)
        or resolved.is_symlink()
        or not resolved.is_file()
        or current_size != value.size
        or current_sha256 != value.sha256
    ):
        raise StyleReferenceRemovalError(
            f"风格参考图“{value.name}”在移除前发生变化，本次操作已停止。"
            "请保留现场并交由顾问核对。"
        )


def _receipt_payload(
    context: StyleReferenceBatchContext,
    request_id: str,
    files: list[StyleReferenceRemovalFile],
    *,
    status: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "receipt_type": REMOVAL_RECEIPT_TYPE,
        "product_id": context.batch_id,
        "request_id": request_id,
        "status": status,
        "file_count": len(files),
        "removed_count": sum(1 for item in files if item.removed),
        "files": [item.receipt_value() for item in files],
    }
    if error_message:
        payload["error_message"] = error_message
    return payload


def remove_style_references(
    manifest_path: Path,
    request_id: str,
    *,
    batch_lock_root: Path | None = None,
    recycle_executor: Callable[[Path], None] | None = None,
    route_reader: Callable[[Path], Mapping[str, Any]] = state_reader.read_batch_route,
    lifecycle_reader: Callable[[Path], Any] = read_batch_lifecycle,
    event_appender: Callable[..., Mapping[str, Any]] = run_controller.append_event,
) -> StyleReferenceRemovalResult:
    """Move every registered style file to the Recycle Bin under strict gates."""

    if not REQUEST_ID_RE.fullmatch(request_id):
        raise StyleReferenceRemovalError("风格移除请求编号无效。")
    context = resolve_style_reference_batch_context(manifest_path)

    locked_batch_id = context.batch_id
    executor = recycle_executor or WindowsRecycleBinExecutor()
    try:
        with BatchOperationLock(locked_batch_id, lock_root=batch_lock_root):
            # 锁取得后重新解析一次，避免校验与实际移除之间的现场漂移。
            context = resolve_style_reference_batch_context(manifest_path)
            if context.batch_id != locked_batch_id:
                raise StyleReferenceRemovalError(
                    "批次清单在移除前发生变化，风格参考图未移除。"
                    "请保留现场并交由顾问核对。"
                )
            receipt_path = (
                context.workspace
                / "manifests"
                / f"style_reference_removal_receipt.{request_id}.json"
            )
            if not _inside(receipt_path, context.workspace):
                raise StyleReferenceRemovalError(
                    "风格移除回执路径越过批准的批次边界。"
                )
            route = _read_route(context, route_reader)
            if _output_count(route, "renders") or _output_count(route, "repaired"):
                raise StyleReferenceRemovalError(RENDERS_PRESENT_MESSAGE)
            try:
                lifecycle = lifecycle_reader(context.journal_path)
            except BatchLifecycleReadError:
                raise StyleReferenceRemovalError(
                    "批次账本暂时无法读取，风格参考图未移除。请稍后重试。"
                ) from None
            if lifecycle.recycled:
                raise StyleReferenceRemovalError(
                    "本批已回收，不能移除风格参考图；请先恢复批次后再试。"
                )
            if lifecycle.closed:
                raise StyleReferenceRemovalError(CLOSED_BATCH_MESSAGE)

            files = _hash_removal_files(context)
            if not files:
                raise StyleReferenceRemovalError(EMPTY_STYLE_REFERENCE_MESSAGE)

            with open_new_json_receipt(
                receipt_path,
                exists_message="这次风格移除请求已有回执，不会重写或自动重试。",
            ) as receipt_handle:
                failure_message: str | None = None
                for value in files:
                    try:
                        _assert_file_unchanged(context, value)
                        executor(value.path)
                        if value.path.exists():
                            raise RecycleBinError(
                                "文件仍在原目录，未能确认已进入 Windows 回收站。"
                            )
                        value.removed = True
                    except (
                        OSError,
                        RecycleBinError,
                        StyleReferenceIntakeError,
                    ) as exc:
                        if not value.path.exists():
                            value.removed = True
                        failure_message = (
                            f"已移除 {sum(1 for item in files if item.removed)}/{len(files)} 张；"
                            f"处理“{value.name}”时停止：{exc}"
                            "系统不会回滚或自动重试，请保留移除回执并交由顾问核对。"
                        )
                        break

                if failure_message is not None:
                    write_json_receipt(
                        receipt_handle,
                        _receipt_payload(
                            context,
                            request_id,
                            files,
                            status="failed",
                            error_message=failure_message,
                        ),
                    )
                    raise StyleReferenceRemovalError(failure_message)

                event_files = [
                    {"name": item.name, "sha256": item.sha256}
                    for item in files
                ]
                try:
                    event_appender(
                        context.journal_path,
                        STYLE_REFERENCE_REMOVED_EVENT,
                        request_id=request_id,
                        file_count=len(files),
                        files=event_files,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    failure_message = (
                        f"已移除 {len(files)}/{len(files)} 张，但批次账本追加失败。"
                        "系统不会自动重试，请保留移除回执并交由顾问核对。"
                    )
                    write_json_receipt(
                        receipt_handle,
                        _receipt_payload(
                            context,
                            request_id,
                            files,
                            status="failed",
                            error_message=failure_message,
                        ),
                    )
                    raise StyleReferenceRemovalError(failure_message) from None

                write_json_receipt(
                    receipt_handle,
                    _receipt_payload(
                        context,
                        request_id,
                        files,
                        status="completed",
                    ),
                )
    except BatchOperationBusy:
        raise StyleReferenceRemovalError(BUSY_REMOVAL_MESSAGE) from None
    except BatchOperationLockUnavailable:
        raise StyleReferenceRemovalError(LOCK_UNAVAILABLE_MESSAGE) from None

    return StyleReferenceRemovalResult(
        batch_id=context.batch_id,
        file_count=len(files),
        receipt_path=str(receipt_path),
        files=tuple(item.name for item in files),
    )


class WorkflowStyleReferenceRemovalHandler:
    """Handle removal metadata inside the existing style-intake poll."""

    def __init__(
        self,
        repository_root: Path,
        *,
        client: Any,
        clock_ms: Callable[[], int] | None = None,
        batch_lock_root: Path | None = None,
        recycle_executor: Callable[[Path], None] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.client = client
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.batch_lock_root = batch_lock_root
        self.recycle_executor = recycle_executor
        self.consumed_request_ids: set[str] = set()

    @staticmethod
    def _metadata(node: Mapping[str, Any]) -> dict[str, Any]:
        value = node.get("metadata")
        return dict(value) if isinstance(value, Mapping) else {}

    def _update(
        self,
        node: dict[str, Any],
        status: str,
        *,
        error_message: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        metadata = self._metadata(node)
        current = metadata.get("styleReferenceRemoval")
        removal = dict(current) if isinstance(current, Mapping) else {}
        removal.update(dict(fields or {}))
        removal["status"] = status
        removal["updatedAt"] = self.clock_ms()
        if error_message:
            removal["errorMessage"] = error_message
        else:
            removal.pop("errorMessage", None)
        self.client.apply_ops(
            [
                {
                    "type": "update_node",
                    "id": str(node.get("id") or ""),
                    "metadata": {"styleReferenceRemoval": removal},
                }
            ]
        )
        metadata["styleReferenceRemoval"] = removal
        node["metadata"] = metadata

    def is_queued(self, node: Mapping[str, Any]) -> bool:
        removal = self._metadata(node).get("styleReferenceRemoval")
        return isinstance(removal, Mapping) and removal.get("status") == "queued"

    def reject(self, node: dict[str, Any], message: str) -> None:
        removal = self._metadata(node).get("styleReferenceRemoval")
        request_id = removal.get("requestId") if isinstance(removal, Mapping) else None
        if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
            self.consumed_request_ids.add(request_id)
        self._update(node, "failed", error_message=message)

    def process_node(self, node: dict[str, Any]) -> None:
        metadata = self._metadata(node)
        removal = metadata.get("styleReferenceRemoval")
        if not isinstance(removal, Mapping) or removal.get("status") != "queued":
            return
        request_id = removal.get("requestId")
        try:
            request = validate_registered_style_reference_request(
                metadata,
                removal,
                now_ms=self.clock_ms(),
                action_label="风格移除",
                retry_label="移除",
            )
            request_id = request.request_id
            if request_id in self.consumed_request_ids:
                raise StyleReferenceRemovalError("这次风格移除请求已被处理。")
            self.consumed_request_ids.add(request_id)
            manifest_path = resolve_style_reference_manifest_path(
                self.repository_root,
                request.batch_id,
                action_label="风格移除",
            )
            result = remove_style_references(
                manifest_path,
                request_id,
                batch_lock_root=self.batch_lock_root,
                recycle_executor=self.recycle_executor,
            )
            self._update(
                node,
                "completed",
                fields={
                    "receipt": {
                        "batchId": result.batch_id,
                        "fileCount": result.file_count,
                        "files": list(result.files),
                        "receiptPath": result.receipt_path,
                    }
                },
            )
        except ic_client.CanvasAgentError:
            raise
        except StyleReferenceIntakeError as exc:
            if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
                self.consumed_request_ids.add(request_id)
            self._update(node, "failed", error_message=str(exc))
        except Exception:
            if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
                self.consumed_request_ids.add(request_id)
            self._update(
                node,
                "failed",
                error_message=(
                    "风格参考图移除遇到异常，系统已停止且不会自动重试。"
                    "请保留现场并交由顾问核对。"
                ),
            )
