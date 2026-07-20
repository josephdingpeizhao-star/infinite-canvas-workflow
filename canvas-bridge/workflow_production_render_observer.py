"""Observe each real render without changing image-production internals."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from executor_contract import ExecutionRequest, ExecutionResult, Executor, ExecutorExecutionError
from workflow_production_projection import WorkflowProductionArtifact, artifact_from_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_original(source: Path, audit_root: Path) -> Path:
    target = audit_root / "render_originals" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    try:
        with target.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if _sha256(target) != hashlib.sha256(data).hexdigest():
            raise ExecutorExecutionError("尺寸审计目录已有同名但不同内容的原图，已停止。") from None
    return target


def _validate_ratio(artifact: WorkflowProductionArtifact, audit_root: Path) -> None:
    width, height = artifact.width, artifact.height
    if artifact.kind == "main":
        if width != height:
            raise ExecutorExecutionError("主图不是正方形，已停止且不会自动重试。")
        return
    if width * 4 == height * 3:
        return
    if width * 3 == height * 2:
        _audit_original(artifact.path, audit_root)
        raise ExecutorExecutionError("详情图返回 2:3，供应端原图已审计保留，等待人工扩边批准。")
    raise ExecutorExecutionError("详情图比例既不是 3:4 也不是受控的 2:3，已停止。")


class ProductionRenderObserverExecutor:
    """Wrap one provider executor and notify after each accepted disk file."""

    name = "workflow-production-observer"

    def __init__(
        self,
        delegate: Executor,
        *,
        batch_id: str,
        audit_root: Path,
        on_output: Callable[[WorkflowProductionArtifact], None],
    ) -> None:
        self.delegate = delegate
        self.batch_id = batch_id
        self.audit_root = audit_root
        self.on_output = on_output

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        result = self.delegate.execute(request)
        for path in result.outputs:
            artifact = artifact_from_path(self.batch_id, path)
            _validate_ratio(artifact, self.audit_root)
            self.on_output(artifact)
        return result
