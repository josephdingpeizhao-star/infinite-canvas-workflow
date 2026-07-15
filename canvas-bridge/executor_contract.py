"""Provider-neutral execution request and result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


class ExecutorExecutionError(RuntimeError):
    """A provider adapter could not complete an execution request."""


@dataclass(frozen=True)
class ImageGenerationTask:
    """A provider-neutral request to create one image artifact."""

    prompt: str
    output_path: Path
    reference_images: tuple[Path, ...] = ()
    size: str = "auto"
    quality: str = "auto"
    output_format: str = "png"


@dataclass(frozen=True)
class ExecutionRequest:
    """One workflow step request passed from orchestration to an executor."""

    step: str
    payload: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    """Provider-neutral execution outcome returned to orchestration."""

    detail: str
    outputs: tuple[Path, ...] = ()
    provider: str = ""
    model: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutorContext:
    """Construction-time data shared with an executor adapter."""

    manifest: Mapping[str, Any]
    manifest_path: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class Executor(Protocol):
    name: str

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute one provider-neutral request."""
