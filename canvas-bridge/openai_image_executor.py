"""GPT Image adapter behind the provider-neutral executor contract."""

from __future__ import annotations

import base64
from http import client as http_client
import ipaddress
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib import error, request
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from failure_text_safety import is_sensitive_identifier
from reference_image_compression import compress_reference_image
from white_bg_recovery import sanitize_filename


CLIENT_USER_AGENT = "Codex-Canvas-Bridge/1.0"
IMAGE_TIMEOUT_ENV = "OPENAI_IMAGE_TIMEOUT_SECONDS"
DEFAULT_IMAGE_TIMEOUT_SECONDS = 180.0
MIN_IMAGE_TIMEOUT_SECONDS = 30
MAX_IMAGE_TIMEOUT_SECONDS = 1800
REFERENCE_IMAGE_MAX_BYTES_ENV = "OPENAI_IMAGE_REFERENCE_MAX_BYTES"
DEFAULT_REFERENCE_IMAGE_MAX_BYTES = 2_000_000
MIN_REFERENCE_IMAGE_MAX_BYTES = 500_000
MAX_REFERENCE_IMAGE_MAX_BYTES = 20_000_000
IMAGE_DOWNLOAD_MAX_BYTES_ENV = "OPENAI_IMAGE_DOWNLOAD_MAX_BYTES"
DEFAULT_IMAGE_DOWNLOAD_MAX_BYTES = 20_000_000
MIN_IMAGE_DOWNLOAD_MAX_BYTES = 1_000_000
MAX_IMAGE_DOWNLOAD_MAX_BYTES = 100_000_000
MAX_IMAGE_DOWNLOAD_URL_LENGTH = 2048
_SAFE_UPSTREAM_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_UNSAFE_UPSTREAM_TOKEN_PATTERN = re.compile(
    r"(?:bearer|token|api[_-]?key|secret|sk-[A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_BODY_REQUEST_ID_PATTERN = re.compile(
    r"\brequest\s+id\s*:\s*([A-Za-z0-9_.-]{1,64})(?![A-Za-z0-9_.-])",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str = ""


def _safe_upstream_token(value: object) -> str:
    if type(value) is not str or _SAFE_UPSTREAM_TOKEN_PATTERN.fullmatch(value) is None:
        return ""
    if _UNSAFE_UPSTREAM_TOKEN_PATTERN.search(value):
        return ""
    return value


def _response_shape_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        sorted(
            {
                token
                for key in value
                if (token := _safe_upstream_token(key))
                and not is_sensitive_identifier(token)
            }
        )[:8]
    )


def _extract_response_shape(payload: object) -> dict[str, tuple[str, ...]]:
    """Return sanitized response key names without retaining provider values."""

    top_keys = _response_shape_keys(payload)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    data0 = data[0] if isinstance(data, list) and data else None
    return {
        "response_top_keys": top_keys,
        "response_data0_keys": _response_shape_keys(data0),
    }


def _response_header(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    return next(
        (
            value
            for key, value in headers.items()
            if type(key) is str and key.lower() == target and type(value) is str
        ),
        "",
    )


def _parse_retry_after_seconds(response: HttpResponse) -> int | None:
    if response.status != 429:
        return None
    raw = _response_header(response.headers, "retry-after").strip()
    if re.fullmatch(r"[0-9]+", raw) is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 600 else None


def _extract_upstream_failure(response: HttpResponse) -> dict[str, object]:
    """Extract only fixed-shape fields that are safe to surface to users."""

    http_status = response.status if type(response.status) is int and 100 <= response.status <= 599 else None
    error_value: Mapping[str, object] = {}
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error_value = payload["error"]

    provider_error_type = _safe_upstream_token(error_value.get("type"))
    provider_error_code = _safe_upstream_token(error_value.get("code"))
    provider_request_id = _safe_upstream_token(
        _response_header(response.headers, "x-request-id")
    )
    if not provider_request_id:
        message = error_value.get("message")
        if type(message) is str:
            match = _BODY_REQUEST_ID_PATTERN.search(message)
            if match is not None:
                provider_request_id = _safe_upstream_token(match.group(1))

    fields: dict[str, object] = {
        "http_status": http_status,
        "provider_error_type": provider_error_type,
        "provider_error_code": provider_error_code,
        "provider_request_id": provider_request_id,
    }
    retry_after_seconds = _parse_retry_after_seconds(response)
    if retry_after_seconds is not None:
        fields["retry_after_seconds"] = retry_after_seconds
    return fields


def _attach_render_failure(
    failure: ExecutorExecutionError,
    code: str,
    **fields: object,
) -> ExecutorExecutionError:
    failure.code = code
    for name, value in fields.items():
        setattr(failure, name, value)
    return failure


def _validate_image_download_url(url: object) -> str:
    """Validate one provider-supplied image URL without resolving its host."""

    if type(url) is not str or not url or len(url) > MAX_IMAGE_DOWNLOAD_URL_LENGTH:
        raise ValueError("unsafe_url")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError("unsafe_url")
    if url != url.strip():
        raise ValueError("unsafe_url")
    if "#" in url:
        raise ValueError("unsafe_url")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("unsafe_url") from exc
    if parts.scheme.lower() != "https" or not hostname or parts.fragment:
        raise ValueError("unsafe_url")
    if parts.username is not None or parts.password is not None:
        raise ValueError("unsafe_url")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("unsafe_url")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(hostname))
        except OSError:
            address = None

    if address is None:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("unsafe_url") from exc
        if len(ascii_hostname) > 253:
            raise ValueError("unsafe_url")
        labels = ascii_hostname.rstrip(".").split(".")
        if labels and all(
            re.fullmatch(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)", label) is not None
            for label in labels
        ):
            raise ValueError("unsafe_url")
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
            for label in labels
        ):
            raise ValueError("unsafe_url")
    else:
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_reserved,
                address.is_unspecified,
                address.is_multicast,
                getattr(address, "is_site_local", False),
                not address.is_global,
            )
        ):
            raise ValueError("unsafe_url")
    return url


class _ImageDownloadRedirectError(Exception):
    pass


class _ImageDownloadRedirectHandler(request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        try:
            _validate_image_download_url(newurl)
        except ValueError as exc:
            raise _ImageDownloadRedirectError from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):  # type: ignore[no-untyped-def]
        try:
            location = headers.get("location") or headers.get("uri")
            if not location:
                return None
            redirect_count = getattr(req, "_image_download_redirect_count", 0)
            if type(redirect_count) is not int or redirect_count >= self.max_redirections:
                raise _ImageDownloadRedirectError
            newurl = quote(
                str(location),
                encoding="iso-8859-1",
                safe="!$&'()*+,/:;=?@[]~%#",
            )
            newurl = urljoin(req.full_url, newurl)
            redirected = self.redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is None:
                return None
            redirected._image_download_redirect_count = redirect_count + 1
            redirected.timeout = req.timeout
            return self.parent.open(redirected, timeout=req.timeout)
        finally:
            fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class HttpTransport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        """Send one HTTP POST request."""

    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        """Download one generated image without generation credentials."""


class UrllibTransport:
    """Standard-library HTTP transport used by the production adapter."""

    def __init__(
        self,
        *,
        image_download_max_bytes: int = DEFAULT_IMAGE_DOWNLOAD_MAX_BYTES,
    ) -> None:
        self.image_download_max_bytes = image_download_max_bytes

    @staticmethod
    def _timeout_error(timeout: float, exc: BaseException) -> ExecutorExecutionError:
        timeout_label = f"{timeout:g}"
        timeout_seconds = (
            int(timeout)
            if isinstance(timeout, (int, float))
            and not isinstance(timeout, bool)
            and float(timeout).is_integer()
            and 1 <= int(timeout) <= 9_999
            else None
        )
        return _attach_render_failure(
            ExecutorExecutionError(
                f"图片服务连续 {timeout_label} 秒未返回新数据，已停止等待"
            ),
            "render_timeout",
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def _read_body(cls, response: object, timeout: float) -> bytes:
        try:
            return response.read()  # type: ignore[attr-defined]
        except (TimeoutError, socket.timeout) as exc:
            raise cls._timeout_error(timeout, exc) from exc

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        request_headers = dict(headers)
        if not any(name.lower() == "user-agent" for name in request_headers):
            request_headers["User-Agent"] = CLIENT_USER_AGENT
        outbound = request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with request.urlopen(outbound, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_body(response, timeout),
                )
        except error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()),
                body=self._read_body(exc, timeout),
            )
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_error(timeout, exc) from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise self._timeout_error(timeout, exc) from exc
            raise _attach_render_failure(
                ExecutorExecutionError(f"无法连接图片生成服务：{exc.reason}"),
                "render_network_error",
            ) from exc

    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        user_agent = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == "user-agent" and type(value) is str
            ),
            CLIENT_USER_AGENT,
        )
        outbound = request.Request(
            url,
            headers={"User-Agent": user_agent},
            method="GET",
        )
        opener = request.build_opener(
            request.ProxyHandler({}),
            _ImageDownloadRedirectHandler(),
        )
        try:
            with opener.open(outbound, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_download_body(response, timeout),
                    final_url=str(response.geturl()),
                )
        except error.HTTPError as exc:
            with exc:
                return HttpResponse(
                    status=int(exc.code),
                    headers=dict(exc.headers.items()),
                    body=self._read_download_body(exc, timeout),
                    final_url=str(exc.geturl()),
                )
        except _ImageDownloadRedirectError as exc:
            raise _attach_render_failure(
                ExecutorExecutionError("图片下载地址未通过安全校验"),
                "render_image_download_failed",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_error(timeout, exc) from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise self._timeout_error(timeout, exc) from exc
            raise _attach_render_failure(
                ExecutorExecutionError("无法连接图片下载服务"),
                "render_network_error",
            ) from exc
        except (
            ConnectionError,
            http_client.RemoteDisconnected,
            http_client.IncompleteRead,
            OSError,
        ) as exc:
            raise _attach_render_failure(
                ExecutorExecutionError("无法连接图片下载服务"),
                "render_network_error",
            ) from exc

    def _read_download_body(self, response: object, timeout: float) -> bytes:
        try:
            return response.read(self.image_download_max_bytes + 1)  # type: ignore[attr-defined]
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_error(timeout, exc) from exc
        except (
            ConnectionError,
            http_client.RemoteDisconnected,
            http_client.IncompleteRead,
            OSError,
        ) as exc:
            raise _attach_render_failure(
                ExecutorExecutionError("无法连接图片下载服务"),
                "render_network_error",
            ) from exc


class OpenAIImageExecutor:
    """Generate or edit one image through the OpenAI Image API."""

    name = "openai-image"

    def __init__(
        self,
        context: ExecutorContext,
        *,
        transport: HttpTransport | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        boundary_factory: Callable[[], str] | None = None,
    ) -> None:
        self.environment = context.environment
        configured_base_url = (
            base_url or self.environment.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        base_parts = urlsplit(configured_base_url)
        if not base_parts.path:
            configured_base_url = urlunsplit(base_parts._replace(path="/v1"))
        self.base_url = configured_base_url
        self.model = model or self.environment.get("OPENAI_IMAGE_MODEL") or "gpt-image-2"
        self.timeout = float(timeout) if timeout is not None else self._environment_timeout()
        self.reference_image_max_bytes = self._environment_reference_image_max_bytes()
        self.image_download_max_bytes = self._environment_image_download_max_bytes()
        self.transport = transport or UrllibTransport(
            image_download_max_bytes=self.image_download_max_bytes
        )
        self.boundary_factory = boundary_factory or (lambda: f"executor-{uuid.uuid4().hex}")

    def _environment_timeout(self) -> float:
        if IMAGE_TIMEOUT_ENV not in self.environment:
            return DEFAULT_IMAGE_TIMEOUT_SECONDS
        raw = str(self.environment.get(IMAGE_TIMEOUT_ENV) or "").strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ExecutorExecutionError(
                f"{IMAGE_TIMEOUT_ENV} 必须是 {MIN_IMAGE_TIMEOUT_SECONDS} 到 "
                f"{MAX_IMAGE_TIMEOUT_SECONDS} 的整数"
            ) from exc
        if (
            str(value) != raw
            or value < MIN_IMAGE_TIMEOUT_SECONDS
            or value > MAX_IMAGE_TIMEOUT_SECONDS
        ):
            raise ExecutorExecutionError(
                f"{IMAGE_TIMEOUT_ENV} 必须是 {MIN_IMAGE_TIMEOUT_SECONDS} 到 "
                f"{MAX_IMAGE_TIMEOUT_SECONDS} 的整数"
            )
        return float(value)

    def _environment_reference_image_max_bytes(self) -> int:
        if REFERENCE_IMAGE_MAX_BYTES_ENV not in self.environment:
            return DEFAULT_REFERENCE_IMAGE_MAX_BYTES
        raw = str(self.environment.get(REFERENCE_IMAGE_MAX_BYTES_ENV) or "").strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ExecutorExecutionError(
                f"{REFERENCE_IMAGE_MAX_BYTES_ENV} 必须是 {MIN_REFERENCE_IMAGE_MAX_BYTES} 到 "
                f"{MAX_REFERENCE_IMAGE_MAX_BYTES} 的整数"
            ) from exc
        if (
            str(value) != raw
            or value < MIN_REFERENCE_IMAGE_MAX_BYTES
            or value > MAX_REFERENCE_IMAGE_MAX_BYTES
        ):
            raise ExecutorExecutionError(
                f"{REFERENCE_IMAGE_MAX_BYTES_ENV} 必须是 {MIN_REFERENCE_IMAGE_MAX_BYTES} 到 "
                f"{MAX_REFERENCE_IMAGE_MAX_BYTES} 的整数"
            )
        return value

    def _environment_image_download_max_bytes(self) -> int:
        if IMAGE_DOWNLOAD_MAX_BYTES_ENV not in self.environment:
            return DEFAULT_IMAGE_DOWNLOAD_MAX_BYTES
        raw = str(self.environment.get(IMAGE_DOWNLOAD_MAX_BYTES_ENV) or "").strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ExecutorExecutionError(
                f"{IMAGE_DOWNLOAD_MAX_BYTES_ENV} 必须是 {MIN_IMAGE_DOWNLOAD_MAX_BYTES} 到 "
                f"{MAX_IMAGE_DOWNLOAD_MAX_BYTES} 的整数"
            ) from exc
        if (
            str(value) != raw
            or value < MIN_IMAGE_DOWNLOAD_MAX_BYTES
            or value > MAX_IMAGE_DOWNLOAD_MAX_BYTES
        ):
            raise ExecutorExecutionError(
                f"{IMAGE_DOWNLOAD_MAX_BYTES_ENV} 必须是 {MIN_IMAGE_DOWNLOAD_MAX_BYTES} 到 "
                f"{MAX_IMAGE_DOWNLOAD_MAX_BYTES} 的整数"
            )
        return value

    def execute(self, request_value: ExecutionRequest) -> ExecutionResult:
        if request_value.step != "renders" or not isinstance(request_value.payload, ImageGenerationTask):
            raise ExecutorExecutionError("openai-image 仅接受 renders 图片任务")
        task = request_value.payload
        api_key = (self.environment.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise ExecutorExecutionError("服务器未配置 OPENAI_API_KEY")
        self._validate_task(task)

        headers = {"Authorization": f"Bearer {api_key}"}
        reference_image_records: list[dict[str, object]] = []
        if task.reference_images:
            endpoint = f"{self.base_url}/images/edits"
            boundary = self.boundary_factory()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            body, reference_image_records = self._multipart_body(task, boundary)
        else:
            endpoint = f"{self.base_url}/images/generations"
            headers["Content-Type"] = "application/json"
            body = json.dumps(self._request_fields(task), ensure_ascii=False).encode("utf-8")

        response = self.transport.post(endpoint, headers, body, self.timeout)
        payload = self._response_payload(response, api_key)
        image_bytes, retrieval = self._retrieve_image(
            payload,
            response_status=response.status,
        )
        self._atomic_write(task.output_path, image_bytes)

        request_id = self._header(response.headers, "x-request-id")
        metadata = {
            "request_id": request_id,
            "usage": payload.get("usage") or {},
            "output_format": payload.get("output_format") or task.output_format,
            "reference_images": reference_image_records,
            "retrieval": retrieval,
        }
        return ExecutionResult(
            detail=f"generated {task.output_path.name}",
            outputs=(task.output_path,),
            provider=self.name,
            model=self.model,
            metadata=metadata,
        )

    def _validate_task(self, task: ImageGenerationTask) -> None:
        if not task.prompt.strip():
            raise ExecutorExecutionError("图片任务缺少 prompt")
        if task.output_format not in {"png", "jpeg", "webp"}:
            raise ExecutorExecutionError(f"不支持的图片格式：{task.output_format}")
        if task.quality not in {"auto", "low", "medium", "high"}:
            raise ExecutorExecutionError(f"不支持的图片质量：{task.quality}")
        for image in task.reference_images:
            if not image.is_file():
                filename = sanitize_filename(image.name)
                message = (
                    f"参考图不存在：{filename}"
                    if filename is not None
                    else "参考图缺失 1 张"
                )
                raise _attach_render_failure(
                    ExecutorExecutionError(message),
                    "render_input_missing",
                    missing_files=(filename,) if filename is not None else (),
                    missing_count=1,
                )

    def _request_fields(self, task: ImageGenerationTask) -> dict[str, object]:
        return {
            "model": self.model,
            "prompt": task.prompt,
            "n": 1,
            "size": task.size,
            "quality": task.quality,
            "output_format": task.output_format,
        }

    def _multipart_body(
        self,
        task: ImageGenerationTask,
        boundary: str,
    ) -> tuple[bytes, list[dict[str, object]]]:
        chunks: list[bytes] = []
        reference_image_records: list[dict[str, object]] = []

        def field(name: str, value: str) -> None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        for name, value in self._request_fields(task).items():
            field(name, str(value))
        for image in task.reference_images:
            filename = image.name.replace('"', "_")
            compressed = compress_reference_image(
                image.read_bytes(),
                max_bytes=self.reference_image_max_bytes,
                filename=filename,
            )
            sent_bytes = len(compressed.data)
            if sent_bytes > self.reference_image_max_bytes:
                raise ExecutorExecutionError("参考图发送字节超过上限，已停止")
            part_filename = Path(compressed.filename).name.replace('"', "_")
            reference_image_records.append(
                {
                    "name": part_filename,
                    "original_bytes": compressed.original_bytes,
                    "sent_bytes": sent_bytes,
                    "compressed": compressed.compressed,
                    "quality": compressed.quality,
                    "long_edge": compressed.long_edge,
                }
            )
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="image[]"; filename="{part_filename}"\r\n'.encode(
                        "utf-8"
                    ),
                    f"Content-Type: {compressed.content_type}\r\n\r\n".encode("ascii"),
                    compressed.data,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks), reference_image_records

    def _response_payload(self, response: HttpResponse, api_key: str) -> dict:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failure = ExecutorExecutionError(
                f"图片服务返回了无法解析的响应（HTTP {response.status}）"
            )
            if response.status >= 400:
                failure = _attach_render_failure(
                    failure,
                    "render_http_error",
                    **_extract_upstream_failure(response),
                )
            elif 200 <= response.status < 300:
                failure = _attach_render_failure(
                    failure,
                    "render_response_invalid",
                    **_extract_response_shape(None),
                )
            raise failure from exc
        if not isinstance(payload, dict):
            failure = ExecutorExecutionError("图片服务响应格式不正确")
            if response.status >= 400:
                failure = _attach_render_failure(
                    failure,
                    "render_http_error",
                    **_extract_upstream_failure(response),
                )
            elif 200 <= response.status < 300:
                failure = _attach_render_failure(
                    failure,
                    "render_response_invalid",
                    **_extract_response_shape(payload),
                )
            raise failure
        if response.status >= 400:
            error_value = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error_value.get("code") or "api_error")
            message = str(error_value.get("message") or f"HTTP {response.status}").replace(api_key, "[REDACTED]")
            raise _attach_render_failure(
                ExecutorExecutionError(f"OpenAI Image API {response.status} {code}: {message}"),
                "render_http_error",
                **_extract_upstream_failure(response),
            )
        return payload

    def _decode_image(self, payload: dict, *, response_status: int = 200) -> bytes:
        data = payload.get("data")
        encoded = data[0].get("b64_json") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(encoded, str) or not encoded:
            failure = ExecutorExecutionError("图片服务响应缺少 data[0].b64_json")
            if 200 <= response_status < 300:
                failure = _attach_render_failure(
                    failure,
                    "render_response_invalid",
                    **_extract_response_shape(payload),
                )
            raise failure
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            failure = ExecutorExecutionError("图片服务返回的 Base64 图片无效")
            if 200 <= response_status < 300:
                failure = _attach_render_failure(
                    failure,
                    "render_response_invalid",
                    **_extract_response_shape(payload),
                )
            raise failure from exc

    def _retrieve_image(
        self,
        payload: dict,
        *,
        response_status: int,
    ) -> tuple[bytes, str]:
        data = payload.get("data")
        data0 = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if isinstance(data0, dict) and "b64_json" in data0:
            return self._decode_image(payload, response_status=response_status), "inline"
        if (
            not 200 <= response_status < 300
            or not isinstance(data0, dict)
            or "url" not in data0
        ):
            return self._decode_image(payload, response_status=response_status), "inline"

        response_shape = _extract_response_shape(payload)
        try:
            download_url = _validate_image_download_url(data0.get("url"))
        except ValueError as exc:
            raise _attach_render_failure(
                ExecutorExecutionError("图片服务返回的图片地址无法使用"),
                "render_response_invalid",
                **response_shape,
            ) from exc

        try:
            response = self.transport.get(
                download_url,
                {"User-Agent": CLIENT_USER_AGENT},
                self.timeout,
            )
        except ExecutorExecutionError as exc:
            if getattr(exc, "code", None) in {
                "render_timeout",
                "render_network_error",
                "render_image_download_failed",
            }:
                for name, value in response_shape.items():
                    setattr(exc, name, value)
                raise
            raise _attach_render_failure(
                ExecutorExecutionError("图片下载失败"),
                "render_image_download_failed",
                **response_shape,
            ) from exc
        except Exception as exc:
            raise _attach_render_failure(
                ExecutorExecutionError("图片下载失败"),
                "render_image_download_failed",
                **response_shape,
            ) from exc

        if response.final_url:
            try:
                _validate_image_download_url(response.final_url)
            except ValueError as exc:
                raise _attach_render_failure(
                    ExecutorExecutionError("图片下载地址未通过安全校验"),
                    "render_image_download_failed",
                    **response_shape,
                ) from exc
        if not 200 <= response.status < 300:
            fields: dict[str, object] = dict(response_shape)
            if type(response.status) is int and 100 <= response.status <= 599:
                fields["http_status"] = response.status
            raise _attach_render_failure(
                ExecutorExecutionError(f"图片下载服务返回 HTTP {response.status}"),
                "render_image_download_failed",
                **fields,
            )
        if len(response.body) > self.image_download_max_bytes:
            raise _attach_render_failure(
                ExecutorExecutionError(
                    f"图片下载响应超过 {self.image_download_max_bytes} 字节上限"
                ),
                "render_image_download_failed",
                **response_shape,
            )
        if not response.body:
            raise _attach_render_failure(
                ExecutorExecutionError("图片下载响应为空"),
                "render_image_download_failed",
                **response_shape,
            )
        return response.body, "url"

    def _atomic_write(self, output_path: Path, content: bytes) -> None:
        temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(content)
            os.replace(temporary, output_path)
        except OSError as exc:
            raise ExecutorExecutionError(f"无法保存生成图片：{output_path}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        return _response_header(headers, name)
