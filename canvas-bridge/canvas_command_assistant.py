"""M3-b natural-language intent router that drafts, but never executes, commands."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from codex_dev_executor import CanvasAgentCodexTransport


REAL_EXECUTION_SWITCH = "CODEX_DEV_ALLOW_REAL_EXECUTION"
MAX_UTTERANCE_BYTES = 2 * 1024
MAX_ACTIVE_SECONDS = 300.0
MAX_RETAINED_JOBS = 32
COMMAND_STEPS = (
    "identity",
    "style_master",
    "angle_inventory",
    "main_vc",
    "detail_vc",
    "final_prompts",
    "integrity",
    "renders",
    "qc",
)
CLOSED_COMMANDS = frozenset(
    ("run: next",)
    + tuple(f"run: {step}" for step in COMMAND_STEPS)
    + tuple(f"retry: {step}" for step in COMMAND_STEPS)
)
WORKING_MESSAGE = "助手正在辨认你要执行的步骤…"
COMPLETED_MESSAGE = "命令草稿已准备好。"
TIMEOUT_MESSAGE = "指令辨认超时，已停止；没有发出命令、没有产生费用。"
FAILED_MESSAGE = (
    "我没能安全确定你要做哪一步，没有生成草稿。"
    "请换一种说法或使用画布上的对应按钮。"
)
UNSUPPORTED_MESSAGE = (
    "这个我还不会，请用画布上的对应按钮操作。"
    "没有执行任何命令，也没有产生费用。"
)

STEP_COPY = {
    "next": (
        "继续下一步",
        "让机器按当前状态选择下一项允许的工作。",
    ),
    "identity": (
        "产品身份档案",
        "核对产品身份，并分清已确认事实、可见推断和未知项。",
    ),
    "style_master": (
        "风格母版",
        "提取整批图片要遵循的画面风格，不制作图片。",
    ),
    "angle_inventory": (
        "角度槽位盘点",
        "检查哪些产品角度真实可用，并登记对应槽位。",
    ),
    "main_vc": (
        "主图变量配置",
        "安排 6 张主图分别要表达的内容和拍摄条件。",
    ),
    "detail_vc": (
        "详情图变量配置",
        "安排 8 张详情图分别要表达的内容和拍摄条件。",
    ),
    "final_prompts": (
        "最终制作说明",
        "整理 14 张图片各自的最终制作说明。",
    ),
    "integrity": (
        "出图前完整性检查",
        "在制作图片前检查说明是否完整、合规且没有冲突。",
    ),
    "renders": (
        "制作图片",
        "按既有配置制作或重新制作图片。",
    ),
    "qc": (
        "成图质检",
        "逐张检查 14 张成图的质量。",
    ),
}

_GENERIC_NEXT = frozenset(
    {
        "开始",
        "继续",
        "下一步",
        "继续下一步",
        "开始下一步",
        "开始做图",
    }
)
_STEP_ALIASES = {
    "identity": (
        "产品识别",
        "产品身份",
        "身份档案",
        "产品身份档案",
    ),
    "style_master": (
        "提取风格",
        "风格母版",
        "定风格",
    ),
    "angle_inventory": (
        "检查角度",
        "角度盘点",
        "角度槽位",
        "角度槽位盘点",
    ),
    "main_vc": (
        "主图配置",
        "主图变量配置",
    ),
    "detail_vc": (
        "详情配置",
        "详情图配置",
        "详情图变量配置",
    ),
    "final_prompts": (
        "最终提示词",
        "整理最终提示词",
        "制作说明",
        "整理制作说明",
        "最终制作说明",
    ),
    "integrity": (
        "完整性检查",
        "出图前检查",
        "出图前完整性检查",
    ),
    "renders": (
        "生成图片",
        "制作图片",
        "出图",
        "做图",
        "渲染",
    ),
    "qc": (
        "质检",
        "成图质检",
        "质量检查",
        "检查质量",
        "质量",
    ),
}
_RETRY_PREFIXES = (
    "重新跑",
    "再来一次",
    "再做一次",
    "再查一遍",
    "重跑",
    "重做",
)
_RUN_PREFIXES = (
    "开始",
    "执行",
    "进行",
    "生成",
    "整理",
    "检查",
    "制作",
    "做",
    "跑",
)
_EXCLUDED_PATTERNS = (
    re.compile(r"建(?:个)?批次|创建批次|新建批次|建批"),
    re.compile(r"风格补登|补登风格|补登.*参考图"),
    re.compile(r"收货|关账|交付|上桌"),
    re.compile(r"repair|单图.*(?:重做|返修)|返修.*单图", re.IGNORECASE),
    re.compile(r"comfyui", re.IGNORECASE),
    re.compile(r"拖图|拖.*图片|连线|连接.*图片"),
)
_QUESTION_PATTERNS = (
    re.compile(r"什么|为何|为什么|怎么|怎样|多少|哪里|哪个|是否|能否|有没有"),
    re.compile(r"状态|进度|结果|还缺|完成了|问题|失败原因|怎么样"),
)
_LEADING_POLITE = ("麻烦你", "麻烦", "请你", "请", "帮我")
_TRAILING_POLITE = ("好吗", "可以吗", "一下", "吧", "谢谢")


class CommandAssistantError(RuntimeError):
    http_status = 400
    error_code = "command_assistant_rejected"


class CommandIntentRejected(CommandAssistantError):
    """An intent did not match the exact closed contract."""


class CommandAssistantBusy(CommandAssistantError):
    http_status = 409
    error_code = "command_assistant_busy"


class CommandAssistantRealExecutionDisabled(CommandAssistantError):
    http_status = 403
    error_code = "command_assistant_not_allowed"


class CommandDraftNotFound(CommandAssistantError):
    http_status = 404
    error_code = "command_draft_not_found"


@dataclass(frozen=True)
class ResolvedIntent:
    kind: str
    command: str = ""
    verb: str = ""
    target: str = ""
    message: str = ""


@dataclass
class _IntentJob:
    request_id: str
    utterance: str
    status: str
    message: str
    started_at: int
    updated_at: int
    deadline_at: int
    intent: ResolvedIntent | None = None
    timer: threading.Timer | None = None


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _utterance_text(value: Any) -> str:
    if not isinstance(value, str):
        raise CommandIntentRejected("指令内容必须是文字")
    text = value.strip()
    if not text:
        raise CommandIntentRejected("请先说出想让机器做什么")
    if "\x00" in text or _utf8_size(text) > MAX_UTTERANCE_BYTES:
        raise CommandIntentRejected("指令内容过长或含有无效字符")
    return text


def _normalize_utterance(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = re.sub(r"\s+", "", text)
    text = text.strip("，。；、！？!?.,;：:")
    changed = True
    while changed:
        changed = False
        for prefix in _LEADING_POLITE:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix) :]
                changed = True
                break
        for suffix in _TRAILING_POLITE:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text.strip("，。；、！？!?.,;：:")


def _target_for_alias(value: str) -> str:
    for target, aliases in _STEP_ALIASES.items():
        if value in aliases:
            return target
    return ""


def validate_closed_command(verb: Any, target: Any) -> str:
    if type(verb) is not str or type(target) is not str:
        raise CommandIntentRejected("命令意图必须是文字")
    command = f"{verb}: {target}"
    if command not in CLOSED_COMMANDS:
        raise CommandIntentRejected("命令没有命中允许的封闭词汇表")
    return command


def _command_intent(verb: str, target: str) -> ResolvedIntent:
    return ResolvedIntent(
        kind="command",
        command=validate_closed_command(verb, target),
        verb=verb,
        target=target,
    )


def _unsupported_message(normalized: str) -> str:
    if re.search(r"建(?:个)?批次|创建批次|新建批次|建批", normalized):
        return "这个我还不会，请使用画布下方的“信息卡”完成批次登记。没有执行任何命令，也没有产生费用。"
    if re.search(r"风格补登|补登风格|补登.*参考图", normalized):
        return "这个我还不会，请把风格参考图连到信息卡并使用现有补登按钮。没有执行任何命令，也没有产生费用。"
    if re.search(r"收货|关账|交付|上桌", normalized):
        return "这个我还不会，请使用工作流机器现有的上桌、收货或关账入口。没有执行任何命令，也没有产生费用。"
    return UNSUPPORTED_MESSAGE


def resolve_rule_intent(utterance: Any) -> ResolvedIntent | None:
    normalized = _normalize_utterance(_utterance_text(utterance))
    if normalized in _GENERIC_NEXT:
        return _command_intent("run", "next")
    for pattern in _EXCLUDED_PATTERNS:
        if pattern.search(normalized):
            return ResolvedIntent(
                kind="unsupported",
                message=_unsupported_message(normalized),
            )
    for prefix in _RETRY_PREFIXES:
        if normalized.startswith(prefix):
            target = _target_for_alias(normalized[len(prefix) :])
            if target:
                return _command_intent("retry", target)
    for prefix in _RUN_PREFIXES:
        if normalized.startswith(prefix):
            target = _target_for_alias(normalized[len(prefix) :])
            if target:
                return _command_intent("run", target)
    direct_target = _target_for_alias(normalized)
    if direct_target:
        return _command_intent("run", direct_target)
    if any(pattern.search(normalized) for pattern in _QUESTION_PATTERNS):
        return ResolvedIntent(kind="question")
    return None


def parse_model_intent(value: Any) -> ResolvedIntent:
    if not isinstance(value, str) or not value.strip():
        raise CommandIntentRejected("模型没有返回结构化意图")
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise CommandIntentRejected("模型没有返回单一 JSON 对象") from None
    if type(payload) is not dict or type(payload.get("intent")) is not str:
        raise CommandIntentRejected("模型意图结构无效")
    intent = payload["intent"]
    if intent == "command":
        if set(payload) != {"intent", "verb", "target"}:
            raise CommandIntentRejected("命令意图含有额外字段")
        return _command_intent(payload.get("verb"), payload.get("target"))
    if intent == "question" and set(payload) == {"intent"}:
        return ResolvedIntent(kind="question")
    if intent == "unsupported" and set(payload) == {"intent"}:
        return ResolvedIntent(kind="unsupported", message=UNSUPPORTED_MESSAGE)
    raise CommandIntentRejected("模型意图没有命中允许的结构")


def build_command_intent_prompt(utterance: str) -> str:
    utterance_json = json.dumps(_utterance_text(utterance), ensure_ascii=False)
    step_list = ", ".join(COMMAND_STEPS)
    return (
        "你只做一次中文话术意图分类，不执行工具、不读取文件、不解释。"
        "仅返回一个 JSON 对象，不能有 Markdown、前后说明或额外字段。\n"
        "允许三种输出：\n"
        '1. {"intent":"command","verb":"run","target":"next"}\n'
        '2. {"intent":"command","verb":"run","target":"步骤"} 或 '
        '{"intent":"command","verb":"retry","target":"步骤"}\n'
        '3. {"intent":"question"}\n'
        '4. {"intent":"unsupported"}\n'
        f"步骤只能是：{step_list}。retry 不能使用 next。"
        "查询批次状态、进度、问题或结果归为 question。"
        "建批、风格补登、收货、关账、交付、单图返修、ComfyUI、拖图连线归为 unsupported。"
        "只有用户明确要求机器行动时才归为 command；不确定时返回 unsupported。\n"
        f"用户原话（JSON 字符串）：{utterance_json}"
    )


def _draft(intent: ResolvedIntent) -> dict[str, str]:
    if intent.kind != "command" or not intent.command:
        raise CommandIntentRejected("只有封闭命令可以形成草稿")
    title, description = STEP_COPY[intent.target]
    if intent.verb == "retry":
        title = f"重新执行{title}"
        description = f"重新{description}"
    return {
        "command": intent.command,
        "verb": intent.verb,
        "target": intent.target,
        "title": title,
        "description": description,
    }


class CanvasCommandAssistant:
    """Resolve one command-like utterance at a time without touching the canvas."""

    def __init__(
        self,
        repository_root: Path,
        *,
        transport: Any | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = MAX_ACTIVE_SECONDS,
        wall_clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_ACTIVE_SECONDS:
            raise ValueError("指令辨认超时必须在 0 到 300 秒之间")
        self.repository_root = repository_root.resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.environment = environment if environment is not None else os.environ
        self.transport = transport or CanvasAgentCodexTransport(
            timeout=self.timeout_seconds
        )
        self._jobs: dict[str, _IntentJob] = {}
        self._lock = threading.Lock()
        self._transport_busy = False
        self._active_request_id = ""

    def _real_execution_allowed(self) -> bool:
        return self.environment.get(REAL_EXECUTION_SWITCH) == "1"

    def _clean_jobs_locked(self) -> None:
        if len(self._jobs) < MAX_RETAINED_JOBS:
            return
        for request_id in tuple(self._jobs):
            if len(self._jobs) < MAX_RETAINED_JOBS:
                break
            if request_id != self._active_request_id:
                self._jobs.pop(request_id, None)

    def _new_job(
        self,
        utterance: str,
        *,
        status: str,
        message: str,
        intent: ResolvedIntent | None = None,
    ) -> _IntentJob:
        now = self.wall_clock_ms()
        return _IntentJob(
            request_id=self.id_factory(),
            utterance=utterance,
            status=status,
            message=message,
            started_at=now,
            updated_at=now,
            deadline_at=now + int(self.timeout_seconds * 1000),
            intent=intent,
        )

    @staticmethod
    def _snapshot(job: _IntentJob) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "requestId": job.request_id,
            "status": job.status,
            "message": job.message,
            "startedAt": job.started_at,
            "updatedAt": job.updated_at,
            "deadlineAt": job.deadline_at,
        }
        if job.status == "completed" and job.intent is not None:
            result["intent"] = job.intent.kind
            if job.intent.kind == "command":
                result["draft"] = _draft(job.intent)
            elif job.intent.kind == "question":
                result["route"] = "readonly"
        return result

    def submit(self, utterance: Any) -> dict[str, Any]:
        normalized = _utterance_text(utterance)
        rule_intent = resolve_rule_intent(normalized)
        if rule_intent is not None:
            message = (
                COMPLETED_MESSAGE
                if rule_intent.kind == "command"
                else rule_intent.message or "已识别为批次问题。"
            )
            with self._lock:
                self._clean_jobs_locked()
                job = self._new_job(
                    normalized,
                    status="completed",
                    message=message,
                    intent=rule_intent,
                )
                self._jobs[job.request_id] = job
                return self._snapshot(job)
        if not self._real_execution_allowed():
            raise CommandAssistantRealExecutionDisabled(
                "指令助手尚未获准使用本机 codex-dev 进行模糊话术辨认。"
            )
        with self._lock:
            if self._transport_busy:
                raise CommandAssistantBusy(
                    "上一条指令仍在辨认或安全收尾，请稍后再试；本次没有排队。"
                )
            self._clean_jobs_locked()
            job = self._new_job(
                normalized,
                status="working",
                message=WORKING_MESSAGE,
            )
            self._jobs[job.request_id] = job
            self._transport_busy = True
            self._active_request_id = job.request_id
            timer = threading.Timer(
                self.timeout_seconds,
                self._timeout_job,
                args=(job.request_id,),
            )
            timer.daemon = True
            job.timer = timer
            timer.start()
            worker = threading.Thread(
                target=self._run_job,
                args=(job.request_id,),
                name="canvas-command-assistant",
                daemon=True,
            )
            worker.start()
            return self._snapshot(job)

    def _timeout_job(self, request_id: str) -> None:
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job.status != "working":
                return
            job.status = "failed"
            job.message = TIMEOUT_MESSAGE
            job.updated_at = self.wall_clock_ms()

    def _run_job(self, request_id: str) -> None:
        intent: ResolvedIntent | None = None
        failed_message = ""
        try:
            with self._lock:
                job = self._jobs[request_id]
                utterance = job.utterance
            prompt = build_command_intent_prompt(utterance)
            with self._lock:
                if self._jobs[request_id].status != "working":
                    return
            if not self._real_execution_allowed():
                raise CommandAssistantRealExecutionDisabled(
                    "指令助手尚未获准使用本机 codex-dev。"
                )
            turn = self.transport.run_turn(prompt, ())
            intent = parse_model_intent(str(turn.text or "").strip())
        except CommandAssistantRealExecutionDisabled:
            failed_message = "指令助手尚未获准使用本机 codex-dev。"
        except CommandAssistantError:
            failed_message = FAILED_MESSAGE
        except Exception:
            failed_message = FAILED_MESSAGE
        finally:
            with self._lock:
                job = self._jobs.get(request_id)
                if job is not None:
                    if job.timer is not None:
                        job.timer.cancel()
                    if job.status == "working":
                        if intent is not None:
                            job.status = "completed"
                            job.intent = intent
                            job.message = (
                                COMPLETED_MESSAGE
                                if intent.kind == "command"
                                else intent.message or "已识别为批次问题。"
                            )
                        else:
                            job.status = "failed"
                            job.message = failed_message or FAILED_MESSAGE
                        job.updated_at = self.wall_clock_ms()
                self._transport_busy = False
                if self._active_request_id == request_id:
                    self._active_request_id = ""

    def status(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id:
            raise CommandDraftNotFound("命令草稿编号不存在")
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None:
                raise CommandDraftNotFound("命令草稿编号不存在")
            if job.status == "working":
                job.updated_at = self.wall_clock_ms()
            return self._snapshot(job)
