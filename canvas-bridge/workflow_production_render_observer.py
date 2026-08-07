"""Validate registered render outputs and stream each valid PNG."""

from __future__ import annotations

from typing import Callable

from executor_contract import ExecutionRequest, ExecutionResult, Executor, ExecutorExecutionError
from workflow_production_projection import WorkflowProductionArtifact, artifact_from_path


class ProductionRenderObserverExecutor:
    """Wrap one provider executor and notify after each accepted disk file."""

    name = "workflow-production-observer"

    def __init__(
        self,
        delegate: Executor,
        *,
        batch_id: str,
        on_output: Callable[[WorkflowProductionArtifact], None],
        expected_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.delegate = delegate
        self.batch_id = batch_id
        self.expected_ids = expected_ids
        self.on_output = on_output

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        result = self.delegate.execute(request)
        for path in result.outputs:
            if self.expected_ids is not None and path.stem not in self.expected_ids:
                raise ExecutorExecutionError("渲染结果不在当前批次登记图位中。")
            artifact = artifact_from_path(self.batch_id, path)
            self.on_output(artifact)
        return result
