"""Explicit registry for replaceable executor adapters."""

from __future__ import annotations

from collections.abc import Callable

from executor_contract import Executor, ExecutorContext


ExecutorFactory = Callable[[ExecutorContext], Executor]


class ExecutorRegistryError(ValueError):
    """Base error for executor registration and lookup."""


class DuplicateExecutorError(ExecutorRegistryError):
    """Raised when an executor name is registered twice."""


class UnknownExecutorError(ExecutorRegistryError):
    """Raised when an executor name has no registered adapter."""


class InvalidExecutorError(ExecutorRegistryError):
    """Raised when a factory returns an object outside the executor protocol."""


class ExecutorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ExecutorFactory] = {}

    def register(self, name: str, factory: ExecutorFactory) -> None:
        if name in self._factories:
            raise DuplicateExecutorError(f"执行器已注册：{name}")
        self._factories[name] = factory

    def create(self, name: str, context: ExecutorContext) -> Executor:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "无"
            raise UnknownExecutorError(f"未注册的执行器：{name}；可用：{available}") from exc
        executor = factory(context)
        if not isinstance(executor, Executor):
            raise InvalidExecutorError(f"执行器工厂返回了无效对象：{name}")
        return executor

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
