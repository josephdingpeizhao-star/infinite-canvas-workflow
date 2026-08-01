from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_RENDER_CREDENTIALS_PATH = Path("~/.infinite-canvas/render-credentials.json")


class RenderCredentialsError(RuntimeError):
    """A local render credential file that cannot be used safely."""


@dataclass(frozen=True)
class RenderCredentials:
    api_key: str = field(repr=False)
    base_url: str
    max_images_per_run: int | None = None


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        raise RenderCredentialsError("无法读取渲染凭据文件") from None

    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        raise RenderCredentialsError("渲染凭据文件不是有效的 JSON") from None
    if not isinstance(value, dict):
        raise RenderCredentialsError("渲染凭据文件必须是一个 JSON 对象")
    return value


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RenderCredentialsError("渲染凭据文件中的 base_url 不能为空")
    base_url = value.strip()
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise RenderCredentialsError(
            "渲染凭据文件中的 base_url 必须是有效的 http(s) 地址"
        ) from None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise RenderCredentialsError("渲染凭据文件中的 base_url 必须是有效的 http(s) 地址")
    return base_url


def _validate_max_images_per_run(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RenderCredentialsError("渲染凭据文件中的 max_images_per_run 必须是正整数或 null")
    return value


def load_render_credentials(path: Path) -> RenderCredentials | None:
    """Load and validate a local credential file without exposing its secret."""

    credential_path = Path(path).expanduser()
    try:
        value = _read_json_object(credential_path)
    except FileNotFoundError:
        return None

    api_key_value = value.get("api_key")
    if not isinstance(api_key_value, str) or not api_key_value.strip():
        raise RenderCredentialsError("渲染凭据文件中的 api_key 不能为空")

    return RenderCredentials(
        api_key=api_key_value.strip(),
        base_url=_validate_base_url(value.get("base_url")),
        max_images_per_run=_validate_max_images_per_run(value.get("max_images_per_run")),
    )
