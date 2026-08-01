from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from launcher.canvas_launcher import _load_render_credentials_for_launcher
from launcher.config import load_config
from launcher.orchestrator import build_service_specs, build_watchdog_spec
from launcher.render_credentials import (
    RenderCredentials,
    RenderCredentialsError,
    load_render_credentials,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "launcher" / "launcher_config.json"
RENDER_ENVIRONMENT_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "RENDER_ALLOW_REAL_EXECUTION",
        "RENDER_MAX_IMAGES",
    }
)
FAKE_API_KEY = "test-key-not-real"


def _write_credentials(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _load_default_config(temp_root: Path) -> dict[str, object]:
    return load_config(
        DEFAULT_CONFIG_PATH,
        override_path=temp_root / "missing-config.override.json",
    )


def _config_with_credential_path(temp_root: Path, credential_path: Path) -> dict[str, object]:
    config = _load_default_config(temp_root)
    config["paths"]["render_credentials_file"] = str(credential_path)
    return config


def _build_specs(
    config: dict[str, object],
    credentials: RenderCredentials | None,
) -> dict[str, object]:
    specs = build_service_specs(
        config,
        launcher_dir=REPO_ROOT / "launcher",
        pythonw_path=Path("C:/test-runtime/pythonw.exe"),
        render_credentials=credentials,
    )
    result = {spec.name: spec for spec in specs}
    watchdog = build_watchdog_spec(
        launcher_dir=REPO_ROOT / "launcher",
        pythonw_path=Path("C:/test-runtime/pythonw.exe"),
    )
    result[watchdog.name] = watchdog
    return result


class _RenderScopeAssertions:
    def assert_non_workbench_specs_are_clean(self, specs: dict[str, object]) -> None:
        for name in ("agent", "web", "watchdog"):
            with self.subTest(service=name):
                self.assertTrue(
                    RENDER_ENVIRONMENT_KEYS.isdisjoint(specs[name].environment),
                    f"{name} 不得收到渲染环境变量",
                )


class RenderCredentialsParsingTests(unittest.TestCase):
    def test_loads_complete_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "render-credentials.json"
            _write_credentials(
                path,
                {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://70api.top",
                    "max_images_per_run": 7,
                },
            )

            credentials = load_render_credentials(path)

        self.assertEqual(credentials.api_key, FAKE_API_KEY)
        self.assertEqual(credentials.base_url, "https://70api.top")
        self.assertEqual(credentials.max_images_per_run, 7)

    def test_omitted_or_null_max_images_per_run_means_no_limit(self) -> None:
        for marker, value in (("omitted", object()), ("null", None)):
            with self.subTest(case=marker), tempfile.TemporaryDirectory() as temp_dir:
                payload = {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://70api.top",
                }
                if marker == "null":
                    payload["max_images_per_run"] = value
                path = Path(temp_dir) / "render-credentials.json"
                _write_credentials(path, payload)

                credentials = load_render_credentials(path)

                self.assertIsNone(credentials.max_images_per_run)

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing-render-credentials.json"

            self.assertIsNone(load_render_credentials(path))

    def test_invalid_json_is_rejected_with_human_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "render-credentials.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RenderCredentialsError, "不是有效的 JSON"):
                load_render_credentials(path)

    def test_missing_or_blank_api_key_is_rejected(self) -> None:
        for marker, api_key in (("missing", None), ("blank", "   ")):
            with self.subTest(case=marker), tempfile.TemporaryDirectory() as temp_dir:
                payload = {"base_url": "https://70api.top"}
                if marker == "blank":
                    payload["api_key"] = api_key
                path = Path(temp_dir) / "render-credentials.json"
                _write_credentials(path, payload)

                with self.assertRaisesRegex(RenderCredentialsError, "api_key 不能为空"):
                    load_render_credentials(path)

    def test_invalid_base_url_is_rejected(self) -> None:
        for base_url in (None, "", "ftp://70api.top", "https:///missing-host", "https://["):
            with self.subTest(base_url=base_url), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "render-credentials.json"
                _write_credentials(
                    path,
                    {"api_key": FAKE_API_KEY, "base_url": base_url},
                )

                with self.assertRaisesRegex(RenderCredentialsError, "base_url"):
                    load_render_credentials(path)

    def test_invalid_max_images_per_run_is_rejected(self) -> None:
        for value in (0, -1, "1", True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "render-credentials.json"
                _write_credentials(
                    path,
                    {
                        "api_key": FAKE_API_KEY,
                        "base_url": "https://70api.top",
                        "max_images_per_run": value,
                    },
                )

                with self.assertRaisesRegex(RenderCredentialsError, "必须是正整数或 null"):
                    load_render_credentials(path)


class RenderCredentialInjectionTests(_RenderScopeAssertions, unittest.TestCase):
    def test_valid_credentials_are_injected_only_into_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            path = temp_root / "render-credentials.json"
            _write_credentials(
                path,
                {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://70api.top",
                    "max_images_per_run": 7,
                },
            )
            credentials = load_render_credentials(path)
            specs = _build_specs(_load_default_config(temp_root), credentials)

        self.assertEqual(specs["workbench"].environment["OPENAI_API_KEY"], FAKE_API_KEY)
        self.assertEqual(specs["workbench"].environment["OPENAI_BASE_URL"], "https://70api.top")
        self.assertEqual(specs["workbench"].environment["RENDER_ALLOW_REAL_EXECUTION"], "1")
        self.assertEqual(specs["workbench"].environment["RENDER_MAX_IMAGES"], "7")
        self.assert_non_workbench_specs_are_clean(specs)

    def test_omitted_or_null_limit_never_injects_render_max_images(self) -> None:
        for marker in ("omitted", "null"):
            with self.subTest(case=marker), tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                path = temp_root / "render-credentials.json"
                payload = {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://70api.top",
                }
                if marker == "null":
                    payload["max_images_per_run"] = None
                _write_credentials(path, payload)
                credentials = load_render_credentials(path)

                specs = _build_specs(_load_default_config(temp_root), credentials)

                self.assertNotIn("RENDER_MAX_IMAGES", specs["workbench"].environment)
                self.assert_non_workbench_specs_are_clean(specs)

    def test_missing_file_preserves_exact_workbench_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            credentials = load_render_credentials(temp_root / "missing-render-credentials.json")

            specs = _build_specs(_load_default_config(temp_root), credentials)

        self.assertEqual(
            specs["workbench"].environment,
            {"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
        )
        self.assert_non_workbench_specs_are_clean(specs)

    def test_invalid_file_degrades_to_exact_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            path = temp_root / "render-credentials.json"
            _write_credentials(
                path,
                {"api_key": FAKE_API_KEY, "base_url": "not-http"},
            )
            config = _config_with_credential_path(temp_root, path)
            logger = logging.getLogger(f"gt01.invalid-injection.{id(self)}")
            with self.assertLogs(logger, level="WARNING"):
                credentials = _load_render_credentials_for_launcher(config, logger)

            specs = _build_specs(config, credentials)

        self.assertEqual(
            specs["workbench"].environment,
            {"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
        )
        self.assert_non_workbench_specs_are_clean(specs)

    def test_explicit_environment_values_are_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            path = temp_root / "render-credentials.json"
            _write_credentials(
                path,
                {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://credential.example",
                    "max_images_per_run": 7,
                },
            )
            config = _load_default_config(temp_root)
            explicit = {
                "OPENAI_API_KEY": "explicit-test-key",
                "OPENAI_BASE_URL": "https://explicit.example",
                "RENDER_ALLOW_REAL_EXECUTION": "0",
                "RENDER_MAX_IMAGES": "3",
            }
            config["services"]["workbench"]["environment"].update(explicit)

            specs = _build_specs(config, load_render_credentials(path))

        for key, value in explicit.items():
            self.assertEqual(specs["workbench"].environment[key], value)
        self.assert_non_workbench_specs_are_clean(specs)


class RenderCredentialSecretSafetyTests(unittest.TestCase):
    def test_repr_and_str_do_not_expose_api_key(self) -> None:
        credentials = RenderCredentials(
            api_key=FAKE_API_KEY,
            base_url="https://70api.top",
        )

        self.assertNotIn(FAKE_API_KEY, repr(credentials))
        self.assertNotIn(FAKE_API_KEY, str(credentials))

    def test_all_three_launcher_log_results_hide_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            valid_path = temp_root / "valid.json"
            invalid_path = temp_root / "invalid.json"
            missing_path = temp_root / "missing.json"
            _write_credentials(
                valid_path,
                {"api_key": FAKE_API_KEY, "base_url": "https://70api.top"},
            )
            _write_credentials(
                invalid_path,
                {"api_key": FAKE_API_KEY, "base_url": "not-http"},
            )
            logger = logging.getLogger(f"gt01.three-log-results.{id(self)}")

            with self.assertLogs(logger, level="INFO") as captured:
                _load_render_credentials_for_launcher(
                    _config_with_credential_path(temp_root, valid_path),
                    logger,
                )
                _load_render_credentials_for_launcher(
                    _config_with_credential_path(temp_root, missing_path),
                    logger,
                )
                _load_render_credentials_for_launcher(
                    _config_with_credential_path(temp_root, invalid_path),
                    logger,
                )

        combined = "\n".join(captured.output)
        self.assertIn("已加载渲染凭据（出图已启用）", combined)
        self.assertIn("未找到渲染凭据文件（本次启动不含出图能力）", combined)
        self.assertIn("渲染凭据文件无效：", combined)
        self.assertNotIn(FAKE_API_KEY, combined)

    def test_validation_exception_messages_hide_api_key(self) -> None:
        invalid_values = (
            {"api_key": FAKE_API_KEY, "base_url": "not-http"},
            {
                "api_key": FAKE_API_KEY,
                "base_url": "https://70api.top",
                "max_images_per_run": 0,
            },
        )
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "render-credentials.json"
                _write_credentials(path, value)

                with self.assertRaises(RenderCredentialsError) as captured:
                    load_render_credentials(path)

                self.assertNotIn(FAKE_API_KEY, str(captured.exception))


class RenderCredentialFailClosedTests(_RenderScopeAssertions, unittest.TestCase):
    def test_invalid_credentials_do_not_prevent_spec_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            path = temp_root / "render-credentials.json"
            _write_credentials(
                path,
                {
                    "api_key": FAKE_API_KEY,
                    "base_url": "https://70api.top",
                    "max_images_per_run": "7",
                },
            )
            config = _config_with_credential_path(temp_root, path)
            logger = logging.getLogger(f"gt01.fail-closed.{id(self)}")

            with self.assertLogs(logger, level="WARNING") as captured:
                credentials = _load_render_credentials_for_launcher(config, logger)
            specs = _build_specs(config, credentials)

        self.assertIsNone(credentials)
        self.assertEqual(
            specs["workbench"].environment,
            {"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
        )
        self.assert_non_workbench_specs_are_clean(specs)
        self.assertNotIn(FAKE_API_KEY, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
