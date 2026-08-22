from __future__ import annotations

import ast
import copy
import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOWNSTREAM_SOURCE = ROOT / "canvas-bridge" / "codex_dev_downstream.py"


BUILDER_BYTE_SHA256 = {
    "build_variable_config_prompt": (
        "7bd389bf67e0e2e1fe92d9be57aeae30ee403b34f1aa6071623c6a83779d9d8c"
    ),
    "build_set_variable_config_prompt": (
        "bb1e644100a70d17567726ac767b07b0a5682ab0d7a61105c16e9258799c7529"
    ),
    "build_final_prompt_batch_prompt": (
        "7dc8846b818440836e063249d74fb155676b47b2b60c333313d2c4f855f64589"
    ),
    "build_set_final_prompt_batch_prompt": (
        "85e8eefe463faf099841ecabaae2b9f8820d10d268338e95b6373a54b185e2b6"
    ),
    "build_final_prompt_repair_prompt": (
        "92a038f7bca597f12467741e7fbff2f8588d1e1bdb2873e629122e8bf70c8227"
    ),
    "build_set_final_prompt_repair_prompt": (
        "86164b99dbca46238ace35271d42dc9338bdf7d00c565cf3cfae1de426e682be"
    ),
}

VARIABLE_CONFIG_AST_SHA256 = {
    "parse_variable_config_response": (
        "a41414964b3d4680c366ca17e76b7c66c9de5f2c1bcf2bb1a5241749bd347399"
    ),
    "parse_set_variable_config_response": (
        "a5baf7e0797ffb31c0aef9be8acdf9ffc5773baff5a8b90b6b2bb472c07a31b6"
    ),
    "_validate_handheld_summary": (
        "35856dd5391692ea8dbf62a9c24d239bd07bdb305634e649b4ba4f6c92511ba0"
    ),
    "_validate_set_handheld_summary": (
        "8e024a55d95786bafeed1966a4ab82c6ec554ca4ed5c44eabd0cda18deab35e2"
    ),
}


def _function_nodes(source_text: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(source_text, filename=str(DOWNSTREAM_SOURCE))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _raw_function_bytes(raw_source: bytes, node: ast.FunctionDef) -> bytes:
    """Return the exact, reproducible byte range protected by a builder hash.

    The algorithm is intentionally frozen: locate the top-level FunctionDef with
    AST; exclude decorator lines; start at column zero of the physical line named
    by FunctionDef.lineno; include every physical line through end_lineno and the
    existing line ending of that final line; preserve original CRLF/LF bytes; and
    never synthesize a missing final line ending.  The hash does not consult Git.
    """

    physical_lines = raw_source.splitlines(keepends=True)
    return b"".join(physical_lines[node.lineno - 1 : node.end_lineno])


def _ast_digest(node: ast.FunctionDef) -> str:
    semantic_dump = ast.dump(node, include_attributes=False).encode("utf-8")
    return hashlib.sha256(semantic_dump).hexdigest()


class P2dContractFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_source = DOWNSTREAM_SOURCE.read_bytes()
        cls.source_text = cls.raw_source.decode("utf-8")
        cls.functions = _function_nodes(cls.source_text)

    def test_full_batch_prompt_builders_remain_byte_identical(self) -> None:
        for function_name, expected_digest in BUILDER_BYTE_SHA256.items():
            with self.subTest(function=function_name):
                node = self.functions[function_name]
                self.assertEqual(
                    node.decorator_list,
                    [],
                    f"{function_name} must remain undecorated",
                )
                actual_digest = hashlib.sha256(
                    _raw_function_bytes(self.raw_source, node)
                ).hexdigest()
                self.assertEqual(actual_digest, expected_digest)

    def test_variable_config_full_parsers_and_validators_remain_ast_identical(
        self,
    ) -> None:
        for function_name, expected_digest in VARIABLE_CONFIG_AST_SHA256.items():
            with self.subTest(function=function_name):
                self.assertEqual(
                    _ast_digest(self.functions[function_name]),
                    expected_digest,
                )

    def test_ast_digest_detects_an_in_memory_semantic_mutation(self) -> None:
        original = self.functions["_validate_handheld_summary"]
        mutated = copy.deepcopy(original)
        mutated.body.append(
            ast.Raise(
                exc=ast.Call(
                    func=ast.Name(id="RuntimeError", ctx=ast.Load()),
                    args=[ast.Constant(value="p2d semantic mutation sentinel")],
                    keywords=[],
                ),
                cause=None,
            )
        )

        self.assertNotEqual(_ast_digest(mutated), _ast_digest(original))


if __name__ == "__main__":
    unittest.main()
