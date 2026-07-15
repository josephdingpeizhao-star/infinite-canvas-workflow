"""GPT Image adapter behind the provider-neutral executor contract."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib import error, request

from executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        """Send one HTTP POST request."""


class UrllibTransport:
    """Standard-library HTTP transport used by the production adapter."""

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        outbound = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outbound, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except error.HTTPError as exc:
            return HttpResponse(status=int(exc.code), headers=dict(exc.headers.items()), body=exc.read())
        except error.URLError as exc:
            raise ExecutorExecutionError(f"无法连接图片生成服务：{exc.reason}") from exc


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
        timeout: float = 180.0,
        boundary_factory: Callable[[], str] | None = None,
    ) -> None:
        self.environment = context.environment
        self.transport = transport or UrllibTransport()
        self.base_url = (base_url or self.environment.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or self.environment.get("OPENAI_IMAGE_MODEL") or "gpt-image-2"
        self.timeout = timeout
        self.boundary_factory = boundary_factory or (lambda: f"executor-{uuid.uuid4().hex}")

    def execute(self, request_value: ExecutionRequest) -> ExecutionResult:
        if request_value.step != "renders" or not isinstance(request_value.payload, ImageGenerationTask):
            raise ExecutorExecutionError("openai-image 仅接受 renders 图片任务")
        task = request_value.payload
        api_key = (self.environment.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise ExecutorExecutionError("服务器未配置 OPENAI_API_KEY")
        self._validate_task(task)

        headers = {"Authorization": f"Bearer {api_key}"}
        if task.reference_images:
            endpoint = f"{self.base_url}/images/edits"
            boundary = self.boundary_factory()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            body = self._multipart_body(task, boundary)
        else:
            endpoint = f"{self.base_url}/images/generations"
            headers["Content-Type"] = "application/json"
            body = json.dumps(self._request_fields(task), ensure_ascii=False).encode("utf-8")

        response = self.transport.post(endpoint, headers, body, self.timeout)
        payload = self._response_payload(response, api_key)
        image_bytes = self._decode_image(payload)
        self._atomic_write(task.output_path, image_bytes)

        request_id = self._header(response.headers, "x-request-id")
        metadata = {
            "request_id": request_id,
            "usage": payload.get("usage") or {},
            "output_format": payload.get("output_format") or task.output_format,
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
                raise ExecutorExecutionError(f"参考图不存在：{image}")

    def _request_fields(self, task: ImageGenerationTask) -> dict[str, object]:
        return {
            "model": self.model,
            "prompt": task.prompt,
            "n": 1,
            "size": task.size,
            "quality": task.quality,
            "output_format": task.output_format,
        }

    def _multipart_body(self, task: ImageGenerationTask, boundary: str) -> bytes:
        chunks: list[bytes] = []

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
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="image[]"; filename="{filename}"\r\n'.encode(
                        "utf-8"
                    ),
                    f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                    image.read_bytes(),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks)

    def _response_payload(self, response: HttpResponse, api_key: str) -> dict:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutorExecutionError(f"图片服务返回了无法解析的响应（HTTP {response.status}）") from exc
        if not isinstance(payload, dict):
            raise ExecutorExecutionError("图片服务响应格式不正确")
        if response.status >= 400:
            error_value = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error_value.get("code") or "api_error")
            message = str(error_value.get("message") or f"HTTP {response.status}").replace(api_key, "[REDACTED]")
            raise ExecutorExecutionError(f"OpenAI Image API {response.status} {code}: {message}")
        return payload

    def _decode_image(self, payload: dict) -> bytes:
        data = payload.get("data")
        encoded = data[0].get("b64_json") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ExecutorExecutionError("图片服务响应缺少 data[0].b64_json")
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ExecutorExecutionError("图片服务返回的 Base64 图片无效") from exc

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
        target = name.lower()
        return next((str(value) for key, value in headers.items() if key.lower() == target), "")
