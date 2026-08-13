from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from batch_intake_contract import (
    BatchIntakeContractError,
    batch_intake_contract_sha256,
)
from category_recipes import DEFAULT_CATEGORY_KEY
from codex_dev_downstream import ExecutorExecutionError, parse_user_confirmed_requirements
from image_count_contract import (
    detail_handheld_limit_message,
    handheld_count_maximum,
)


BATCH_INFO_NODE_TYPE = "batch-info"
BATCH_INTAKE_METADATA_KEY = "batchIntake"
BATCH_BUILD_VERB = "build"
BATCH_BUILD_TARGET = "batch"
BATCH_TYPE_SINGLE = "single"
BATCH_TYPE_SET = "set"
IMAGE_CATEGORY_WHITE_BG = "white_bg"
IMAGE_CATEGORY_SET_GROUP = "set_group"
IMAGE_CATEGORY_COMPONENT_WHITE_BG = "component_white_bg"
SET_GROUP_IMAGE_COUNT_MINIMUM = 1
SET_GROUP_IMAGE_COUNT_MAXIMUM = 3
COMPONENT_WHITE_BG_IMAGE_COUNT_MINIMUM = 2
COMPONENT_WHITE_BG_IMAGE_COUNT_MAXIMUM = 8
DEFAULT_MAX_AGE_MS = 8_000
DEFAULT_FUTURE_TOLERANCE_MS = 0
DUPLICATE_PRODUCT_IMAGE_MESSAGE = (
    "同一张图被重复加入本次产品原图登记，不能建批。"
    "请删除重复项，只保留一张；产品原图连工作流机器，风格参考图连信息卡。"
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_BATCH_INTAKE_ALLOWED_KEYS = frozenset(
    {
        "status",
        "category",
        "contractHash",
        "batch_type",
        "productType",
        "productLengthCm",
        "productWidthCm",
        "productHeightCm",
        "allowClearWater",
        "prohibitPouringAndHeating",
        "skipMissingDAngle",
        "mainImageCount",
        "detailImageCount",
        "handheldMainCount",
        "handheldDetailCount",
        "facts",
        "requestId",
        "requestedAt",
        "updatedAt",
        "workflowNodeId",
        "sourceImageNodeIds",
        "setGroupImageNodeIds",
        "componentWhiteBgImageNodeIds",
        "batchId",
        "uploadBaseUrl",
        "expectedCount",
        "receivedCount",
        "errorMessage",
        "receipt",
    }
)


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
    height_cm: int | None
    main_image_count: int
    detail_image_count: int
    handheld_main: int
    handheld_detail: int
    forbid_pouring_and_heating: bool
    missing_d_no_retake: bool
    length_cm: int | None = None
    width_cm: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_type": self.product_type,
            "length_cm": self.length_cm,
            "width_cm": self.width_cm,
            "height_cm": self.height_cm,
            "main_image_count": self.main_image_count,
            "detail_image_count": self.detail_image_count,
            "handheld_main": self.handheld_main,
            "handheld_detail": self.handheld_detail,
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
    image_category: str = IMAGE_CATEGORY_WHITE_BG

    def route_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "storageKey": self.storage_key,
            "name": self.name,
            "size": self.size,
            "mimeType": self.mime_type,
            "lastModified": self.last_modified,
            "sha256": self.expected_sha256,
            "imageCategory": self.image_category,
        }


@dataclass(frozen=True)
class BatchIntakeRequest:
    request_id: str
    requested_at: int
    info_node_id: str
    workflow_node_id: str
    facts: ConfirmedFacts
    source_images: tuple[SourceImage, ...]
    category: str = DEFAULT_CATEGORY_KEY
    contract_hash: str = ""
    batch_type: str = BATCH_TYPE_SINGLE

    def route_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "requestedAt": self.requested_at,
            "infoNodeId": self.info_node_id,
            "workflowNodeId": self.workflow_node_id,
            "category": self.category,
            "contractHash": self.contract_hash,
            "batch_type": self.batch_type,
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
    category: str,
    batch_type: str = BATCH_TYPE_SINGLE,
    repository_root: Path,
    info_node_id: str,
    request_id: str,
) -> ConfirmedFacts:
    if isinstance(raw, Mapping):
        detail_image_count = raw.get("detail_image_count")
        handheld_detail = raw.get("handheld_detail")
        handheld_main = raw.get("handheld_main")
        if batch_type == BATCH_TYPE_SET and any(
            raw.get(field) is not None for field in ("length_cm", "width_cm", "height_cm")
        ):
            raise _error(
                "invalid_facts",
                "套装批次不填写长、宽、高，请清空三项尺寸后再登记。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
        if (
            batch_type == BATCH_TYPE_SET
            and type(handheld_main) is int
            and type(handheld_detail) is int
            and (handheld_main != 0 or handheld_detail != 0)
        ):
            raise _error(
                "invalid_facts",
                "套装批次暂不支持手持，主图与详情手持数量必须为 0。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
        if (
            type(detail_image_count) is int
            and type(handheld_detail) is int
            and handheld_detail
            > handheld_count_maximum("detail", detail_image_count)
        ):
            raise _error(
                "invalid_facts",
                detail_handheld_limit_message(detail_image_count),
                info_node_id=info_node_id,
                request_id=request_id,
            )
    try:
        if (
            not isinstance(raw, Mapping)
            or "main_image_count" not in raw
            or "detail_image_count" not in raw
            or "allow_clear_water" in raw
        ):
            raise ExecutorExecutionError("image counts missing")
        parsed = parse_user_confirmed_requirements(
            {
                "category": category,
                "batch_type": batch_type,
                "user_confirmed_facts": raw,
            },
            repository_root,
        )
        if parsed.recipe is None or parsed.product_type != parsed.recipe.product_noun:
            raise ExecutorExecutionError("category product noun mismatch")
    except ExecutorExecutionError:
        message = (
            "商品信息没有填写完整，请检查品类、图片张数、手持数量和高级选项。"
            if batch_type == BATCH_TYPE_SET
            else "商品信息没有填写完整，请检查品类、必填尺寸、图片张数、手持数量和高级选项。"
        )
        raise _error(
            "invalid_facts",
            message,
            info_node_id=info_node_id,
            request_id=request_id,
        ) from None
    return ConfirmedFacts(
        product_type=parsed.product_type,
        length_cm=parsed.length_cm,
        width_cm=parsed.width_cm,
        height_cm=parsed.height_cm,
        main_image_count=parsed.main_image_count,
        detail_image_count=parsed.detail_image_count,
        handheld_main=parsed.handheld_main,
        handheld_detail=parsed.handheld_detail,
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
    image_category: str,
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
        image_category=image_category,
    )


def _parse_image_node_ids(
    raw: Any,
    *,
    allow_missing: bool,
    info_node_id: str,
    request_id: str,
) -> tuple[str, ...]:
    if raw is None and allow_missing:
        return ()
    if (
        not isinstance(raw, list)
        or any(not isinstance(value, str) or not value for value in raw)
        or len(raw) != len(set(raw))
    ):
        raise _error(
            "invalid_images",
            "套装图片选择不完整，请重新选择后再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    return tuple(raw)


def parse_queued_request(
    state: Mapping[str, Any],
    info_node: Mapping[str, Any],
    *,
    now_ms: int,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    future_tolerance_ms: int = DEFAULT_FUTURE_TOLERANCE_MS,
    repository_root: Path | None = None,
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
    root = repository_root or Path(__file__).resolve().parent.parent
    category_value = batch.get("category")
    contract_hash_value = batch.get("contractHash")
    batch_type_value = batch.get("batch_type")
    try:
        expected_contract_hash = batch_intake_contract_sha256(root)
    except BatchIntakeContractError:
        raise _error(
            "contract_unavailable",
            "批次信息字段契约无法核对，请重启工作台并刷新画布后再试。",
            info_node_id=info_node_id,
            request_id=request_id,
        ) from None
    if (
        not set(batch).issubset(_BATCH_INTAKE_ALLOWED_KEYS)
        or not isinstance(category_value, str)
        or not category_value.strip()
        or not isinstance(contract_hash_value, str)
        or not _SHA256.fullmatch(contract_hash_value)
        or contract_hash_value.lower() != expected_contract_hash
        or type(batch_type_value) is not str
        or batch_type_value not in {BATCH_TYPE_SINGLE, BATCH_TYPE_SET}
    ):
        raise _error(
            "contract_mismatch",
            "批次信息卡版本与工作台不一致，请重启工作台并刷新画布后再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    category = category_value.strip()
    contract_hash = contract_hash_value.lower()
    batch_type = batch_type_value
    _parse_command(
        metadata.get("content"),
        expected_request_id=request_id,
        expected_requested_at=requested_at,
        info_node_id=info_node_id,
    )
    facts = _parse_facts(
        batch.get("facts"),
        category=category,
        batch_type=batch_type,
        repository_root=root,
        info_node_id=info_node_id,
        request_id=request_id,
    )
    set_group_image_ids = _parse_image_node_ids(
        batch.get("setGroupImageNodeIds"),
        allow_missing="setGroupImageNodeIds" not in batch,
        info_node_id=info_node_id,
        request_id=request_id,
    )
    component_white_bg_image_ids = _parse_image_node_ids(
        batch.get("componentWhiteBgImageNodeIds"),
        allow_missing="componentWhiteBgImageNodeIds" not in batch,
        info_node_id=info_node_id,
        request_id=request_id,
    )
    if batch_type == BATCH_TYPE_SINGLE:
        if set_group_image_ids or component_white_bg_image_ids:
            raise _error(
                "invalid_images",
                "单品批次不能登记套装图片，请清空套装图片后再登记。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
    elif not (
        SET_GROUP_IMAGE_COUNT_MINIMUM
        <= len(set_group_image_ids)
        <= SET_GROUP_IMAGE_COUNT_MAXIMUM
    ) or not (
        COMPONENT_WHITE_BG_IMAGE_COUNT_MINIMUM
        <= len(component_white_bg_image_ids)
        <= COMPONENT_WHITE_BG_IMAGE_COUNT_MAXIMUM
    ):
        raise _error(
            "invalid_images",
            "套装合影白底图须为 1–3 张，各单件白底图须为 2–8 张。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    has_declared_source_image_ids = "sourceImageNodeIds" in batch
    declared_source_image_ids = _parse_image_node_ids(
        batch.get("sourceImageNodeIds"),
        allow_missing=batch_type == BATCH_TYPE_SINGLE and not has_declared_source_image_ids,
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

    connected_image_ids = []
    seen_image_ids: set[str] = set()
    for connection in valid_connections:
        source_id = connection["fromNodeId"]
        if connection["toNodeId"] != workflow_node_id:
            continue
        source_node = node_by_id[source_id]
        if source_node.get("type") != "image" or source_id in seen_image_ids:
            continue
        seen_image_ids.add(source_id)
        connected_image_ids.append(source_id)
    if not connected_image_ids:
        raise _error(
            "missing_images",
            "请至少把一张磁盘原图连接到同一台工作流机器。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    connected_image_id_set = set(connected_image_ids)
    set_group_image_id_set = set(set_group_image_ids)
    component_white_bg_image_id_set = set(component_white_bg_image_ids)
    if set_group_image_id_set & component_white_bg_image_id_set:
        raise _error(
            "invalid_images",
            "同一张图片不能同时用于多个商品图片类别，请重新选择后再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    if (
        batch_type == BATCH_TYPE_SET
        and (set_group_image_id_set | component_white_bg_image_id_set)
        != connected_image_id_set
    ):
        raise _error(
            "invalid_images",
            "套装的合影与单件白底图必须恰好覆盖全部已连接原图，请重新勾选后再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )
    expected_source_image_ids = connected_image_id_set
    if has_declared_source_image_ids:
        if set(declared_source_image_ids) != expected_source_image_ids:
            raise _error(
                "invalid_images",
                "登记图片与画布选择不一致，请重新选择后再登记。",
                info_node_id=info_node_id,
                request_id=request_id,
            )
        image_ids = declared_source_image_ids
    else:
        image_ids = tuple(connected_image_ids)
    if any(
        node_id not in node_by_id or node_by_id[node_id].get("type") != "image"
        for node_id in image_ids
    ):
        raise _error(
            "invalid_images",
            "登记图片与画布选择不一致，请重新选择后再登记。",
            info_node_id=info_node_id,
            request_id=request_id,
        )

    def image_category(node_id: str) -> str:
        if node_id in set_group_image_id_set:
            return IMAGE_CATEGORY_SET_GROUP
        if node_id in component_white_bg_image_id_set:
            return IMAGE_CATEGORY_COMPONENT_WHITE_BG
        return IMAGE_CATEGORY_WHITE_BG

    sources = tuple(
        _parse_source_image(
            node_by_id[node_id],
            image_category=image_category(node_id),
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
        category=category,
        contract_hash=contract_hash,
        batch_type=batch_type,
    )
