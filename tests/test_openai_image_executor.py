from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from executor_factory import build_registry  # noqa: E402
from openai_image_executor import HttpResponse, OpenAIImageExecutor, UrllibTransport  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\nprovider-neutral-test"


class RecordingTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return self.response


class UrlopenResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self) -> UrlopenResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class TimeoutBody:
    def read(self, *args, **kwargs) -> bytes:
        raise TimeoutError("secret transport detail")

    def close(self) -> None:
        return None


class TimeoutResponse(UrlopenResponse):
    def read(self) -> bytes:
        raise TimeoutError("secret transport detail")


def success_response(image: bytes = PNG_BYTES) -> HttpResponse:
    payload = {
        "created": 123,
        "background": "opaque",
        "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        "output_format": "png",
        "quality": "medium",
        "size": "1024x1024",
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"image_tokens": 0, "text_tokens": 12},
            "output_tokens": 30,
            "output_tokens_details": {"image_tokens": 30, "text_tokens": 0},
            "total_tokens": 42,
        },
    }
    return HttpResponse(status=200, headers={"x-request-id": "req_test"}, body=json.dumps(payload).encode())


class OpenAIImageExecutorTest(unittest.TestCase):
    def _executor(
        self,
        transport: RecordingTransport,
        *,
        key: str = "server-secret",
        base_url: str | None = None,
        timeout: float | None = None,
        timeout_environment: str | None = None,
        include_timeout_environment: bool = False,
    ) -> OpenAIImageExecutor:
        environment = {"OPENAI_API_KEY": key}
        if base_url is not None:
            environment["OPENAI_BASE_URL"] = base_url
        if include_timeout_environment:
            environment["OPENAI_IMAGE_TIMEOUT_SECONDS"] = timeout_environment
        context = ExecutorContext(manifest={}, environment=environment)
        kwargs = {
            "transport": transport,
            "boundary_factory": lambda: "test-boundary",
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        return OpenAIImageExecutor(context, **kwargs)

    def test_missing_api_key_fails_before_network(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport, key="")

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ExecutorExecutionError) as ctx:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

        self.assertIn("OPENAI_API_KEY", str(ctx.exception))
        self.assertEqual([], transport.calls)

    def test_urllib_transport_sends_fixed_honest_user_agent(self) -> None:
        with mock.patch(
            "openai_image_executor.request.urlopen",
            return_value=UrlopenResponse(),
        ) as urlopen:
            UrllibTransport().post(
                "https://70api.top/v1/images/edits",
                {"Content-Type": "application/json"},
                b"{}",
                10.0,
            )

        outbound = urlopen.call_args.args[0]
        self.assertEqual("Codex-Canvas-Bridge/1.0", outbound.get_header("User-agent"))
        self.assertEqual("application/json", outbound.get_header("Content-type"))

    def test_urllib_transport_preserves_explicit_user_agent(self) -> None:
        headers = {"user-agent": "Caller-Agent/2.0"}
        with mock.patch(
            "openai_image_executor.request.urlopen",
            return_value=UrlopenResponse(),
        ) as urlopen:
            UrllibTransport().post(
                "https://70api.top/v1/images/edits",
                headers,
                b"{}",
                10.0,
            )

        outbound = urlopen.call_args.args[0]
        self.assertEqual("Caller-Agent/2.0", outbound.get_header("User-agent"))
        self.assertEqual(
            1,
            sum(name.lower() == "user-agent" for name, _ in outbound.header_items()),
        )
        self.assertEqual({"user-agent": "Caller-Agent/2.0"}, headers)

    def test_default_timeout_is_180_seconds(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport)

        with tempfile.TemporaryDirectory() as tmp:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

        self.assertEqual(180.0, transport.calls[0]["timeout"])

    def test_environment_timeout_is_forwarded_to_transport(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(
            transport,
            timeout_environment="900",
            include_timeout_environment=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

        self.assertEqual(900.0, transport.calls[0]["timeout"])

    def test_explicit_timeout_overrides_environment(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(
            transport,
            timeout=45.0,
            timeout_environment="900",
            include_timeout_environment=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

        self.assertEqual(45.0, transport.calls[0]["timeout"])

    def test_invalid_environment_timeout_is_rejected_before_network(self) -> None:
        invalid_values = ("", "abc", "0", "-1", "29", "1801", "30.5", "0900")
        for value in invalid_values:
            with self.subTest(value=value):
                transport = RecordingTransport(success_response())
                with self.assertRaises(ExecutorExecutionError) as ctx:
                    self._executor(
                        transport,
                        timeout_environment=value,
                        include_timeout_environment=True,
                    )
                self.assertIn("OPENAI_IMAGE_TIMEOUT_SECONDS", str(ctx.exception))
                self.assertEqual([], transport.calls)

    def test_normal_response_read_timeout_is_sanitized_without_output_or_retry(self) -> None:
        with mock.patch(
            "openai_image_executor.request.urlopen",
            return_value=TimeoutResponse(),
        ) as urlopen, tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.png"
            executor = self._executor(
                UrllibTransport(),
                key="server-secret",
                timeout_environment="900",
                include_timeout_environment=True,
            )
            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(prompt="private prompt", output_path=output),
                    )
                )

            self.assertEqual("图片服务连续 900 秒未返回新数据，已停止等待", str(ctx.exception))
            self.assertNotIn("server-secret", str(ctx.exception))
            self.assertNotIn("private prompt", str(ctx.exception))
            self.assertEqual(1, urlopen.call_count)
            self.assertFalse(output.exists())
            self.assertEqual([], list(output.parent.glob(".out.png.*.tmp")))

    def test_connection_and_wrapped_timeouts_are_sanitized(self) -> None:
        timeout_cases = (
            TimeoutError("secret direct timeout"),
            error.URLError(TimeoutError("secret wrapped timeout")),
            error.HTTPError(
                "https://70api.top/v1/images/edits",
                504,
                "gateway timeout",
                {},
                TimeoutBody(),
            ),
        )
        for failure in timeout_cases:
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "openai_image_executor.request.urlopen",
                side_effect=failure,
            ) as urlopen:
                with self.assertRaises(ExecutorExecutionError) as ctx:
                    UrllibTransport().post(
                        "https://70api.top/v1/images/edits",
                        {"Authorization": "Bearer server-secret"},
                        b"private prompt",
                        900.0,
                    )
                self.assertEqual("图片服务连续 900 秒未返回新数据，已停止等待", str(ctx.exception))
                self.assertNotIn("secret", str(ctx.exception))
                self.assertEqual(1, urlopen.call_count)

    def test_text_only_task_uses_generation_endpoint_and_writes_image(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "product.png"
            result = executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="white background kettle",
                        output_path=output,
                        size="1024x1024",
                        quality="medium",
                    ),
                )
            )

            self.assertEqual(PNG_BYTES, output.read_bytes())
            self.assertEqual((output,), result.outputs)
            self.assertEqual([], list(output.parent.glob(".product.png.*.tmp")))

        call = transport.calls[0]
        self.assertEqual("https://api.openai.com/v1/images/generations", call["url"])
        self.assertEqual("Bearer server-secret", call["headers"]["Authorization"])
        body = json.loads(call["body"])
        self.assertEqual("gpt-image-2", body["model"])
        self.assertEqual("white background kettle", body["prompt"])
        self.assertEqual("png", body["output_format"])
        self.assertEqual("medium", body["quality"])
        self.assertEqual("openai-image", result.provider)
        self.assertEqual("gpt-image-2", result.model)
        self.assertEqual("req_test", result.metadata["request_id"])
        self.assertNotIn("server-secret", repr(result))

    def test_reference_images_use_edit_endpoint_and_multipart(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            front = root / "front.jpg"
            back = root / "back.png"
            front.write_bytes(b"front-image")
            back.write_bytes(b"back-image")
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="preserve the product",
                        output_path=root / "edited.png",
                        reference_images=(front, back),
                    ),
                )
            )

        call = transport.calls[0]
        self.assertEqual("https://api.openai.com/v1/images/edits", call["url"])
        self.assertEqual("multipart/form-data; boundary=test-boundary", call["headers"]["Content-Type"])
        body = call["body"]
        self.assertIn(b'name="model"', body)
        self.assertIn(b"gpt-image-2", body)
        self.assertEqual(2, body.count(b'name="image[]"'))
        self.assertIn(b"front-image", body)
        self.assertIn(b"back-image", body)

    def test_bare_base_url_is_normalized_to_v1_edit_endpoint(self) -> None:
        for base_url in ("https://70api.top", "https://70api.top/"):
            with self.subTest(base_url=base_url), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                reference = root / "reference.jpg"
                reference.write_bytes(b"reference-image")
                transport = RecordingTransport(success_response())
                executor = self._executor(transport, base_url=base_url)

                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(
                            prompt="preserve the product",
                            output_path=root / "edited.png",
                            reference_images=(reference,),
                        ),
                    )
                )

                self.assertEqual("https://70api.top/v1/images/edits", transport.calls[0]["url"])

    def test_existing_v1_base_url_is_not_duplicated(self) -> None:
        for base_url in ("https://70api.top/v1", "https://70api.top/v1/"):
            with self.subTest(base_url=base_url), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                reference = root / "reference.jpg"
                reference.write_bytes(b"reference-image")
                transport = RecordingTransport(success_response())
                executor = self._executor(transport, base_url=base_url)

                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(
                            prompt="preserve the product",
                            output_path=root / "edited.png",
                            reference_images=(reference,),
                        ),
                    )
                )

                self.assertEqual("https://70api.top/v1/images/edits", transport.calls[0]["url"])

    def test_bare_base_url_is_normalized_to_v1_generation_endpoint(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport, base_url="https://70api.top")

        with tempfile.TemporaryDirectory() as tmp:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(
                        prompt="white background kettle",
                        output_path=Path(tmp) / "generated.png",
                    ),
                )
            )

        self.assertEqual("https://70api.top/v1/images/generations", transport.calls[0]["url"])

    def test_http_error_is_sanitized(self) -> None:
        response = HttpResponse(
            status=400,
            headers={},
            body=json.dumps({"error": {"code": "bad_request", "message": "bad server-secret value"}}).encode(),
        )
        executor = self._executor(RecordingTransport(response))

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ExecutorExecutionError) as ctx:
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

        self.assertIn("bad_request", str(ctx.exception))
        self.assertNotIn("server-secret", str(ctx.exception))

    def test_malformed_success_response_does_not_create_output(self) -> None:
        response = HttpResponse(status=200, headers={}, body=b'{"data": []}')
        executor = self._executor(RecordingTransport(response))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.png"
            with self.assertRaises(ExecutorExecutionError):
                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(prompt="product", output_path=output),
                    )
                )
            self.assertFalse(output.exists())

    def test_invalid_base64_is_reported_as_execution_error(self) -> None:
        response = HttpResponse(status=200, headers={}, body=b'{"data":[{"b64_json":"%%%"}]}')
        executor = self._executor(RecordingTransport(response))

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ExecutorExecutionError):
            executor.execute(
                ExecutionRequest(
                    step="renders",
                    payload=ImageGenerationTask(prompt="product", output_path=Path(tmp) / "out.png"),
                )
            )

    def test_non_image_payload_is_rejected_before_network(self) -> None:
        transport = RecordingTransport(success_response())
        executor = self._executor(transport)

        with self.assertRaises(ExecutorExecutionError):
            executor.execute(ExecutionRequest(step="identity", payload={"prompt": "wrong boundary"}))

        self.assertEqual([], transport.calls)

    def test_output_write_failure_is_reported_as_execution_error(self) -> None:
        executor = self._executor(RecordingTransport(success_response()))

        with tempfile.TemporaryDirectory() as tmp:
            parent_file = Path(tmp) / "not-a-directory"
            parent_file.write_text("occupied", encoding="utf-8")
            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(
                    ExecutionRequest(
                        step="renders",
                        payload=ImageGenerationTask(
                            prompt="product",
                            output_path=parent_file / "out.png",
                        ),
                    )
                )

        self.assertIn("保存", str(ctx.exception))

    def test_factory_registry_exposes_openai_adapter_without_calling_it(self) -> None:
        self.assertIn("openai-image", build_registry().names())


if __name__ == "__main__":
    unittest.main()
