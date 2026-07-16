from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
import sys

for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from final_prompt_integrity_fixtures import build_final_prompt_bundle, read_json, write_json  # noqa: E402
from render_task_assembler import (  # noqa: E402
    ASPECT_TO_IMAGE_SIZE,
    NEGATIVE_PROMPT_SEPARATOR,
    RenderTaskAssemblyError,
    assemble_render_tasks,
)


class RenderTaskAssemblerTest(unittest.TestCase):
    def test_builds_14_ordered_tasks_with_expected_sizes_paths_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = assemble_render_tasks(bundle.manifest, bundle.index_path)

            self.assertEqual(tuple(f"main_{i:02d}" for i in range(1, 7)) + tuple(f"detail_{i:02d}" for i in range(1, 9)), plan.planned)
            self.assertEqual(14, len(plan.tasks))
            self.assertEqual((), plan.skipped)
            self.assertEqual(["1024x1024"] * 6 + ["1024x1536"] * 8, [task.size for task in plan.tasks])
            self.assertEqual([f"{config_id}.png" for config_id in plan.planned], [task.output_path.name for task in plan.tasks])
            self.assertTrue(all(task.output_path.parent == bundle.renders_dir for task in plan.tasks))
            self.assertTrue(all(len(task.reference_images) == 1 for task in plan.tasks))
            self.assertTrue(all(task.output_format == "png" for task in plan.tasks))
            self.assertEqual("1024x1536", ASPECT_TO_IMAGE_SIZE["3:4"])

    def test_missing_reference_rejects_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            index = read_json(bundle.index_path)
            index["items"][5]["bound_reference"] = "missing.JPG"
            write_json(bundle.index_path, index)

            with self.assertRaises(RenderTaskAssemblyError):
                assemble_render_tasks(bundle.manifest, bundle.index_path)

    def test_unsupported_reference_rejects_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            bad = bundle.white_bg_dir / "unsafe.gif"
            bad.write_bytes(b"gif")
            index = read_json(bundle.index_path)
            index["items"][1]["bound_reference"] = bad.name
            write_json(bundle.index_path, index)

            with self.assertRaises(RenderTaskAssemblyError):
                assemble_render_tasks(bundle.manifest, bundle.index_path)

    def test_combines_negative_prompt_without_rewriting_positive_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            document = read_json(bundle.prompt_path("main_01"))
            plan = assemble_render_tasks(bundle.manifest, bundle.index_path)

            self.assertEqual(
                document["final_prompt"] + NEGATIVE_PROMPT_SEPARATOR + document["negative_prompt"],
                plan.tasks[0].prompt,
            )
            self.assertTrue(plan.tasks[0].prompt.startswith(document["final_prompt"]))

    def test_existing_two_outputs_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            (bundle.renders_dir / "main_01.png").write_bytes(b"existing-one")
            (bundle.renders_dir / "detail_03.png").write_bytes(b"existing-two")
            plan = assemble_render_tasks(bundle.manifest, bundle.index_path)

            self.assertEqual(("main_01", "detail_03"), plan.skipped)
            self.assertNotIn("main_01", plan.planned)
            self.assertNotIn("detail_03", plan.planned)
            self.assertEqual(12, len(plan.tasks))

    def test_render_directory_outside_outputs_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            manifest = dict(bundle.manifest)
            manifest["outputs"] = dict(bundle.manifest["outputs"])
            manifest["outputs"]["renders"] = [str(Path(tmp) / "outside")]

            with self.assertRaises(RenderTaskAssemblyError):
                assemble_render_tasks(manifest, bundle.index_path)

    def test_duplicate_config_or_prompt_path_outside_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            index = read_json(bundle.index_path)
            index["items"][1]["config_id"] = index["items"][0]["config_id"]
            write_json(bundle.index_path, index)
            with self.assertRaises(RenderTaskAssemblyError):
                assemble_render_tasks(bundle.manifest, bundle.index_path)

            index = read_json(bundle.index_path)
            index["items"][1]["config_id"] = "main_02"
            index["items"][1]["final_prompt_path"] = str(Path(tmp) / "outside.json")
            write_json(bundle.index_path, index)
            with self.assertRaises(RenderTaskAssemblyError):
                assemble_render_tasks(bundle.manifest, bundle.index_path)


if __name__ == "__main__":
    unittest.main()
