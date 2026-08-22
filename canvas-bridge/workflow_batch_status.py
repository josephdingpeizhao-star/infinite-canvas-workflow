"""Read-only workflow-production status derived from the batch event ledger."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_RENDER_SUCCESS_COUNTS_PATTERN = re.compile(
    r"^成功 (?P<successful>\d+)/计划 (?P<planned>\d+)（跳过 (?P<skipped>\d+)）"
)
_RENDER_FAILURE_COUNTS_PATTERN = re.compile(
    r"(?:^|；)成功 (?P<successful>\d+)/计划 (?P<planned>\d+)/跳过 (?P<skipped>\d+)(?:；|$)"
)
_LEGACY_RENDER_FAILURE_COUNTS_PATTERN = re.compile(
    r"(?:^|：|；)成功 (?P<successful>\d+)/计划 (?P<planned>\d+)（跳过 (?P<skipped>\d+)）"
)
_MAX_RENDER_COUNT = 9_999
_MAX_RENDER_COUNT_DIGITS = len(str(_MAX_RENDER_COUNT))
_MAX_BATCH_ID_LENGTH = 128
_WORKFLOW_STEPS = frozenset(
    {
        "identity",
        "style_master",
        "angle_inventory",
        "main_vc",
        "detail_vc",
        "final_prompts",
        "integrity",
        "renders",
        "qc",
    }
)
_MODERN_PRODUCTION_COMMAND = "workflow-production"
_REPAIR_COMMAND = "repair"
_REPAIR_REQUEST_EVENTS = frozenset(
    {
        "gate_rejected",
        "step_started",
        "step_failed",
        "repair_item_started",
        "repair_item_skipped_existing",
        "repair_item_failed",
        "repair_item_succeeded",
        "renders_protection_failed",
        "repair_completed",
        "step_completed_with_failures",
        "step_succeeded",
    }
)
_REPAIR_SPECIFIC_EVENTS = _REPAIR_REQUEST_EVENTS - {
    "gate_rejected",
    "step_started",
    "step_failed",
    "step_succeeded",
}
_REPAIR_GATE_DETAILS = frozenset(
    {
        "返修命令未通过解析门禁",
        "返修条件未满足，未调用图片服务",
        "返修计划无效，未调用图片服务",
    }
)
_REPAIR_ITEM_FAILURE_DETAILS = frozenset(
    {
        "已有返修图格式无效，未覆盖且未自动重试",
        "图片返修执行失败，未自动重试",
    }
)
_REPAIR_PROTECTION_FAILURE_DETAIL = "正式 renders 在返修期间发生变化，已停止"
_REPAIR_STEP_FAILURE_DETAIL = "返修执行已停止，未自动重试"
_RENDER_RETRY_FAILURE_CODES = frozenset(
    {"render_http_error", "render_timeout", "render_network_error"}
)
_UNSAFE_WINDOWS_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_DEVICE_DIGIT_ALIASES = str.maketrans({"¹": "1", "²": "2", "³": "3"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class WorkflowBatchStatusError(ValueError):
    """A status ledger cannot be safely resolved."""

    def __init__(self, http_status: int, message: str):
        super().__init__(message)
        self.http_status = http_status


def _unavailable(message: str) -> WorkflowBatchStatusError:
    return WorkflowBatchStatusError(409, message)


def _is_unicode_scalar_text(value: Any, *, nonempty: bool = True) -> bool:
    if type(value) is not str or (nonempty and not value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _object_with_unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_batch_id(batch_id: str) -> None:
    reserved_stem = (
        batch_id.split(".", 1)[0]
        .rstrip(" .")
        .upper()
        .translate(_WINDOWS_DEVICE_DIGIT_ALIASES)
        if type(batch_id) is str
        else ""
    )
    if (
        type(batch_id) is not str
        or not _is_unicode_scalar_text(batch_id)
        or not batch_id
        or len(batch_id) > _MAX_BATCH_ID_LENGTH
        or batch_id != batch_id.strip()
        or batch_id.endswith((".", " "))
        or batch_id in {".", ".."}
        or Path(batch_id).name != batch_id
        or any(char in _UNSAFE_WINDOWS_FILENAME_CHARACTERS for char in batch_id)
        or any(ord(char) < 32 or ord(char) == 127 for char in batch_id)
        or reserved_stem in _WINDOWS_RESERVED_NAMES
    ):
        raise WorkflowBatchStatusError(400, "批次号无效，无法确认制作状态。")


def _validate_event_record(
    value: Mapping[str, Any],
    line_number: int,
    previous_recorded_at: datetime | None,
) -> datetime:
    event = value.get("event")
    timestamp = value.get("ts")
    recorded_at: datetime | None = None
    if type(timestamp) is str and _TIMESTAMP_PATTERN.fullmatch(timestamp) is not None:
        try:
            recorded_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    if type(event) is not str or not event or recorded_at is None:
        raise _unavailable(
            f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
        )
    if previous_recorded_at is not None and recorded_at < previous_recorded_at:
        raise _unavailable(
            f"批次状态账本第 {line_number} 行时间倒序，无法确认制作状态。"
        )
    return recorded_at


def _load_events(repository_root: Path, batch_id: str) -> list[dict[str, Any]]:
    _validate_batch_id(batch_id)
    raw_repository_root = Path(repository_root)
    manifests = raw_repository_root / "manifests"
    ledger = manifests / f"{batch_id}.events.jsonl"
    try:
        repository_resolved = raw_repository_root.resolve(strict=True)
        if manifests.is_symlink() or ledger.is_symlink():
            raise OSError("status ledger boundary is unsafe")
        manifests_resolved = manifests.resolve(strict=True)
        ledger_resolved = ledger.resolve(strict=True)
    except FileNotFoundError:
        raise WorkflowBatchStatusError(404, "批次状态账本不存在。") from None
    except (OSError, RuntimeError):
        raise _unavailable("批次状态账本无法读取，无法确认制作状态。") from None
    if (
        manifests_resolved.parent != repository_resolved
        or not manifests_resolved.is_dir()
        or ledger_resolved.parent != manifests_resolved
        or not ledger_resolved.is_file()
    ):
        raise _unavailable("批次状态账本无法读取，无法确认制作状态。")
    try:
        lines = ledger_resolved.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise _unavailable("批次状态账本无法读取，无法确认制作状态。") from None
    events: list[dict[str, Any]] = []
    previous_recorded_at: datetime | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise _unavailable(
                f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
            )
        try:
            value = json.loads(line, object_pairs_hook=_object_with_unique_keys)
        except (json.JSONDecodeError, ValueError, RecursionError):
            raise _unavailable(
                f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
            ) from None
        if not isinstance(value, dict):
            raise _unavailable(
                f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
            )
        previous_recorded_at = _validate_event_record(
            value,
            line_number,
            previous_recorded_at,
        )
        events.append(value)
    return events


def _nonnegative_integer(
    event: Mapping[str, Any],
    field: str,
    *,
    required: bool = True,
) -> int | None:
    value = event.get(field)
    if value is None and not required:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_RENDER_COUNT:
        raise _unavailable("批次状态账本的出图计数损坏，无法确认制作状态。")
    return value


def _workflow_step(event: Mapping[str, Any]) -> str:
    step = event.get("step")
    if type(step) is not str or step not in _WORKFLOW_STEPS:
        raise _unavailable("批次状态账本的阶段记录损坏，无法确认制作状态。")
    return step


def _modern_request_id(event: Mapping[str, Any]) -> str:
    request_id = event.get("request_id")
    if (
        type(request_id) is not str
        or not request_id
        or request_id != request_id.strip()
    ):
        raise _unavailable("批次状态账本的制作请求归属损坏，无法确认制作状态。")
    return request_id


def _legacy_command_matches_step(command: Any, step: str) -> bool:
    if type(command) is not str:
        return False
    for verb in ("run", "retry"):
        prefix = f"{verb}: "
        if not command.startswith(prefix):
            continue
        target = command[len(prefix) :]
        if not target or target != target.strip():
            return False
        if verb == "retry":
            return target == step
        return target in {"next", step}
    return False


@dataclass
class _RepairSaga:
    request_id: str
    state: str = "queued"
    target_count: int = 0
    work_order_count: int = 0
    active_config_id: str | None = None
    observed_target_count: int = 0
    results: dict[str, str] = field(default_factory=dict)
    protection_failed: bool = False
    completion_counts: tuple[int, int, int] | None = None
    failed_config_ids: tuple[str, ...] = ()


def _repair_count(
    event: Mapping[str, Any],
    field_name: str,
    *,
    positive: bool = False,
) -> int:
    value = event.get(field_name)
    lower_bound = 1 if positive else 0
    if type(value) is not int or not lower_bound <= value <= _MAX_RENDER_COUNT:
        raise _unavailable("批次状态账本的返修计数损坏，无法确认制作状态。")
    return value


def _repair_config_id(event: Mapping[str, Any]) -> str:
    config_id = event.get("config_id")
    if (
        not _is_unicode_scalar_text(config_id)
        or config_id != config_id.strip()
    ):
        raise _unavailable("批次状态账本的返修项记录损坏，无法确认制作状态。")
    return config_id


def _valid_sha256(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_artifact_dimensions(event: Mapping[str, Any]) -> bool:
    return all(
        type(event.get(field_name)) is int
        and 0 < event[field_name] <= 2_147_483_647
        for field_name in ("byte_count", "width", "height")
    )


def _repair_summary(event: Mapping[str, Any]) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    counts = (
        _repair_count(event, "succeeded_count"),
        _repair_count(event, "failed_count"),
        _repair_count(event, "skipped_count"),
    )
    raw_failed_ids = event.get("failed_config_ids")
    if type(raw_failed_ids) is not list:
        raise _unavailable("批次状态账本的返修汇总记录损坏，无法确认制作状态。")
    failed_ids: list[str] = []
    for value in raw_failed_ids:
        if (
            not _is_unicode_scalar_text(value)
            or value != value.strip()
            or value in failed_ids
        ):
            raise _unavailable("批次状态账本的返修汇总记录损坏，无法确认制作状态。")
        failed_ids.append(value)
    if len(failed_ids) != counts[1]:
        raise _unavailable("批次状态账本的返修汇总记录损坏，无法确认制作状态。")
    return counts, tuple(failed_ids)


def _production_events(
    events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Validate and remove the independent QC-repair saga from the shared journal."""

    claimed_request_ids: set[str] = set()
    repair_request_ids: set[str] = set()
    active_repair: _RepairSaga | None = None
    active_production_request_id: str | None = None
    legacy_production_active = False
    production_events: list[Mapping[str, Any]] = []
    for event in events:
        name = event["event"]
        if name == "command_received":
            request_id = _modern_request_id(event)
            if request_id in claimed_request_ids:
                raise _unavailable(
                    "批次状态账本重复登记同一请求，无法确认制作状态。"
                )
            claimed_request_ids.add(request_id)
            if event.get("command") == _REPAIR_COMMAND:
                if (
                    active_repair is not None
                    or active_production_request_id is not None
                    or legacy_production_active
                ):
                    raise _unavailable(
                        "批次状态账本的返修与制作生命周期交叉，无法确认制作状态。"
                    )
                active_repair = _RepairSaga(request_id=request_id)
                repair_request_ids.add(request_id)
                continue
            if active_repair is not None:
                raise _unavailable(
                    "批次状态账本的返修与制作生命周期交叉，无法确认制作状态。"
                )
            # A freshly written workflow command proves that a crashed writer
            # no longer owns the batch lock and supersedes its abandoned saga.
            active_production_request_id = request_id
            legacy_production_active = False
            production_events.append(event)
            continue

        request_id = event.get("request_id")
        if name == "gate_rejected" and event.get("command") == _REPAIR_COMMAND:
            request_id = _modern_request_id(event)
            detail = event.get("detail")
            if (
                detail not in _REPAIR_GATE_DETAILS
                or active_production_request_id is not None
                or legacy_production_active
                or (
                    active_repair is not None
                    and (
                        active_repair.request_id != request_id
                        or active_repair.state != "queued"
                    )
                )
            ):
                raise _unavailable(
                    "批次状态账本的返修门禁记录损坏，无法确认制作状态。"
                )
            if active_repair is None:
                if request_id in claimed_request_ids:
                    raise _unavailable(
                        "批次状态账本重复登记同一请求，无法确认制作状态。"
                    )
                claimed_request_ids.add(request_id)
                repair_request_ids.add(request_id)
            active_repair = None
            continue

        if active_repair is not None:
            request_id = _modern_request_id(event)
            saga = active_repair
            if request_id != saga.request_id:
                raise _unavailable(
                    "批次状态账本的返修与制作生命周期交叉，无法确认制作状态。"
                )
            if name not in _REPAIR_REQUEST_EVENTS or name == "gate_rejected":
                raise _unavailable(
                    "批次状态账本的返修生命周期损坏，无法确认制作状态。"
                )
            if event.get("step") != _REPAIR_COMMAND:
                raise _unavailable(
                    "批次状态账本的返修阶段记录损坏，无法确认制作状态。"
                )
            if name == "step_started":
                target_count = _repair_count(event, "target_count", positive=True)
                work_order_count = _repair_count(
                    event, "work_order_count", positive=True
                )
                if (
                    saga.state != "queued"
                    or work_order_count > target_count
                ):
                    raise _unavailable(
                        "批次状态账本的返修启动记录损坏，无法确认制作状态。"
                    )
                saga.state = "active"
                saga.target_count = target_count
                saga.work_order_count = work_order_count
            elif name == "repair_item_started":
                config_id = _repair_config_id(event)
                item_target_count = _repair_count(
                    event, "target_count", positive=True
                )
                review_count = _repair_count(event, "review_count")
                if (
                    saga.state != "active"
                    or saga.protection_failed
                    or saga.active_config_id is not None
                    or config_id in saga.results
                    or len(saga.results) >= saga.work_order_count
                    or saga.observed_target_count + item_target_count + review_count
                    > saga.target_count
                ):
                    raise _unavailable(
                        "批次状态账本的返修生命周期损坏，无法确认制作状态。"
                    )
                saga.active_config_id = config_id
                saga.observed_target_count += item_target_count + review_count
            elif name in {
                "repair_item_skipped_existing",
                "repair_item_failed",
                "repair_item_succeeded",
            }:
                config_id = _repair_config_id(event)
                if saga.state != "active" or saga.active_config_id != config_id:
                    raise _unavailable(
                        "批次状态账本的返修生命周期损坏，无法确认制作状态。"
                    )
                result = name.removeprefix("repair_item_")
                if name == "repair_item_skipped_existing":
                    valid_payload = _valid_sha256(event.get("sha256"))
                elif name == "repair_item_succeeded":
                    valid_payload = _valid_sha256(
                        event.get("sha256")
                    ) and _valid_artifact_dimensions(event)
                else:
                    valid_payload = event.get("detail") in _REPAIR_ITEM_FAILURE_DETAILS
                if not valid_payload:
                    raise _unavailable(
                        "批次状态账本的返修项记录损坏，无法确认制作状态。"
                    )
                saga.results[config_id] = result
                saga.active_config_id = None
            elif name == "renders_protection_failed":
                if (
                    saga.state != "active"
                    or saga.active_config_id is not None
                    or saga.protection_failed
                    or len(saga.results) != saga.work_order_count
                    or saga.observed_target_count != saga.target_count
                    or event.get("detail") != _REPAIR_PROTECTION_FAILURE_DETAIL
                ):
                    raise _unavailable(
                        "批次状态账本的返修防护记录损坏，无法确认制作状态。"
                    )
                saga.protection_failed = True
            elif name == "repair_completed":
                counts, failed_ids = _repair_summary(event)
                observed_counts = (
                    sum(value == "succeeded" for value in saga.results.values()),
                    sum(value == "failed" for value in saga.results.values()),
                    sum(value == "skipped_existing" for value in saga.results.values()),
                )
                observed_failed_ids = tuple(
                    config_id
                    for config_id, result in saga.results.items()
                    if result == "failed"
                )
                if (
                    saga.state != "active"
                    or saga.active_config_id is not None
                    or saga.protection_failed
                    or len(saga.results) != saga.work_order_count
                    or saga.observed_target_count != saga.target_count
                    or counts != observed_counts
                    or failed_ids != observed_failed_ids
                ):
                    raise _unavailable(
                        "批次状态账本的返修生命周期损坏，无法确认制作状态。"
                    )
                saga.state = "finishing"
                saga.completion_counts = counts
                saga.failed_config_ids = failed_ids
            elif name == "step_completed_with_failures":
                counts, failed_ids = _repair_summary(event)
                if (
                    saga.state != "finishing"
                    or saga.completion_counts is None
                    or saga.completion_counts[1] == 0
                    or counts != saga.completion_counts
                    or failed_ids != saga.failed_config_ids
                ):
                    raise _unavailable(
                        "批次状态账本的返修生命周期损坏，无法确认制作状态。"
                    )
                active_repair = None
            elif name == "step_succeeded":
                succeeded, failed, skipped = saga.completion_counts or (-1, -1, -1)
                expected_detail = f"返修处理完成：成功 {succeeded}，跳过 {skipped}"
                if (
                    saga.state != "finishing"
                    or failed != 0
                    or event.get("detail") != expected_detail
                ):
                    raise _unavailable(
                        "批次状态账本的返修收尾记录损坏，无法确认制作状态。"
                    )
                active_repair = None
            elif name == "step_failed":
                if (
                    saga.state != "active"
                    or saga.active_config_id is not None
                    or event.get("detail") != _REPAIR_STEP_FAILURE_DETAIL
                ):
                    raise _unavailable(
                        "批次状态账本的返修失败记录损坏，无法确认制作状态。"
                    )
                active_repair = None
            continue

        if (
            (type(request_id) is str and request_id in repair_request_ids)
            or name in _REPAIR_SPECIFIC_EVENTS
            or (
            name in {"step_started", "step_succeeded", "step_failed"}
            and event.get("step") == _REPAIR_COMMAND
            )
        ):
            raise _unavailable(
                "批次状态账本的返修请求归属损坏，无法确认制作状态。"
            )

        if "request_id" in event:
            event_request_id = _modern_request_id(event)
            if event_request_id == active_production_request_id and (
                name in {"step_failed", "production_paused", "production_completed"}
                or (name == "step_succeeded" and event.get("step") == "qc")
            ):
                active_production_request_id = None
        elif name == "step_started":
            legacy_production_active = True
        elif name in {"step_succeeded", "step_failed"}:
            legacy_production_active = False
        elif name == "white_bg_rebind_recompute":
            active_production_request_id = None
            legacy_production_active = False
        production_events.append(event)
    return production_events


def _validate_image_persisted(event: Mapping[str, Any]) -> str:
    config_id = event.get("config_id")
    source = event.get("source")
    sha256 = event.get("sha256")
    backfilled = event.get("backfilled")
    if (
        not _is_unicode_scalar_text(config_id)
        or config_id != config_id.strip()
        or source not in {"renders", "repaired"}
        or not _valid_sha256(sha256)
        or (
            "backfilled" in event
            and (type(backfilled) is not bool or backfilled is not True)
        )
        or not _valid_artifact_dimensions(event)
    ):
        raise _unavailable("批次状态账本的成图记录损坏，无法确认制作状态。")
    return config_id


def _validate_render_retry(
    event: Mapping[str, Any],
    active_step: str | None,
    active_mode: str | None,
) -> None:
    config_id = event.get("config_id")
    attempt = event.get("attempt")
    failure_code = event.get("failure_code")
    delay_seconds = event.get("delay_seconds")
    http_status = event.get("http_status")
    if (
        active_step != "renders"
        or active_mode != "modern"
        or "request_id" in event
        or type(config_id) is not str
        or not config_id
        or type(attempt) is not int
        or not 1 <= attempt <= 2
        or type(failure_code) is not str
        or failure_code not in _RENDER_RETRY_FAILURE_CODES
        or type(delay_seconds) is not int
        or not 0 <= delay_seconds <= 600
        or (
            http_status is not None
            and (type(http_status) is not int or not 100 <= http_status <= 599)
        )
    ):
        raise _unavailable("批次状态账本的出图重试记录损坏，无法确认制作状态。")


def _render_counts(detail: Any) -> tuple[int, int] | None:
    if type(detail) is not str:
        return None
    match = _RENDER_SUCCESS_COUNTS_PATTERN.search(detail)
    if match is None:
        match = _RENDER_FAILURE_COUNTS_PATTERN.search(detail)
    if match is None:
        match = _LEGACY_RENDER_FAILURE_COUNTS_PATTERN.search(detail)
    if match is None:
        return None

    def parse_count(field: str) -> int:
        raw_value = match.group(field)
        if len(raw_value) > _MAX_RENDER_COUNT_DIGITS:
            raise _unavailable("批次状态账本的出图计数损坏，无法确认制作状态。")
        try:
            return int(raw_value)
        except (ValueError, OverflowError):
            raise _unavailable(
                "批次状态账本的出图计数损坏，无法确认制作状态。"
            ) from None

    successful = parse_count("successful")
    planned = parse_count("planned")
    skipped = parse_count("skipped")
    if (
        successful > planned
        or any(value > _MAX_RENDER_COUNT for value in (successful, planned, skipped))
        or planned + skipped > _MAX_RENDER_COUNT
    ):
        raise _unavailable("批次状态账本的出图计数损坏，无法确认制作状态。")
    return successful + skipped, planned + skipped


def _derive_render_progress(events: Sequence[Mapping[str, Any]]) -> dict[str, int | None]:
    persisted: set[str] = set()
    completed_candidates: list[int] = []
    current_attempt_planned_candidates: set[int] = set()
    latest_planned_count: int | None = None
    active_step: str | None = None

    def finish_render_attempt() -> None:
        nonlocal latest_planned_count
        if len(current_attempt_planned_candidates) > 1:
            raise _unavailable("批次状态账本的计划张数互相冲突，无法确认制作状态。")
        if current_attempt_planned_candidates:
            latest_planned_count = next(iter(current_attempt_planned_candidates))
        current_attempt_planned_candidates.clear()

    for event in events:
        name = event["event"]
        if name == "white_bg_rebind_recompute":
            persisted.clear()
            completed_candidates.clear()
            current_attempt_planned_candidates.clear()
            latest_planned_count = None
            active_step = None
        elif name == "command_received":
            active_step = None
        elif name == "step_started":
            step = event.get("step")
            if type(step) is not str or not step:
                raise _unavailable("批次状态账本的阶段记录损坏，无法确认制作状态。")
            active_step = step
            if step == "renders":
                # A renders step is the durable attempt boundary in both legacy
                # and current ledgers. Plans may legitimately differ between
                # attempts, while conflicting evidence inside one attempt is unsafe.
                finish_render_attempt()
        elif name == "image_persisted":
            persisted.add(_validate_image_persisted(event))
        elif name in {"step_succeeded", "step_failed"}:
            raw_step = event.get("step")
            if raw_step is not None and (type(raw_step) is not str or not raw_step):
                raise _unavailable("批次状态账本的阶段记录损坏，无法确认制作状态。")
            if active_step is not None and raw_step is not None and raw_step != active_step:
                raise _unavailable("批次状态账本的阶段起止记录不一致，无法确认制作状态。")
            step = raw_step or active_step
            if step == "renders":
                counts = _render_counts(event.get("detail"))
                if counts is not None:
                    completed, planned = counts
                    completed_candidates.append(completed)
                    current_attempt_planned_candidates.add(planned)
            active_step = None
        elif name == "production_completed":
            produced_count = _nonnegative_integer(event, "produced_count")
            assert produced_count is not None
            completed_candidates.append(produced_count)
            current_attempt_planned_candidates.add(produced_count)
        elif name == "production_paused":
            produced_count = _nonnegative_integer(event, "produced_count")
            assert produced_count is not None
            completed_candidates.append(produced_count)

    finish_render_attempt()
    completed_count = max([len(persisted), *completed_candidates])
    planned_count = latest_planned_count
    if planned_count is not None and completed_count > planned_count:
        raise _unavailable("批次状态账本的完成张数超过计划，无法确认制作状态。")
    return {
        "completedCount": completed_count,
        "plannedCount": planned_count,
    }


def _derive_lifecycle(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    status = "queued"
    current_stage: str | None = None
    stage_started_at: str | None = None
    stage_ended_at: str | None = None
    active_step: str | None = None
    active_started_at: str | None = None
    active_mode: str | None = None
    last_succeeded_step: str | None = None
    last_succeeded_request_id: str | None = None
    completed_is_sticky = False
    production_completed_seen = False
    qc_completed_since_rebind = False
    current_request_id: str | None = None
    seen_request_ids: set[str] = set()
    has_modern_history = False
    request_closed = False
    request_has_lifecycle = False
    last_lifecycle_name: str | None = None
    persisted_since_rebind: set[str] = set()
    current_request_render_counts: tuple[int, int] | None = None
    failure_code: str | None = None
    message: str | None = None

    for event_index, event in enumerate(events):
        name = event["event"]
        timestamp = str(event["ts"])

        if name == "command_received":
            request_id = _modern_request_id(event)
            command = event.get("command")
            if type(command) is not str or command != _MODERN_PRODUCTION_COMMAND:
                raise _unavailable(
                    "批次状态账本的制作命令记录损坏，无法确认制作状态。"
                )
            if request_id in seen_request_ids:
                raise _unavailable(
                    "批次状态账本重复登记同一制作请求，无法确认制作状态。"
                )
            seen_request_ids.add(request_id)
            has_modern_history = True
            current_request_id = request_id
            active_step = None
            active_started_at = None
            active_mode = None
            last_succeeded_step = None
            last_succeeded_request_id = None
            request_closed = False
            request_has_lifecycle = False
            current_request_render_counts = None
            if not completed_is_sticky:
                status = "queued"
                current_stage = None
                stage_started_at = None
                stage_ended_at = None
                failure_code = None
                message = None
            last_lifecycle_name = name
            continue

        if name == "white_bg_rebind_recompute":
            if active_step is not None:
                raise _unavailable(
                    "批次状态账本的重新绑定前序损坏，无法确认制作状态。"
                )
            status = "queued"
            current_stage = None
            stage_started_at = None
            stage_ended_at = None
            active_step = None
            active_started_at = None
            active_mode = None
            last_succeeded_step = None
            last_succeeded_request_id = None
            completed_is_sticky = False
            production_completed_seen = False
            qc_completed_since_rebind = False
            current_request_id = None
            request_closed = False
            request_has_lifecycle = False
            last_lifecycle_name = None
            persisted_since_rebind.clear()
            current_request_render_counts = None
            failure_code = None
            message = None
            continue

        if name == "image_persisted":
            request_id = _modern_request_id(event)
            if (
                request_id != current_request_id
                or request_closed
                or last_lifecycle_name == "production_completed"
                or (
                    active_step is not None
                    and (active_step != "renders" or active_mode != "modern")
                )
            ):
                raise _unavailable(
                    "批次状态账本的成图请求归属损坏，无法确认制作状态。"
                )
            config_id = _validate_image_persisted(event)
            persisted_since_rebind.add(config_id)
            continue

        if name == "step_started":
            step = _workflow_step(event)
            if "request_id" in event:
                request_id = _modern_request_id(event)
                if request_id != current_request_id:
                    raise _unavailable(
                        "批次状态账本的制作请求归属不一致，无法确认制作状态。"
                    )
                if (
                    "command" in event
                    or active_step is not None
                    or request_closed
                    or qc_completed_since_rebind
                ):
                    raise _unavailable(
                        "批次状态账本的制作生命周期顺序损坏，无法确认制作状态。"
                    )
                if (
                    status == "completed"
                    and request_has_lifecycle
                    and last_lifecycle_name == "production_completed"
                    and step != "qc"
                ):
                    raise _unavailable(
                        "批次状态账本的制作生命周期顺序损坏，无法确认制作状态。"
                    )
                active_mode = "modern"
                request_has_lifecycle = True
            else:
                if has_modern_history or not _legacy_command_matches_step(
                    event.get("command"),
                    step,
                ):
                    raise _unavailable(
                        "批次状态账本的旧版阶段命令损坏，无法确认制作状态。"
                    )
                # The legacy writer had no request id. Each step_started row was
                # itself a fresh command, so a later valid start superseded an
                # abandoned active row (this occurs in shuiping_20260712).
                active_mode = "legacy"
            status = "running"
            current_stage = step
            stage_started_at = timestamp
            stage_ended_at = None
            active_step = step
            active_started_at = timestamp
            if step == "renders":
                current_request_render_counts = None
            completed_is_sticky = False
            failure_code = None
            message = None
            last_lifecycle_name = name
            continue

        if name == "step_succeeded":
            step = _workflow_step(event)
            if "request_id" in event:
                request_id = _modern_request_id(event)
                if request_id != current_request_id:
                    raise _unavailable(
                        "批次状态账本的制作请求归属不一致，无法确认制作状态。"
                    )
                expected_mode = "modern"
            else:
                if "command" in event or has_modern_history:
                    raise _unavailable(
                        "批次状态账本的旧版阶段记录损坏，无法确认制作状态。"
                    )
                request_id = None
                expected_mode = "legacy"
            if (
                active_step is None
                or step != active_step
                or active_mode != expected_mode
                or request_closed
            ):
                raise _unavailable(
                    "批次状态账本的阶段起止记录不一致，无法确认制作状态。"
                )
            status = "completed" if step == "qc" else "running"
            current_stage = step
            stage_started_at = active_started_at
            stage_ended_at = timestamp
            active_step = None
            active_started_at = None
            active_mode = None
            last_succeeded_step = step
            last_succeeded_request_id = request_id
            if step == "renders" and expected_mode == "modern":
                current_request_render_counts = _render_counts(event.get("detail"))
            completed_is_sticky = step == "qc"
            if step == "qc" and expected_mode == "modern":
                request_closed = True
            if step == "qc":
                qc_completed_since_rebind = True
            failure_code = None
            message = None
            last_lifecycle_name = name
            continue

        if name == "step_failed":
            detail = event.get("detail")
            if not _is_unicode_scalar_text(detail):
                raise _unavailable(
                    "批次状态账本的失败记录损坏，无法确认制作状态。"
                )
            raw_failure_code = event.get("failure_code")
            if "failure_code" in event and (
                not _is_unicode_scalar_text(raw_failure_code)
            ):
                raise _unavailable(
                    "批次状态账本的失败记录损坏，无法确认制作状态。"
                )
            if "request_id" in event:
                request_id = _modern_request_id(event)
                if request_id != current_request_id:
                    raise _unavailable(
                        "批次状态账本的制作请求归属不一致，无法确认制作状态。"
                    )
                if request_closed:
                    raise _unavailable(
                        "批次状态账本的制作生命周期顺序损坏，无法确认制作状态。"
                    )
                if active_step is not None:
                    if active_mode != "modern":
                        raise _unavailable(
                            "批次状态账本的阶段起止记录不一致，无法确认制作状态。"
                        )
                    if "step" in event:
                        step = _workflow_step(event)
                        if step != active_step:
                            raise _unavailable(
                                "批次状态账本的阶段起止记录不一致，无法确认制作状态。"
                            )
                    else:
                        step = active_step
                else:
                    # The modern producer writes no step for failures caught
                    # before the next executor starts. A supplied step invents
                    # evidence that the append site never records.
                    if "step" in event:
                        raise _unavailable(
                            "批次状态账本的启动前失败记录损坏，无法确认制作状态。"
                        )
                    step = None
                request_closed = True
            else:
                if "command" in event or has_modern_history:
                    raise _unavailable(
                        "批次状态账本的旧版失败记录损坏，无法确认制作状态。"
                    )
                step = _workflow_step(event)
                previous_event = events[event_index - 1] if event_index else None
                legacy_after_success = (
                    active_step is None
                    and step == "renders"
                    and previous_event is not None
                    and previous_event.get("event") == "step_succeeded"
                    and previous_event.get("step") == "renders"
                    and "request_id" not in previous_event
                    and last_lifecycle_name == "step_succeeded"
                    and last_succeeded_step == "renders"
                    and last_succeeded_request_id is None
                )
                if not legacy_after_success and (
                    active_step is None
                    or active_mode != "legacy"
                    or step != active_step
                ):
                    raise _unavailable(
                        "批次状态账本的阶段起止记录不一致，无法确认制作状态。"
                    )
            resolved_step = step or active_step or current_stage
            status = "failed"
            current_stage = resolved_step
            if active_step == resolved_step:
                stage_started_at = active_started_at
            stage_ended_at = timestamp
            active_step = None
            active_started_at = None
            active_mode = None
            completed_is_sticky = False
            failure_code = raw_failure_code
            message = detail
            last_lifecycle_name = name
            continue

        if name == "production_paused":
            produced_count = _nonnegative_integer(event, "produced_count")
            assert produced_count is not None
            if "request_id" not in event:
                raise _unavailable(
                    "批次状态账本的暂停前序损坏，无法确认制作状态。"
                )
            request_id = _modern_request_id(event)
            if (
                request_id != current_request_id
                or active_step is not None
                or request_closed
            ):
                raise _unavailable(
                    "批次状态账本的暂停前序损坏，无法确认制作状态。"
                )
            reason_present = "reason" in event
            reason = event.get("reason")
            if reason_present and reason != "awaiting_render_gate":
                raise _unavailable(
                    "批次状态账本的暂停原因损坏，无法确认制作状态。"
                )
            status = "paused"
            if reason == "awaiting_render_gate":
                gated_stage = {
                    "final_prompts": "integrity",
                    "integrity": "renders",
                }.get(last_succeeded_step or "")
                if gated_stage is None:
                    raise _unavailable(
                        "批次状态账本的出图闸门前序损坏，无法确认制作状态。"
                    )
                current_stage = gated_stage
                stage_started_at = None
                stage_ended_at = None
            else:
                if (
                    last_lifecycle_name != "step_succeeded"
                    or last_succeeded_step != "renders"
                    or last_succeeded_request_id != current_request_id
                    or current_request_render_counts is None
                    or current_request_render_counts[0] != produced_count
                ):
                    raise _unavailable(
                        "批次状态账本的暂停前序损坏，无法确认制作状态。"
                    )
                stage_ended_at = timestamp
            completed_is_sticky = False
            request_closed = True
            failure_code = None
            message = None
            last_lifecycle_name = name
            continue

        if name == "production_completed":
            produced_count = _nonnegative_integer(event, "produced_count")
            assert produced_count is not None
            if "request_id" not in event:
                raise _unavailable(
                    "批次状态账本的完成前序损坏，无法确认制作状态。"
                )
            request_id = _modern_request_id(event)
            render_proof = (
                last_lifecycle_name == "step_succeeded"
                and last_succeeded_step == "renders"
                and last_succeeded_request_id == current_request_id
                and current_request_render_counts is not None
                and current_request_render_counts[0] == produced_count
            )
            sync_existing_proof = (
                last_lifecycle_name == "command_received"
                and produced_count > 0
                and len(persisted_since_rebind) == produced_count
            )
            if (
                request_id != current_request_id
                or active_step is not None
                or request_closed
                or status == "failed"
                or not (render_proof or sync_existing_proof)
                or production_completed_seen
            ):
                raise _unavailable(
                    "批次状态账本的完成前序损坏，无法确认制作状态。"
                )
            status = "completed"
            stage_ended_at = stage_ended_at or timestamp
            completed_is_sticky = True
            production_completed_seen = True
            request_has_lifecycle = True
            failure_code = None
            message = None
            last_lifecycle_name = name
            continue

        if name == "render_retry":
            _validate_render_retry(event, active_step, active_mode)

    result: dict[str, Any] = {
        "status": status,
        "currentStage": current_stage,
        "stageStartedAt": stage_started_at,
        "stageEndedAt": stage_ended_at,
    }
    if status == "failed":
        if failure_code is not None:
            result["failureCode"] = failure_code
        result["message"] = message
    return result


def _validate_native_json_shape(value: Any) -> None:
    stack: list[tuple[Any, bool]] = [(value, False)]
    active_containers: set[int] = set()
    while stack:
        current, exiting = stack.pop()
        if exiting:
            active_containers.remove(id(current))
            continue
        if current is None or type(current) in {bool, int, float, str}:
            continue
        if type(current) not in {list, dict}:
            raise TypeError("value is not a native JSON type")
        identity = id(current)
        if identity in active_containers:
            raise ValueError("circular JSON value")
        active_containers.add(identity)
        stack.append((current, True))
        if type(current) is list:
            stack.extend((item, False) for item in reversed(current))
            continue
        for key in current:
            if type(key) is not str:
                raise TypeError("JSON object key must be text")
        stack.extend((item, False) for item in reversed(tuple(current.values())))


def build_workflow_batch_status_from_events(
    batch_id: str,
    events: Iterable[object],
) -> dict[str, Any]:
    """Return a status summary from Python-JSON-compatible events in memory.

    Constructed dictionaries cannot represent duplicate keys; disk-backed calls
    retain their duplicate-key rejection while parsing before reaching this seam.
    """

    _validate_batch_id(batch_id)
    try:
        supplied_events = list(events)
    except Exception:
        raise _unavailable("批次状态账本损坏，无法确认制作状态。") from None
    if not supplied_events:
        raise _unavailable("批次状态账本为空，无法确认制作状态。")

    validated_events: list[Mapping[str, Any]] = []
    previous_recorded_at: datetime | None = None
    for line_number, supplied_value in enumerate(supplied_events, start=1):
        if type(supplied_value) is not dict:
            raise _unavailable(
                f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
            )
        try:
            _validate_native_json_shape(supplied_value)
            value = json.loads(
                json.dumps(supplied_value),
                object_pairs_hook=_object_with_unique_keys,
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise _unavailable(
                f"批次状态账本第 {line_number} 行损坏，无法确认制作状态。"
            ) from None
        previous_recorded_at = _validate_event_record(
            value,
            line_number,
            previous_recorded_at,
        )
        validated_events.append(value)

    production_events = _production_events(validated_events)
    summary: dict[str, Any] = {
        "ok": True,
        "batchId": batch_id,
        **_derive_lifecycle(production_events),
        "renders": _derive_render_progress(production_events),
    }
    return summary


def build_workflow_batch_status(repository_root: Path, batch_id: str) -> dict[str, Any]:
    """Return a read-only status summary without loading the batch manifest."""

    return build_workflow_batch_status_from_events(
        batch_id,
        _load_events(repository_root, batch_id),
    )
