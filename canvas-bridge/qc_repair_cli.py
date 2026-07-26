"""Offline-testable CLI entry for one gated QC repair run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Callable, Mapping, TextIO

from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
import run_controller
import state_reader
from executor_contract import Executor, ExecutorContext, ExecutorExecutionError
from qc_repair import prepare_repair_plan
from qc_repair_executor import QcRepairExecutor


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行一次受门禁保护的 QC 单图返修")
    parser.add_argument("--batch-manifest", type=Path, required=True)
    parser.add_argument("--command", required=True)
    return parser


def run_cli(
    argv: list[str],
    *,
    environment: Mapping[str, str] | None = None,
    repo_reports_dir: Path | None = None,
    route_reader: Callable[[Path], dict] | None = None,
    image_executor_factory: Callable[[ExecutorContext], Executor] | None = None,
    output: TextIO | None = None,
    batch_lock_root: Path | None = None,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if environment is None else environment
    route_reader = route_reader or state_reader.read_batch_route
    output = output or sys.stdout
    try:
        manifest = json.loads(args.batch_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        output.write("返修门禁未通过：批次清单无效。\n")
        return 1
    if not isinstance(manifest, dict):
        output.write("返修门禁未通过：批次清单无效。\n")
        return 1
    product_id = str(manifest.get("product_id") or "")
    if not product_id:
        output.write("返修门禁未通过：批次清单无效。\n")
        return 1
    journal = run_controller.journal_path(args.batch_manifest, product_id)
    try:
        with existing_batch_operation(
            product_id,
            lock_root=batch_lock_root,
        ):
            try:
                lifecycle = read_batch_lifecycle(journal)
            except BatchLifecycleReadError:
                output.write("返修门禁未通过：批次账本暂时无法读取。\n")
                return 1
            if lifecycle.recycled:
                output.write("返修门禁未通过：批次已回收，未写入任何事件。\n")
                return 1
            if lifecycle.closed:
                output.write("返修门禁未通过：批次已关账，未写入任何事件。\n")
                return 1
            return _run_active_repair(
                args,
                manifest,
                journal,
                environment=environment,
                repo_reports_dir=repo_reports_dir,
                route_reader=route_reader,
                image_executor_factory=image_executor_factory,
                output=output,
            )
    except BatchOperationBusy:
        output.write("返修门禁未通过：本批次有任务正在运行，未写入任何事件。\n")
        return 1


def _run_active_repair(
    args,
    manifest: dict,
    journal: Path,
    *,
    environment: Mapping[str, str],
    repo_reports_dir: Path | None,
    route_reader: Callable[[Path], dict],
    image_executor_factory: Callable[[ExecutorContext], Executor] | None,
    output: TextIO,
) -> int:
    request_id = uuid.uuid4().hex
    try:
        command = run_controller.parse_run_content(args.command)
        if command is None:
            raise run_controller.RunValidationError("缺少返修命令")
    except run_controller.RunValidationError:
        run_controller.append_event(
            journal,
            "gate_rejected",
            request_id=request_id,
            command="repair",
            detail="返修命令未通过解析门禁",
        )
        output.write("返修门禁未通过：命令无效。\n")
        return 1
    run_controller.append_event(
        journal,
        "command_received",
        request_id=request_id,
        command="repair",
    )
    prepared = prepare_repair_plan(
        manifest,
        args.batch_manifest,
        repo_reports_dir=repo_reports_dir or ROOT / "reports",
    )
    try:
        route = route_reader(args.batch_manifest)
        step = run_controller.resolve_repair_command(
            command,
            route,
            prepared.gate_facts(environment),
        )
    except (OSError, ValueError, run_controller.RunValidationError):
        run_controller.append_event(
            journal,
            "gate_rejected",
            request_id=request_id,
            command="repair",
            detail="返修条件未满足，未调用图片服务",
        )
        output.write("返修门禁未通过：未调用图片服务。\n")
        return 1
    if prepared.plan is None:
        run_controller.append_event(
            journal,
            "gate_rejected",
            request_id=request_id,
            command="repair",
            detail="返修计划无效，未调用图片服务",
        )
        output.write("返修门禁未通过：未调用图片服务。\n")
        return 1
    run_controller.append_event(
        journal,
        "step_started",
        request_id=request_id,
        step=step,
        target_count=prepared.target_count,
        work_order_count=len(prepared.plan.work_orders),
    )
    context = ExecutorContext(
        manifest=manifest,
        manifest_path=args.batch_manifest,
        environment=environment,
    )
    executor = QcRepairExecutor(
        context,
        plan=prepared.plan,
        journal_path=journal,
        request_id=request_id,
        image_executor_factory=image_executor_factory,
    )
    try:
        result = run_controller.execute_step(executor, step)
    except ExecutorExecutionError:
        run_controller.append_event(
            journal,
            "step_failed",
            request_id=request_id,
            step=step,
            detail="返修执行已停止，未自动重试",
        )
        output.write("返修执行已停止，未自动重试。\n")
        return 1
    output.write(result.detail + "\n")
    return 2 if result.metadata.get("status") == "completed_with_failures" else 0


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
