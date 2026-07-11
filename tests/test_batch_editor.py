from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_editor  # noqa: E402


class ParseEditorContentTest(unittest.TestCase):
    def test_render_parse_roundtrip(self) -> None:
        manifest = {"requested_outputs": ["main", "detail"], "notes": "hello"}
        text = batch_editor.render_editor_content(manifest)
        fields = batch_editor.parse_editor_content(text)
        self.assertEqual(["main", "detail"], fields["requested_outputs"])
        self.assertEqual("hello", fields["notes"])

    def test_chinese_colon_and_comma_tolerated(self) -> None:
        fields = batch_editor.parse_editor_content("requested_outputs：main，final_prompts")
        self.assertEqual(["main", "final_prompts"], fields["requested_outputs"])

    def test_notes_keeps_inner_colon(self) -> None:
        fields = batch_editor.parse_editor_content("notes: 备注: 含冒号")
        self.assertEqual("备注: 含冒号", fields["notes"])

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(batch_editor.EditValidationError) as ctx:
            batch_editor.parse_editor_content("batch_type: set")
        self.assertIn("白名单", str(ctx.exception))

    def test_invalid_output_value_rejected(self) -> None:
        with self.assertRaises(batch_editor.EditValidationError) as ctx:
            batch_editor.parse_editor_content("requested_outputs: main, banana")
        self.assertIn("banana", str(ctx.exception))

    def test_duplicate_key_rejected(self) -> None:
        with self.assertRaises(batch_editor.EditValidationError):
            batch_editor.parse_editor_content("notes: a\nnotes: b")

    def test_empty_requested_outputs_allowed(self) -> None:
        fields = batch_editor.parse_editor_content("requested_outputs: ")
        self.assertEqual([], fields["requested_outputs"])

    def test_no_editable_fields_rejected(self) -> None:
        with self.assertRaises(batch_editor.EditValidationError):
            batch_editor.parse_editor_content("# only comments\n\n")

    def test_canonical_order_and_dedupe(self) -> None:
        fields = batch_editor.parse_editor_content("requested_outputs: qc_reports, main, main")
        self.assertEqual(["main", "qc_reports"], fields["requested_outputs"])


class ApplyEditsTest(unittest.TestCase):
    def test_patches_whitelist_only_and_writes_atomically(self) -> None:
        manifest = {
            "product_id": "edit_test",
            "batch_type": "single",
            "user_declared_set_product": False,
            "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
            "notes": "",
            "workspace": {"mode": "external", "root": "X:/nonexistent"},
            "keep_me": {"nested": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edit_test.batch_manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            result = batch_editor.apply_edits(path, {"requested_outputs": ["main"], "notes": "已修改"})
            self.assertEqual(["notes", "requested_outputs"], result["applied_fields"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(["main"], saved["requested_outputs"])
            self.assertEqual("已修改", saved["notes"])
            self.assertEqual({"nested": True}, saved["keep_me"])
            self.assertEqual("single", saved["batch_type"])
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_non_whitelist_field_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.batch_manifest.json"
            path.write_text(json.dumps({"product_id": "p", "requested_outputs": []}), encoding="utf-8")
            with self.assertRaises(batch_editor.EditValidationError):
                batch_editor.apply_edits(path, {"batch_type": "set"})
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("batch_type", saved)


if __name__ == "__main__":
    unittest.main()
