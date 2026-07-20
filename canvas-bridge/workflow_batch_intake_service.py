"""M2-a local batch-intake service and lossless binary upload listener.

The canvas command remains a small control message.  Original image bytes take
an independent loopback-only route and are never embedded in canvas state or a
log entry.  This module does not consume M1 ``workflowDemo`` commands.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable

import batch_creator
import batch_intake_controller
import ic_client


COMMAND_MAX_AGE_MS = 8_000
DEFAULT_UPLOAD_HOST = "127.0.0.1"
DEFAULT_UPLOAD_PORT = 17_372
ALLOWED_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000"})
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024
MAX_SOURCE_FILES = 100
SERVICE_EVENT_NAME = "batch_intake_service.events.jsonl"
SERVICE_LOCK_NAME = ".batch_intake_service.lock"
SERVICE_OWNER_NAME = ".batch_intake_service.owner.json"
SPOOL_MARKER_NAME = ".canvas_batch_intake_request"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MIME_ALIASES = {"image/jpg": "image/jpeg"}
_SUPPORTED_IMAGE_MIME = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
)


def constant_time_token_matches(expected: str, provided: str) -> bool:
    """Compare the existing canvas token without content-dependent timing."""
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _normalize_mime(value: str) -> str:
    normalized = value.strip().lower()
    return _MIME_ALIASES.get(normalized, normalized)


def _valid_image_magic(mime_type: str, prefix: bytes) -> bool:
    if mime_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    if mime_type == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/bmp":
        return prefix.startswith(b"BM")
    return False


def _decode_route_value(value: str, *, label: str) -> str:
    if not value or _BAD_PERCENT_RE.search(value):
        raise UploadRejected("bad_route", f"上传地址中的{label}不正确。", http_status=400)
    try:
        decoded = urllib.parse.unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise UploadRejected("bad_route", f"上传地址中的{label}不正确。", http_status=400) from None
    if not decoded or any(character in decoded for character in ("/", "\\", "\x00")):
        raise UploadRejected("bad_route", f"上传地址中的{label}不正确。", http_status=400)
    return decoded


def _safe_human_error(exc: BaseException, fallback: str) -> str:
    value = getattr(exc, "user_message", None)
    return value if isinstance(value, str) and value.strip() else fallback


class UploadRejected(RuntimeError):
    """A sanitized upload rejection safe to return to the local browser."""

    def __init__(self, code: str, user_message: str, *, http_status: int = 409):
        self.code = code
        self.user_message = user_message
        self.http_status = http_status
        super().__init__(user_message)


@dataclass(frozen=True)
class UploadOutcome:
    sha256: str
    completed: bool


@dataclass
class _UploadSession:
    request: batch_intake_controller.BatchIntakeRequest
    info_node: dict[str, Any]
    batch_id: str
    uploads: dict[str, batch_creator.UploadedFile] = field(default_factory=dict)
    inflight: set[str] = field(default_factory=set)
    status: str = "upload_ready"
    commit_started: bool = False

    @property
    def sources(self) -> dict[str, batch_intake_controller.SourceImage]:
        return {item.node_id: item for item in self.request.source_images}


class WorkflowBatchIntakeService:
    """Poll queued batch-info nodes and coordinate exact-byte uploads."""

    def __init__(
        self,
        repo_root: Path,
        state_root: Path,
        *,
        client: Any = ic_client,
        creator: Any | None = None,
        controller: Any = batch_intake_controller,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
        upload_host: str = DEFAULT_UPLOAD_HOST,
        upload_port: int = DEFAULT_UPLOAD_PORT,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.state_root = batch_creator.require_state_root(state_root)
        self.client = client
        self.controller = controller
        self.creator = creator or batch_creator.BatchCreator(self.repo_root, self.state_root)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.interval = interval
        self.upload_host = self._require_upload_host(upload_host)
        self.upload_port = self._require_upload_port(upload_port, allow_zero=True)
        self.event_path = self.state_root / SERVICE_EVENT_NAME
        self.seen_request_ids = self._read_seen_request_ids()
        self.sessions: dict[str, _UploadSession] = {}
        self.consumed_queued: dict[str, str] = {}
        self._session_lock = threading.RLock()
        self.stopping = False

    @staticmethod
    def _require_upload_host(host: str) -> str:
        if host != DEFAULT_UPLOAD_HOST:
            raise ValueError("原图上传监听器只允许绑定 127.0.0.1 回环地址")
        return host

    @staticmethod
    def _require_upload_port(port: int, *, allow_zero: bool) -> int:
        if type(port) is not int or port < (0 if allow_zero else 1) or port > 65_535:
            raise ValueError("原图上传端口不正确")
        return port

    def set_upload_endpoint(self, host: str, port: int) -> None:
        self.upload_host = self._require_upload_host(host)
        self.upload_port = self._require_upload_port(port, allow_zero=False)

    def _read_seen_request_ids(self) -> set[str]:
        batch_creator.require_state_root(self.state_root)
        if not self.event_path.exists():
            return set()
        if not self.event_path.is_file() or self.event_path.is_symlink():
            raise RuntimeError("建批服务账本不是安全的普通文件，服务已停止。")
        seen: set[str] = set()
        try:
            lines = self.event_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise RuntimeError("建批服务账本无法安全读取，服务已停止。") from None
        for line_number, line in enumerate(lines, start=1):
            if not line:
                raise RuntimeError(f"建批服务账本第 {line_number} 行损坏，服务已停止。")
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                raise RuntimeError(f"建批服务账本第 {line_number} 行损坏，服务已停止。") from None
            if not isinstance(entry, dict):
                raise RuntimeError(f"建批服务账本第 {line_number} 行损坏，服务已停止。")
            request_id = entry.get("request_id")
            event = entry.get("event")
            recorded_at = entry.get("recorded_at")
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID_RE.fullmatch(request_id)
                or not isinstance(event, str)
                or not event
                or type(recorded_at) is not int
            ):
                raise RuntimeError(f"建批服务账本第 {line_number} 行损坏，服务已停止。")
            seen.add(request_id)
        return seen

    def _append_event(self, event: str, request_id: str, **safe_fields: Any) -> None:
        batch_creator.require_state_root(self.state_root)
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise RuntimeError("建批请求编号不安全，未写入账本。")
        try:
            event_parent = self.event_path.parent.resolve(strict=True)
            state_root = self.state_root.resolve(strict=True)
            unsafe_event = self.event_path.is_symlink()
            is_junction = getattr(self.event_path, "is_junction", None)
            unsafe_event = unsafe_event or bool(is_junction and is_junction())
            if event_parent != state_root or unsafe_event:
                raise OSError
            if self.event_path.exists() and not self.event_path.is_file():
                raise OSError
        except (OSError, RuntimeError):
            self.stopping = True
            raise RuntimeError("建批服务账本路径不安全，服务已停止。") from None
        entry = {
            "event": event,
            "request_id": request_id,
            "recorded_at": self.clock_ms(),
            **safe_fields,
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.event_path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            self.stopping = True
            raise RuntimeError("建批服务账本无法安全写入，服务已停止。") from None
        self.seen_request_ids.add(request_id)

    @staticmethod
    def _intake_state(node: dict[str, Any]) -> dict[str, Any]:
        metadata = node.get("metadata") or {}
        value = metadata.get("batchIntake") or {}
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _node_signature(node: dict[str, Any]) -> str:
        metadata = node.get("metadata") or {}
        payload = {
            "content": metadata.get("content"),
            "batchIntake": metadata.get("batchIntake"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def _apply_node_update(
        self,
        node: dict[str, Any],
        *,
        status: str,
        request_id: str | None,
        error_message: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        state = self._intake_state(node)
        state.update(fields or {})
        state["status"] = status
        state["updatedAt"] = self.clock_ms()
        if error_message:
            state["errorMessage"] = error_message
        else:
            state.pop("errorMessage", None)
        safe_request_id = request_id if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id) else "unavailable"
        content = f"# batch-intake\n# request-id: {safe_request_id}\n# {status}"
        self.client.apply_ops(
            [
                {
                    "type": "update_node",
                    "id": str(node.get("id") or ""),
                    "metadata": {"content": content, "batchIntake": state},
                }
            ]
        )
        metadata = node.setdefault("metadata", {})
        metadata["content"] = content
        metadata["batchIntake"] = state

    def _session_update(
        self,
        session: _UploadSession,
        *,
        status: str,
        error_message: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self._apply_node_update(
            session.info_node,
            status=status,
            request_id=session.request.request_id,
            error_message=error_message,
            fields=fields,
        )

    def _request_id_from_node(self, node: dict[str, Any]) -> str | None:
        value = self._intake_state(node).get("requestId")
        return value if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) else None

    def _reject_gate(self, node: dict[str, Any], exc: batch_intake_controller.BatchIntakeGateError) -> None:
        request_id = self._request_id_from_node(node)
        if request_id and request_id not in self.seen_request_ids:
            self._append_event("gate_rejected", request_id, code=str(getattr(exc, "code", "invalid_request")))
        self._apply_node_update(
            node,
            status="failed",
            request_id=request_id,
            error_message=_safe_human_error(exc, "这次登记请求不完整，已经停止。"),
        )

    def _validate_request_sources(self, request: batch_intake_controller.BatchIntakeRequest) -> None:
        sources = tuple(request.source_images)
        if not sources or len(sources) > MAX_SOURCE_FILES:
            raise ValueError("原始图片数量不在可登记范围内。")
        if len({source.node_id for source in sources}) != len(sources):
            raise ValueError("原始图片节点有重复，已经停止登记。")
        total = 0
        for source in sources:
            if type(source.size) is not int or source.size <= 0 or source.size > MAX_UPLOAD_BYTES:
                raise ValueError("有一张原始图片大小超出本机登记限制。")
            mime_type = _normalize_mime(source.mime_type)
            if mime_type not in _SUPPORTED_IMAGE_MIME:
                raise ValueError("有一张原始图片格式暂不支持登记。")
            expected_sha256 = str(source.expected_sha256).lower()
            if not _SHA256_RE.fullmatch(expected_sha256):
                raise ValueError("有一张原始图片缺少完整校验值。")
            total += source.size
        if total > MAX_BATCH_BYTES:
            raise ValueError("本批原始图片总大小超出本机登记限制。")

    def _require_new_batch_target(self, batch_id: str) -> None:
        workspace_parent_value = getattr(self.creator, "workspace_parent", None)
        if not isinstance(workspace_parent_value, Path):
            return
        workspace_parent = workspace_parent_value
        manifests_parent = self.repo_root / "manifests"
        try:
            if (
                not workspace_parent.is_dir()
                or self._unsafe_reparse(workspace_parent)
                or not manifests_parent.is_dir()
                or self._unsafe_reparse(manifests_parent)
            ):
                raise OSError
            target = workspace_parent / batch_id
            manifest = manifests_parent / f"{batch_id}.batch_manifest.json"
            if (
                target.resolve(strict=False).parent != workspace_parent.resolve(strict=True)
                or manifest.resolve(strict=False).parent != manifests_parent.resolve(strict=True)
            ):
                raise OSError
            target_exists = target.exists() or self._unsafe_reparse(target)
            manifest_exists = manifest.exists() or self._unsafe_reparse(manifest)
        except (OSError, RuntimeError):
            raise batch_creator.BatchCreationError(
                "unsafe_target",
                "无法安全核对新批次位置，已停止登记。",
            ) from None
        if target_exists or manifest_exists:
            raise batch_creator.BatchCreationError(
                "batch_exists",
                "这个批次已经存在，未接收原图，也未覆盖任何文件。",
            )

    @staticmethod
    def _unsafe_reparse(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            return bool(is_junction and is_junction())
        except OSError:
            return True

    def _spool_root(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.state_root / "spool" / digest

    def _begin_spool(self, request_id: str) -> Path:
        batch_creator.require_state_root(self.state_root)
        parent = self.state_root / "spool"
        root: Path | None = None
        created_root = False
        try:
            parent.mkdir(exist_ok=True)
            if self._unsafe_reparse(parent) or parent.resolve(strict=True).parent != self.state_root.resolve(strict=True):
                raise OSError
            root = self._spool_root(request_id)
            if root.exists() or self._unsafe_reparse(root):
                raise FileExistsError
            root.mkdir()
            created_root = True
            marker = root / SPOOL_MARKER_NAME
            with marker.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(request_id + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return root
        except (OSError, RuntimeError):
            if created_root and root is not None:
                try:
                    if (
                        root.is_dir()
                        and not self._unsafe_reparse(root)
                        and root.resolve(strict=True).parent == parent.resolve(strict=True)
                        and not any(root.iterdir())
                    ):
                        root.rmdir()
                except (OSError, RuntimeError):
                    pass
            raise batch_creator.BatchCreationError(
                "unsafe_spool",
                "本机原图接收区不安全，已停止登记并保留现场。",
            ) from None

    def _require_spool(self, request_id: str) -> Path:
        batch_creator.require_state_root(self.state_root)
        root = self._spool_root(request_id)
        marker = root / SPOOL_MARKER_NAME
        try:
            valid = (
                root.is_dir()
                and not self._unsafe_reparse(root)
                and root.resolve(strict=True).parent == (self.state_root / "spool").resolve(strict=True)
                and marker.is_file()
                and not self._unsafe_reparse(marker)
                and marker.read_text(encoding="utf-8") == request_id + "\n"
            )
        except (OSError, RuntimeError, UnicodeError):
            valid = False
        if not valid:
            raise batch_creator.BatchCreationError(
                "unsafe_spool",
                "本机原图接收区标记不一致，已停止并保留现场。",
            )
        return root

    def _spool_path(self, request: batch_intake_controller.BatchIntakeRequest, source_node_id: str) -> Path:
        root = self._require_spool(request.request_id)
        try:
            index = next(
                index
                for index, source in enumerate(request.source_images, start=1)
                if source.node_id == source_node_id
            )
        except StopIteration:
            raise batch_creator.BatchCreationError("unknown_source", "这张图片不属于本次登记。") from None
        return root / f"{index:03d}.upload"

    def _abort_spool(self, request_id: str) -> None:
        root = self._spool_root(request_id)
        if not root.exists():
            return
        try:
            root = self._require_spool(request_id)
            allowed_names = {SPOOL_MARKER_NAME}
            allowed_names.update(f"{index:03d}.upload" for index in range(1, MAX_SOURCE_FILES + 1))
            allowed_names.update(f"{index:03d}.upload.part" for index in range(1, MAX_SOURCE_FILES + 1))
            children = list(root.iterdir())
            if any(
                child.name not in allowed_names
                or child.is_dir()
                or self._unsafe_reparse(child)
                for child in children
            ):
                raise OSError
            for child in children:
                child.unlink()
            root.rmdir()
        except (OSError, RuntimeError, batch_creator.BatchCreationError):
            raise RuntimeError("本机接收区无法按请求标记安全清理，已停止并保留现场。") from None

    def _accept_request(self, node: dict[str, Any], request: batch_intake_controller.BatchIntakeRequest) -> None:
        request_id = request.request_id
        if request_id in self.seen_request_ids:
            self._apply_node_update(
                node,
                status="failed",
                request_id=request_id,
                error_message="这次登记请求已经处理，不会重复建批。",
            )
            return
        try:
            self._validate_request_sources(request)
            batch_id = self.creator.product_id_for(request)
            self._require_new_batch_target(batch_id)
        except (ValueError, batch_creator.BatchCreationError) as exc:
            self._append_event("gate_rejected", request_id, code=str(getattr(exc, "code", "invalid_source")))
            self._apply_node_update(
                node,
                status="failed",
                request_id=request_id,
                error_message=_safe_human_error(exc, str(exc) if isinstance(exc, ValueError) else "这次登记请求无法创建批次。"),
            )
            return

        self._append_event("request_received", request_id, expected_count=len(request.source_images))
        try:
            self._begin_spool(request_id)
        except batch_creator.BatchCreationError as exc:
            self._append_event("request_failed", request_id, code=exc.code)
            self._apply_node_update(
                node,
                status="failed",
                request_id=request_id,
                error_message=_safe_human_error(exc, "本机未能准备安全的登记空间。"),
            )
            return

        session = _UploadSession(request=request, info_node=node, batch_id=batch_id)
        with self._session_lock:
            self.sessions[request_id] = session
        try:
            self._session_update(
                session,
                status="upload_ready",
                fields={
                    "batchId": batch_id,
                    "workflowNodeId": request.workflow_node_id,
                    "sourceImageNodeIds": [source.node_id for source in request.source_images],
                    "uploadBaseUrl": f"http://{self.upload_host}:{self.upload_port}",
                    "expectedCount": len(request.source_images),
                    "receivedCount": 0,
                },
            )
        except Exception:
            with self._session_lock:
                session.status = "failed"
            self._abort_spool(request_id)
            self._append_event("request_failed", request_id, code="canvas_update_failed")
            raise

    def poll_once(self) -> None:
        state = self.client.call_tool("canvas_get_state")
        for raw_node in self.controller.queued_info_nodes(state):
            node = dict(raw_node)
            node_id = str(node.get("id") or "")
            signature = self._node_signature(node)
            if not node_id or self.consumed_queued.get(node_id) == signature:
                continue
            self.consumed_queued[node_id] = signature
            try:
                request = self.controller.parse_queued_request(
                    state,
                    raw_node,
                    now_ms=self.clock_ms(),
                    max_age_ms=COMMAND_MAX_AGE_MS,
                    future_tolerance_ms=0,
                )
            except batch_intake_controller.BatchIntakeGateError as exc:
                self._reject_gate(node, exc)
                continue
            self._accept_request(node, request)

    def _integrity_block(self, session: _UploadSession, message: str) -> None:
        with self._session_lock:
            if session.status in {"integrity_blocked", "failed", "completed"}:
                return
            session.status = "integrity_blocked"
            session.inflight.clear()
        try:
            self._abort_spool(session.request.request_id)
        except RuntimeError:
            self.stopping = True
        try:
            self._session_update(session, status="integrity_blocked", error_message=message)
        except Exception:
            self.stopping = True
        try:
            self._append_event("integrity_blocked", session.request.request_id)
        except Exception:
            self.stopping = True

    def _fail_upload(self, session: _UploadSession, message: str, *, code: str = "upload_failed") -> None:
        with self._session_lock:
            if session.status in {"failed", "integrity_blocked", "completed"}:
                return
            session.status = "failed"
            session.inflight.clear()
        try:
            self._abort_spool(session.request.request_id)
        except RuntimeError:
            self.stopping = True
        self._append_event("request_failed", session.request.request_id, code=code)
        self._session_update(session, status="failed", error_message=message)

    def _expected_upload(
        self,
        *,
        batch_id: str,
        request_id: str,
        source_node_id: str,
    ) -> tuple[_UploadSession, batch_intake_controller.SourceImage]:
        with self._session_lock:
            session = self.sessions.get(request_id)
            if session is None or session.batch_id != batch_id:
                raise UploadRejected("unknown_request", "这次上传没有对应的登记请求。", http_status=404)
            if session.status == "integrity_blocked":
                raise UploadRejected("integrity_blocked", "原图一致性检查未通过，这次登记已经停止。")
            if session.status in {"failed", "completed"} or session.commit_started:
                raise UploadRejected("closed_request", "这次登记已经结束，不再接收图片。")
            expected = session.sources.get(source_node_id)
            if expected is None:
                raise UploadRejected("unknown_source", "这张图片不属于本次登记。", http_status=404)
            if source_node_id in session.uploads or source_node_id in session.inflight:
                raise UploadRejected("duplicate_source", "这张原图已经接收，不会重复写入。")
            session.inflight.add(source_node_id)
            session.status = "uploading"
            return session, expected

    def accept_upload(
        self,
        *,
        batch_id: str,
        request_id: str,
        source_node_id: str,
        file_name: str,
        declared_size: int,
        declared_sha256: str,
        declared_last_modified: int,
        content_type: str,
        content_length: int,
        stream: BinaryIO,
    ) -> UploadOutcome:
        session, expected = self._expected_upload(
            batch_id=batch_id,
            request_id=request_id,
            source_node_id=source_node_id,
        )
        destination: Path | None = None
        temporary: Path | None = None
        try:
            try:
                decoded_name = _decode_route_value(file_name, label="文件名")
            except UploadRejected as exc:
                self._integrity_block(session, exc.user_message)
                raise
            expected_mime = _normalize_mime(expected.mime_type)
            actual_mime = _normalize_mime(content_type)
            declared_sha256 = declared_sha256.strip().lower()
            if decoded_name != expected.name:
                message = "文件名称与画布原图记录不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)
            if (
                type(declared_size) is not int
                or type(content_length) is not int
                or declared_size != expected.size
                or content_length != expected.size
                or content_length <= 0
                or content_length > MAX_UPLOAD_BYTES
            ):
                message = "文件大小与画布原图记录不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)
            if type(declared_last_modified) is not int or declared_last_modified != expected.last_modified:
                message = "文件时间与画布原图记录不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)
            if actual_mime != expected_mime or actual_mime not in _SUPPORTED_IMAGE_MIME:
                message = "文件类型与画布原图记录不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message, http_status=415)
            if not _SHA256_RE.fullmatch(declared_sha256) or declared_sha256 != expected.expected_sha256.lower():
                message = "原图校验值不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)

            destination = self._spool_path(session.request, source_node_id)
            temporary = destination.with_name(destination.name + ".part")
            if destination.exists() or temporary.exists():
                message = "本机接收区已有冲突文件，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)

            digest = hashlib.sha256()
            prefix = bytearray()
            remaining = content_length
            with temporary.open("xb") as handle:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise UploadRejected("short_body", "原图传输不完整，已停止登记。")
                    remaining -= len(chunk)
                    digest.update(chunk)
                    if len(prefix) < 16:
                        prefix.extend(chunk[: 16 - len(prefix)])
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            actual_sha256 = digest.hexdigest()
            if not _valid_image_magic(actual_mime, bytes(prefix)):
                message = "文件内容与声明的图片类型不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message, http_status=415)
            if actual_sha256 != declared_sha256 or actual_sha256 != expected.expected_sha256.lower():
                message = "浏览器原图与实际接收文件的校验值不一致，已停止登记。"
                self._integrity_block(session, message)
                raise UploadRejected("integrity_blocked", message)
            os.replace(temporary, destination)
            temporary = None

            upload = batch_creator.UploadedFile(
                source_node_id=source_node_id,
                path=destination,
                name=expected.name,
                size=expected.size,
                mime_type=expected.mime_type,
                sha256=actual_sha256,
            )
            with self._session_lock:
                session.inflight.discard(source_node_id)
                if session.status == "integrity_blocked":
                    destination.unlink(missing_ok=True)
                    raise UploadRejected("integrity_blocked", "原图一致性检查未通过，这次登记已经停止。")
                session.uploads[source_node_id] = upload
                received = len(session.uploads)
                expected_count = len(session.request.source_images)
                should_commit = received == expected_count
                if should_commit:
                    session.commit_started = True

            if not should_commit:
                self._session_update(
                    session,
                    status="uploading",
                    fields={"receivedCount": received, "expectedCount": expected_count},
                )
                return UploadOutcome(sha256=actual_sha256, completed=False)

            ordered_uploads = tuple(session.uploads[source.node_id] for source in session.request.source_images)
            try:
                result = self.creator.create(session.request, ordered_uploads)
            except batch_creator.BatchCreationError as exc:
                if exc.code == "integrity_mismatch":
                    message = _safe_human_error(
                        exc,
                        "原图最终一致性检查未通过，已经停止且未创建批次。",
                    )
                    self._integrity_block(session, message)
                    raise UploadRejected("integrity_blocked", message) from None
                self._fail_upload(
                    session,
                    _safe_human_error(exc, "本机未能安全完成建批，没有继续执行。"),
                    code=exc.code,
                )
                raise UploadRejected("creation_failed", _safe_human_error(exc, "本机未能安全完成建批。")) from None

            with self._session_lock:
                session.status = "completed"
            receipt_pending = False
            try:
                self._append_event("batch_completed", request_id, image_count=result.image_count)
            except Exception:
                self.stopping = True
                receipt_pending = True
            try:
                self._abort_spool(request_id)
            except RuntimeError:
                self.stopping = True
                receipt_pending = True
            receipt = {
                "batchId": result.product_id,
                "imageCount": result.image_count,
                "facts": session.request.facts.as_dict(),
            }
            try:
                self._session_update(
                    session,
                    status="completed",
                    fields={
                        "receivedCount": result.image_count,
                        "expectedCount": len(session.request.source_images),
                        "receipt": receipt,
                    },
                )
            except Exception:
                receipt_pending = True
            if receipt_pending:
                try:
                    self._append_event("receipt_pending", request_id)
                except Exception:
                    self.stopping = True
                print(json.dumps({"batch_intake": "receipt_pending"}, ensure_ascii=False), flush=True)
            return UploadOutcome(sha256=actual_sha256, completed=True)
        except UploadRejected as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            with self._session_lock:
                session.inflight.discard(source_node_id)
            if session.status not in {"integrity_blocked", "failed", "completed"}:
                if exc.code in {"short_body"}:
                    self._integrity_block(session, exc.user_message)
            raise
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            with self._session_lock:
                session.inflight.discard(source_node_id)
            message = "本机无法安全接收原图，这次登记已经停止。"
            self._fail_upload(session, message, code="spool_io_failed")
            raise UploadRejected("spool_io_failed", message, http_status=500) from None

    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.poll_once()
            except ic_client.CanvasAgentError:
                print(json.dumps({"batch_intake": "waiting_canvas"}, ensure_ascii=False), flush=True)
            self.sleep(self.interval)


class BatchIntakeServiceLock:
    """An independent one-byte OS lock for the M2 intake service."""

    def __init__(self, state_root: Path):
        self.state_root = batch_creator.require_state_root(state_root)
        self.path = self.state_root / SERVICE_LOCK_NAME
        self.owner_path = self.state_root / SERVICE_OWNER_NAME
        self.handle = None

    def _write_owner(self) -> None:
        state_root = batch_creator.require_state_root(self.state_root)
        try:
            unsafe = self.owner_path.is_symlink()
            is_junction = getattr(self.owner_path, "is_junction", None)
            unsafe = unsafe or bool(is_junction and is_junction())
            if self.owner_path.parent.resolve(strict=True) != state_root or unsafe:
                raise OSError
            if self.owner_path.exists() and not self.owner_path.is_file():
                raise OSError
            flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.owner_path, flags, 0o600)
            payload = json.dumps(
                {"pid": os.getpid(), "acquired_at": int(time.time() * 1000)},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as owner:
                owner.write(payload)
                owner.flush()
                os.fsync(owner.fileno())
        except OSError:
            raise RuntimeError("建批服务持有者说明无法安全写入") from None

    def _unlock(self) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        try:
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self):
        batch_creator.require_state_root(self.state_root)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            try:
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("建批服务已在运行") from exc
        try:
            self._write_owner()
        except Exception:
            try:
                self._unlock()
            finally:
                self.handle.close()
                self.handle = None
            raise
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self.handle is None:
            return
        try:
            try:
                state_root = batch_creator.require_state_root(self.state_root)
                if (
                    self.owner_path.parent.resolve(strict=True) == state_root
                    and self.owner_path.is_file()
                    and not self.owner_path.is_symlink()
                ):
                    self.owner_path.unlink()
            except (OSError, RuntimeError):
                pass
            self._unlock()
        finally:
            self.handle.close()
            self.handle = None


class _LocalThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class BatchUploadServer:
    """Lifecycle wrapper for the loopback raw-file HTTP listener."""

    def __init__(
        self,
        service: WorkflowBatchIntakeService,
        *,
        token: str,
        host: str = DEFAULT_UPLOAD_HOST,
        port: int = DEFAULT_UPLOAD_PORT,
    ) -> None:
        self.service = service
        self.host = service._require_upload_host(host)
        self.port = service._require_upload_port(port, allow_zero=True)
        if not isinstance(token, str) or not token:
            raise ValueError("必须复用现有 canvas-agent 令牌")
        self._token = token
        self._httpd: _LocalThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port = port

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _handler_type(self):
        facade = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _one_header(self, name: str, *, required: bool = True) -> str:
                values = self.headers.get_all(name) or []
                if len(values) != 1:
                    if required:
                        raise UploadRejected("bad_headers", "上传请求缺少必要信息。", http_status=400)
                    return ""
                return values[0]

            def _origin(self) -> str:
                origin = self._one_header("Origin")
                if origin not in ALLOWED_ORIGINS:
                    raise UploadRejected("forbidden_origin", "只接受本机画布发起的上传。", http_status=403)
                return origin

            def _write_json(self, status: int, payload: dict[str, Any], *, origin: str | None = None) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)

            def _authenticate(self) -> tuple[str, str]:
                origin = self._origin()
                provided = self._one_header("X-Canvas-Agent-Token")
                if not constant_time_token_matches(facade._token, provided):
                    raise UploadRejected("unauthorized", "本机画布身份校验未通过。", http_status=401)
                return origin, provided

            @staticmethod
            def _integer_header(value: str) -> int:
                if not value or not value.isascii() or not value.isdecimal():
                    raise UploadRejected("bad_headers", "上传请求中的数字信息不正确。", http_status=400)
                return int(value)

            def _discard_declared_body(self) -> None:
                values = self.headers.get_all("Content-Length") or []
                if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
                    return
                remaining = int(values[0])
                if remaining < 0 or remaining > MAX_UPLOAD_BYTES:
                    return
                while remaining:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)

            def do_OPTIONS(self) -> None:  # noqa: N802
                try:
                    origin = self._origin()
                    if self._one_header("Access-Control-Request-Method") != "POST":
                        raise UploadRejected("bad_preflight", "本机画布上传预检不正确。", http_status=400)
                except UploadRejected as exc:
                    self.close_connection = True
                    self._write_json(
                        exc.http_status,
                        {"ok": False, "errorCode": exc.code, "error": exc.user_message},
                    )
                    return
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-Canvas-Agent-Token, X-Canvas-File-Name, X-Canvas-File-Size, "
                    "X-Canvas-File-Sha256, X-Canvas-File-Last-Modified",
                )
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                origin: str | None = None
                try:
                    origin, _provided = self._authenticate()
                    if self.headers.get("Transfer-Encoding"):
                        raise UploadRejected("bad_length", "原图上传必须提供明确文件大小。", http_status=400)
                    content_length = self._integer_header(self._one_header("Content-Length"))
                    if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
                        raise UploadRejected("bad_length", "原图文件大小超出本机登记限制。", http_status=413)
                    parsed = urllib.parse.urlsplit(self.path)
                    segments = parsed.path.strip("/").split("/")
                    if parsed.query or parsed.fragment or len(segments) != 5 or segments[0] != "batch-intake" or segments[3] != "files":
                        raise UploadRejected("bad_route", "原图上传地址不正确。", http_status=404)
                    batch_id = _decode_route_value(segments[1], label="批次号")
                    request_id = _decode_route_value(segments[2], label="请求编号")
                    source_node_id = _decode_route_value(segments[4], label="图片编号")
                    outcome = facade.service.accept_upload(
                        batch_id=batch_id,
                        request_id=request_id,
                        source_node_id=source_node_id,
                        file_name=self._one_header("X-Canvas-File-Name"),
                        declared_size=self._integer_header(self._one_header("X-Canvas-File-Size")),
                        declared_sha256=self._one_header("X-Canvas-File-Sha256"),
                        declared_last_modified=self._integer_header(self._one_header("X-Canvas-File-Last-Modified")),
                        content_type=self._one_header("Content-Type"),
                        content_length=content_length,
                        stream=self.rfile,
                    )
                except UploadRejected as exc:
                    if exc.code in {"forbidden_origin", "unauthorized"}:
                        self._discard_declared_body()
                    self.close_connection = True
                    self._write_json(
                        exc.http_status,
                        {"ok": False, "errorCode": exc.code, "error": exc.user_message},
                        origin=origin,
                    )
                    return
                except Exception:
                    self.close_connection = True
                    self._write_json(
                        500,
                        {"ok": False, "errorCode": "service_error", "error": "本机登记服务发生异常，已经停止本次上传。"},
                        origin=origin,
                    )
                    return
                self._write_json(200, {"ok": True, "sha256": outcome.sha256}, origin=origin)

        return Handler

    def start(self) -> None:
        if self.is_running:
            return
        httpd = _LocalThreadingHTTPServer((self.host, self.port), self._handler_type())
        self._httpd = httpd
        self._bound_port = int(httpd.server_address[1])
        self.service.set_upload_endpoint(self.host, self._bound_port)
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="canvas-batch-upload",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is None:
            return
        if thread is not None and thread.is_alive():
            httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
