"""Minimal canvas-agent HTTP client (stdlib only).

Reads the agent endpoint and token from ``~/.infinite-canvas/canvas-agent.json``
(written by canvas-agent on first start) and calls ``POST /api/tools``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".infinite-canvas" / "canvas-agent.json"


class CanvasAgentError(RuntimeError):
    pass


def load_agent_config(path: Path | None = None) -> dict[str, str]:
    target = path or DEFAULT_CONFIG_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanvasAgentError(f"canvas-agent config not found: {target}. Start canvas-agent first.") from exc
    url = str(data.get("url") or "").rstrip("/")
    token = str(data.get("token") or "")
    if not url or not token:
        raise CanvasAgentError(f"canvas-agent config missing url/token: {target}")
    return {"url": url, "token": token}


def _request(method: str, url: str, token: str, payload: Any | None = None, timeout: float = 60.0) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("x-canvas-agent-token", token)
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CanvasAgentError(f"HTTP {exc.code} from {url}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise CanvasAgentError(f"cannot reach canvas-agent at {url}: {exc.reason}") from exc


def health(config: dict[str, str] | None = None) -> dict[str, Any]:
    config = config or load_agent_config()
    return _request("GET", f"{config['url']}/health", config["token"])


def call_tool(name: str, input_payload: dict[str, Any] | None = None, *, config: dict[str, str] | None = None, timeout: float = 60.0) -> Any:
    config = config or load_agent_config()
    body = {"name": name, "input": input_payload or {}}
    result = _request("POST", f"{config['url']}/api/tools", config["token"], body, timeout=timeout)
    if not isinstance(result, dict) or not result.get("ok"):
        raise CanvasAgentError(f"tool {name} failed: {json.dumps(result, ensure_ascii=False)[:500]}")
    return result.get("result")


def apply_ops(ops: list[dict[str, Any]], *, config: dict[str, str] | None = None, chunk_size: int = 120, delay_seconds: float = 0.2) -> int:
    """Send ops in chunks; returns number of chunks sent."""
    config = config or load_agent_config()
    chunks = 0
    for start in range(0, len(ops), chunk_size):
        call_tool("canvas_apply_ops", {"ops": ops[start : start + chunk_size]}, config=config)
        chunks += 1
        if start + chunk_size < len(ops):
            time.sleep(delay_seconds)
    return chunks
