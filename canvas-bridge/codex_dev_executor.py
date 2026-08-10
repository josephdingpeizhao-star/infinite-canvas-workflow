"""Optional development adapter backed by canvas-agent's Codex HTTP/SSE API.

The canvas runtime still depends only on the provider-neutral executor contract.
All Codex thread, transport, prompt, attachment, and response details stay here.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from category_recipes import (
    CategoryRecipe,
    CategoryRecipeError,
    load_manifest_category,
    load_shared_prompt,
)
from content_correction import ContentPredicateViolation
from codex_dev_downstream import (
    DetailChunkEnvelopeCorrection,
    DetailChunkTransportCorruption,
    FinalPromptLiteralViolation,
    UserConfirmedRequirements,
    artifact_file_under_root,
    assemble_detail_variable_config_chunks,
    build_detail_variable_config_chunk_prompt,
    detail_chunk_business_fingerprint,
    detail_variable_config_chunk_count,
    build_final_prompt_batch_prompt,
    build_final_prompt_repair_prompt,
    build_final_prompt_bundle,
    build_variable_config_correction_prompt,
    build_variable_config_prompt,
    final_prompt_bundle_targets,
    load_typed_artifact,
    parse_final_prompt_batch_response,
    parse_detail_variable_config_chunk,
    parse_user_confirmed_requirements,
    parse_variable_config_response,
    style_master_material_reference_text,
    write_bundle_exclusive,
    write_json_exclusive,
)
from codex_dev_qc import (
    QcTransportCorruption,
    assemble_qc_report,
    build_qc_batch_prompt,
    build_qc_summary_prompt,
    load_qc_plan,
    parse_qc_batch_response,
    parse_qc_summary_response,
    qc_batch_attachment_paths,
    write_qc_report_exclusive,
)
from executor_contract import ExecutionRequest, ExecutionResult, ExecutorContext, ExecutorExecutionError


DEFAULT_CONFIG_PATH = Path.home() / ".infinite-canvas" / "canvas-agent.json"
PRODUCTION_CODEX_MODEL = "gpt-5.5"
PRODUCTION_CODEX_REASONING_EFFORT = "xhigh"
FINAL_PROMPT_CORRECTION_LIMIT = 2
SAFE_EXCEPTION_DETAIL_LIMIT = 160
REDACTED_EXCEPTION_SUMMARY = "异常摘要已脱敏"
_SENSITIVE_EXCEPTION_DETAIL_PATTERN = re.compile(
    r"(?ix)"
    r"(?:"
    r"(?:token|secret|bearer|authorization|password|credential)"
    r"|api[\s_-]*key"
    r"|access[\s_-]*key"
    r"|sk-[a-z0-9][a-z0-9_-]{5,}"
    r"|令牌|密钥|秘钥"
    r"|[a-z][a-z0-9+.-]*://"
    r"|www\."
    r"|(?:^|[\s(\"'])(?:[a-z]:[\\/]|\\\\|/(?:[^/\s]+/)*[^/\s]*)"
    r")"
)
_SAFE_EXCEPTION_TYPE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")
SUPPORTED_IMAGE_SUFFIXES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
REQUIRED_EVIDENCE_FIELDS = (
    "confirmed_facts",
    "visible_inferences",
    "unknowns",
    "prohibited_inventions",
)


def _contains_sensitive_exception_detail(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or bool(_SENSITIVE_EXCEPTION_DETAIL_PATTERN.search(value))
    )


def _single_line_exception_detail(value: str) -> str:
    return " ".join(value.split())


def _validated_safe_detail(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    detail = _single_line_exception_detail(value)
    if not detail:
        return ""
    if _contains_sensitive_exception_detail(detail):
        return REDACTED_EXCEPTION_SUMMARY
    return detail[:SAFE_EXCEPTION_DETAIL_LIMIT]


def _safe_exception_detail(exc: BaseException) -> str:
    exception_type = type(exc).__name__
    if (
        not _SAFE_EXCEPTION_TYPE_PATTERN.fullmatch(exception_type)
        or _contains_sensitive_exception_detail(exception_type)
    ):
        exception_type = "Exception"
    try:
        raw_summary = str(exc)
    except Exception:
        raw_summary = ""
    summary = _single_line_exception_detail(raw_summary)
    if _contains_sensitive_exception_detail(summary):
        summary = REDACTED_EXCEPTION_SUMMARY
    elif not summary:
        summary = "无摘要"
    return f"{exception_type}: {summary}"[:SAFE_EXCEPTION_DETAIL_LIMIT]


def _with_safe_exception_detail(message: str, safe_detail: str) -> str:
    safe_detail = _validated_safe_detail(safe_detail)
    if not safe_detail:
        return message
    return f"{message}：{safe_detail}"[:SAFE_EXCEPTION_DETAIL_LIMIT]
SUPPORTED_STEPS = frozenset(
    {
        "identity",
        "style_master",
        "angle_inventory",
        "main_vc",
        "detail_vc",
        "final_prompts",
        "qc",
    }
)
FORBIDDEN_OUTPUT_FIELDS = {
    "style_master",
    "angle_inventory",
    "angle_slots",
    "variable_configs",
    "main_variable_configs",
    "detail_variable_configs",
    "final_prompt",
    "final_prompts",
    "images",
    "qc_results",
}
SET_IDENTITY_EMBEDDED_ARCHIVE_FIELDS = {
    "identity",
    "identity_archive",
    "product_identity_archive",
    "component_archives",
    "component_identity_archives",
}
SET_IDENTITY_FORBIDDEN_OUTPUT_FIELDS = (
    FORBIDDEN_OUTPUT_FIELDS
    | {
        "qc_reports",
        "set_angle_layout_inventory",
        "set_layouts",
    }
    | SET_IDENTITY_EMBEDDED_ARCHIVE_FIELDS
)
SET_IDENTITY_COMPONENT_FORBIDDEN_FIELDS = SET_IDENTITY_FORBIDDEN_OUTPUT_FIELDS
STYLE_MASTER_FORBIDDEN_OUTPUT_FIELDS = {
    "product_identity_archive",
    "identity",
    "angle_inventory",
    "angle_slots",
    "variable_configs",
    "main_variable_configs",
    "detail_variable_configs",
    "final_prompt",
    "final_prompts",
    "images",
    "qc_results",
}
REQUIRED_STYLE_MASTER_FIELDS = (
    "visual_positioning",
    "composition_and_layout",
    "background_rules",
    "color_rules",
    "lighting_rules",
    "subject_presentation_rules",
    "prop_rules",
    "typography_rules",
    "negative_space_rules",
    "visual_mood",
    "reusable_rules",
    "fidelity_enhancements",
    "forbidden_elements",
    "concise_style_master",
)
ANGLE_SLOT_VALUES = frozenset({"A", "B", "C", "D", "不适合归入现有槽位"})
ANGLE_ADMISSION_VALUES = frozenset(
    {
        "合格，可进入对应槽位",
        "勉强可用，但建议重拍",
        "不适合入库，需重拍",
    }
)
ANGLE_SUITABILITY_PREFIXES = ("适合", "勉强适合", "不适合")
ANGLE_SLOT_REQUIRED_FIELDS = (
    "angle_slot",
    "source_asset_id",
    "camera_angle",
    "decision_basis",
    "naturally_visible_content",
    "must_not_force_content",
    "suitable_page_tasks",
    "unsuitable_page_tasks",
    "main_image_suitability",
    "detail_image_suitability",
    "risk_notes",
    "recommended_task_binding",
    "admission_result",
    "merged_reference_note",
    "usable_for",
    "notes",
)
ANGLE_LIST_FIELDS = (
    "naturally_visible_content",
    "must_not_force_content",
    "suitable_page_tasks",
    "unsuitable_page_tasks",
    "usable_for",
)
ANGLE_FORBIDDEN_OUTPUT_FIELDS = {
    "product_identity_archive",
    "identity",
    "style_master",
    "set_product_identity",
    "set_angle_layout_inventory",
    "set_layouts",
    "set_arrangements",
    "variable_configs",
    "main_variable_configs",
    "detail_variable_configs",
    "final_prompt",
    "final_prompts",
    "images",
    "qc_results",
}
ANGLE_ALLOWED_OUTPUT_FIELDS = {
    "product_id",
    "artifact_type",
    "image_assets",
    "angle_slots",
    "missing_angle_slots",
    "retake_recommendations",
    "notes",
}
SET_ANGLE_CAMERA_VALUES = frozenset({"A", "B", "C", "D", "不适合归入现有机位"})
SET_ANGLE_LAYOUT_VALUES = frozenset(
    {
        "编排槽位一",
        "编排槽位二",
        "编排槽位三",
        "编排槽位四",
        "不适合归入现有编排",
    }
)
SET_ANGLE_COMPONENT_LAYOUT_TEXT = "单件白底图，不涉及编排"
SET_ANGLE_ADMISSION_VALUES = frozenset(
    {
        "合格，可进入对应机位与编排槽位",
        "勉强可用，但建议重拍",
        "不适合入库，需重拍",
    }
)
SET_ANGLE_LAYOUT_REQUIRED_FIELDS = (
    "layout_id",
    "image_index",
    "file_name",
    "is_set_group",
    "overall_camera",
    "camera_decision_basis",
    "layout_slot",
    "layout_decision_basis",
    "piece_count_check",
    "component_visibility",
    "naturally_visible_content",
    "must_not_force_content",
    "suitable_page_tasks",
    "unsuitable_page_tasks",
    "main_image_suitability",
    "detail_image_suitability",
    "risk_notes",
    "recommended_task_binding",
    "admission_result",
    "merged_reference_note",
)
SET_ANGLE_LAYOUT_TEXT_FIELDS = (
    "camera_decision_basis",
    "layout_decision_basis",
    "piece_count_check",
    "component_visibility",
    "naturally_visible_content",
    "must_not_force_content",
    "suitable_page_tasks",
    "unsuitable_page_tasks",
    "main_image_suitability",
    "detail_image_suitability",
    "risk_notes",
    "recommended_task_binding",
    "admission_result",
    "merged_reference_note",
)
SET_ANGLE_LAYOUT_INJECTED_FIELDS = {
    "product_id",
    "user_declared_set_product",
    "set_group_assets",
    "explicit_set_request",
    "set_product_identity",
}
SET_ANGLE_LAYOUT_FORBIDDEN_FIELDS = {
    "angle_inventory",
    "angle_slots",
    "variable_configs",
    "main_variable_configs",
    "detail_variable_configs",
    "final_prompt",
    "final_prompts",
    "qc_results",
    "images",
}
SET_ANGLE_LAYOUT_ALLOWED_OUTPUT_FIELDS = {"artifact_type", "layouts", "notes"}


@dataclass(frozen=True)
class CodexAttachment:
    """One image attachment accepted by canvas-agent's Codex endpoint."""

    name: str
    mime_type: str
    data_url: str

    def as_payload(self) -> dict[str, str]:
        return {"name": self.name, "type": self.mime_type, "dataUrl": self.data_url}


@dataclass(frozen=True)
class CodexTurnResult:
    """The final assistant text and the dedicated canvas-agent thread id."""

    text: str
    thread_id: str


class CodexTransport(Protocol):
    def run_turn(self, prompt: str, attachments: tuple[CodexAttachment, ...]) -> CodexTurnResult:
        """Run one Codex turn through canvas-agent."""

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        """Continue an existing dedicated canvas-agent Codex thread."""


class CanvasAgentTransportError(RuntimeError):
    """Sanitized canvas-agent failure classified for the adapter boundary."""

    _MESSAGES = {
        "missing_config": "canvas-agent 配置缺失",
        "unsafe_config": "canvas-agent 配置不是安全的本机地址",
        "connection": "canvas-agent 连接失败",
        "thread": "Codex 线程失败",
        "response": "canvas-agent 返回异常",
        "empty_response": "Codex 本轮没有返回内容",
        "timeout": "Codex 线程等待超时",
    }

    def __init__(
        self,
        code: str,
        _private_detail: str = "",
        *,
        safe_detail: str = "",
    ):
        self.code = code
        self.safe_detail = _validated_safe_detail(safe_detail)
        super().__init__(self._MESSAGES.get(code, "canvas-agent 执行失败"))


class CodexDevExecutionError(ExecutorExecutionError):
    """Sanitized adapter failure with a stable diagnostic code."""

    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


class CanvasAgentCodexTransport:
    """Standard-library HTTP/SSE client for canvas-agent's existing Codex API."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        config: Mapping[str, str] | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: float = 300.0,
        turn_timeout: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        max_attachment_payload_bytes: int = 20 * 1024 * 1024,
        max_request_body_bytes: int = 28 * 1024 * 1024,
    ) -> None:
        if (
            isinstance(turn_timeout, bool)
            or not isinstance(turn_timeout, (int, float))
            or not math.isfinite(float(turn_timeout))
            or turn_timeout <= 0
        ):
            raise ValueError("turn_timeout must be a finite positive number")
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config = dict(config) if config is not None else None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        self.timeout = timeout
        self.turn_timeout = float(turn_timeout)
        self.monotonic = monotonic
        self.model = PRODUCTION_CODEX_MODEL
        self.effort = PRODUCTION_CODEX_REASONING_EFFORT
        self.max_attachment_payload_bytes = max_attachment_payload_bytes
        self.max_request_body_bytes = max_request_body_bytes

    def run_turn(self, prompt: str, attachments: tuple[CodexAttachment, ...]) -> CodexTurnResult:
        error_code = ""
        safe_detail = ""
        try:
            return self._run_turn(prompt, attachments)
        except CanvasAgentTransportError as exc:
            error_code = exc.code
            safe_detail = exc.safe_detail
        except Exception as exc:
            error_code = "thread"
            safe_detail = _safe_exception_detail(exc)
        raise CanvasAgentTransportError(
            error_code or "thread",
            safe_detail=safe_detail,
        )

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        error_code = ""
        safe_detail = ""
        try:
            return self._continue_turn(thread_id, prompt, attachments)
        except CanvasAgentTransportError as exc:
            error_code = exc.code
            safe_detail = exc.safe_detail
        except Exception as exc:
            error_code = "thread"
            safe_detail = _safe_exception_detail(exc)
        raise CanvasAgentTransportError(
            error_code or "thread",
            safe_detail=safe_detail,
        )

    def _run_turn(self, prompt: str, attachments: tuple[CodexAttachment, ...]) -> CodexTurnResult:
        chunks = self._attachment_chunks(attachments)
        config = self._load_config()
        base_url = config["url"].rstrip("/")
        token = config["token"]
        client_id = f"codex-dev-{uuid.uuid4()}"
        events_url = f"{base_url}/events?{urllib.parse.urlencode({'clientId': client_id})}"
        events_request = self._request("GET", events_url, token)

        try:
            events_response = self.opener(events_request, timeout=self.timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CanvasAgentTransportError("connection") from None

        try:
            with events_response as event_stream:
                thread_response = self._json_request(
                    "POST",
                    f"{base_url}/agent/codex/threads/new",
                    token,
                    {"model": self.model, "effort": self.effort},
                    error_code="thread",
                )
                thread = thread_response.get("thread") if isinstance(thread_response, dict) else None
                thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
                if not thread_response.get("ok") or not thread_id:
                    raise CanvasAgentTransportError("thread")

                if len(chunks) == 1:
                    self._start_turn(base_url, token, thread_id, prompt, chunks[0])
                    final_text, _assistant_count, _user_count = self._read_new_assistant(
                        event_stream,
                        base_url,
                        token,
                        thread_id,
                        previous_assistant_count=0,
                        previous_user_count=0,
                        deadline=self.monotonic() + self.turn_timeout,
                    )
                    return CodexTurnResult(text=final_text, thread_id=thread_id)

                assistant_count = 0
                user_count = 0
                total = len(chunks)
                for index, chunk in enumerate(chunks, start=1):
                    if index == 1:
                        batch_prompt = prompt
                    else:
                        batch_prompt = "继续同一 identity 产品身份建档任务。"
                    batch_prompt += (
                        f"\n\n由于图片总量超过本地接口单次上限，这是第 {index}/{total} 批图片。"
                        "本轮只观察并记录这些图片中的产品事实、可见推断、无法确认项和禁止虚构项；"
                        "不要提前生成最终档案或其他工作流产物。"
                        "本轮必须返回非空 JSON，对象顶层键为 batch_observation，"
                        "其内容明确包含 confirmed_facts、visible_inferences、unknowns、prohibited_inventions 四个数组，"
                        "仅记录本批图片观察，不得虚构。"
                    )
                    self._start_turn(base_url, token, thread_id, batch_prompt, chunk)
                    _text, assistant_count, user_count = self._read_new_assistant(
                        event_stream,
                        base_url,
                        token,
                        thread_id,
                        previous_assistant_count=assistant_count,
                        previous_user_count=user_count,
                        deadline=self.monotonic() + self.turn_timeout,
                    )

                final_prompt = (
                    prompt
                    + "\n\n全部图片批次已经提供完毕。现在综合本线程全部图片观察，"
                    "严格按上述 Skill、required reference 和 JSON 结构要求，只返回最终产品身份档案 JSON。"
                )
                self._start_turn(base_url, token, thread_id, final_prompt, ())
                final_text, _assistant_count, _user_count = self._read_new_assistant(
                    event_stream,
                    base_url,
                    token,
                    thread_id,
                    previous_assistant_count=assistant_count,
                    previous_user_count=user_count,
                    deadline=self.monotonic() + self.turn_timeout,
                )
                return CodexTurnResult(text=final_text, thread_id=thread_id)
        except CanvasAgentTransportError:
            raise
        except (TimeoutError, OSError) as exc:
            raise CanvasAgentTransportError("timeout") from None

    def _continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        if not thread_id:
            raise CanvasAgentTransportError("thread")
        chunks = self._attachment_chunks(attachments)
        if len(chunks) != 1:
            raise CanvasAgentTransportError("response")
        config = self._load_config()
        base_url = config["url"].rstrip("/")
        token = config["token"]
        client_id = f"codex-dev-{uuid.uuid4()}"
        events_url = f"{base_url}/events?{urllib.parse.urlencode({'clientId': client_id})}"
        events_request = self._request("GET", events_url, token)
        try:
            events_response = self.opener(events_request, timeout=self.timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise CanvasAgentTransportError("connection") from None

        try:
            with events_response as event_stream:
                previous_messages, previous_user_count = self._thread_message_summary(
                    base_url,
                    token,
                    thread_id,
                )
                self._start_turn(base_url, token, thread_id, prompt, chunks[0])
                final_text, _assistant_count, _user_count = self._read_new_assistant(
                    event_stream,
                    base_url,
                    token,
                    thread_id,
                    previous_assistant_count=len(previous_messages),
                    previous_user_count=previous_user_count,
                    deadline=self.monotonic() + self.turn_timeout,
                )
                return CodexTurnResult(text=final_text, thread_id=thread_id)
        except CanvasAgentTransportError:
            raise
        except (TimeoutError, OSError):
            raise CanvasAgentTransportError("timeout") from None

    def _load_config(self) -> dict[str, str]:
        data: Any = self.config
        if data is None:
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
                raise CanvasAgentTransportError("missing_config") from None
        if not isinstance(data, Mapping):
            raise CanvasAgentTransportError("missing_config")
        url = str(data.get("url") or "").rstrip("/")
        token = str(data.get("token") or "")
        if not url or not token:
            raise CanvasAgentTransportError("missing_config")
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise CanvasAgentTransportError("unsafe_config")
        return {"url": url, "token": token}

    def _request(self, method: str, url: str, token: str, payload: Any | None = None) -> urllib.request.Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("x-canvas-agent-token", token)
        if body is not None:
            request.add_header("content-type", "application/json")
        return request

    def _json_request(
        self,
        method: str,
        url: str,
        token: str,
        payload: Any,
        *,
        error_code: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request = self._request(method, url, token, payload)
        try:
            with self.opener(
                request,
                timeout=self.timeout if timeout is None else timeout,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanvasAgentTransportError(error_code) from None
        if not isinstance(result, dict):
            raise CanvasAgentTransportError(error_code)
        return result

    def _start_turn(
        self,
        base_url: str,
        token: str,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> None:
        payload = {
            "threadId": thread_id,
            "prompt": prompt,
            "attachments": [item.as_payload() for item in attachments],
            "model": self.model,
            "effort": self.effort,
        }
        body_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if body_size > self.max_request_body_bytes:
            raise CanvasAgentTransportError("response")
        turn_response = self._json_request(
            "POST",
            f"{base_url}/agent/codex/turn",
            token,
            payload,
            error_code="thread",
        )
        if not turn_response.get("ok") or str(turn_response.get("threadId") or "") != thread_id:
            raise CanvasAgentTransportError("thread")

    def _attachment_chunks(
        self, attachments: tuple[CodexAttachment, ...]
    ) -> tuple[tuple[CodexAttachment, ...], ...]:
        if not attachments:
            return ((),)
        chunks: list[tuple[CodexAttachment, ...]] = []
        current: list[CodexAttachment] = []
        current_size = 0
        for attachment in attachments:
            attachment_size = len(attachment.data_url.encode("ascii"))
            if attachment_size > self.max_attachment_payload_bytes:
                raise CanvasAgentTransportError("response")
            if current and current_size + attachment_size > self.max_attachment_payload_bytes:
                chunks.append(tuple(current))
                current = []
                current_size = 0
            current.append(attachment)
            current_size += attachment_size
        if current:
            chunks.append(tuple(current))
        return tuple(chunks)

    def _read_new_assistant(
        self,
        stream: Any,
        base_url: str,
        token: str,
        thread_id: str,
        *,
        previous_assistant_count: int,
        previous_user_count: int,
        deadline: float,
    ) -> tuple[str, int, int]:
        event_name = ""
        data_lines: list[str] = []

        while True:
            self._raise_if_turn_expired(base_url, token, deadline)
            raw_line = stream.readline()
            self._raise_if_turn_expired(base_url, token, deadline)
            if not raw_line:
                raise CanvasAgentTransportError("thread")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
                continue
            if line:
                continue

            payload = self._event_payload(data_lines)
            if event_name == "agent_error" and payload.get("agent") == "codex":
                if payload.get("failureCode") == "empty_assistant_response":
                    raise CanvasAgentTransportError("empty_response")
                raise CanvasAgentTransportError("thread")
            if event_name == "agent_done" and payload.get("agent") == "codex":
                status = str(payload.get("status") or "")
                if status and status != "completed":
                    if payload.get("failureCode") == "empty_assistant_response":
                        raise CanvasAgentTransportError("empty_response")
                    raise CanvasAgentTransportError("thread")
                messages, user_count = self._thread_message_summary(base_url, token, thread_id)
                if len(messages) > previous_assistant_count:
                    return messages[-1], len(messages), user_count
                if user_count > previous_user_count:
                    raise CanvasAgentTransportError("empty_response")

            event_name = ""
            data_lines = []

    def _raise_if_turn_expired(
        self,
        base_url: str,
        token: str,
        deadline: float,
    ) -> None:
        if self.monotonic() < deadline:
            return
        try:
            self._json_request(
                "POST",
                f"{base_url}/agent/codex/interrupt",
                token,
                {},
                error_code="thread",
                timeout=min(self.timeout, 2.0),
            )
        except CanvasAgentTransportError:
            pass
        raise CanvasAgentTransportError("timeout")

    def _thread_message_summary(
        self, base_url: str, token: str, thread_id: str
    ) -> tuple[tuple[str, ...], int]:
        safe_thread_id = urllib.parse.quote(thread_id, safe="")
        response = self._json_request(
            "GET",
            f"{base_url}/agent/codex/threads/{safe_thread_id}",
            token,
            None,
            error_code="thread",
        )
        if not response.get("ok"):
            raise CanvasAgentTransportError("thread")
        thread = response.get("thread")
        if isinstance(thread, dict) and thread.get("id") and str(thread.get("id")) != thread_id:
            raise CanvasAgentTransportError("response")
        messages = response.get("messages")
        if not isinstance(messages, list):
            raise CanvasAgentTransportError("response")
        assistant_messages: list[str] = []
        user_count = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                user_count += 1
            if message.get("role") == "assistant":
                text = str(message.get("text") or "").strip()
                if text:
                    assistant_messages.append(text)
        return tuple(assistant_messages), user_count

    @staticmethod
    def _event_payload(data_lines: list[str]) -> dict[str, Any]:
        if not data_lines:
            return {}
        try:
            value = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


class CodexDevExecutor:
    """Development-only structured-artifact executor using canvas-agent Codex turns."""

    name = "codex-dev"

    def __init__(
        self,
        context: ExecutorContext,
        *,
        transport: CodexTransport | None = None,
        repository_root: Path | None = None,
    ) -> None:
        self.context = context
        self.transport = transport or CanvasAgentCodexTransport()
        self.repository_root = repository_root or self._default_repository_root(context.manifest_path)
        self._qc_progress_callback: Callable[[int, int], None] | None = None
        self._turn_progress_callback: Callable[[], None] | None = None
        self._content_correction_callback: (
            Callable[[int, str, str], None] | None
        ) = None

    def set_turn_progress_callback(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        self._turn_progress_callback = callback

    def _emit_turn_progress(self) -> None:
        callback = self._turn_progress_callback
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def set_qc_progress_callback(
        self,
        callback: Callable[[int, int], None] | None,
    ) -> None:
        self._qc_progress_callback = callback

    def _emit_qc_progress(self, completed: int, total: int) -> None:
        callback = self._qc_progress_callback
        if callback is None:
            return
        try:
            callback(completed, total)
        except Exception:
            pass

    def set_content_correction_callback(
        self,
        callback: Callable[[int, str, str], None] | None,
    ) -> None:
        self._content_correction_callback = callback

    def _emit_content_correction(
        self,
        chunk_index: int,
        error: ContentPredicateViolation,
    ) -> None:
        callback = self._content_correction_callback
        if callback is not None:
            callback(chunk_index, error.code, error.details.config_id)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        safe_message = ""
        safe_code = ""
        try:
            return self._execute(request)
        except CodexDevExecutionError as exc:
            safe_message = str(exc)
            safe_code = exc.code
        except ExecutorExecutionError as exc:
            safe_message = str(exc)
        except Exception:
            safe_message = "codex-dev 执行失败"
        if safe_code:
            raise CodexDevExecutionError(safe_message, safe_code)
        raise ExecutorExecutionError(safe_message or "codex-dev 执行失败")

    def _execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.step not in SUPPORTED_STEPS:
            raise ExecutorExecutionError(
                "codex-dev 仅支持 identity、style_master、angle_inventory、main_vc，"
                f"detail_vc、final_prompts、qc，拒绝步骤：{request.step}"
            )
        if self.context.environment.get("CODEX_DEV_ALLOW_REAL_EXECUTION") != "1":
            raise CodexDevExecutionError(
                "codex-dev 未获准真实执行；阶段 B 批准前保持禁用",
                "real_execution_disabled",
            )

        product_id = str(self.context.manifest.get("product_id") or "").strip()
        if not product_id:
            raise ExecutorExecutionError("codex-dev 无法执行：manifest.product_id 缺失")

        if request.step == "style_master":
            return self._execute_style_master(product_id)
        if request.step == "angle_inventory":
            return self._execute_angle_inventory(product_id)
        if request.step == "main_vc":
            return self._execute_main_variable_config(product_id)
        if request.step == "detail_vc":
            return self._execute_detail_variable_config(product_id)
        if request.step == "final_prompts":
            return self._execute_final_prompts(product_id)
        if request.step == "qc":
            return self._execute_qc(product_id)
        return self._execute_identity(product_id)

    def _execute_identity(self, product_id: str) -> ExecutionResult:
        if self.context.manifest.get("batch_type", "single") != "single":
            return self._execute_set_identity_archives(product_id)
        skill_text, reference_text = self._load_required_rules()
        output_path = self._identity_output_path()
        if output_path.exists():
            raise ExecutorExecutionError("产品身份档案已存在，codex-dev 不会覆盖")
        attachments, source_inputs = self._load_white_background_images()
        prompt = self._build_prompt(product_id, source_inputs, skill_text, reference_text)
        turn = self._run_transport(prompt, attachments)

        archive = self._parse_archive(turn.text, product_id, source_inputs)
        self._write_archive(output_path, archive)
        return ExecutionResult(
            detail="产品身份档案已生成",
            outputs=(output_path,),
            provider=self.name,
            metadata={"thread_id": turn.thread_id},
        )

    def _execute_set_identity_archives(self, product_id: str) -> ExecutionResult:
        identity_directory = self._identity_output_path().parent
        self._validate_empty_set_identity_directory(identity_directory)

        component_paths = self._manifest_image_paths(
            "component_white_bg_images",
            sort_by_filename=True,
        )
        if not 2 <= len(component_paths) <= 8:
            raise ExecutorExecutionError("套装身份建档要求 2–8 张组成单件白底图")
        group_paths = self._manifest_image_paths(
            "set_group_images",
            sort_by_filename=True,
        )
        if not 1 <= len(group_paths) <= 3:
            raise ExecutorExecutionError("套装身份建档要求 1–3 张套装合影图")

        component_total = len(component_paths)
        component_names = tuple(path.name for path in component_paths)
        group_names = tuple(path.name for path in group_paths)
        component_output_names = tuple(
            f"component_{index:02d}_product_identity_archive.json"
            for index in range(1, component_total + 1)
        )
        component_skill, component_reference = self._load_required_rules()
        set_skill, set_identity_reference, set_workflow_reference = (
            self._load_set_identity_rules()
        )
        component_turns = tuple(
            (
                component_path,
                self._image_attachments(
                    (component_path,),
                    "套装组成单件白底图",
                ),
                self._build_component_identity_prompt(
                    product_id,
                    component_index,
                    component_total,
                    component_path.name,
                    component_skill,
                    component_reference,
                ),
            )
            for component_index, component_path in enumerate(
                component_paths,
                start=1,
            )
        )
        group_attachments = self._image_attachments(group_paths, "套装合影图")

        component_archives: list[dict[str, Any]] = []
        component_thread_ids: list[str] = []
        for component_index, (component_path, attachments, prompt) in enumerate(
            component_turns,
            start=1,
        ):
            turn = self._run_transport(prompt, attachments)
            archive = self._parse_archive(
                turn.text,
                product_id,
                (component_path.name,),
            )
            archive["component_index"] = component_index
            archive["component_source_image"] = component_path.name
            self._emit_turn_progress()
            component_archives.append(archive)
            component_thread_ids.append(turn.thread_id)

        set_prompt = self._build_set_identity_prompt(
            product_id,
            group_names,
            component_names,
            component_archives,
            set_skill,
            set_identity_reference,
            set_workflow_reference,
        )
        set_turn = self._run_transport(set_prompt, group_attachments)
        set_archive = self._parse_set_identity_archive(
            set_turn.text,
            product_id,
            group_names,
            component_names,
            component_output_names,
        )
        self._emit_turn_progress()

        self._validate_empty_set_identity_directory(identity_directory)
        output_paths = tuple(
            identity_directory / filename for filename in component_output_names
        )
        for output_path, archive in zip(output_paths, component_archives):
            self._write_archive(output_path, archive)
        set_output_path = identity_directory / "set_product_identity.json"
        self._write_archive(set_output_path, set_archive)
        return ExecutionResult(
            detail="套装两级身份档案已生成",
            outputs=(*output_paths, set_output_path),
            provider=self.name,
            metadata={
                "thread_id": set_turn.thread_id,
                "component_thread_ids": component_thread_ids,
            },
        )

    def _execute_style_master(self, product_id: str) -> ExecutionResult:
        skill_text, reference_text = self._load_style_master_rules()
        output_path = self._style_master_output_path()
        if output_path.exists():
            raise ExecutorExecutionError("风格母版已存在，codex-dev 不会覆盖")
        attachments, source_references = self._load_style_reference_images()
        identity_archive = (
            self._load_product_identity_archive()
            if self.context.manifest.get("batch_type", "single") == "single"
            else self._load_set_product_identity()
        )
        prompt = self._build_style_master_prompt(
            product_id,
            source_references,
            identity_archive,
            skill_text,
            reference_text,
        )
        turn = self._run_transport(prompt, attachments)
        artifact = self._parse_style_master(turn.text, product_id, source_references)
        self._write_style_master(output_path, artifact)
        return ExecutionResult(
            detail="风格母版已生成",
            outputs=(output_path,),
            provider=self.name,
            metadata={"thread_id": turn.thread_id},
        )

    def _execute_angle_inventory(self, product_id: str) -> ExecutionResult:
        if self.context.manifest.get("batch_type", "single") != "single":
            return self._execute_set_angle_layout_inventory(product_id)
        self._validate_single_product_batch()
        skill_text, reference_text = self._load_angle_inventory_rules()
        output_path = self._angle_inventory_output_path()
        if output_path.exists():
            raise ExecutorExecutionError("角度槽位入库表已存在，codex-dev 不会覆盖")
        attachments, source_inputs = self._load_white_background_images("angle_inventory")
        identity_archive = self._load_product_identity_archive()
        image_assets = self._build_angle_image_assets(source_inputs)
        display_orientations = self._load_angle_display_orientations(source_inputs)
        prompt = self._build_angle_inventory_prompt(
            product_id,
            image_assets,
            display_orientations,
            identity_archive,
            skill_text,
            reference_text,
        )
        turn = self._run_transport(prompt, attachments)
        artifact = self._parse_angle_inventory(turn.text, product_id, image_assets)
        self._write_angle_inventory(output_path, artifact)
        return ExecutionResult(
            detail="角度槽位入库表已生成",
            outputs=(output_path,),
            provider=self.name,
            metadata={"thread_id": turn.thread_id},
        )

    def _execute_set_angle_layout_inventory(self, product_id: str) -> ExecutionResult:
        if self.context.manifest.get("user_declared_set_product") is not True:
            self._validate_single_product_batch()
        output_path = self._set_angle_layout_output_path()
        if output_path.exists():
            raise ExecutorExecutionError("套装角度与编排入库表已存在，codex-dev 不会覆盖")

        component_paths = self._manifest_image_paths(
            "component_white_bg_images",
            sort_by_filename=True,
        )
        if not 2 <= len(component_paths) <= 8:
            raise ExecutorExecutionError("套装角度与编排入库要求 2–8 张组成单件白底图")
        group_paths = self._manifest_image_paths(
            "set_group_images",
            sort_by_filename=True,
        )
        if not 1 <= len(group_paths) <= 3:
            raise ExecutorExecutionError("套装角度与编排入库要求 1–3 张套装合影图")

        set_identity = self._load_set_product_identity()
        group_names = tuple(path.name for path in group_paths)
        component_names = tuple(path.name for path in component_paths)
        attachments = (
            *self._image_attachments(group_paths, "套装合影图"),
            *self._image_attachments(component_paths, "套装组成单件白底图"),
        )
        skill_text, inventory_reference, layout_reference = (
            self._load_set_angle_layout_rules()
        )
        prompt = self._build_set_angle_layout_prompt(
            product_id,
            group_names,
            component_names,
            set_identity,
            skill_text,
            inventory_reference,
            layout_reference,
        )
        turn = self._run_transport(prompt, attachments)
        artifact = self._parse_set_angle_layout_inventory(
            turn.text,
            product_id,
            group_names,
            component_names,
        )
        self._write_set_angle_layout_inventory(output_path, artifact)
        return ExecutionResult(
            detail="套装角度与编排入库表已生成",
            outputs=(output_path,),
            provider=self.name,
            metadata={"thread_id": turn.thread_id},
        )

    def _execute_main_variable_config(self, product_id: str) -> ExecutionResult:
        self._validate_single_product_batch()
        output_path = artifact_file_under_root(
            self.context.manifest,
            "main_variable_configs",
            "main_variable_configs.json",
        )
        if output_path.exists():
            raise ExecutorExecutionError("正式主图变量配置已存在，codex-dev 不会覆盖")

        requirements = parse_user_confirmed_requirements(
            self.context.manifest,
            self.repository_root,
        )
        identity, identity_path = load_typed_artifact(
            self.context.manifest,
            "product_identity_archive",
            "product_identity_archive.json",
            "product_identity_archive",
            "产品身份档案",
        )
        style_master, style_path = load_typed_artifact(
            self.context.manifest,
            "style_master",
            "style_master.json",
            "style_master",
            "风格母版",
        )
        angle_inventory, angle_path = load_typed_artifact(
            self.context.manifest,
            "angle_inventory",
            "angle_inventory.json",
            "angle_inventory",
            "角度槽位入库表",
        )
        prompt = build_variable_config_prompt(
            mode="main",
            product_id=product_id,
            repository_root=self.repository_root,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=requirements,
        )
        turn = self._run_transport(prompt, ())
        self._emit_turn_progress()
        upstream_paths = {
            "product_identity_archive": identity_path,
            "style_master": style_path,
            "angle_inventory": angle_path,
        }
        try:
            artifact = parse_variable_config_response(
                turn.text,
                mode="main",
                product_id=product_id,
                requirements=requirements,
                angle_inventory=angle_inventory,
                upstream_paths=upstream_paths,
            )
        except ContentPredicateViolation as error:
            if self._content_correction_callback is None:
                raise
            self._emit_content_correction(1, error)
            corrected_turn = self._continue_transport(
                turn.thread_id,
                build_variable_config_correction_prompt(
                    error,
                    mode="main",
                    requirements=requirements,
                ),
                (),
            )
            self._emit_turn_progress()
            if corrected_turn.thread_id != turn.thread_id:
                raise ExecutorExecutionError(
                    "codex-dev 收到无效的主图变量配置线程返回"
                )
            turn = corrected_turn
            artifact = parse_variable_config_response(
                turn.text,
                mode="main",
                product_id=product_id,
                requirements=requirements,
                angle_inventory=angle_inventory,
                upstream_paths=upstream_paths,
            )
        write_json_exclusive(output_path, artifact, "主图变量配置")
        return ExecutionResult(
            detail="主图变量配置已生成",
            outputs=(output_path,),
            provider=self.name,
            metadata={"thread_id": turn.thread_id},
        )

    def _execute_detail_variable_config(self, product_id: str) -> ExecutionResult:
        self._validate_single_product_batch()
        output_path = artifact_file_under_root(
            self.context.manifest,
            "detail_variable_configs",
            "detail_variable_configs.json",
        )
        if output_path.exists():
            raise ExecutorExecutionError("正式详情图变量配置已存在，codex-dev 不会覆盖")

        requirements = parse_user_confirmed_requirements(
            self.context.manifest,
            self.repository_root,
        )
        identity, identity_path = load_typed_artifact(
            self.context.manifest,
            "product_identity_archive",
            "product_identity_archive.json",
            "product_identity_archive",
            "产品身份档案",
        )
        style_master, style_path = load_typed_artifact(
            self.context.manifest,
            "style_master",
            "style_master.json",
            "style_master",
            "风格母版",
        )
        angle_inventory, angle_path = load_typed_artifact(
            self.context.manifest,
            "angle_inventory",
            "angle_inventory.json",
            "angle_inventory",
            "角度槽位入库表",
        )
        main_variable_config, main_path = load_typed_artifact(
            self.context.manifest,
            "main_variable_configs",
            "main_variable_configs.json",
            "main_variable_config",
            "正式主图变量配置",
        )
        base_prompt = build_variable_config_prompt(
            mode="detail",
            product_id=product_id,
            repository_root=self.repository_root,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            requirements=requirements,
            main_variable_config=main_variable_config,
        )
        chunks: list[Mapping[str, Any]] = []
        recovery_attempts = 0
        structure_correction_attempts = 0
        content_correction_chunks: set[int] = set()
        thread_id = ""
        chunk_count = detail_variable_config_chunk_count(requirements)
        for chunk_index in range(1, chunk_count + 1):
            expected_business_fingerprint = ""
            prompt = build_detail_variable_config_chunk_prompt(
                base_prompt,
                chunk_index,
                requirements=requirements,
            )
            turn = (
                self._run_transport(prompt, ())
                if chunk_index == 1
                else self._continue_transport(thread_id, prompt, ())
            )
            self._emit_turn_progress()
            if chunk_index == 1:
                thread_id = turn.thread_id
            elif turn.thread_id != thread_id:
                raise ExecutorExecutionError("codex-dev 收到无效的详情图变量配置线程返回")

            while True:
                try:
                    chunk = parse_detail_variable_config_chunk(
                        turn.text,
                        chunk_index,
                        requirements=requirements,
                        angle_inventory=angle_inventory,
                        prior_chunks=chunks,
                    )
                    if (
                        expected_business_fingerprint
                        and detail_chunk_business_fingerprint(chunk, chunk_index)
                        != expected_business_fingerprint
                    ):
                        raise ExecutorExecutionError(
                            "codex-dev 详情图变量配置格式纠正改变了业务内容"
                        )
                    break
                except DetailChunkTransportCorruption:
                    if recovery_attempts >= 2:
                        raise ExecutorExecutionError(
                            "codex-dev 详情图变量配置传输恢复已达到上限"
                        ) from None
                    recovery_attempts += 1
                    repair_prompt = build_detail_variable_config_chunk_prompt(
                        base_prompt,
                        chunk_index,
                        requirements=requirements,
                        repair=True,
                    )
                    turn = self._continue_transport(thread_id, repair_prompt, ())
                    self._emit_turn_progress()
                    if turn.thread_id != thread_id:
                        raise ExecutorExecutionError(
                            "codex-dev 收到无效的详情图变量配置线程返回"
                        )
                except DetailChunkEnvelopeCorrection as error:
                    if structure_correction_attempts >= 1:
                        raise ExecutorExecutionError(
                            "codex-dev 详情图变量配置格式纠正已达到上限"
                        ) from None
                    structure_correction_attempts += 1
                    expected_business_fingerprint = error.business_fingerprint
                    correction_prompt = build_detail_variable_config_chunk_prompt(
                        base_prompt,
                        chunk_index,
                        requirements=requirements,
                        structure_correction=True,
                    )
                    turn = self._continue_transport(thread_id, correction_prompt, ())
                    self._emit_turn_progress()
                    if turn.thread_id != thread_id:
                        raise ExecutorExecutionError(
                            "codex-dev 收到无效的详情图变量配置线程返回"
                        )
                except ContentPredicateViolation as error:
                    if self._content_correction_callback is None:
                        raise
                    if chunk_index in content_correction_chunks:
                        raise
                    content_correction_chunks.add(chunk_index)
                    expected_business_fingerprint = ""
                    self._emit_content_correction(chunk_index, error)
                    correction_prompt = build_detail_variable_config_chunk_prompt(
                        base_prompt,
                        chunk_index,
                        requirements=requirements,
                        correction=error,
                    )
                    turn = self._continue_transport(
                        thread_id,
                        correction_prompt,
                        (),
                    )
                    self._emit_turn_progress()
                    if turn.thread_id != thread_id:
                        raise ExecutorExecutionError(
                            "codex-dev 收到无效的详情图变量配置线程返回"
                        )
            chunks.append(chunk)

        assembled_response = assemble_detail_variable_config_chunks(
            chunks,
            requirements=requirements,
        )
        artifact = parse_variable_config_response(
            json.dumps(assembled_response, ensure_ascii=False),
            mode="detail",
            product_id=product_id,
            requirements=requirements,
            angle_inventory=angle_inventory,
            upstream_paths={
                "product_identity_archive": identity_path,
                "style_master": style_path,
                "angle_inventory": angle_path,
                "main_variable_configs": main_path,
            },
        )
        write_json_exclusive(output_path, artifact, "详情图变量配置")
        detail = "详情图变量配置已生成"
        recovery_notes: list[str] = []
        if recovery_attempts:
            recovery_notes.append(f"受控恢复 {recovery_attempts} 次")
        if structure_correction_attempts:
            recovery_notes.append(f"格式纠正 {structure_correction_attempts} 次")
        if recovery_notes:
            detail += f"（{'，'.join(recovery_notes)}）"
        return ExecutionResult(
            detail=detail,
            outputs=(output_path,),
            provider=self.name,
            metadata={
                "thread_id": thread_id,
                "recovery_attempts": recovery_attempts,
                "structure_correction_attempts": structure_correction_attempts,
            },
        )

    def _parse_final_prompt_with_bounded_correction(
        self,
        turn: CodexTurnResult,
        *,
        mode: str,
        product_id: str,
        requirements: UserConfirmedRequirements,
        angle_inventory: Mapping[str, Any],
        variable_config: Mapping[str, Any],
        style_master_text: str,
        correction_attempts: int,
    ) -> tuple[dict[str, dict[str, str]], CodexTurnResult, int]:
        thread_id = turn.thread_id
        label = "主图" if mode == "main" else "详情图"
        while True:
            try:
                batch = parse_final_prompt_batch_response(
                    turn.text,
                    mode=mode,
                    product_id=product_id,
                    requirements=requirements,
                    angle_inventory=angle_inventory,
                    variable_config=variable_config,
                    style_master_text=style_master_text,
                )
            except FinalPromptLiteralViolation as error:
                if correction_attempts >= FINAL_PROMPT_CORRECTION_LIMIT:
                    limit_detail = (
                        f"codex-dev {label}最终提示词纠正已达到上限："
                        f"{error.safe_reason}"
                    )
                    raise ExecutorExecutionError(limit_detail[:160]) from None
                correction_attempts += 1
                turn = self._continue_transport(
                    thread_id,
                    build_final_prompt_repair_prompt(
                        mode=mode,
                        requirements=requirements,
                    ),
                    (),
                )
                self._emit_turn_progress()
                if turn.thread_id != thread_id:
                    raise ExecutorExecutionError(
                        f"codex-dev 收到无效的{label}最终提示词线程返回"
                    )
                continue
            return batch, turn, correction_attempts

    def _execute_final_prompts(self, product_id: str) -> ExecutionResult:
        self._validate_single_product_batch()
        index_path = artifact_file_under_root(
            self.context.manifest,
            "final_prompts",
            "final_prompt_index.json",
        )
        output_dir = index_path.parent
        requirements = parse_user_confirmed_requirements(
            self.context.manifest,
            self.repository_root,
        )
        if any(
            path.exists()
            for path in final_prompt_bundle_targets(
                output_dir,
                requirements=requirements,
            )
        ):
            raise ExecutorExecutionError("正式最终提示词已存在，codex-dev 不会覆盖")
        identity, identity_path = load_typed_artifact(
            self.context.manifest,
            "product_identity_archive",
            "product_identity_archive.json",
            "product_identity_archive",
            "产品身份档案",
        )
        style_master, style_path = load_typed_artifact(
            self.context.manifest,
            "style_master",
            "style_master.json",
            "style_master",
            "风格母版",
        )
        angle_inventory, angle_path = load_typed_artifact(
            self.context.manifest,
            "angle_inventory",
            "angle_inventory.json",
            "angle_inventory",
            "角度槽位入库表",
        )
        main_variable_config, main_path = load_typed_artifact(
            self.context.manifest,
            "main_variable_configs",
            "main_variable_configs.json",
            "main_variable_config",
            "正式主图变量配置",
        )
        detail_variable_config, detail_path = load_typed_artifact(
            self.context.manifest,
            "detail_variable_configs",
            "detail_variable_configs.json",
            "detail_variable_config",
            "正式详情图变量配置",
        )
        style_master_text = style_master_material_reference_text(
            style_master,
            product_id=product_id,
        )

        main_prompt = build_final_prompt_batch_prompt(
            mode="main",
            product_id=product_id,
            repository_root=self.repository_root,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=main_variable_config,
            requirements=requirements,
        )
        main_turn = self._run_transport(main_prompt, ())
        self._emit_turn_progress()
        correction_attempts = 0
        main_batch, main_turn, correction_attempts = (
            self._parse_final_prompt_with_bounded_correction(
                main_turn,
                mode="main",
                product_id=product_id,
                requirements=requirements,
                angle_inventory=angle_inventory,
                variable_config=main_variable_config,
                style_master_text=style_master_text,
                correction_attempts=correction_attempts,
            )
        )

        detail_prompt = build_final_prompt_batch_prompt(
            mode="detail",
            product_id=product_id,
            repository_root=self.repository_root,
            identity=identity,
            style_master=style_master,
            angle_inventory=angle_inventory,
            variable_config=detail_variable_config,
            requirements=requirements,
        )
        detail_turn = self._run_transport(detail_prompt, ())
        self._emit_turn_progress()
        detail_batch, detail_turn, correction_attempts = (
            self._parse_final_prompt_with_bounded_correction(
                detail_turn,
                mode="detail",
                product_id=product_id,
                requirements=requirements,
                angle_inventory=angle_inventory,
                variable_config=detail_variable_config,
                style_master_text=style_master_text,
                correction_attempts=correction_attempts,
            )
        )

        bundle = build_final_prompt_bundle(
            product_id=product_id,
            output_dir=output_dir,
            prompt_batches={"main": main_batch, "detail": detail_batch},
            variable_configs={
                "main": (main_variable_config, main_path),
                "detail": (detail_variable_config, detail_path),
            },
            upstream_paths={
                "product_identity_archive": identity_path,
                "style_master": style_path,
                "angle_inventory": angle_path,
            },
            angle_inventory=angle_inventory,
            requirements=requirements,
        )
        write_bundle_exclusive(bundle, "最终提示词")
        detail = "最终提示词已生成"
        if correction_attempts:
            detail += f"（受控纠正 {correction_attempts} 次）"
        return ExecutionResult(
            detail=detail,
            outputs=(index_path,),
            provider=self.name,
            metadata={
                "main_thread_id": main_turn.thread_id,
                "detail_thread_id": detail_turn.thread_id,
                "correction_attempts": correction_attempts,
            },
        )

    def _execute_qc(self, product_id: str) -> ExecutionResult:
        plan = load_qc_plan(self.context.manifest, self.repository_root)
        if plan.product_id != product_id:
            raise ExecutorExecutionError("codex-dev 检测到 QC 计划与当前商品不匹配")

        chunks: list[Mapping[str, Any]] = []
        recovery_attempts = 0
        thread_ids: list[str] = []
        chunk_count = plan.chunk_count
        if type(chunk_count) is not int:
            raise ExecutorExecutionError("codex-dev QC 分批计划无效")
        for batch in plan.batches:
            attachments = self._qc_attachments(qc_batch_attachment_paths(batch))
            prompt = build_qc_batch_prompt(plan, batch)
            turn = self._run_transport(prompt, attachments)
            batch_thread_id = turn.thread_id
            thread_ids.append(batch_thread_id)

            while True:
                try:
                    chunk = parse_qc_batch_response(
                        turn.text,
                        batch,
                        prior_chunks=tuple(chunks),
                    )
                    break
                except QcTransportCorruption:
                    if recovery_attempts >= 2:
                        raise ExecutorExecutionError("codex-dev QC 传输恢复已达到上限") from None
                    recovery_attempts += 1
                    turn = self._continue_transport(
                        batch_thread_id,
                        build_qc_batch_prompt(plan, batch, repair=True),
                        (),
                    )
                    if turn.thread_id != batch_thread_id:
                        raise ExecutorExecutionError(
                            "codex-dev 收到无效的 QC 组内线程返回"
                        )
            chunks.append(chunk)
            self._emit_qc_progress(len(chunks), chunk_count)

        summary_turn = self._run_transport(
            build_qc_summary_prompt(plan, tuple(chunks)),
            (),
        )
        summary_thread_id = summary_turn.thread_id
        thread_ids.append(summary_thread_id)
        while True:
            try:
                summary = parse_qc_summary_response(
                    summary_turn.text,
                    plan,
                    prior_chunks=tuple(chunks),
                )
                break
            except QcTransportCorruption:
                if recovery_attempts >= 2:
                    raise ExecutorExecutionError("codex-dev QC 传输恢复已达到上限") from None
                recovery_attempts += 1
                summary_turn = self._continue_transport(
                    summary_thread_id,
                    build_qc_summary_prompt(plan, tuple(chunks), repair=True),
                    (),
                )
                if summary_turn.thread_id != summary_thread_id:
                    raise ExecutorExecutionError(
                        "codex-dev 收到无效的 QC 组内线程返回"
                    )

        self._emit_qc_progress(chunk_count, chunk_count)
        report = assemble_qc_report(plan, tuple(chunks), summary)
        output_path = write_qc_report_exclusive(plan, report)
        detail = "QC 报告已生成"
        if recovery_attempts:
            detail += f"（受控恢复 {recovery_attempts} 次）"
        return ExecutionResult(
            detail=detail,
            outputs=(output_path,),
            provider=self.name,
            metadata={
                "thread_ids": thread_ids,
                "batch_count": chunk_count,
                "recovery_attempts": recovery_attempts,
            },
        )

    @staticmethod
    def _qc_attachments(paths: tuple[Path, ...]) -> tuple[CodexAttachment, ...]:
        attachments: list[CodexAttachment] = []
        for path in paths:
            mime_type = SUPPORTED_IMAGE_SUFFIXES.get(path.suffix.lower())
            if mime_type is None:
                raise ExecutorExecutionError("codex-dev 检测到 QC 附件格式无效")
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                raise ExecutorExecutionError("codex-dev 无法读取 QC 附件") from None
            attachments.append(
                CodexAttachment(
                    name=path.name,
                    mime_type=mime_type,
                    data_url=f"data:{mime_type};base64,{encoded}",
                )
            )
        return tuple(attachments)

    def _run_transport(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        error_code = ""
        safe_detail = ""
        try:
            return self.transport.run_turn(prompt, attachments)
        except CanvasAgentTransportError as exc:
            error_code = exc.code
            safe_detail = exc.safe_detail
        except Exception as exc:
            error_code = "thread"
            safe_detail = _safe_exception_detail(exc)
        self._raise_transport_execution_error(error_code, safe_detail)

    def _continue_transport(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        error_code = ""
        safe_detail = ""
        try:
            return self.transport.continue_turn(thread_id, prompt, attachments)
        except CanvasAgentTransportError as exc:
            error_code = exc.code
            safe_detail = exc.safe_detail
        except Exception as exc:
            error_code = "thread"
            safe_detail = _safe_exception_detail(exc)
        self._raise_transport_execution_error(error_code, safe_detail)

    @staticmethod
    def _raise_transport_execution_error(error_code: str, safe_detail: str) -> None:
        if error_code == "empty_response":
            raise CodexDevExecutionError(
                _with_safe_exception_detail(
                    "codex-dev 本轮没有返回内容",
                    safe_detail,
                ),
                "empty_assistant_response",
            )
        messages = {
            "missing_config": "codex-dev 无法使用：canvas-agent 配置缺失",
            "unsafe_config": "codex-dev 无法使用：canvas-agent 配置不是安全的本机地址",
            "connection": "codex-dev 无法连接 canvas-agent",
            "thread": "codex-dev 的 Codex 线程执行失败",
            "response": "codex-dev 收到无效的 canvas-agent 返回",
            "timeout": "codex-dev 等待 Codex 线程超时",
        }
        raise ExecutorExecutionError(
            _with_safe_exception_detail(
                messages.get(error_code, "codex-dev 执行失败"),
                safe_detail,
            )
        )

    @staticmethod
    def _default_repository_root(manifest_path: Path | None) -> Path:
        if manifest_path is not None and manifest_path.parent.name == "manifests":
            return manifest_path.parent.parent
        return Path(__file__).resolve().parent.parent

    def _load_required_rules(self) -> tuple[str, str]:
        skill_root = self.repository_root / ".agents" / "skills" / "product-identity-archive"
        try:
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            reference_text = self._category_recipe().prompts["identity_prompt"]
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法加载产品身份建档规则") from None
        return skill_text, reference_text

    def _load_set_identity_rules(self) -> tuple[str, str, str]:
        skill_root = self.repository_root / ".agents" / "skills" / "set-product-identity"
        try:
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            identity_reference = load_shared_prompt(
                self.repository_root,
                "set_identity_prompt",
            )
            workflow_reference = load_shared_prompt(
                self.repository_root,
                "set_workflow_supplement",
            )
        except (OSError, CategoryRecipeError):
            raise ExecutorExecutionError("codex-dev 无法加载套装产品身份建档规则") from None
        return skill_text, identity_reference, workflow_reference

    def _load_set_angle_layout_rules(self) -> tuple[str, str, str]:
        skill_root = self.repository_root / ".agents" / "skills" / "set-angle-layout-inventory"
        try:
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            inventory_reference = load_shared_prompt(
                self.repository_root,
                "set_angle_layout_prompt",
            )
            layout_reference = load_shared_prompt(
                self.repository_root,
                "set_layout_rules",
            )
        except (OSError, CategoryRecipeError):
            raise ExecutorExecutionError("codex-dev 无法加载套装角度与编排入库规则") from None
        return skill_text, inventory_reference, layout_reference

    def _load_style_master_rules(self) -> tuple[str, str]:
        skill_root = self.repository_root / ".agents" / "skills" / "style-master-extractor"
        try:
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            reference_text = self._category_recipe().prompts["style_prompt"]
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法加载风格母版提取规则") from None
        return skill_text, reference_text

    def _load_angle_inventory_rules(self) -> tuple[str, str]:
        skill_root = self.repository_root / ".agents" / "skills" / "angle-inventory"
        try:
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            reference_text = self._category_recipe().prompts["angle_prompt"]
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法加载角度槽位入库规则") from None
        return skill_text, reference_text

    def _category_recipe(self) -> CategoryRecipe:
        try:
            return load_manifest_category(self.repository_root, self.context.manifest)
        except CategoryRecipeError as exc:
            raise ExecutorExecutionError(f"codex-dev 无法加载产品品类配方：{exc}") from None

    def _validate_single_product_batch(self) -> None:
        batch_type = str(self.context.manifest.get("batch_type") or "single").strip().lower()
        declared_set = self.context.manifest.get("user_declared_set_product") is True
        if batch_type != "single" or declared_set:
            raise ExecutorExecutionError("codex-dev 角度槽位入库只支持单品批次")

    def _load_white_background_images(
        self,
        purpose: str = "identity",
    ) -> tuple[tuple[CodexAttachment, ...], tuple[str, ...]]:
        image_paths = self._white_background_image_paths(purpose)
        attachments = self._image_attachments(image_paths, f"{purpose} 输入图片")
        return attachments, tuple(path.name for path in image_paths)

    def _white_background_image_paths(self, purpose: str) -> tuple[Path, ...]:
        image_paths = self._manifest_image_paths("white_bg_images")
        if not image_paths:
            raise ExecutorExecutionError(f"codex-dev 未找到 {purpose} 可用的白底图")
        return image_paths

    def _manifest_image_paths(
        self,
        input_key: str,
        *,
        sort_by_filename: bool = False,
    ) -> tuple[Path, ...]:
        inputs = self.context.manifest.get("inputs")
        raw_paths = inputs.get(input_key) if isinstance(inputs, Mapping) else None
        values = raw_paths if isinstance(raw_paths, list) else []
        image_paths: list[Path] = []
        for value in values:
            path = Path(str(value))
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                image_paths.append(path)
            elif path.is_dir():
                image_paths.extend(
                    item for item in sorted(path.iterdir(), key=lambda item: item.name.lower())
                    if item.is_file() and item.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
                )
        if sort_by_filename:
            image_paths.sort(key=lambda item: item.name.lower())
        return tuple(image_paths)

    @staticmethod
    def _image_attachments(
        image_paths: tuple[Path, ...],
        purpose: str,
    ) -> tuple[CodexAttachment, ...]:
        attachments: list[CodexAttachment] = []
        try:
            for path in image_paths:
                mime_type = SUPPORTED_IMAGE_SUFFIXES[path.suffix.lower()]
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                attachments.append(
                    CodexAttachment(
                        name=path.name,
                        mime_type=mime_type,
                        data_url=f"data:{mime_type};base64,{encoded}",
                    )
                )
        except OSError:
            raise ExecutorExecutionError(f"codex-dev 无法读取 {purpose}") from None
        return tuple(attachments)

    @staticmethod
    def _validate_empty_set_identity_directory(identity_directory: Path) -> None:
        try:
            has_json = identity_directory.is_dir() and any(
                item.is_file() and item.suffix.lower() == ".json"
                for item in identity_directory.rglob("*")
            )
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法检查套装身份档案目录") from None
        if has_json:
            raise ExecutorExecutionError(
                "套装身份档案目录已存在历史文件，codex-dev 不会覆盖，"
                "请先清空该批次身份档案目录或回收批次后重试。"
            )

    def _load_style_reference_images(self) -> tuple[tuple[CodexAttachment, ...], tuple[str, ...]]:
        inputs = self.context.manifest.get("inputs")
        raw_paths = inputs.get("style_reference_images") if isinstance(inputs, Mapping) else None
        values = raw_paths if isinstance(raw_paths, list) else []
        image_paths: list[Path] = []
        for value in values:
            path = Path(str(value))
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                image_paths.append(path)
            elif path.is_dir():
                image_paths.extend(
                    item for item in sorted(path.iterdir(), key=lambda item: item.name.lower())
                    if item.is_file() and item.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
                )
        if not image_paths:
            raise ExecutorExecutionError("codex-dev 未找到可用的风格参考图")

        attachments: list[CodexAttachment] = []
        try:
            for path in image_paths:
                mime_type = SUPPORTED_IMAGE_SUFFIXES[path.suffix.lower()]
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                attachments.append(
                    CodexAttachment(
                        name=path.name,
                        mime_type=mime_type,
                        data_url=f"data:{mime_type};base64,{encoded}",
                    )
                )
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法读取风格参考图") from None
        return tuple(attachments), tuple(path.name for path in image_paths)

    def _load_product_identity_archive(self) -> dict[str, Any]:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("product_identity_archive") if isinstance(artifacts, Mapping) else None
        if not value:
            raise ExecutorExecutionError("codex-dev 无法定位产品身份档案")
        target = Path(str(value))
        identity_path = target if target.suffix.lower() == ".json" else target / "product_identity_archive.json"
        try:
            archive = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ExecutorExecutionError("codex-dev 无法读取有效的产品身份档案") from None
        if not isinstance(archive, dict) or archive.get("artifact_type") != "product_identity_archive":
            raise ExecutorExecutionError("codex-dev 无法读取有效的产品身份档案")
        return archive

    def _load_set_product_identity(self) -> dict[str, Any]:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("set_product_identity") if isinstance(artifacts, Mapping) else None
        target = Path(str(value)) if value else None
        identity_path = (
            target
            if target is not None and target.suffix.lower() == ".json"
            else target / "set_product_identity.json"
            if target is not None
            else None
        )
        try:
            archive = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ExecutorExecutionError("codex-dev 无法读取有效的套装产品身份档案") from None
        if not isinstance(archive, dict) or archive.get("artifact_type") != "set_product_identity":
            raise ExecutorExecutionError("codex-dev 无法读取有效的套装产品身份档案")
        return archive

    def _identity_output_path(self) -> Path:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("product_identity_archive") if isinstance(artifacts, Mapping) else None
        if not value:
            raise ExecutorExecutionError("codex-dev 无法定位产品身份档案输出位置")
        target = Path(str(value))
        output_path = target if target.suffix.lower() == ".json" else target / "product_identity_archive.json"
        workspace = self.context.manifest.get("workspace")
        artifacts_root_value = workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
        if not artifacts_root_value:
            raise ExecutorExecutionError("codex-dev 无法验证 manifest.workspace.artifacts_root")
        try:
            artifacts_root = Path(str(artifacts_root_value)).resolve()
            resolved_output = output_path.resolve()
            if not resolved_output.is_relative_to(artifacts_root):
                raise ExecutorExecutionError("产品身份档案输出位置不在 manifest.workspace.artifacts_root 内")
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法验证产品身份档案输出位置") from None
        return resolved_output

    def _style_master_output_path(self) -> Path:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("style_master") if isinstance(artifacts, Mapping) else None
        if not value:
            raise ExecutorExecutionError("codex-dev 无法定位风格母版输出位置")
        target = Path(str(value))
        output_path = target if target.suffix.lower() == ".json" else target / "style_master.json"
        workspace = self.context.manifest.get("workspace")
        artifacts_root_value = workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
        if not artifacts_root_value:
            raise ExecutorExecutionError("codex-dev 无法验证 manifest.workspace.artifacts_root")
        try:
            artifacts_root = Path(str(artifacts_root_value)).resolve()
            resolved_output = output_path.resolve()
            if not resolved_output.is_relative_to(artifacts_root):
                raise ExecutorExecutionError("风格母版输出位置不在 manifest.workspace.artifacts_root 内")
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法验证风格母版输出位置") from None
        return resolved_output

    def _angle_inventory_output_path(self) -> Path:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("angle_inventory") if isinstance(artifacts, Mapping) else None
        if not value:
            raise ExecutorExecutionError("codex-dev 无法定位角度槽位入库表输出位置")
        target = Path(str(value))
        output_path = target if target.suffix.lower() == ".json" else target / "angle_inventory.json"
        workspace = self.context.manifest.get("workspace")
        artifacts_root_value = workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
        if not artifacts_root_value:
            raise ExecutorExecutionError("codex-dev 无法验证 manifest.workspace.artifacts_root")
        try:
            artifacts_root = Path(str(artifacts_root_value)).resolve()
            resolved_output = output_path.resolve()
            if not resolved_output.is_relative_to(artifacts_root):
                raise ExecutorExecutionError("角度槽位入库表输出位置不在 manifest.workspace.artifacts_root 内")
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法验证角度槽位入库表输出位置") from None
        return resolved_output

    def _set_angle_layout_output_path(self) -> Path:
        artifacts = self.context.manifest.get("artifacts")
        value = artifacts.get("set_angle_layout_inventory") if isinstance(artifacts, Mapping) else None
        if not value:
            raise ExecutorExecutionError("codex-dev 无法定位套装角度与编排入库表输出位置")
        target = Path(str(value))
        output_path = (
            target
            if target.suffix.lower() == ".json"
            else target / "set_angle_layout_inventory.json"
        )
        workspace = self.context.manifest.get("workspace")
        artifacts_root_value = workspace.get("artifacts_root") if isinstance(workspace, Mapping) else None
        if not artifacts_root_value:
            raise ExecutorExecutionError("codex-dev 无法验证 manifest.workspace.artifacts_root")
        try:
            artifacts_root = Path(str(artifacts_root_value)).resolve()
            resolved_output = output_path.resolve()
            if not resolved_output.is_relative_to(artifacts_root):
                raise ExecutorExecutionError(
                    "套装角度与编排入库表输出位置不在 manifest.workspace.artifacts_root 内"
                )
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法验证套装角度与编排入库表输出位置") from None
        return resolved_output

    @staticmethod
    def _build_angle_image_assets(source_inputs: tuple[str, ...]) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "asset_id": f"img_{index:03d}",
                "file_path": filename,
                "notes": "",
            }
            for index, filename in enumerate(source_inputs, start=1)
        )

    def _load_angle_display_orientations(
        self,
        source_inputs: tuple[str, ...],
    ) -> tuple[dict[str, Any], ...]:
        image_paths = self._white_background_image_paths("angle_inventory")
        if tuple(path.name for path in image_paths) != source_inputs:
            raise ExecutorExecutionError("codex-dev 无法核对 angle_inventory 图片方向")
        rotations = {
            1: "无需旋转",
            3: "旋转180°",
            6: "顺时针旋转90°",
            8: "逆时针旋转90°",
        }
        result: list[dict[str, Any]] = []
        try:
            for index, path in enumerate(image_paths, start=1):
                with path.open("rb") as stream:
                    orientation = self._jpeg_exif_orientation(stream.read(256 * 1024))
                if orientation in rotations and orientation != 1:
                    result.append(
                        {
                            "source_asset_id": f"img_{index:03d}",
                            "file_path": path.name,
                            "exif_orientation": orientation,
                            "display_rotation": rotations[orientation],
                        }
                    )
        except OSError:
            raise ExecutorExecutionError("codex-dev 无法读取 angle_inventory 图片方向") from None
        return tuple(result)

    @staticmethod
    def _jpeg_exif_orientation(data: bytes) -> int | None:
        try:
            if len(data) < 4 or data[:2] != b"\xff\xd8":
                return None
            offset = 2
            while offset + 4 <= len(data):
                if data[offset] != 0xFF:
                    return None
                while offset < len(data) and data[offset] == 0xFF:
                    offset += 1
                if offset >= len(data):
                    return None
                marker = data[offset]
                offset += 1
                if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(data):
                    return None
                segment_length = int.from_bytes(data[offset : offset + 2], "big")
                if segment_length < 2 or offset + segment_length > len(data):
                    return None
                segment = data[offset + 2 : offset + segment_length]
                offset += segment_length
                if marker != 0xE1 or not segment.startswith(b"Exif\x00\x00"):
                    continue
                tiff = segment[6:]
                if len(tiff) < 10 or tiff[:2] not in {b"II", b"MM"}:
                    return None
                byteorder = "little" if tiff[:2] == b"II" else "big"
                if int.from_bytes(tiff[2:4], byteorder) != 42:
                    return None
                ifd_offset = int.from_bytes(tiff[4:8], byteorder)
                if ifd_offset + 2 > len(tiff):
                    return None
                entry_count = int.from_bytes(tiff[ifd_offset : ifd_offset + 2], byteorder)
                for entry_index in range(entry_count):
                    start = ifd_offset + 2 + entry_index * 12
                    entry = tiff[start : start + 12]
                    if len(entry) < 12:
                        return None
                    tag = int.from_bytes(entry[0:2], byteorder)
                    field_type = int.from_bytes(entry[2:4], byteorder)
                    count = int.from_bytes(entry[4:8], byteorder)
                    if tag == 0x0112 and field_type == 3 and count == 1:
                        orientation = int.from_bytes(entry[8:10], byteorder)
                        return orientation if 1 <= orientation <= 8 else None
                return None
        except (IndexError, ValueError):
            return None
        return None

    def _build_prompt(
        self,
        product_id: str,
        source_inputs: tuple[str, ...],
        skill_text: str,
        reference_text: str,
    ) -> str:
        notes = str(self.context.manifest.get("notes") or "")
        return f"""你正在执行受限的开发适配器任务。只处理 identity（单品《产品身份档案》），不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
输入图片文件名：{json.dumps(source_inputs, ensure_ascii=False)}
用户备注：{json.dumps(notes, ensure_ascii=False)}

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下 required reference：
--- REFERENCE START ---
{reference_text}
--- REFERENCE END ---

仅返回一个 JSON 对象，不要 Markdown 说明。顶层必须包含 artifact_type、identity、missing_information、blocked_reasons、notes。
artifact_type 必须为 product_identity_archive。identity 必须明确包含四个数组：
- confirmed_facts：已确认事实
- visible_inferences：可见推断
- unknowns：无法确认
- prohibited_inventions：禁止虚构内容

identity 同时应按 required reference 提供可复用字段，包括 product_name、product_category、components、core_shape、visual_proportions、true_dimensions、color_and_material、texture_and_surface、pattern_and_decoration、structural_details、angle_usage_rules、must_keep、allowed_changes、negative_prompt_constraints、product_lock_description。无法确认的内容必须明确写无法确认，不得虚构尺寸、容量、材质、认证、配件或不可见结构。

不得返回 style_master、angle_inventory、angle_slots、variable_configs、final_prompt、final_prompts、images 或 qc_results。
"""

    def _build_component_identity_prompt(
        self,
        product_id: str,
        component_index: int,
        component_total: int,
        source_filename: str,
        skill_text: str,
        reference_text: str,
    ) -> str:
        notes = str(self.context.manifest.get("notes") or "")
        return f"""你正在执行受限的开发适配器任务。只处理 identity（套装批次的组成单件建档），不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
本单件为套装第 {component_index}/{component_total} 件，文件名 {source_filename}
用户备注：{json.dumps(notes, ensure_ascii=False)}

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下 required reference：
--- REFERENCE START ---
{reference_text}
--- REFERENCE END ---

仅返回一个 JSON 对象，不要 Markdown 说明。顶层必须包含 artifact_type、identity、missing_information、blocked_reasons、notes。
artifact_type 必须为 product_identity_archive。identity 必须明确包含四个数组：
- confirmed_facts：已确认事实
- visible_inferences：可见推断
- unknowns：无法确认
- prohibited_inventions：禁止虚构内容

identity 同时应按 required reference 提供可复用字段，包括 product_name、product_category、components、core_shape、visual_proportions、true_dimensions、color_and_material、texture_and_surface、pattern_and_decoration、structural_details、angle_usage_rules、must_keep、allowed_changes、negative_prompt_constraints、product_lock_description。无法确认的内容必须明确写无法确认，不得虚构尺寸、容量、材质、认证、配件或不可见结构。

不得返回 style_master、angle_inventory、angle_slots、variable_configs、final_prompt、final_prompts、images 或 qc_results。
"""

    def _build_set_identity_prompt(
        self,
        product_id: str,
        group_source_inputs: tuple[str, ...],
        component_source_inputs: tuple[str, ...],
        component_archives: list[dict[str, Any]],
        skill_text: str,
        identity_reference_text: str,
        workflow_reference_text: str,
    ) -> str:
        component_records = [
            {
                "component_index": index,
                "component_source_image": source_filename,
                "identity_archive": archive,
            }
            for index, (source_filename, archive) in enumerate(
                zip(component_source_inputs, component_archives),
                start=1,
            )
        ]
        component_records_json = json.dumps(
            component_records,
            ensure_ascii=False,
            indent=2,
        )
        return f"""你正在执行受限的开发适配器任务。只处理 identity，只产出《套装产品身份档案》，不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
套装合影文件名：{json.dumps(group_source_inputs, ensure_ascii=False)}
组成单件数量：{len(component_archives)}

以下是按顺序完成建档的全部组成单件。每项包含机器序号、原图文件名和该单件《产品身份档案》JSON 全文：
--- COMPONENT ARCHIVES START ---
{component_records_json}
--- COMPONENT ARCHIVES END ---

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下 required reference：
--- REFERENCE START ---
{identity_reference_text}
--- REFERENCE END ---

必须完整遵守以下 required reference：
--- REFERENCE START ---
{workflow_reference_text}
--- REFERENCE END ---

仅返回一个 JSON 对象，不要 Markdown 说明。顶层必须包含 artifact_type、set_identity、components、missing_information、notes；artifact_type 必须为 set_product_identity。

set_identity 必须是非空对象，并按教学要求承载组合层信息，包括套装名称、套装类别、件数、可分性、固定搭配、主次关系、相对比例、尺寸汇总、排列组合、组成与道具边界、必须保持不变、允许变化、禁止错误和套装锁定描述。

components 必须是数组，严格按上述单件顺序逐项给出组合层描述，包括单件名称、数量、主次地位等；数组项数必须恰好为 {len(component_archives)}。不要把单件身份档案全文复制到 components 中。

不得返回 angle_slots、variable_configs、final_prompt、final_prompts、qc_results、style_master、angle_inventory、images、qc_reports、set_angle_layout_inventory 或 set_layouts。
"""

    def _build_style_master_prompt(
        self,
        product_id: str,
        source_references: tuple[str, ...],
        identity_archive: Mapping[str, Any],
        skill_text: str,
        reference_text: str,
    ) -> str:
        required_fields = json.dumps(REQUIRED_STYLE_MASTER_FIELDS, ensure_ascii=False)
        identity_json = json.dumps(identity_archive, ensure_ascii=False, indent=2)
        structure_example = json.dumps(
            {
                "artifact_type": "style_master",
                "style_master": {
                    "visual_positioning": "按规则填写",
                    "composition_and_layout": "按规则填写",
                    "background_rules": "按规则填写",
                    "color_rules": "按规则填写",
                    "lighting_rules": "按规则填写",
                    "subject_presentation_rules": "按规则填写",
                    "prop_rules": "按规则填写",
                    "typography_rules": "按规则填写",
                    "negative_space_rules": "按规则填写",
                    "visual_mood": "按规则填写",
                    "reusable_rules": ["按规则填写"],
                    "fidelity_enhancements": {
                        "style_anchors": ["按规则填写"],
                        "reusable_prop_clusters": {
                            "must_keep": ["按规则填写"],
                            "replaceable": ["按规则填写"],
                            "optional": [],
                        },
                        "background_layers": {
                            "foreground": "按规则填写",
                            "midground": "按规则填写",
                            "background": "按规则填写",
                        },
                        "prop_density_level": "按规则填写",
                        "contents_and_usage_state": "按规则填写",
                        "text_inheritance": "按规则填写",
                        "anti_degradation_rules": ["按规则填写"],
                    },
                    "forbidden_elements": ["至少 8 条明确禁项"],
                    "concise_style_master": "按 required reference 填写 300 至 500 字精简版",
                },
                "missing_information": ["无法确认项；没有则返回空数组"],
                "notes": "",
            },
            ensure_ascii=False,
            indent=2,
        )
        return f"""你正在执行受限的开发适配器任务。只处理 style_master（单品《风格母版》），不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
风格参考图文件名：{json.dumps(source_references, ensure_ascii=False)}

以下既有《产品身份档案》是上位约束。只用它防止风格规则覆盖产品结构、比例、颜色、材质、纹理、图案、配件关系或真实尺寸；不得把其中的产品事实复制成风格规则：
--- PRODUCT IDENTITY START ---
{identity_json}
--- PRODUCT IDENTITY END ---

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下 required reference：
--- REFERENCE START ---
{reference_text}
--- REFERENCE END ---

仅返回一个 JSON 对象，不要 Markdown 说明。顶层只返回 artifact_type、style_master、missing_information、notes。
artifact_type 必须为 style_master。style_master 必须是对象并完整包含以下键：{required_fields}。
fidelity_enhancements 必须覆盖风格贴合锚点、可复用道具簇、背景层次结构、道具密度等级、内容物与使用状态、文字有无继承和风格防退化规则。
forbidden_elements 至少包含 8 条明确禁项。concise_style_master 必须是 required reference 要求的最终可复制精简版。

返回层级必须严格遵循以下有效 JSON 骨架，只替换内容，不改变字段层级或括号位置：
--- JSON SHAPE START ---
{structure_example}
--- JSON SHAPE END ---
forbidden_elements 和 concise_style_master 必须位于 style_master 对象内部，不能放到顶层。
missing_information 和 notes 必须在 style_master 对象关闭后、根对象关闭前。
返回前逐层检查括号：整个回复只能有一个根对象，根对象最后一个字符才是最终右花括号；不得在根对象关闭后追加任何字段或说明。

不得返回 product_identity_archive、identity、angle_inventory、angle_slots、variable_configs、main_variable_configs、detail_variable_configs、final_prompt、final_prompts、images 或 qc_results。
        不得虚构产品规格、功能、认证、销量或不可见结构；参考图中的具体产品、品牌、文案和图案不得作为固定模板复制。
"""

    def _build_angle_inventory_prompt(
        self,
        product_id: str,
        image_assets: tuple[Mapping[str, str], ...],
        display_orientations: tuple[Mapping[str, Any], ...],
        identity_archive: Mapping[str, Any],
        skill_text: str,
        reference_text: str,
    ) -> str:
        identity_json = json.dumps(identity_archive, ensure_ascii=False, indent=2)
        image_assets_json = json.dumps(image_assets, ensure_ascii=False, indent=2)
        display_orientations_json = json.dumps(display_orientations, ensure_ascii=False, indent=2)
        structure_example = json.dumps(
            {
                "artifact_type": "angle_inventory",
                "angle_slots": [
                    {
                        "angle_slot": "A、B、C、D 或 不适合归入现有槽位",
                        "source_asset_id": "严格使用上方映射中的 img_编号",
                        "camera_angle": "按单张白底图实际角度填写",
                        "decision_basis": "说明朝向、俯仰和可见结构依据",
                        "naturally_visible_content": ["该角度自然可见内容"],
                        "must_not_force_content": ["该角度不应强行展示内容"],
                        "suitable_page_tasks": ["适合承担的页面任务"],
                        "unsuitable_page_tasks": ["不适合承担的页面任务"],
                        "main_image_suitability": "适合/勉强适合/不适合：简要理由",
                        "detail_image_suitability": "适合/勉强适合/不适合：简要理由",
                        "risk_notes": "风险说明；无明显风险时写无明显风险",
                        "recommended_task_binding": "建议绑定任务",
                        "admission_result": "三种固定入库结论之一",
                        "merged_reference_note": "未提供时写无",
                        "usable_for": ["主图或详情图用途"],
                        "notes": "",
                    }
                ],
                "missing_angle_slots": ["A、B、C、D 中缺失的槽位"],
                "retake_recommendations": ["仅针对角度、清晰度、遮挡和完整性的重拍建议"],
                "notes": "",
            },
            ensure_ascii=False,
            indent=2,
        )
        angle_boundary = self._category_recipe().prompts["angle_boundary"].rstrip("\r\n")
        return f"""你正在执行受限的开发适配器任务。只处理 angle_inventory（单品《角度槽位入库表》），不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
批次类型：single

以下 image_assets 是适配器根据真实附件固定生成的唯一图片映射。每个 source_asset_id 必须恰好使用一次，不得遗漏、重复或新增；不要在返回中改写文件路径：
--- IMAGE ASSETS START ---
{image_assets_json}
--- IMAGE ASSETS END ---

以下是图片文件的 EXIF 正确显示方向。视觉附件可能呈现未应用 EXIF 的原始横向像素；必须先按每项 display_rotation 在脑中旋转到正确显示方向，再判断产品真实姿态。未列出的图片按当前显示方向判断。不得把未应用 EXIF 时看到的侧躺外观当成产品真实横放：
--- DISPLAY ORIENTATIONS START ---
{display_orientations_json}
--- DISPLAY ORIENTATIONS END ---

先按 EXIF 方向纠正显示，再判断产品是否直立、底部是否自然朝下以及镜头俯仰。图片中的品牌、型号、认证或其他小字不可辨认时，不得猜测、转写或拼接字符；不可辨认文字统一写“无法辨认”，不得输出 Unicode 替换字符或其他乱码。

以下既有《产品身份档案》只用于核对产品身份和防止虚构。不得用它改变任何单张白底图的实际角度，也不得输出产品身份档案：
--- PRODUCT IDENTITY START ---
{identity_json}
--- PRODUCT IDENTITY END ---

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下 required reference 的单品主体规则：
--- REFERENCE START ---
{reference_text}
--- REFERENCE END ---

本批次已确认是单品。required reference 末尾出现的“是否套装合影白底图、套装编排槽位判断、件数核对”和 Markdown 代码块要求与本 Skill 的单品职责冲突；必须忽略末尾误植的套装字段，不得输出套装判断或编排。只按 A、B、C、D 固定单品槽位逐张识别；不符合任何槽位时写“不适合归入现有槽位”，不得为了凑齐槽位强行归类。

{angle_boundary}

仅返回一个 JSON 对象，不要 Markdown 说明或代码块外文字。顶层只返回 artifact_type、angle_slots、missing_angle_slots、retake_recommendations、notes；artifact_type 必须为 angle_inventory。angle_slots 数量必须等于 image_assets 数量，并严格遵循以下字段层级：
--- JSON SHAPE START ---
{structure_example}
--- JSON SHAPE END ---

admission_result 只允许“合格，可进入对应槽位”“勉强可用，但建议重拍”“不适合入库，需重拍”。main_image_suitability 和 detail_image_suitability 必须以“适合”“勉强适合”或“不适合”开头并附简要理由。missing_angle_slots 只列 A、B、C、D；retake_recommendations 只涉及角度、清晰度、遮挡和产品完整性。

不得返回 product_identity_archive、identity、style_master、set_layouts、set_arrangements、variable_configs、main_variable_configs、detail_variable_configs、final_prompt、final_prompts、images 或 qc_results。不得虚构尺寸、容量、材质、认证、配件或不可见结构；不得让风格、页面任务或身份档案反向改变白底图实际角度。
"""

    def _build_set_angle_layout_prompt(
        self,
        product_id: str,
        group_names: tuple[str, ...],
        component_names: tuple[str, ...],
        set_identity: Mapping[str, Any],
        skill_text: str,
        inventory_reference: str,
        layout_reference: str,
    ) -> str:
        group_listing = tuple(
            {"image_index": index, "file_name": filename}
            for index, filename in enumerate(group_names, start=1)
        )
        component_listing = tuple(
            {"image_index": len(group_names) + index, "file_name": filename}
            for index, filename in enumerate(component_names, start=1)
        )
        identity_json = json.dumps(set_identity, ensure_ascii=False, indent=2)
        group_json = json.dumps(group_listing, ensure_ascii=False, indent=2)
        component_json = json.dumps(component_listing, ensure_ascii=False, indent=2)
        required_fields = json.dumps(SET_ANGLE_LAYOUT_REQUIRED_FIELDS, ensure_ascii=False)
        structure_example = json.dumps(
            {
                "artifact_type": "set_angle_layout_inventory",
                "layouts": [
                    {
                        "layout_id": "layout_001",
                        "image_index": 1,
                        "file_name": "严格使用对应附件的真实文件名",
                        "is_set_group": True,
                        "overall_camera": "A、B、C、D 或 不适合归入现有机位",
                        "camera_decision_basis": "整体机位判断依据",
                        "layout_slot": "编排槽位一至四或不适合归入现有编排",
                        "layout_decision_basis": "编排槽位判断依据",
                        "piece_count_check": "与套装产品身份档案锁定件数的核对结论",
                        "component_visibility": "各单件可见性",
                        "naturally_visible_content": "该组合下自然可展示的内容",
                        "must_not_force_content": "该组合下不应强行展示的内容",
                        "suitable_page_tasks": "适合承担的页面任务",
                        "unsuitable_page_tasks": "不适合承担的页面任务",
                        "main_image_suitability": "适合、勉强适合或不适合，并说明原因",
                        "detail_image_suitability": "适合、勉强适合或不适合，并说明原因",
                        "risk_notes": "风险说明",
                        "recommended_task_binding": "建议绑定任务",
                        "admission_result": "三种固定入库结论之一",
                        "merged_reference_note": "未提供多角度单件合并参考图时写无",
                    }
                ],
                "notes": "",
            },
            ensure_ascii=False,
            indent=2,
        )
        return f"""你正在执行受限的开发适配器任务。只处理 set_angle_layout_inventory（套装《套装角度与编排入库表》），不得操作画布，不得调用其他工作流步骤，不得生成图片。

批次产品 ID：{json.dumps(product_id, ensure_ascii=False)}
批次类型：set
用户已明确声明这是套装产品。

附件严格按以下顺序提供：先是套装合影图，再是套装组成单件白底图。layouts 必须按同一附件顺序逐项输出，不得遗漏、重复、调换或新增。

套装合影图文件名清单：
--- SET GROUP FILES START ---
{group_json}
--- SET GROUP FILES END ---

套装组成单件白底图文件名清单：
--- COMPONENT FILES START ---
{component_json}
--- COMPONENT FILES END ---

以下既有《套装产品身份档案》只用于核对锁定件数、组成、主次、相对比例和组合关系，不得用它改变附件中的实际机位或编排，也不得在返回中输出该档案：
--- SET PRODUCT IDENTITY START ---
{identity_json}
--- SET PRODUCT IDENTITY END ---

必须完整遵守以下 Skill：
--- SKILL START ---
{skill_text}
--- SKILL END ---

必须完整遵守以下《套装角度与编排入库表提示词》：
--- SET ANGLE LAYOUT PROMPT START ---
{inventory_reference}
--- SET ANGLE LAYOUT PROMPT END ---

必须完整遵守以下《套装编排规则》：
--- SET LAYOUT RULES START ---
{layout_reference}
--- SET LAYOUT RULES END ---

仅返回一个 JSON 对象；允许把 JSON 放在单个 ```json 围栏中，但不要返回围栏外说明。顶层只允许 artifact_type、layouts、notes，其中 notes 可省略。artifact_type 必须为 set_angle_layout_inventory。

layouts 条目数必须恰好为 {len(group_names) + len(component_names)}，并按附件顺序对齐。每条只允许以下字段且必须全部存在：{required_fields}。
layout_id 必须按附件顺序从 layout_001 连续编号；image_index 必须是从 1 开始的整数；file_name 必须与对应附件真实文件名全等；is_set_group 必须是 JSON 布尔值。
overall_camera 只允许 A、B、C、D 或“不适合归入现有机位”。套装合影条目的 layout_slot 只允许“编排槽位一”“编排槽位二”“编排槽位三”“编排槽位四”“不适合归入现有编排”；单件条目的 layout_slot 与 piece_count_check 都固定写“{SET_ANGLE_COMPONENT_LAYOUT_TEXT}”。
camera_decision_basis、layout_decision_basis、piece_count_check、component_visibility、naturally_visible_content、must_not_force_content、suitable_page_tasks、unsuitable_page_tasks、main_image_suitability、detail_image_suitability、risk_notes、recommended_task_binding、admission_result、merged_reference_note 都必须是非空字符串。
admission_result 只允许“合格，可进入对应机位与编排槽位”“勉强可用，但建议重拍”“不适合入库，需重拍”。main_image_suitability 和 detail_image_suitability 必须以“适合”“勉强适合”或“不适合”开头并说明原因。

返回结构示例：
--- JSON SHAPE START ---
{structure_example}
--- JSON SHAPE END ---

product_id、user_declared_set_product、set_group_assets 由代码依据 manifest 和真实附件注入，模型不得返回；也不得返回 explicit_set_request、set_product_identity、angle_inventory、angle_slots、variable_configs、main_variable_configs、detail_variable_configs、final_prompt、final_prompts、qc_results 或 images。不得虚构套装件数、尺寸、容量、材质、认证、配件或不可见结构，不得输出 Unicode 替换字符。
"""

    def _parse_archive(self, text: str, product_id: str, source_inputs: tuple[str, ...]) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ExecutorExecutionError("codex-dev 返回格式异常：不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ExecutorExecutionError("codex-dev 返回格式异常：根对象无效")
        if value.get("artifact_type") != "product_identity_archive":
            raise ExecutorExecutionError("codex-dev 返回格式异常：产物类型无效")
        if FORBIDDEN_OUTPUT_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")
        identity = value.get("identity")
        if not isinstance(identity, dict) or FORBIDDEN_OUTPUT_FIELDS.intersection(identity):
            raise ExecutorExecutionError("codex-dev 返回格式异常：identity 无效")
        for field in REQUIRED_EVIDENCE_FIELDS:
            items = identity.get(field)
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ExecutorExecutionError("codex-dev 返回格式异常：四类证据未完整区分")

        archive = dict(value)
        archive["product_id"] = product_id
        archive["artifact_type"] = "product_identity_archive"
        archive["source_inputs"] = list(source_inputs)
        archive["identity"] = identity
        archive["missing_information"] = self._string_list(value.get("missing_information"))
        archive["blocked_reasons"] = self._string_list(value.get("blocked_reasons"))
        archive["notes"] = str(value.get("notes") or "")
        return archive

    def _parse_set_identity_archive(
        self,
        text: str,
        product_id: str,
        source_inputs: tuple[str, ...],
        component_source_inputs: tuple[str, ...],
        identity_archive_files: tuple[str, ...],
    ) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            candidate,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidate = fenced.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            raise ExecutorExecutionError("codex-dev 返回格式异常：不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ExecutorExecutionError("codex-dev 返回格式异常：根对象无效")
        if value.get("artifact_type") != "set_product_identity":
            raise ExecutorExecutionError("codex-dev 返回格式异常：产物类型无效")
        if SET_IDENTITY_FORBIDDEN_OUTPUT_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")

        set_identity = value.get("set_identity")
        if not isinstance(set_identity, dict) or not set_identity:
            raise ExecutorExecutionError("codex-dev 返回格式异常：set_identity 无效")
        if SET_IDENTITY_FORBIDDEN_OUTPUT_FIELDS.intersection(set_identity):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")

        expected_component_count = len(component_source_inputs)
        components = value.get("components")
        if not isinstance(components, list) or len(components) != expected_component_count:
            raise ExecutorExecutionError("codex-dev 返回格式异常：套装组成条目数量无效")
        normalized_components: list[dict[str, Any]] = []
        for index, component in enumerate(components):
            if (
                not isinstance(component, dict)
                or SET_IDENTITY_COMPONENT_FORBIDDEN_FIELDS.intersection(component)
            ):
                raise ExecutorExecutionError("codex-dev 返回格式异常：套装组成条目无效")
            normalized_component = dict(component)
            normalized_component["component_index"] = index + 1
            normalized_component["component_source_image"] = component_source_inputs[index]
            normalized_component["identity_archive_file"] = identity_archive_files[index]
            normalized_components.append(normalized_component)

        archive = dict(value)
        archive["product_id"] = product_id
        archive["artifact_type"] = "set_product_identity"
        archive["user_declared_set_product"] = True
        archive["source_inputs"] = list(source_inputs)
        archive["set_identity"] = dict(set_identity)
        archive["components"] = normalized_components
        archive["missing_information"] = self._string_list(
            value.get("missing_information")
        )
        archive["notes"] = str(value.get("notes") or "")
        return archive

    def _parse_style_master(
        self,
        text: str,
        product_id: str,
        source_references: tuple[str, ...],
    ) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            raise ExecutorExecutionError("codex-dev 返回格式异常：不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ExecutorExecutionError("codex-dev 返回格式异常：根对象无效")
        if value.get("artifact_type") != "style_master":
            raise ExecutorExecutionError("codex-dev 返回格式异常：产物类型无效")
        if STYLE_MASTER_FORBIDDEN_OUTPUT_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")
        style_master = value.get("style_master")
        if not isinstance(style_master, dict) or STYLE_MASTER_FORBIDDEN_OUTPUT_FIELDS.intersection(style_master):
            raise ExecutorExecutionError("codex-dev 返回格式异常：style_master 无效")
        missing_fields = [
            field
            for field in REQUIRED_STYLE_MASTER_FIELDS
            if field not in style_master or style_master[field] in (None, "", [], {})
        ]
        if missing_fields:
            raise ExecutorExecutionError("codex-dev 返回格式异常：风格母版栏目不完整")

        artifact = dict(value)
        artifact["product_id"] = product_id
        artifact["artifact_type"] = "style_master"
        artifact["source_references"] = list(source_references)
        artifact["style_master"] = style_master
        artifact["missing_information"] = self._string_list(value.get("missing_information"))
        artifact["notes"] = str(value.get("notes") or "")
        return artifact

    def _parse_angle_inventory(
        self,
        text: str,
        product_id: str,
        image_assets: tuple[Mapping[str, str], ...],
    ) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        if "\ufffd" in candidate:
            raise ExecutorExecutionError("codex-dev 返回格式异常：文本包含损坏字符")
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            raise ExecutorExecutionError("codex-dev 返回格式异常：不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ExecutorExecutionError("codex-dev 返回格式异常：根对象无效")
        if value.get("artifact_type") != "angle_inventory":
            raise ExecutorExecutionError("codex-dev 返回格式异常：产物类型无效")
        if not set(value).issubset(ANGLE_ALLOWED_OUTPUT_FIELDS):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含未声明顶层字段")
        if ANGLE_FORBIDDEN_OUTPUT_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")

        angle_slots = value.get("angle_slots")
        if not isinstance(angle_slots, list) or len(angle_slots) != len(image_assets):
            raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目数量无效")

        expected_asset_ids = {item["asset_id"] for item in image_assets}
        observed_asset_ids: list[str] = []
        for slot in angle_slots:
            if not isinstance(slot, dict) or ANGLE_FORBIDDEN_OUTPUT_FIELDS.intersection(slot):
                raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目无效")
            if any(field not in slot for field in ANGLE_SLOT_REQUIRED_FIELDS):
                raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目栏目不完整")

            angle_slot = slot.get("angle_slot")
            source_asset_id = slot.get("source_asset_id")
            if angle_slot not in ANGLE_SLOT_VALUES or not isinstance(source_asset_id, str):
                raise ExecutorExecutionError("codex-dev 返回格式异常：角度槽位无效")
            observed_asset_ids.append(source_asset_id)

            for field in ANGLE_LIST_FIELDS:
                items = slot.get(field)
                if (
                    not isinstance(items, list)
                    or not items
                    or any(not isinstance(item, str) or not item.strip() for item in items)
                ):
                    raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目列表无效")

            for field in (
                "camera_angle",
                "decision_basis",
                "main_image_suitability",
                "detail_image_suitability",
                "risk_notes",
                "recommended_task_binding",
                "admission_result",
                "merged_reference_note",
            ):
                item = slot.get(field)
                if not isinstance(item, str) or not item.strip():
                    raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目文本无效")
            if not isinstance(slot.get("notes"), str):
                raise ExecutorExecutionError("codex-dev 返回格式异常：角度条目备注无效")
            if slot["admission_result"] not in ANGLE_ADMISSION_VALUES:
                raise ExecutorExecutionError("codex-dev 返回格式异常：入库结论无效")
            for field in ("main_image_suitability", "detail_image_suitability"):
                if not str(slot[field]).startswith(ANGLE_SUITABILITY_PREFIXES):
                    raise ExecutorExecutionError("codex-dev 返回格式异常：页面适用性无效")

        if len(observed_asset_ids) != len(set(observed_asset_ids)) or set(observed_asset_ids) != expected_asset_ids:
            raise ExecutorExecutionError("codex-dev 返回格式异常：图片对应关系无效")

        missing_angle_slots = value.get("missing_angle_slots")
        if (
            not isinstance(missing_angle_slots, list)
            or any(item not in {"A", "B", "C", "D"} for item in missing_angle_slots)
            or len(missing_angle_slots) != len(set(missing_angle_slots))
        ):
            raise ExecutorExecutionError("codex-dev 返回格式异常：缺失槽位无效")
        usable_angle_slots = {
            slot["angle_slot"]
            for slot in angle_slots
            if slot["angle_slot"] in {"A", "B", "C", "D"}
            and slot["admission_result"] != "不适合入库，需重拍"
        }
        if set(missing_angle_slots) != {"A", "B", "C", "D"} - usable_angle_slots:
            raise ExecutorExecutionError("codex-dev 返回格式异常：缺失槽位与逐图结论不一致")
        retake_recommendations = value.get("retake_recommendations", [])
        if not isinstance(retake_recommendations, list) or any(
            not isinstance(item, str) for item in retake_recommendations
        ):
            raise ExecutorExecutionError("codex-dev 返回格式异常：重拍建议无效")

        artifact = dict(value)
        artifact["product_id"] = product_id
        artifact["artifact_type"] = "angle_inventory"
        artifact["image_assets"] = [dict(item) for item in image_assets]
        artifact["angle_slots"] = angle_slots
        artifact["missing_angle_slots"] = missing_angle_slots
        artifact["retake_recommendations"] = retake_recommendations
        artifact["notes"] = str(value.get("notes") or "")
        return artifact

    def _parse_set_angle_layout_inventory(
        self,
        text: str,
        product_id: str,
        group_names: tuple[str, ...],
        component_names: tuple[str, ...],
    ) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            candidate,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidate = fenced.group(1)
        if "\ufffd" in candidate:
            raise ExecutorExecutionError("codex-dev 返回格式异常：文本包含损坏字符")
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            raise ExecutorExecutionError("codex-dev 返回格式异常：不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ExecutorExecutionError("codex-dev 返回格式异常：根对象无效")
        if value.get("artifact_type") != "set_angle_layout_inventory":
            raise ExecutorExecutionError("codex-dev 返回格式异常：产物类型无效")
        if SET_ANGLE_LAYOUT_INJECTED_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含代码注入域字段")
        if SET_ANGLE_LAYOUT_FORBIDDEN_FIELDS.intersection(value):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含越界工作流产物")
        if not set(value).issubset(SET_ANGLE_LAYOUT_ALLOWED_OUTPUT_FIELDS):
            raise ExecutorExecutionError("codex-dev 返回格式异常：包含未声明顶层字段")

        expected_names = (*group_names, *component_names)
        layouts = value.get("layouts")
        if not isinstance(layouts, list) or len(layouts) != len(expected_names):
            raise ExecutorExecutionError("codex-dev 返回格式异常：套装角度与编排条目数量无效")

        normalized_layouts: list[dict[str, Any]] = []
        required_fields = set(SET_ANGLE_LAYOUT_REQUIRED_FIELDS)
        group_count = len(group_names)
        for index, (layout, expected_name) in enumerate(
            zip(layouts, expected_names),
            start=1,
        ):
            if not isinstance(layout, dict):
                raise ExecutorExecutionError("codex-dev 返回格式异常：套装角度与编排条目无效")
            if set(layout) != required_fields:
                raise ExecutorExecutionError("codex-dev 返回格式异常：套装角度与编排条目字段无效")
            if layout.get("layout_id") != f"layout_{index:03d}":
                raise ExecutorExecutionError("codex-dev 返回格式异常：图序号无效")
            if type(layout.get("image_index")) is not int or layout["image_index"] != index:
                raise ExecutorExecutionError("codex-dev 返回格式异常：图序号无效")
            if layout.get("file_name") != expected_name:
                raise ExecutorExecutionError("codex-dev 返回格式异常：文件名对应关系无效")

            expected_group = index <= group_count
            if type(layout.get("is_set_group")) is not bool or layout["is_set_group"] is not expected_group:
                raise ExecutorExecutionError("codex-dev 返回格式异常：套装合影标记无效")
            if layout.get("overall_camera") not in SET_ANGLE_CAMERA_VALUES:
                raise ExecutorExecutionError("codex-dev 返回格式异常：整体机位无效")

            layout_slot = layout.get("layout_slot")
            if expected_group:
                if layout_slot not in SET_ANGLE_LAYOUT_VALUES:
                    raise ExecutorExecutionError("codex-dev 返回格式异常：套装编排槽位无效")
            elif layout_slot != SET_ANGLE_COMPONENT_LAYOUT_TEXT:
                raise ExecutorExecutionError("codex-dev 返回格式异常：单件编排声明无效")
            if not expected_group and layout.get("piece_count_check") != SET_ANGLE_COMPONENT_LAYOUT_TEXT:
                raise ExecutorExecutionError("codex-dev 返回格式异常：单件件数核对声明无效")

            for field in SET_ANGLE_LAYOUT_TEXT_FIELDS:
                item = layout.get(field)
                if not isinstance(item, str) or not item.strip():
                    raise ExecutorExecutionError("codex-dev 返回格式异常：套装角度与编排文本无效")
            if layout["admission_result"] not in SET_ANGLE_ADMISSION_VALUES:
                raise ExecutorExecutionError("codex-dev 返回格式异常：入库结论无效")
            for field in ("main_image_suitability", "detail_image_suitability"):
                if not layout[field].startswith(ANGLE_SUITABILITY_PREFIXES):
                    raise ExecutorExecutionError("codex-dev 返回格式异常：页面适用性无效")
            normalized_layouts.append(dict(layout))

        notes = value.get("notes", "")
        if not isinstance(notes, str):
            raise ExecutorExecutionError("codex-dev 返回格式异常：套装角度与编排备注无效")
        artifact = {
            "product_id": product_id,
            "artifact_type": "set_angle_layout_inventory",
            "user_declared_set_product": True,
            "set_group_assets": [
                {
                    "asset_id": f"set_group_{index:03d}",
                    "file_path": filename,
                }
                for index, filename in enumerate(group_names, start=1)
            ],
            "layouts": normalized_layouts,
            "notes": notes,
        }
        self._validate_set_angle_layout_schema_contract(artifact)
        return artifact

    @staticmethod
    def _validate_set_angle_layout_schema_contract(artifact: Mapping[str, Any]) -> None:
        required = {"product_id", "artifact_type", "set_group_assets", "layouts"}
        forbidden = {"angle_slots", "variable_configs", "final_prompt", "qc_results"}
        valid = (
            required.issubset(artifact)
            and isinstance(artifact.get("product_id"), str)
            and bool(str(artifact.get("product_id") or "").strip())
            and artifact.get("artifact_type") == "set_angle_layout_inventory"
            and (
                artifact.get("user_declared_set_product") is True
                or "explicit_set_request" in artifact
            )
            and not forbidden.intersection(artifact)
        )
        set_group_assets = artifact.get("set_group_assets")
        valid = valid and isinstance(set_group_assets, list) and all(
            isinstance(item, Mapping)
            and {"asset_id", "file_path"}.issubset(item)
            and isinstance(item.get("asset_id"), str)
            and isinstance(item.get("file_path"), str)
            for item in set_group_assets or []
        )
        layouts = artifact.get("layouts")
        valid = valid and isinstance(layouts, list) and all(
            isinstance(item, Mapping) and isinstance(item.get("layout_id"), str)
            for item in layouts or []
        )
        if not valid:
            raise ExecutorExecutionError(
                "codex-dev 返回格式异常：套装角度与编排产物不符合 schema"
            )

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @staticmethod
    def _write_archive(output_path: Path, archive: Mapping[str, Any]) -> None:
        temporary: Path | None = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                raise FileExistsError
            content = json.dumps(archive, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.link(temporary, output_path)
            temporary.unlink()
        except FileExistsError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ExecutorExecutionError("产品身份档案已存在，codex-dev 不会覆盖") from None
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ExecutorExecutionError("codex-dev 无法写入产品身份档案") from None

    @staticmethod
    def _write_style_master(output_path: Path, artifact: Mapping[str, Any]) -> None:
        temporary: Path | None = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                raise FileExistsError
            content = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.link(temporary, output_path)
            temporary.unlink()
        except FileExistsError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ExecutorExecutionError("风格母版已存在，codex-dev 不会覆盖") from None
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ExecutorExecutionError("codex-dev 无法写入风格母版") from None

    @staticmethod
    def _write_angle_inventory(output_path: Path, artifact: Mapping[str, Any]) -> None:
        temporary: Path | None = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                raise FileExistsError
            content = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.link(temporary, output_path)
            temporary.unlink()
        except FileExistsError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ExecutorExecutionError("角度槽位入库表已存在，codex-dev 不会覆盖") from None
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ExecutorExecutionError("codex-dev 无法写入角度槽位入库表") from None

    @staticmethod
    def _write_set_angle_layout_inventory(
        output_path: Path,
        artifact: Mapping[str, Any],
    ) -> None:
        temporary: Path | None = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                raise FileExistsError
            content = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.link(temporary, output_path)
            temporary.unlink()
        except FileExistsError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ExecutorExecutionError(
                "套装角度与编排入库表已存在，codex-dev 不会覆盖"
            ) from None
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ExecutorExecutionError("codex-dev 无法写入套装角度与编排入库表") from None
