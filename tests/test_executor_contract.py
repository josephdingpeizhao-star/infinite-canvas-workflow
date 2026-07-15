from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ImageGenerationTask,
)
from executor_registry import (  # noqa: E402
    DuplicateExecutorError,
    ExecutorRegistry,
    InvalidExecutorError,
    UnknownExecutorError,
)


class RecordingExecutor:
    name = "recording"

    def __init__(self, context: ExecutorContext):
        self.context = context

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(detail=f"handled {request.step}", provider=self.name)


class ExecutorContractTest(unittest.TestCase):
    def test_request_and_result_are_immutable(self) -> None:
        request = ExecutionRequest(step="renders", payload={"value": 1})
        result = ExecutionResult(detail="ok")

        with self.assertRaises(FrozenInstanceError):
            request.step = "identity"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.detail = "changed"  # type: ignore[misc]

    def test_image_task_normalizes_paths_to_tuples(self) -> None:
        task = ImageGenerationTask(
            prompt="white background product image",
            output_path=Path("out/product.png"),
            reference_images=(Path("inputs/front.jpg"), Path("inputs/back.jpg")),
        )

        self.assertEqual("png", task.output_format)
        self.assertEqual(2, len(task.reference_images))

    def test_registry_creates_executor_from_context(self) -> None:
        registry = ExecutorRegistry()
        registry.register("recording", RecordingExecutor)
        context = ExecutorContext(manifest={"product_id": "p1"}, manifest_path=Path("p1.json"))

        executor = registry.create("recording", context)
        result = executor.execute(ExecutionRequest(step="identity"))

        self.assertIs(context, executor.context)
        self.assertEqual("handled identity", result.detail)
        self.assertEqual(("recording",), registry.names())

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = ExecutorRegistry()
        registry.register("recording", RecordingExecutor)

        with self.assertRaises(DuplicateExecutorError):
            registry.register("recording", RecordingExecutor)

    def test_unknown_executor_is_rejected_with_available_names(self) -> None:
        registry = ExecutorRegistry()
        registry.register("recording", RecordingExecutor)

        with self.assertRaises(UnknownExecutorError) as ctx:
            registry.create("missing", ExecutorContext(manifest={}))

        self.assertIn("missing", str(ctx.exception))
        self.assertIn("recording", str(ctx.exception))

    def test_factory_must_return_executor_protocol(self) -> None:
        registry = ExecutorRegistry()
        registry.register("broken", lambda _context: object())  # type: ignore[arg-type,return-value]

        with self.assertRaises(InvalidExecutorError):
            registry.create("broken", ExecutorContext(manifest={}))


if __name__ == "__main__":
    unittest.main()
