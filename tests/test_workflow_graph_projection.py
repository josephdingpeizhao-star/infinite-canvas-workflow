from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import detect_current_state  # noqa: E402


GRAPH_PATH = ROOT / "manifests" / "workflow_graph.template.json"
BATCH_TEMPLATE_PATH = ROOT / "manifests" / "batch_manifest.template.json"

ALL_REQUESTED = ("main", "detail", "final_prompts", "qc_reports")

NODE_KINDS = {"input", "stage", "gate", "artifact", "output"}
EDGE_KINDS = {"produces", "data", "sequence"}

# Maps each pipeline skill to the batch-manifest artifacts key it fills and the
# artifact_type marker that detect_current_state.artifact_present() detects.
SKILL_ARTIFACTS = {
    "product-identity-archive": ("product_identity_archive", "product_identity_archive"),
    "style-master-extractor": ("style_master", "style_master"),
    "angle-inventory": ("angle_inventory", "angle_inventory"),
    "set-product-identity": ("set_product_identity", "set_product_identity"),
    "set-angle-layout-inventory": ("set_angle_layout_inventory", "set_angle_layout_inventory"),
    "main-variable-config": ("main_variable_configs", "main_variable_config"),
    "detail-variable-config": ("detail_variable_configs", "detail_variable_config"),
    "final-prompt-compiler": ("final_prompts", "final_prompt"),
    "qc-inspector": ("qc_reports", "qc_report"),
}

MANIFEST_ARTIFACT_KEYS = [
    "asset_manifest",
    "product_identity_archive",
    "style_master",
    "angle_inventory",
    "main_variable_configs",
    "detail_variable_configs",
    "set_product_identity",
    "set_angle_layout_inventory",
    "final_prompts",
    "comfyui_jobs",
    "qc_reports",
]


def load_graph() -> dict:
    return json.loads(GRAPH_PATH.read_text(encoding="utf-8"))


def summary(file_count: int = 0, typed: dict | None = None) -> dict:
    return {"paths": [], "file_count": file_count, "typed_artifact_counts": dict(typed or {})}


def condition_active(condition: dict | None, *, set_enabled: bool, requested) -> bool:
    if not condition:
        return True
    if condition["when"] == "set_enabled":
        return set_enabled
    if condition["when"] == "requested_output":
        return condition["requested_output"] in requested
    raise AssertionError(f"unknown condition: {condition}")


def active_subgraph(graph: dict, *, set_enabled: bool, requested):
    nodes = {
        node["id"]: node
        for node in graph["nodes"]
        if condition_active(node.get("condition"), set_enabled=set_enabled, requested=requested)
    }
    edges = [
        edge
        for edge in graph["edges"]
        if condition_active(edge.get("condition"), set_enabled=set_enabled, requested=requested)
        and edge["from"] in nodes
        and edge["to"] in nodes
    ]
    return nodes, edges


def topological_order(nodes: dict, edges: list) -> list[str]:
    pending = {node_id: set() for node_id in nodes}
    outgoing = {node_id: set() for node_id in nodes}
    for edge in edges:
        pending[edge["to"]].add(edge["from"])
        outgoing[edge["from"]].add(edge["to"])
    ready = sorted(node_id for node_id, deps in pending.items() if not deps)
    seen = set(ready)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(outgoing[current]):
            pending[target].discard(current)
            if not pending[target] and target not in seen:
                seen.add(target)
                ready.append(target)
        ready.sort()
    return order


def graph_skill_order(graph: dict, *, set_enabled: bool, requested) -> list[str]:
    nodes, edges = active_subgraph(graph, set_enabled=set_enabled, requested=requested)
    order = topological_order(nodes, edges)
    if len(order) != len(nodes):
        raise AssertionError("workflow graph contains a cycle")
    return [nodes[node_id]["skill"] for node_id in order if nodes[node_id].get("skill")]


def run_route_batch(produced_keys: set[str], *, batch_type: str, user_declared_set_product: bool, requested) -> dict:
    manifest = {
        "batch_type": batch_type,
        "user_declared_set_product": user_declared_set_product,
        "requested_outputs": list(requested),
    }
    inputs = {
        "white_bg_images": summary(file_count=2),
        "style_reference_images": summary(file_count=2),
        "set_group_images": summary(file_count=2),
        "component_white_bg_images": summary(),
    }
    drafts = {
        "product_identity_draft": summary(),
        "style_master_draft": summary(),
    }
    typed_by_key = {key: typed for key, typed in SKILL_ARTIFACTS.values()}
    artifacts = {}
    for key in MANIFEST_ARTIFACT_KEYS:
        typed_type = typed_by_key.get(key)
        present = typed_type is not None and key in produced_keys
        artifacts[key] = summary(file_count=1 if present else 0, typed={typed_type: 1} if present else None)
    renders = 1 if "final_prompts" in produced_keys else 0
    outputs = {"renders": summary(file_count=renders), "repaired": summary()}
    manifest_path = ROOT / "manifests" / "graph_projection_test.batch_manifest.json"
    return detect_current_state.route_batch(
        "graph_projection_test",
        manifest_path,
        manifest,
        inputs,
        drafts,
        artifacts,
        outputs,
    )


class WorkflowGraphStructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = load_graph()

    def test_top_level_shape(self) -> None:
        self.assertEqual("workflow_graph_template", self.graph["artifact_type"])
        self.assertIsInstance(self.graph["graph_version"], int)
        self.assertGreaterEqual(self.graph["graph_version"], 1)
        self.assertTrue(self.graph["graph_id"])

    def test_nodes_and_edges_are_well_formed(self) -> None:
        ids = [node["id"] for node in self.graph["nodes"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate node ids")
        port_types = set(self.graph["port_types"])
        by_id = {node["id"]: node for node in self.graph["nodes"]}
        for node in self.graph["nodes"]:
            self.assertIn(node["kind"], NODE_KINDS, node["id"])
            if node["kind"] in {"stage", "gate"}:
                self.assertIn("executor", node, node["id"])
            else:
                self.assertIn(node["artifact_type"], port_types, node["id"])
        for edge in self.graph["edges"]:
            self.assertIn(edge["from"], by_id, str(edge))
            self.assertIn(edge["to"], by_id, str(edge))
            self.assertIn(edge["port"], port_types, str(edge))
            self.assertIn(edge["edge_kind"], EDGE_KINDS, str(edge))

    def test_graph_is_acyclic_in_widest_configuration(self) -> None:
        nodes, edges = active_subgraph(self.graph, set_enabled=True, requested=ALL_REQUESTED)
        order = topological_order(nodes, edges)
        self.assertEqual(len(nodes), len(order), "cycle detected in workflow graph")

    def test_references_exist_in_repository(self) -> None:
        batch_template = json.loads(BATCH_TEMPLATE_PATH.read_text(encoding="utf-8"))
        for node in self.graph["nodes"]:
            if node.get("schema_ref"):
                self.assertTrue((ROOT / node["schema_ref"]).is_file(), node["schema_ref"])
            if node.get("script"):
                self.assertTrue((ROOT / node["script"]).is_file(), node["script"])
            if node.get("skill"):
                self.assertIn(node["skill"], detect_current_state.ALL_SKILLS, node["id"])
                skill_md = ROOT / ".agents" / "skills" / node["skill"] / "SKILL.md"
                self.assertTrue(skill_md.is_file(), str(skill_md))
            if node.get("manifest_key"):
                section = batch_template[node["manifest_section"]]
                self.assertIn(node["manifest_key"], section, node["id"])

    def test_artifact_type_markers_match_detector(self) -> None:
        for key, typed in SKILL_ARTIFACTS.values():
            self.assertIn(typed, detect_current_state.ARTIFACT_TYPES[key], key)


class WorkflowGraphRouteBatchConsistencyTest(unittest.TestCase):
    def simulated_skill_sequence(self, *, batch_type: str, user_declared_set_product: bool, requested) -> list[str]:
        produced: set[str] = set()
        sequence: list[str] = []
        for _ in range(len(SKILL_ARTIFACTS) + 2):
            result = run_route_batch(
                produced,
                batch_type=batch_type,
                user_declared_set_product=user_declared_set_product,
                requested=requested,
            )
            skill = result["next_required_skill"]
            if skill is None:
                self.assertEqual([], result["blocked_reasons"], result["blocked_reasons"])
                self.assertEqual("ready", result["current_stage"])
                return sequence
            self.assertEqual([], result["blocked_reasons"], f"unexpected block before {skill}: {result['blocked_reasons']}")
            key, _typed = SKILL_ARTIFACTS[skill]
            self.assertNotIn(key, produced, f"route_batch repeated stage {skill}")
            produced.add(key)
            sequence.append(skill)
        self.fail("route_batch did not converge to a terminal state")

    def test_single_product_skill_order_matches_route_batch(self) -> None:
        expected = [
            "product-identity-archive",
            "style-master-extractor",
            "angle-inventory",
            "main-variable-config",
            "detail-variable-config",
            "final-prompt-compiler",
            "qc-inspector",
        ]
        graph = load_graph()
        self.assertEqual(expected, graph_skill_order(graph, set_enabled=False, requested=ALL_REQUESTED))
        self.assertEqual(
            expected,
            self.simulated_skill_sequence(batch_type="single", user_declared_set_product=False, requested=ALL_REQUESTED),
        )

    def test_set_product_skill_order_matches_route_batch(self) -> None:
        expected = [
            "product-identity-archive",
            "style-master-extractor",
            "angle-inventory",
            "set-product-identity",
            "set-angle-layout-inventory",
            "main-variable-config",
            "detail-variable-config",
            "final-prompt-compiler",
            "qc-inspector",
        ]
        graph = load_graph()
        self.assertEqual(expected, graph_skill_order(graph, set_enabled=True, requested=ALL_REQUESTED))
        self.assertEqual(
            expected,
            self.simulated_skill_sequence(batch_type="set", user_declared_set_product=True, requested=ALL_REQUESTED),
        )


if __name__ == "__main__":
    unittest.main()
