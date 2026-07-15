"""Adapter for the existing throwaway demo workspace executor."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from executor_contract import ExecutionRequest, ExecutionResult, ExecutorExecutionError


BRIDGE_DIR = Path(__file__).resolve().parent
SUPPORTED_STEPS = frozenset(
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


class DemoWorkspaceExecutor:
    """Advance the protected demo workspace through the common contract."""

    name = "demo"

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        step = request.step
        if step not in SUPPORTED_STEPS:
            raise ExecutorExecutionError(f"executor 不认识步骤：{step}")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_DIR / "make_demo_workspace.py"),
                    "--advance",
                    step,
                    "--root",
                    str(self.workspace_root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutorExecutionError(f"步骤 {step} 执行超时（120s）") from exc
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            raise ExecutorExecutionError(tail[-1] if tail else f"exit {result.returncode}")
        detail = (result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else "ok"
        return ExecutionResult(detail=detail, provider=self.name)
