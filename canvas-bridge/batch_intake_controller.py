from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codex_dev_downstream import ExecutorExecutionError, parse_user_confirmed_requirements


BATCH_INFO_NODE_TYPE = "batch-info"
BATCH_INTAKE_METADATA_KEY = "batchIntake"
BATCH_BUILD_VERB = "build"
BATCH_BUILD_TARGET = "batch"
DEFAULT_MAX_AGE_MS = 8_000
DEFAULT_FUTURE_TOLERANCE_MS = 0
DUPLICATE_PRODUCT_IMAGE_MESSAGE = (
    "同一张图被重复加入本次产品原图登记，不能建批。"
    "请删除重复项，只保留一张；产品原图连工作流机器，风格参考图连信息卡。"
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class BatchIntakeGateError(ValueError):
    """A fail-closed intake rejection with a canvas-safe explanation."""

    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        info_node_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.info_node_id = info_node_id
        self.request_id = request_id


@dataclass(frozen=True)
class ConfirmedFacts:
    product_type: str
    height_cm: int
    handheld_main: int
    handheld_detail: int
    allow_clear_water: bool
    forbid_pouring_and_heating: bool
    missing_d_no_retake: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_type": self.product_type,
            "height_cm": self.height_cm,
            "handheld_main": self.handheld_main,
            "handheld_detail": self.handheld_detail,
            "allow_clear_water": self.allow_clear_water,
            "forbid_pouring_and_heating": self.forbid_pouring_and_heating,
            "missing_d_no_retake": self.missing_d_no_retake,
        }


@dataclass(frozen=True)
class SourceImage:
    node_id: str
    storage_key: str
    name: str
    size: int
    mime_type: str
    last_modified: int
    expected_sha256: str

    def route_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "storageKey": self.storage_key,
            "name": self.name,
            "size": self.size,
            "mimeType": self.mime_type,
            "lastModified": self.last_modified,
            "sha256": self.expected_sha256,
        }


@dataclass(frozen=True)
class BatchIntakeRequest:
    request_id: str
    requested_at: int
    info_node_id: str
    workflow_node_id: str
    facts: ConfirmedFacts
    source_images: tuple[SourceImage, ...]

    def route_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "requestedAt": self.requested_at,
            "infoNodeId": self.info_node_id,
            "workflowNodeId": self.workflow_node_id,
            "facts": self.facts.as_dict(),
            "sourceImages": [source.route_dict() for source in self.source_images],
        }


def _error(
    code: str,
    message: str,
    *,
    info_node_id: str | None = None,
    request_id: str | None = None,
) -> BatchIntakeGateError:
    return BatchIntakeGateError(
        code,
        message,
        info_node_id=info_node_id,
        request_id=request_id,
    )


def _node_id(node: Mapping[str, Any]) -> str | None:
    value = node.get("id")
    return value if isinstance(value, str) and value else None


def _batch_metadata(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = node.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get(BATCH_INTAKE_METADATA_KEY)
    return value if isinstance(value, Mapping) else None


def queued_info_nodes(state: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        return ()
    return tuple(
        node
        for node in nodes
        if isinstance(node, Mapping)
        and node.get("type") == BATCH_INFO_NODE_TYPE
        and (_batch_metadata(node) or {}).get("status") == "queued"
    )


def _parse_facts(
    raw: Any,
    *,
    info_node_id: str,
    request_id: str,
) -> ConfirmedFacts:
    try:
        parsed = parse_user_confirmed_requirements({"user_confirmed_facts": raw})
    except ExecutorExecutionError:
        raise _error(
            "invalid_facts",
            "商品信息没有填写完整，请检查品类、高度和三个确认开关。",
            info_node_id=info_node_id,
            request_id=request_id,
        ) from None
    return ConfirmedFacts(
        product_type=parsed.product_type,
        height_cm=parsed.height_cm,
        handheld_main=parsed.handheld_main,
        handheld_detail=parsed.handheld_detail,
        allow_clear_water=parsed.allow_clear_water,
        forbid_pouring_and_heating=parsed.forbid_pouring_and_heating,
        missing_d_no_retake=parsed.missing_d_no_retake,
    )


def _parse_command(
    content: Any,
    *,
    expected_request_id: str,
    expected_requested_at: int,
    info_node_id: str,
) -> None:
    if not isinstance(content, str):
        raise _error(
            "invalid_command",
            "登记请求格式不正确，请重新点击“登记批次”。",
            info_node_id=info_node_id,
            request_id=expected_request_id,
        )
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    expected = [
        "# batch-intake",
        f"# request-id: {expected_request_id}",
        f"# requested-at: {expected_requested_at}",
        f"{BATCH_BUILD_VERB}: {BATCH_BUILD_TARGET}",
    ]
    if lines != expected:
        raise _error(
            "invalid_command",
            "登记请求格式不正确，请重新点击“登记批次”。",
            info_node_id=info_node_id,
            request_id=expected_request_id,
        )


def _safe_source_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return None
    if any(character in value for character in ("/", "\\", "\x00")):
        return None
    if value != value.strip() or value.endswith((".", " ")):
        return None
    return value


def _parse_source_image(
    node: Mapping[str, Any],
    *,
    info_node_id: str,
    request_id: str,
) -> SourceImage:
    node_id = _node_id(node)
    metadata = node.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    source_file = metadata.get("sourceFile")
    if not isinstance(source_file, Mapping):
        raise _error(
            "derived_image",
            "连接的图片中包含派生图片；请只连接磁盘原图（从磁盘直接拖入）。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    storage_key = metadata.get("storageKey")
    name = _safe_source_name(source_file.get("name"))
    size = source_file.get("size")
    mime_type = source_file.get("type")
    last_modified = source_file.get("lastModified")
    sha256 = source_file.get("sha256")
    valid = (
        node_id is not None
        and isinstance(storage_key, str)
        and storage_key.strip().startswith("image:")
        and len(storage_key.strip()) > len("image:")
        and name is not None
        and type(size) is int
        and size > 0
        and isinstance(mime_type, str)
        and mime_type.startswith("image/")
        and len(mime_type) <= 127
        and type(last_modified) is int
        and last_modified >= 0
        and isinstance(sha256, str)
        and bool(_SHA256.fullmatch(sha256))
    )
    if not valid:
        raise _error(
            "invalid_image",
            "有一张磁盘原图的信息不完整，请重新拖入该图片。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    return SourceImage(
        node_id=node_id,
        storage_key=storage_key.strip(),
        name=name,
        size=size,
        mime_type=mime_type,
        last_modified=last_modified,
        expected_sha256=sha256.lower(),
    )


def parse_queued_request(
    state: Mapping[str, Any],
    info_node: Mapping[str, Any],
    *,
    now_ms: int,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    future_tolerance_ms: int = DEFAULT_FUTURE_TOLERANCE_MS,
) -> BatchIntakeRequest:
    """Validate one queued `build: batch` card against the authoritative canvas graph."""

    nodes = state.get("nodes") if isinstance(state, Mapping) else None
    connections = state.get("connections") if isinstance(state, Mapping) else None
    if not isinstance(nodes, list) or not isinstance(connections, list):
        raise _error("invalid_canvas", "当前画布状态无法核对，请刷新后重试。")
    if type(now_ms) is not int or now_ms < 0 or type(max_age_ms) is not int or max_age_ms <= 0:
        raise _error("invalid_canvas", "当前画布状态无法核对，请刷新后重试。")
    if type(future_tolerance_ms) is not int or future_tolerance_ms < 0:
        raise _error("invalid_canvas", "当前画布状态无法核对，请刷新后重试。")
    if not isinstance(info_node, Mapping):
        raise _error("invalid_canvas", "当前画布状态无法核对，请刷新后重试。")

    info_node_id = _node_id(info_node)
    batch = _batch_metadata(info_node)
    if info_node.get("type") != BATCH_INFO_NODE_TYPE or info_node_id is None or batch is None:
        raise _error("invalid_request", "这张卡片不是有效的批次信息卡。")
    if batch.get("status") != "queued":
        raise _error(
            "invalid_request",
            "这张信息卡当前没有等待登记的请求。",
            info_node_id=info_node_id,
        )

    request_id_value = batch.get("requestId")
    if not isinstance(request_id_value, str) or not _REQUEST_ID.fullmatch(request_id_value):
        raise _error(
            "invalid_request",
            "登记请求编号无效，请重新点击“登记批次”。",
            info_node_id=info_node_id,
        )
    request_id = request_id_value
    requested_at = batch.get("requestedAt")
    if (
        type(requested_at) is not int
        or requested_at < 0
        or requested_at > now_ms + future_tolerance_ms
        or now_ms - requested_at >= max_age_ms
    ):
        raise _error(
            "expired_request",
            "这次登记请求已失效，请重新点击“登记批次”。",
            info_node_id=info_node_id,
            request_id=request_id,
        )

    metadata = info_node.get("metadata")
    assert isinstance(metadata, Mapping)
    _parse_command(
        metadata.get("content"),
        expected_request_id=request_id,
        expected_requested_at=requested_at,
        info_node_id=info_node_id,
    )
    facts = _parse_facts(
        batch.get("facts"),
        info_node_id=info_node_id,
        request_id=request_id,
    )

    node_values = [node for node in nodes if isinstance(node, Mapping)]
    if len(node_values) != len(nodes):
        raise _error(
            "invalid_canvas",
            "当前画布状态无法核对，请刷新后重试。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    ids = [_node_id(node) for node in node_values]
    if any(node_id is None for node_id in ids):
        raise _error(
            "invalid_canvas",
            "当前画布状态无法核对，请刷新后重试。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    duplicate_ids = {node_id for node_id in ids if ids.count(node_id) > 1}
    if duplicate_ids:
        duplicate_nodes = [node for node in node_values if _node_id(node) in duplicate_ids]
        code = "duplicate_image" if any(node.get("type") == "image" for node in duplicate_nodes) else "invalid_canvas"
        raise _error(
            code,
            "画布中存在重复图片，请删除重复项后再登记。" if code == "duplicate_image" else "当前画布状态无法核对，请刷新后重试。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    node_by_id = {_node_id(node): node for node in node_values}
    authoritative_info = node_by_id.get(info_node_id)
    if authoritative_info is None or authoritative_info.get("type") != BATCH_INFO_NODE_TYPE:
        raise _error(
            "invalid_canvas",
            "当前画布状态无法核对，请刷新后重试。",
            info_node_id=info_node_id,
            request_id=request_id,
        )

    valid_connections: list[Mapping[str, Any]] = []
    for connection in connections:
        if not isinstance(connection, Mapping):
            raise _error(
                "invalid_canvas",
                "当前画布连线无法核对，请刷新后重试。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
        from_id = connection.get("fromNodeId")
        to_id = connection.get("toNodeId")
        if not isinstance(from_id, str) or not isinstance(to_id, str):
            raise _error(
                "invalid_canvas",
                "当前画布连线无法核对，请刷新后重试。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
        if from_id in node_by_id and to_id in node_by_id:
            valid_connections.append(connection)

    workflow_ids = {
        connection["toNodeId"]
        for connection in valid_connections
        if connection["fromNodeId"] == info_node_id
        and node_by_id[connection["toNodeId"]].get("type") == "workflow"
    }
    if len(workflow_ids) != 1:
        raise _error(
            "invalid_connection",
            "请把这张信息卡连接到且只连接到一台工作流机器。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    workflow_node_id = next(iter(workflow_ids))
    connected_info_ids = {
        connection["fromNodeId"]
        for connection in valid_connections
        if connection["toNodeId"] == workflow_node_id
        and node_by_id[connection["fromNodeId"]].get("type") == BATCH_INFO_NODE_TYPE
    }
    if connected_info_ids != {info_node_id}:
        raise _error(
            "invalid_connection",
            "这台工作流机器连接了多张信息卡，请只保留本次登记的一张。",
            info_node_id=info_node_id,
            request_id=request_id,
        )

    image_ids = []
    seen_image_ids: set[str] = set()
    for connection in valid_connections:
        source_id = connection["fromNodeId"]
        if connection["toNodeId"] != workflow_node_id:
            continue
        source_node = node_by_id[source_id]
        if source_node.get("type") != "image" or source_id in seen_image_ids:
            continue
        seen_image_ids.add(source_id)
        image_ids.append(source_id)
    if not image_ids:
        raise _error(
            "missing_images",
            "请至少把一张磁盘原图连接到同一台工作流机器。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    sources = tuple(
        _parse_source_image(
            node_by_id[node_id],
            info_node_id=info_node_id,
            request_id=request_id,
        )
        for node_id in image_ids
    )
    source_hashes = [source.expected_sha256 for source in sources]
    if len(source_hashes) != len(set(source_hashes)):
        raise _error(
            "duplicate_image",
            DUPLICATE_PRODUCT_IMAGE_MESSAGE,
            info_node_id=info_node_id,
            request_id=request_id,
        )
    folded_names = [source.name.casefold() for source in sources]
    if len(folded_names) != len(set(folded_names)):
        raise _error(
            "duplicate_image",
            "原图文件名有重复，请先保留唯一文件名再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    return BatchIntakeRequest(
        request_id=request_id,
        requested_at=requested_at,
        info_node_id=info_node_id,
        workflow_node_id=workflow_node_id,
        facts=facts,
        source_images=sources,
    )
