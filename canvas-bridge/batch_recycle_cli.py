"""CLI for ledger-first batch recycle and restore."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, TextIO

from batch_recycle_service import (
    BatchRecycleError,
    BatchRecycleResult,
    BatchRecycleService,
)


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回收或还原一个已登记批次")
    subparsers = parser.add_subparsers(dest="action", required=True)
    recycle = subparsers.add_parser("recycle", help="冻结批次并把工作区移入回收站")
    recycle.add_argument("batch_id")
    restore = subparsers.add_parser("restore", help="把工作区搬回并解除回收状态")
    restore.add_argument("batch_id")
    return parser


def _result_payload(result: BatchRecycleResult) -> dict[str, object]:
    return {
        "ok": True,
        "batchId": result.batch_id,
        "status": result.status,
        "requestId": result.request_id,
        "deletedCanvasNodes": result.deleted_canvas_nodes,
        "resumed": result.resumed,
    }


def run_cli(
    argv: list[str],
    *,
    output: TextIO | None = None,
    service_factory: Callable[..., BatchRecycleService] = BatchRecycleService,
    repository_root: Path = ROOT,
    lock_root: Path | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = output or sys.stdout
    service = service_factory(
        repository_root,
        lock_root=lock_root,
    )
    try:
        if args.action == "recycle":
            result = service.recycle(args.batch_id, source_entry="cli")
        else:
            result = service.restore(args.batch_id)
    except BatchRecycleError as exc:
        output.write(
            json.dumps(
                {"ok": False, "code": exc.code, "message": str(exc)},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 1
    output.write(json.dumps(_result_payload(result), ensure_ascii=False) + "\n")
    return 0


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
