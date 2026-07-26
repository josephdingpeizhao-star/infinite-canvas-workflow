"""Command-line entry for one local, ledger-authoritative delivery package."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, TextIO

from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
import run_controller
from delivery import DeliveryRejected, package_delivery


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="打包一个已经正式关账的批次")
    parser.add_argument("--batch-manifest", type=Path, required=True)
    parser.add_argument("--command", required=True)
    return parser


def _append_rejection(journal: Path, request_id: str, code: str) -> None:
    try:
        run_controller.append_event(
            journal,
            "delivery_rejected",
            request_id=request_id,
            command="delivery",
            code=code,
        )
    except OSError:
        pass


def run_cli(
    argv: list[str],
    *,
    output: TextIO | None = None,
    request_id_factory: Callable[[], str] | None = None,
    packaged_at_factory: Callable[[], str] | None = None,
    batch_lock_root: Path | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = output or sys.stdout
    request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)
    packaged_at_factory = packaged_at_factory or (
        lambda: time.strftime("%Y-%m-%dT%H:%M:%S")
    )
    try:
        manifest = json.loads(args.batch_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        output.write("交付门禁未通过：批次清单无效。\n")
        return 1
    if not isinstance(manifest, dict):
        output.write("交付门禁未通过：批次清单无效。\n")
        return 1
    product_id = manifest.get("product_id")
    if not isinstance(product_id, str) or not product_id:
        output.write("交付门禁未通过：批次清单无效。\n")
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
                output.write("交付门禁未通过：批次账本无法读取。\n")
                return 1
            if lifecycle.recycled:
                output.write("交付门禁未通过：批次已回收，未写入任何事件。\n")
                return 1
            return _run_active_delivery(
                args,
                manifest,
                journal,
                output=output,
                request_id_factory=request_id_factory,
                packaged_at_factory=packaged_at_factory,
                batch_lock_root=batch_lock_root,
            )
    except BatchOperationBusy:
        output.write("交付门禁未通过：本批次有任务正在运行，未写入任何事件。\n")
        return 1


def _run_active_delivery(
    args,
    manifest: dict,
    journal: Path,
    *,
    output: TextIO,
    request_id_factory: Callable[[], str],
    packaged_at_factory: Callable[[], str],
    batch_lock_root: Path | None,
) -> int:
    request_id = request_id_factory()
    try:
        command = run_controller.parse_run_content(args.command)
        if command != ("run", "delivery"):
            raise run_controller.RunValidationError("交付入口只接受 run: delivery")
    except run_controller.RunValidationError:
        _append_rejection(journal, request_id, "invalid_command")
        output.write("交付门禁未通过：命令无效，只接受 run: delivery。\n")
        return 1
    try:
        result = package_delivery(
            manifest,
            args.batch_manifest,
            journal_path=journal,
            request_id=request_id,
            packaged_at=packaged_at_factory(),
            batch_lock_root=batch_lock_root,
        )
    except DeliveryRejected as exc:
        _append_rejection(journal, request_id, exc.code)
        output.write(str(exc) + "\n")
        return 1
    except Exception:
        _append_rejection(journal, request_id, "unexpected_failure")
        output.write("交付执行未完成；未自动重试，请人工核对。\n")
        return 1
    try:
        run_controller.append_event(
            journal,
            "delivery_packaged",
            request_id=request_id,
            acceptance_request_id=result.acceptance_request_id,
            selection_count=result.item_count,
            source_counts=result.source_counts,
            selection_sha256=result.selection_sha256,
            zip_sha256=result.zip_sha256,
            zip_byte_count=result.zip_byte_count,
            manifest_sha256=result.manifest_sha256,
            manifest_markdown_sha256=result.manifest_markdown_sha256,
            sidecar_sha256=result.sidecar_sha256,
        )
    except OSError:
        output.write("交付包已生成，但账本回执写入失败；请人工核对，勿重复运行。\n")
        return 1
    output.write("交付打包完成：14 张定稿、两份清单、ZIP 和校验文件均已生成。\n")
    return 0


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
