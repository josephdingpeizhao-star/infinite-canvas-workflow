from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BRIDGE = ROOT / "canvas-bridge"
for import_root in (SCRIPTS, BRIDGE):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import detect_current_state  # noqa: E402
import state_reader  # noqa: E402


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)


class RenderCoverageRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.repository = self.workspace / "repository"
        self.repository.mkdir()
        self.manifest_path = self.workspace / "manifests" / "coverage.batch_manifest.json"
        self.renders = self.workspace / "outputs" / "renders"
        self.repaired = self.workspace / "outputs" / "repaired"
        self.renders.mkdir(parents=True)
        self.repaired.mkdir(parents=True)
        self.manifest = self._build_manifest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _build_manifest(self) -> dict:
        identity = self.workspace / "artifacts" / "identity" / "identity.json"
        style = self.workspace / "artifacts" / "style_master" / "style.json"
        angles = self.workspace / "artifacts" / "angle_inventory" / "angles.json"
        variables = self.workspace / "artifacts" / "variable_configs"
        final_prompts = self.workspace / "artifacts" / "final_prompts"
        qc_reports = self.workspace / "artifacts" / "qc_reports"
        comfyui_jobs = self.workspace / "artifacts" / "comfyui_jobs"

        self._write_json(identity, {"artifact_type": "product_identity_archive"})
        self._write_json(style, {"artifact_type": "style_master"})
        self._write_json(angles, {"artifact_type": "angle_inventory"})
        self._write_json(
            variables / "main_variable_configs.json",
            {"artifact_type": "main_variable_config"},
        )
        self._write_json(
            variables / "detail_variable_configs.json",
            {"artifact_type": "detail_variable_config"},
        )
        for config_id in CONFIG_IDS:
            self._write_json(
                final_prompts / f"{config_id}_final_prompt.json",
                {"artifact_type": "final_prompt", "config_id": config_id},
            )
        self._write_json(
            final_prompts / "final_prompt_index.json",
            {
                "artifact_type": "final_prompt_index",
                "product_id": "coverage",
                "prompt_count": len(CONFIG_IDS),
                "items": [{"config_id": config_id} for config_id in CONFIG_IDS],
            },
        )
        qc_reports.mkdir(parents=True)
        comfyui_jobs.mkdir(parents=True)

        return {
            "product_id": "coverage",
            "batch_type": "single",
            "user_declared_set_product": False,
            "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
            "notes": "",
            "artifacts": {
                "product_identity_archive": str(identity.parent),
                "style_master": str(style.parent),
                "angle_inventory": str(angles.parent),
                "main_variable_configs": str(variables),
                "detail_variable_configs": str(variables),
                "final_prompts": str(final_prompts),
                "comfyui_jobs": str(comfyui_jobs),
                "qc_reports": str(qc_reports),
            },
            "outputs": {
                "renders": str(self.renders),
                "repaired": str(self.repaired),
            },
        }

    def _add_pngs(self, directory: Path, config_ids: tuple[str, ...]) -> None:
        for config_id in config_ids:
            (directory / f"{config_id}.png").write_bytes(b"png")

    def _route(self) -> dict:
        return state_reader.route_manifest(
            self.manifest,
            self.manifest_path,
            repository_root=self.repository,
        )

    def _direct_route(self) -> dict:
        def section(name: str, defaults: dict[str, str]) -> dict:
            return {
                key: detect_current_state.summarize_path_values(
                    self.repository,
                    detect_current_state.values_from_manifest_or_default(
                        self.manifest,
                        name,
                        key,
                        "coverage",
                    ),
                )
                for key in defaults
            }

        return detect_current_state.route_batch(
            "coverage",
            self.manifest_path,
            self.manifest,
            section("inputs", detect_current_state.INPUT_DEFAULTS),
            section("drafts", detect_current_state.DRAFT_DEFAULTS),
            section("artifacts", detect_current_state.ARTIFACT_DEFAULTS),
            section("outputs", detect_current_state.OUTPUT_DEFAULTS),
        )

    def test_zero_of_fourteen_stays_before_qc(self) -> None:
        self.assertEqual("needs_generated_images_before_qc", self._route()["current_stage"])

    def test_one_of_fourteen_stays_before_qc(self) -> None:
        self._add_pngs(self.renders, ("main_01",))
        self.assertEqual("needs_generated_images_before_qc", self._route()["current_stage"])

    def test_thirteen_of_fourteen_stays_before_qc(self) -> None:
        self._add_pngs(self.renders, CONFIG_IDS[:-1])
        self.assertEqual("needs_generated_images_before_qc", self._route()["current_stage"])

    def test_fourteen_of_fourteen_routes_to_qc(self) -> None:
        self._add_pngs(self.renders, CONFIG_IDS)
        self.assertEqual("needs_qc_reports", self._route()["current_stage"])

    def test_renders_and_repaired_together_can_complete_coverage(self) -> None:
        self._add_pngs(self.renders, CONFIG_IDS[:6])
        self._add_pngs(self.repaired, CONFIG_IDS[6:])
        self.assertEqual("needs_qc_reports", self._route()["current_stage"])

    def test_state_reader_matches_the_shared_route(self) -> None:
        self._add_pngs(self.renders, ("main_01",))
        mirrored = self._route()
        direct = self._direct_route()
        self.assertEqual("needs_generated_images_before_qc", mirrored["current_stage"])
        self.assertEqual(direct["current_stage"], mirrored["current_stage"])


if __name__ == "__main__":
    unittest.main()
