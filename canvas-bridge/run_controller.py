"""Canvas-triggered execution (phase 4).

The projection gains two control nodes:

- ``wfrun:<pid>:batch`` — the run console. The user writes one command line
  (``run: next`` / ``run: <step>`` / ``retry: <step>``) into the node text;
  the ``--serve`` loop reads it back and executes through three gates:

  1. parse   — only the whitelist verbs ``run`` / ``retry`` are accepted;
  2. route   — the target step must be runnable (or retryable) according to
               the real ``route_batch()`` result, never a raw string match;
  3. execute — the step runs through a registered executor subprocess; the
               canvas never mutates workspace files directly.

- ``wflog:<pid>:events`` — a read-only tail of the append-only event journal
  (``<manifest_dir>/<pid>.events.jsonl``). The journal file, not the canvas,
  is the source of truth for execution history.

Steps are named after the workflow vocabulary. Concrete providers implement a
shared executor contract and are selected through a registry-backed composition
root without changing these gates.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from executor_contract import ExecutionRequest, ExecutionResult, Executor, ExecutorExecutionError

BRIDGE_DIR = Path(__file__).resolve().parent

RUN_PREFIX = "wfrun"
LOG_PREFIX = "wflog"

RUN_VERBS = ("run", "retry")

# step name -> graph node id (stage/gate the step lights up while executing)
STEP_GRAPH_NODES = {
    "identity": "stage_product_identity",
    "style_master": "stage_style_master",
    "angle_inventory": "stage_angle_inventory",
    "main_vc": "stage_main_variable_config",
    "detail_vc": "stage_detail_variable_config",
    "final_prompts": "stage_final_prompt_compile",
    "integrity": "gate_final_prompt_integrity",
    "renders": "stage_render",
    "qc": "stage_qc",
}

SKILL_TO_STEP = {
    "product-identity-archive": "identity",
    "style-master-extractor": "style_master",
    "angle-inventory": "angle_inventory",
    "main-variable-config": "main_vc",
    "detail-variable-config": "detail_vc",
    "final-prompt-compiler": "final_prompts",
    "qc-inspector": "qc",
}

# step -> artifact key in route.available_artifacts marking the step as done
STEP_DONE_ARTIFACTS = {
    "identity": "product_identity_archive",
    "style_master": "style_master",
    "angle_inventory": "angle_inventory",
    "main_vc": "main_variable_configs",
    "detail_vc": "detail_variable_configs",
    "final_prompts": "final_prompts",
    "qc": "qc_reports",
}


class RunValidationError(ValueError):
    """Command rejected by gate 1 (parse) or gate 2 (route check)."""


RunExecutionError = ExecutorExecutionError


def run_node_id(product_id: str) -> str:
    return f"{RUN_PREFIX}:{product_id}:batch"


def log_node_id(product_id: str) -> str:
    return f"{LOG_PREFIX}:{product_id}:events"


def journal_path(manifest_path: Path, product_id: str) -> Path:
    return manifest_path.parent / f"{product_id}.events.jsonl"


def append_event(path: Path, event: str, **fields: Any) -> dict[str, Any]:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_journal_tail(path: Path, limit: int = 8) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return events


def _renders_present(route: dict[str, Any]) -> bool:
    outputs = route.get("outputs") or {}
    total = 0
    for key in ("renders", "repaired"):
        summary = outputs.get(key) or {}
        count = summary.get("file_count")
        total += int(count) if isinstance(count, int) else 0
    return total > 0


def runnable_steps(route: dict[str, Any], integrity: dict[str, Any]) -> list[str]:
    """Steps allowed for ``run`` right now, derived from route_batch state."""
    stage = str(route.get("current_stage") or "")
    if stage == "needs_generated_images_before_qc":
        # Off the skill ladder: the integrity gate must pass before rendering.
        if not integrity.get("found"):
            return ["integrity"]
        if integrity.get("render_blocked") or integrity.get("status") == "fail":
            return ["integrity"]
        return ["renders"]
    if route.get("blocked_reasons"):
        return []
    skill = route.get("next_required_skill")
    step = SKILL_TO_STEP.get(str(skill)) if skill else None
    return [step] if step else []


def retryable_steps(route: dict[str, Any], integrity: dict[str, Any]) -> list[str]:
    """Steps already completed once (their done-marker exists)."""
    available = set(route.get("available_artifacts") or [])
    done = [step for step, key in STEP_DONE_ARTIFACTS.items() if key in available]
    if integrity.get("found"):
        done.append("integrity")
    if _renders_present(route):
        done.append("renders")
    return [step for step in STEP_GRAPH_NODES if step in set(done)]


def _split_key_value(line: str) -> tuple[str, str]:
    positions = [index for index in (line.find(":"), line.find("：")) if index != -1]
    if not positions:
        raise RunValidationError(f"无法解析的命令行（缺少冒号）：{line}")
    split_at = min(positions)
    return line[:split_at].strip().lower(), line[split_at + 1 :].strip()


def parse_run_content(text: str) -> tuple[str, str] | None:
    """Gate 1. Returns (verb, target) or None when no command is written."""
    command: tuple[str, str] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        verb, target = _split_key_value(line)
        if verb not in RUN_VERBS:
            raise RunValidationError(f"不允许的命令：{verb}；可用：run / retry")
        if command is not None:
            raise RunValidationError("一次只能写一条命令")
        if not target:
            raise RunValidationError(f"{verb} 缺少目标步骤（可写 next 或步骤名）")
        command = (verb, target)
    return command


def resolve_command(
    command: tuple[str, str],
    route: dict[str, Any],
    integrity: dict[str, Any],
) -> str:
    """Gate 2. Maps the parsed command onto a step the route actually allows."""
    verb, target = command
    runnable = runnable_steps(route, integrity)
    retryable = retryable_steps(route, integrity)

    if verb == "run":
        if target == "next":
            if not runnable:
                reasons = "；".join(str(item) for item in route.get("blocked_reasons") or [])
                raise RunValidationError(
                    f"当前没有可运行步骤（stage={route.get('current_stage')}）" + (f"：{reasons}" if reasons else "")
                )
            return runnable[0]
        if target not in STEP_GRAPH_NODES:
            raise RunValidationError(f"未知步骤：{target}；可用步骤：{', '.join(STEP_GRAPH_NODES)}")
        if target not in runnable:
            hint = f"当前可运行：{', '.join(runnable) or '无'}"
            if target in retryable:
                hint += f"；{target} 已完成，如需重做请用 retry: {target}"
            raise RunValidationError(f"步骤 {target} 现在不可运行；{hint}")
        return target

    # verb == "retry"
    if target == "next":
        raise RunValidationError("retry 需要指定具体步骤名")
    if target not in STEP_GRAPH_NODES:
        raise RunValidationError(f"未知步骤：{target}；可用步骤：{', '.join(STEP_GRAPH_NODES)}")
    if target not in retryable:
        raise RunValidationError(f"步骤 {target} 尚未完成过，不能 retry；可重试：{', '.join(retryable) or '无'}")
    return target


def render_run_content(route: dict[str, Any], integrity: dict[str, Any], note: str = "") -> str:
    runnable = runnable_steps(route, integrity)
    retryable = retryable_steps(route, integrity)
    lines = [
        "# 批次运行台：在本节点最后一行写一条命令，桥接 --serve 轮询执行",
        "# 可用命令：run: next ｜ run: <步骤> ｜ retry: <已完成步骤>",
        f"# 当前阶段：{route.get('current_stage')}",
        f"# 可运行：{', '.join(runnable) or '无'}",
        f"# 可重试：{', '.join(retryable) or '无'}",
    ]
    if note:
        lines.append(f"# 上次结果：{note}")
    return "\n".join(lines)


def run_node_op(
    product_id: str,
    route: dict[str, Any],
    integrity: dict[str, Any],
    *,
    note: str = "",
    status: str = "idle",
    x: int = 620,
    y: int = -180,
) -> dict[str, Any]:
    return {
        "type": "add_node",
        "id": run_node_id(product_id),
        "nodeType": "text",
        "title": "▶ 批次运行台（写命令）",
        "position": {"x": x, "y": y},
        "width": 520,
        "height": 170,
        "metadata": {
            "content": render_run_content(route, integrity, note),
            "fontSize": 13,
            "status": status,
            "workflowRef": {"controller": "batch_run", "product_id": product_id},
        },
    }


_EVENT_MARKS = {
    "command_received": "⏳",
    "step_started": "⏳",
    "step_succeeded": "✔",
    "step_failed": "✘",
    "gate_rejected": "🚫",
}


def render_log_content(events: list[dict[str, Any]]) -> str:
    if not events:
        return "（暂无事件）"
    lines = []
    for entry in reversed(events):  # newest first
        mark = _EVENT_MARKS.get(str(entry.get("event")), "·")
        ts = str(entry.get("ts") or "")[-8:]
        step = entry.get("step") or entry.get("command") or ""
        detail = str(entry.get("detail") or "")
        line = f"{ts} {mark} {entry.get('event')} {step}".rstrip()
        if detail:
            line += f" — {detail[:80]}"
        lines.append(line)
    return "\n".join(lines)


def log_node_op(product_id: str, events: list[dict[str, Any]], *, x: int = 1160, y: int = -180) -> dict[str, Any]:
    return {
        "type": "add_node",
        "id": log_node_id(product_id),
        "nodeType": "text",
        "title": "📜 执行日志（只读投影）",
        "position": {"x": x, "y": y},
        "width": 560,
        "height": 190,
        "metadata": {
            "content": render_log_content(events),
            "fontSize": 12,
            "status": "idle",
            "workflowRef": {"controller": "batch_events", "product_id": product_id},
        },
    }


def execute_step(executor: Executor, step: str, payload: Any = None) -> ExecutionResult:
    """Invoke an executor without exposing provider details to orchestration."""

    return executor.execute(ExecutionRequest(step=step, payload=payload))


def execute_step_with_metadata(
    executor: Executor,
    step: str,
    payload: Any = None,
    *,
    metadata: dict[str, Any] | None = None,
) -> ExecutionResult:
    """M1-b additive entry for a demo run's progress/cancellation callbacks.

    The original ``execute_step`` entry and every real-batch caller remain
    unchanged.  Metadata is provider-neutral and is used only by the separately
    registered ``workflow-demo`` adapter.
    """

    return executor.execute(ExecutionRequest(step=step, payload=payload, metadata=metadata or {}))
