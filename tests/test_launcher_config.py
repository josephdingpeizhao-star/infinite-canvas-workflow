import json
import tempfile
import unittest
from pathlib import Path

from launcher.config import LauncherConfigError, load_config
from launcher.orchestrator import build_service_specs


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "launcher" / "launcher_config.json"


class LauncherConfigTests(unittest.TestCase):
    def test_repository_default_config_is_valid_and_preserves_current_commands(self):
        config = load_config(DEFAULT_CONFIG, override_path=REPO_ROOT / "missing-override.json")
        specs = build_service_specs(
            config,
            launcher_dir=REPO_ROOT / "launcher",
            pythonw_path=Path(r"C:\Python312\pythonw.exe"),
        )

        self.assertEqual(config["web"]["mode"], "dist")
        self.assertEqual(config["runtime"]["startup_timeout_seconds"], 60)
        self.assertEqual(config["runtime"]["connection_ready_timeout_seconds"], 90)
        self.assertEqual(
            config["browser"]["url"],
            "http://localhost:3000/canvas/hPbkNXg3WA0p2i46VOh3s",
        )
        self.assertEqual([spec.name for spec in specs], ["agent", "web", "workbench"])
        agent, web, workbench = specs
        self.assertTrue(agent.command[0])
        self.assertIn(Path(agent.command[0]).name.casefold(), {"bun", "bun.exe", "bun.cmd"})
        self.assertEqual(
            agent.command[1:],
            (
                "run",
                "--cwd",
                f"{REPO_ROOT.parent}/infinite-canvas/canvas-agent",
                "dev",
            ),
        )
        self.assertIn("static_server.py", " ".join(web.command))
        self.assertEqual(
            workbench.environment["CODEX_DEV_ALLOW_REAL_EXECUTION"],
            "1",
        )
        self.assertEqual(workbench.ports, (17372, 17373))
        self.assertEqual(agent.health_url, "http://127.0.0.1:17371/config")

    def test_override_is_deep_merged_without_erasing_sibling_defaults(self):
        with tempfile.TemporaryDirectory() as raw:
            override = Path(raw) / "config.override.json"
            override.write_text(
                json.dumps(
                    {
                        "runtime": {"startup_timeout_seconds": 12},
                        "web": {"mode": "dev"},
                        "browser": {"url": "http://127.0.0.1:3000/canvas/test"},
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(DEFAULT_CONFIG, override_path=override)

        self.assertEqual(config["runtime"]["startup_timeout_seconds"], 12)
        self.assertIn("log_max_bytes", config["runtime"])
        self.assertEqual(config["web"]["mode"], "dev")
        self.assertIn("dist", config["web"])
        self.assertEqual(config["browser"]["url"], "http://127.0.0.1:3000/canvas/test")

    def test_invalid_json_has_a_human_readable_error(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher_config.json"
            path.write_text("{bad", encoding="utf-8")

            with self.assertRaisesRegex(LauncherConfigError, "不是有效的 JSON"):
                load_config(path, override_path=Path(raw) / "none.json")

    def test_invalid_override_json_has_a_human_readable_error(self):
        with tempfile.TemporaryDirectory() as raw:
            override = Path(raw) / "config.override.json"
            override.write_text("{bad", encoding="utf-8")

            with self.assertRaisesRegex(LauncherConfigError, "本机覆盖配置不是有效的 JSON"):
                load_config(DEFAULT_CONFIG, override_path=override)

    def test_missing_required_key_has_a_human_readable_error(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher_config.json"
            path.write_text(json.dumps({"browser": {"url": "http://localhost:3000"}}), encoding="utf-8")

            with self.assertRaisesRegex(LauncherConfigError, "缺少"):
                load_config(path, override_path=Path(raw) / "none.json")


if __name__ == "__main__":
    unittest.main()
