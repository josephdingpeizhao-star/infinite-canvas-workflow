"""Composition root for concrete executor adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from codex_dev_executor import CodexDevExecutor
from demo_executor import DemoWorkspaceExecutor
from executor_contract import Executor, ExecutorContext
from executor_registry import ExecutorRegistry
from openai_image_executor import OpenAIImageExecutor


def _demo_factory(context: ExecutorContext) -> DemoWorkspaceExecutor:
    workspace = context.manifest.get("workspace") or {}
    root = workspace.get("root")
    if not root:
        raise ValueError("manifest.workspace.root 缺失，demo 执行器无法定位工作区")
    return DemoWorkspaceExecutor(Path(str(root)))


def build_registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register("codex-dev", CodexDevExecutor)
    registry.register("demo", _demo_factory)
    registry.register("openai-image", OpenAIImageExecutor)
    return registry


def build_executor(
    name: str,
    manifest: Mapping[str, Any],
    manifest_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Executor:
    context = ExecutorContext(
        manifest=manifest,
        manifest_path=manifest_path,
        environment=os.environ if environment is None else environment,
    )
    return build_registry().create(name, context)
