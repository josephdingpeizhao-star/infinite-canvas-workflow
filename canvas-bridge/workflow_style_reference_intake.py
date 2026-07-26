"""Exact-byte supplemental style-reference publication for an existing batch."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
import ic_client
import run_controller


SUPPORTED_SUFFIXES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMAND_MAX_AGE_MS = 8_000
DEFAULT_STYLE_UPLOAD_HOST = "127.0.0.1"
DEFAULT_STYLE_UPLOAD_PORT = 17_373
MAX_STYLE_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_STYLE_REFERENCE_FILES = 20
CROSS_ROLE_IMAGE_MESSAGE = (
    "这张图已经是本批的产品原图，不能再登记为风格参考。"
    "若是接反了：产品原图连工作流机器，风格参考图连信息卡。"
)
UNSAFE_PRODUCT_EVIDENCE_MESSAGE = (
    "无法安全核对本批已登记的产品原图，风格补登已停止。"
    "请保留现场并交由顾问核对，系统不会自动重试。"
)


class StyleReferenceIntakeError(ValueError):
    """Supplemental evidence failed closed; no retry is implied."""


class StyleReferenceUploadRejected(StyleReferenceIntakeError):
    """One browser upload was rejected with a safe local-only response."""

    def __init__(self, user_message: str, *, http_status: int = 409):
        self.user_message = user_message
        self.http_status = http_status
        super().__init__(user_message)


@dataclass(frozen=True)
class StyleReferenceUpload:
    node_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class StyleReferencePublishResult:
    batch_id: str
    file_count: int
    receipt_path: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class StyleReferenceUploadOutcome:
    sha256: str
    completed: bool


@dataclass(frozen=True)
class _StyleSource:
    node_id: str
    name: str
    mime_type: str
    size: int
    sha256: str


@dataclass
class _StyleUploadSession:
    request_id: str
    batch_id: str
    card: dict[str, Any]
    sources: dict[str, _StyleSource]
    uploads: dict[str, StyleReferenceUpload]
    blocked: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StyleReferenceIntakeError(f"{label}无法读取。") from exc
    if not isinstance(value, dict):
        raise StyleReferenceIntakeError(f"{label}格式无效。")
    return value


def _validate_upload(upload: StyleReferenceUpload) -> None:
    if not upload.node_id or len(upload.node_id) > 200:
        raise StyleReferenceIntakeError("风格参考图节点编号无效。")
    if not upload.name or Path(upload.name).name != upload.name or upload.name in {".", ".."}:
        raise StyleReferenceIntakeError("风格参考图文件名无效。")
    suffix = Path(upload.name).suffix.lower()
    expected_mime = SUPPORTED_SUFFIXES.get(suffix)
    if expected_mime is None or upload.mime_type.lower() != expected_mime:
        raise StyleReferenceIntakeError("风格参考图格式不受支持。")
    if upload.size <= 0 or upload.size != len(upload.data):
        raise StyleReferenceIntakeError("风格参考图字节数不一致。")
    if not re.fullmatch(r"[0-9a-f]{64}", upload.sha256):
        raise StyleReferenceIntakeError("风格参考图哈希格式无效。")
    if _sha256(upload.data) != upload.sha256:
        raise StyleReferenceIntakeError("风格参考图 SHA-256 不一致，已硬停止。")
    if expected_mime == "image/jpeg" and not upload.data.startswith(b"\xff\xd8\xff"):
        raise StyleReferenceIntakeError("风格参考图内容不是有效 JPEG。")
    if expected_mime == "image/png" and not upload.data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise StyleReferenceIntakeError("风格参考图内容不是有效 PNG。")
    if expected_mime == "image/webp" and not (
        len(upload.data) >= 12 and upload.data[:4] == b"RIFF" and upload.data[8:12] == b"WEBP"
    ):
        raise StyleReferenceIntakeError("风格参考图内容不是有效 WebP。")


def _registered_product_sha256s(manifest_path: Path) -> frozenset[str]:
    try:
        manifest = _read_json(manifest_path, "批次清单")
        batch_id = manifest.get("product_id")
        workspace_value = (manifest.get("workspace") or {}).get("root") if isinstance(manifest.get("workspace"), dict) else None
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else None
        asset_manifest_value = artifacts.get("asset_manifest") if artifacts else None
        if not isinstance(batch_id, str) or not batch_id or not isinstance(workspace_value, str) or not workspace_value:
            raise ValueError
        if not isinstance(asset_manifest_value, str) or not asset_manifest_value:
            raise ValueError
        workspace = Path(workspace_value).resolve(strict=True)
        marker = _read_json(workspace / ".canvas_batch", "批次安全标记")
        if marker.get("type") != "canvas-batch-v1" or marker.get("product_id") != batch_id:
            raise ValueError
        asset_manifest_path = Path(asset_manifest_value).resolve(strict=True)
        expected_asset_manifest = (workspace / "manifests" / "asset_manifest.json").resolve(strict=True)
        if asset_manifest_path != expected_asset_manifest or not _inside(asset_manifest_path, workspace):
            raise ValueError
        asset_manifest = _read_json(asset_manifest_path, "资产清单")
        assets = asset_manifest.get("assets")
        if not isinstance(assets, list) or not assets:
            raise ValueError
        white_bg_root = (workspace / "inputs" / "white_bg").resolve(strict=True)
        hashes: set[str] = set()
        for asset in assets:
            if (
                not isinstance(asset, Mapping)
                or asset.get("asset_role") != "white_bg"
                or asset.get("is_single_product_white_bg") is not True
                or asset.get("is_style_reference") is not False
            ):
                raise ValueError
            relative_value = asset.get("file_path")
            if not isinstance(relative_value, str) or not relative_value:
                raise ValueError
            relative = Path(relative_value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError
            target = (workspace / relative).resolve(strict=True)
            if target.parent != white_bg_root or not target.is_file() or target.is_symlink():
                raise ValueError
            hashes.add(_sha256_file(target))
        if not hashes:
            raise ValueError
        return frozenset(hashes)
    except (OSError, RuntimeError, TypeError, ValueError, StyleReferenceIntakeError):
        raise StyleReferenceIntakeError(UNSAFE_PRODUCT_EVIDENCE_MESSAGE) from None


def _reject_registered_product_hashes(manifest_path: Path, sha256s: tuple[str, ...]) -> None:
    if _registered_product_sha256s(manifest_path).intersection(sha256s):
        raise StyleReferenceIntakeError(CROSS_ROLE_IMAGE_MESSAGE)


def _publish_style_references_active(
    manifest_path: Path,
    request_id: str,
    uploads: tuple[StyleReferenceUpload, ...],
) -> StyleReferencePublishResult:
    """Publish verified direct files and a new receipt without rewriting intake evidence."""

    if not REQUEST_ID_RE.fullmatch(request_id):
        raise StyleReferenceIntakeError("风格补登请求编号无效。")
    if not uploads:
        raise StyleReferenceIntakeError("没有可补登的风格参考图。")
    manifest = _read_json(manifest_path, "批次清单")
    batch_id = str(manifest.get("product_id") or "").strip()
    workspace_value = (manifest.get("workspace") or {}).get("root") if isinstance(manifest.get("workspace"), dict) else None
    if not batch_id or not isinstance(workspace_value, str) or not workspace_value:
        raise StyleReferenceIntakeError("批次清单缺少工作区信息。")
    workspace = Path(workspace_value).resolve()
    marker = _read_json(workspace / ".canvas_batch", "批次安全标记")
    if marker.get("type") != "canvas-batch-v1" or marker.get("product_id") != batch_id:
        raise StyleReferenceIntakeError("批次安全标记与目标批次不一致。")
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
    raw_roots = inputs.get("style_reference_images")
    roots = raw_roots if isinstance(raw_roots, list) else []
    if len(roots) != 1 or not isinstance(roots[0], str):
        raise StyleReferenceIntakeError("批次没有唯一的风格参考目录。")
    style_root = Path(roots[0]).resolve(strict=False)
    expected_root = (workspace / "inputs" / "style_refs").resolve(strict=False)
    if style_root != expected_root or not _inside(style_root, workspace):
        raise StyleReferenceIntakeError("风格参考目录越过批准的批次边界。")

    names: set[str] = set()
    for upload in uploads:
        _validate_upload(upload)
        lowered = upload.name.casefold()
        if lowered in names:
            raise StyleReferenceIntakeError("同一次补登含重复文件名。")
        names.add(lowered)
    _reject_registered_product_hashes(
        manifest_path,
        tuple(upload.sha256 for upload in uploads),
    )

    targets = tuple(style_root / upload.name for upload in uploads)
    for upload, target in zip(uploads, targets, strict=True):
        if target.exists() and _sha256(target.read_bytes()) != upload.sha256:
            raise StyleReferenceIntakeError("风格参考目录已有同名但不同内容的文件，未覆盖。")

    receipt_path = workspace / "manifests" / f"style_reference_intake_receipt.{request_id}.json"
    if not _inside(receipt_path, workspace):
        raise StyleReferenceIntakeError("风格补登凭证路径越界。")
    if receipt_path.exists():
        raise StyleReferenceIntakeError("这次风格补登请求已经完成，不会重复写入。")

    style_root.mkdir(parents=True, exist_ok=True)
    for upload, target in zip(uploads, targets, strict=True):
        if target.exists():
            continue
        try:
            with target.open("xb") as handle:
                handle.write(upload.data)
        except FileExistsError:
            if _sha256(target.read_bytes()) != upload.sha256:
                raise StyleReferenceIntakeError("风格参考图并发写入冲突，已停止。") from None

    receipt = {
        "receipt_type": "style_reference_intake_v1",
        "product_id": batch_id,
        "request_id": request_id,
        "file_count": len(uploads),
        "files": [
            {
                "node_id": upload.node_id,
                "name": upload.name,
                "size": upload.size,
                "mime_type": upload.mime_type,
                "sha256": upload.sha256,
            }
            for upload in uploads
        ],
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        raise StyleReferenceIntakeError("这次风格补登请求已经完成，不会重复写入。") from None
    return StyleReferencePublishResult(
        batch_id=batch_id,
        file_count=len(uploads),
        receipt_path=str(receipt_path),
        files=tuple(upload.name for upload in uploads),
    )


def publish_style_references(
    manifest_path: Path,
    request_id: str,
    uploads: tuple[StyleReferenceUpload, ...],
    *,
    batch_lock_root: Path | None = None,
) -> StyleReferencePublishResult:
    """Freeze the publish boundary before its first workspace write."""

    manifest = _read_json(manifest_path, "批次清单")
    batch_id = str(manifest.get("product_id") or "").strip()
    if not batch_id:
        raise StyleReferenceIntakeError("批次清单缺少批次号。")
    journal = run_controller.journal_path(manifest_path, batch_id)
    try:
        with existing_batch_operation(
            batch_id,
            lock_root=batch_lock_root,
        ):
            try:
                lifecycle = read_batch_lifecycle(journal)
            except BatchLifecycleReadError:
                raise StyleReferenceIntakeError(
                    "批次账本暂时无法读取，风格补登未写入。"
                ) from None
            if lifecycle.recycled:
                raise StyleReferenceIntakeError(
                    "批次已回收，风格补登未写入。"
                )
            return _publish_style_references_active(
                manifest_path,
                request_id,
                uploads,
            )
    except BatchOperationBusy:
        raise StyleReferenceIntakeError(
            "本批次有任务正在运行，风格补登未写入。"
        ) from None


class WorkflowStyleReferenceService:
    """Open exact-byte upload sessions declared by a registered batch card.

    Bytes remain in memory until every declared source passes its own size and
    SHA-256 checks.  Only then does :func:`publish_style_references` create the
    supplemental files and a new receipt; original intake evidence is never
    rewritten.
    """

    def __init__(
        self,
        repository_root: Path,
        *,
        client: Any,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
        upload_host: str = DEFAULT_STYLE_UPLOAD_HOST,
        upload_port: int = DEFAULT_STYLE_UPLOAD_PORT,
        batch_lock_root: Path | None = None,
    ) -> None:
        if upload_host != DEFAULT_STYLE_UPLOAD_HOST:
            raise ValueError("风格参考上传只允许绑定 127.0.0.1 回环地址。")
        if type(upload_port) is not int or not 0 <= upload_port <= 65_535:
            raise ValueError("风格参考上传端口无效。")
        self.repository_root = repository_root.resolve()
        self.client = client
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.interval = interval
        self.upload_host = upload_host
        self.upload_port = upload_port
        self.batch_lock_root = batch_lock_root
        self.sessions: dict[str, _StyleUploadSession] = {}
        self.consumed_request_ids: set[str] = set()
        self.stopping = False
        self._lock = threading.RLock()
        self._status_callback: Callable[[str], None] | None = None
        self._last_worker_status: str | None = None

    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        self._status_callback = callback

    def _report_worker_status(self, status: str) -> None:
        if status == self._last_worker_status:
            return
        self._last_worker_status = status
        if self._status_callback is not None:
            self._status_callback(status)

    def set_upload_endpoint(self, host: str, port: int) -> None:
        if host != DEFAULT_STYLE_UPLOAD_HOST or type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError("风格参考上传端点无效。")
        self.upload_host = host
        self.upload_port = port

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
        current = metadata.get("styleReferenceIntake")
        intake = dict(current) if isinstance(current, Mapping) else {}
        intake.update(dict(fields or {}))
        intake["status"] = status
        intake["updatedAt"] = self.clock_ms()
        if error_message:
            intake["errorMessage"] = error_message
        else:
            intake.pop("errorMessage", None)
        self.client.apply_ops(
            [
                {
                    "type": "update_node",
                    "id": str(node.get("id") or ""),
                    "metadata": {"styleReferenceIntake": intake},
                }
            ]
        )
        metadata["styleReferenceIntake"] = intake
        node["metadata"] = metadata

    def _manifest_path(self, batch_id: str) -> Path:
        if not batch_id or Path(batch_id).name != batch_id or any(char in batch_id for char in ("/", "\\", "\0")):
            raise StyleReferenceIntakeError("风格补登的批次号无效。")
        path = self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        manifest = _read_json(path, "批次清单")
        if manifest.get("product_id") != batch_id:
            raise StyleReferenceIntakeError("风格补登的批次与清单不一致。")
        return path

    @staticmethod
    def _parse_source(value: Any) -> _StyleSource:
        if not isinstance(value, Mapping):
            raise StyleReferenceIntakeError("风格参考图声明格式无效。")
        node_id = value.get("nodeId")
        name = value.get("name")
        mime_type = value.get("mimeType")
        size = value.get("size")
        sha256 = value.get("sha256")
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(name, str)
            or not name
            or not isinstance(mime_type, str)
            or type(size) is not int
            or not 0 < size <= MAX_STYLE_UPLOAD_BYTES
            or not isinstance(sha256, str)
            or not SHA256_RE.fullmatch(sha256)
        ):
            raise StyleReferenceIntakeError("风格参考图声明不完整。")
        return _StyleSource(node_id, name, mime_type.lower(), size, sha256)

    def _open_session(self, node: dict[str, Any], state: Mapping[str, Any]) -> None:
        metadata = self._metadata(node)
        intake = metadata.get("styleReferenceIntake")
        if not isinstance(intake, Mapping) or intake.get("status") != "queued":
            return
        request_id = intake.get("requestId")
        requested_at = intake.get("requestedAt")
        batch_id = intake.get("batchId")
        try:
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                raise StyleReferenceIntakeError("风格补登请求编号无效。")
            if request_id in self.consumed_request_ids or request_id in self.sessions:
                raise StyleReferenceIntakeError("这次风格补登请求已被处理。")
            if type(requested_at) is not int or not 0 <= self.clock_ms() - requested_at <= COMMAND_MAX_AGE_MS:
                raise StyleReferenceIntakeError("风格补登请求已过期，请重新点击补登。")
            if not isinstance(batch_id, str):
                raise StyleReferenceIntakeError("风格补登缺少批次号。")
            batch_intake = metadata.get("batchIntake")
            receipt = batch_intake.get("receipt") if isinstance(batch_intake, Mapping) else None
            if (
                not isinstance(batch_intake, Mapping)
                or batch_intake.get("status") != "completed"
                or not isinstance(receipt, Mapping)
                or receipt.get("batchId") != batch_id
            ):
                raise StyleReferenceIntakeError("信息卡尚未登记完成，不能补登风格参考图。")
            manifest_path = self._manifest_path(batch_id)
            raw_sources = intake.get("sources")
            if not isinstance(raw_sources, list) or not 0 < len(raw_sources) <= MAX_STYLE_REFERENCE_FILES:
                raise StyleReferenceIntakeError("风格参考图数量无效。")
            sources = [self._parse_source(item) for item in raw_sources]
            source_by_id = {item.node_id: item for item in sources}
            if len(source_by_id) != len(sources):
                raise StyleReferenceIntakeError("风格参考图节点重复。")

            nodes = [item for item in state.get("nodes", []) if isinstance(item, Mapping)]
            node_by_id = {str(item.get("id") or ""): item for item in nodes}
            connected = {
                str(item.get("fromNodeId") or "")
                for item in state.get("connections", [])
                if isinstance(item, Mapping) and item.get("toNodeId") == node.get("id")
            }
            if set(source_by_id) - connected:
                raise StyleReferenceIntakeError("有风格参考图没有连到这张信息卡。")
            for source in sources:
                image = node_by_id.get(source.node_id)
                image_metadata = self._metadata(image or {})
                source_file = image_metadata.get("sourceFile")
                if not isinstance(image, Mapping) or image.get("type") != "image" or not isinstance(source_file, Mapping):
                    raise StyleReferenceIntakeError("风格参考图节点不完整。")
                expected = {
                    "name": source.name,
                    "mimeType": source.mime_type,
                    "size": source.size,
                    "sha256": source.sha256,
                }
                actual = {
                    "name": source_file.get("name"),
                    "mimeType": str(source_file.get("type") or "").lower(),
                    "size": source_file.get("size"),
                    "sha256": source_file.get("sha256"),
                }
                if actual != expected:
                    raise StyleReferenceIntakeError("风格参考图声明与画布原文件不一致。")
            _reject_registered_product_hashes(
                manifest_path,
                tuple(source.sha256 for source in sources),
            )
            session = _StyleUploadSession(
                request_id=request_id,
                batch_id=batch_id,
                card=node,
                sources=source_by_id,
                uploads={},
            )
            self.sessions[request_id] = session
            self.consumed_request_ids.add(request_id)
            self._update(
                node,
                "upload_ready",
                fields={"uploadBaseUrl": f"http://{self.upload_host}:{self.upload_port}"},
            )
        except StyleReferenceIntakeError as exc:
            if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
                self.consumed_request_ids.add(request_id)
            self._update(node, "failed", error_message=str(exc))

    def poll_once(self) -> None:
        state = self.client.call_tool("canvas_get_state")
        if not isinstance(state, Mapping):
            raise RuntimeError("无法读取画布，风格补登服务已停止。")
        for raw_node in state.get("nodes", []):
            if isinstance(raw_node, dict) and raw_node.get("type") == "batch-info":
                self._open_session(raw_node, state)

    def accept_upload(
        self,
        batch_id: str,
        request_id: str,
        node_id: str,
        data: bytes,
    ) -> StyleReferenceUploadOutcome:
        with self._lock:
            session = self.sessions.get(request_id)
            if session is None or session.batch_id != batch_id:
                raise StyleReferenceUploadRejected("找不到这次风格补登请求。", http_status=404)
            if session.blocked:
                raise StyleReferenceUploadRejected("这次风格补登已硬停止，请重新发起。")
            source = session.sources.get(node_id)
            if source is None:
                raise StyleReferenceUploadRejected("这张图不在本次风格补登清单中。", http_status=404)
            if node_id in session.uploads:
                raise StyleReferenceUploadRejected("这张风格参考图已上传，不会重复接收。")
            try:
                upload = StyleReferenceUpload(
                    node_id=source.node_id,
                    name=source.name,
                    mime_type=source.mime_type,
                    size=source.size,
                    sha256=source.sha256,
                    data=data,
                )
                _validate_upload(upload)
            except StyleReferenceIntakeError as exc:
                session.blocked = True
                session.uploads.clear()
                self._update(
                    session.card,
                    "integrity_blocked",
                    error_message="风格参考图与画布原文件不一致，已硬停止，请重新发起。",
                )
                raise StyleReferenceUploadRejected(str(exc)) from None
            session.uploads[node_id] = upload
            if len(session.uploads) < len(session.sources):
                return StyleReferenceUploadOutcome(sha256=upload.sha256, completed=False)
            try:
                result = publish_style_references(
                    self._manifest_path(batch_id),
                    request_id,
                    tuple(session.uploads[item] for item in session.sources),
                    batch_lock_root=self.batch_lock_root,
                )
            except StyleReferenceIntakeError as exc:
                session.blocked = True
                self._update(session.card, "failed", error_message=str(exc))
                raise StyleReferenceUploadRejected(str(exc)) from None
            self._update(
                session.card,
                "completed",
                fields={
                    "receipt": {
                        "batchId": result.batch_id,
                        "fileCount": result.file_count,
                        "files": list(result.files),
                    }
                },
            )
            return StyleReferenceUploadOutcome(sha256=upload.sha256, completed=True)

    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.poll_once()
            except ic_client.CanvasAgentError:
                self._report_worker_status("waiting_canvas")
                print(json.dumps({"style_reference_intake": "waiting_canvas"}, ensure_ascii=False), flush=True)
            else:
                self._report_worker_status("running")
            self.sleep(self.interval)
