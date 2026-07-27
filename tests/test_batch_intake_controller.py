from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_intake_controller import (  # noqa: E402
    BatchIntakeGateError,
    parse_queued_request,
    queued_info_nodes,
)
from batch_intake_contract import batch_intake_contract_sha256  # noqa: E402


NOW_MS = 20_000
REQUEST_ID = "batch-req-0001"
IMAGE_BYTES = b"offline-original-image"
IMAGE_SHA256 = hashlib.sha256(IMAGE_BYTES).hexdigest()
CONTRACT_HASH = batch_intake_contract_sha256(ROOT)
FACTS = {
    "product_type": "杯子",
    "length_cm": None,
    "width_cm": None,
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}


def command(request_id: str = REQUEST_ID, requested_at: int = NOW_MS - 1_000) -> str:
    return (
        "# batch-intake\n"
        f"# request-id: {request_id}\n"
        f"# requested-at: {requested_at}\n"
        "build: batch"
    )


def info_node(
    *,
    request_id: str = REQUEST_ID,
    requested_at: object = NOW_MS - 1_000,
    facts: object | None = None,
    content: str | None = None,
    status: str = "queued",
    node_id: str = "info-1",
    category: object = "杯类",
    contract_hash: object = CONTRACT_HASH,
) -> dict:
    return {
        "id": node_id,
        "type": "batch-info",
        "title": "批次信息卡",
        "metadata": {
            "content": command(request_id, requested_at) if content is None else content,
            "batchIntake": {
                "status": status,
                "requestId": request_id,
                "requestedAt": requested_at,
                "category": category,
                "contractHash": contract_hash,
                "facts": copy.deepcopy(FACTS if facts is None else facts),
            },
        },
    }


def image_node(
    node_id: str = "image-1",
    *,
    name: str = "原图一.png",
    sha256: str = IMAGE_SHA256,
    storage_key: str = "image:stored-original-1",
    size: object = len(IMAGE_BYTES),
    mime_type: object = "image/png",
    last_modified: object = 1_720_000_000_000,
) -> dict:
    return {
        "id": node_id,
        "type": "image",
        "title": name,
        "metadata": {
            "storageKey": storage_key,
            "sourceFile": {
                "name": name,
                "size": size,
                "type": mime_type,
                "lastModified": last_modified,
                "sha256": sha256,
            },
        },
    }


def workflow_node(node_id: str = "workflow-1") -> dict:
    return {"id": node_id, "type": "workflow", "title": "生图工作流", "metadata": {}}


def valid_state(*, info: dict | None = None, images: list[dict] | None = None) -> dict:
    info = info or info_node()
    images = images or [image_node()]
    workflow = workflow_node()
    return {
        "nodes": [info, workflow, *images],
        "connections": [
            {"id": "info-to-machine", "fromNodeId": info["id"], "toNodeId": workflow["id"]},
            *[
                {"id": f"{image['id']}-to-machine", "fromNodeId": image["id"], "toNodeId": workflow["id"]}
                for image in images
            ],
        ],
        "viewport": {"x": 0, "y": 0, "k": 1},
    }


class BatchIntakeControllerTests(unittest.TestCase):
    def parse(self, state: dict | None = None, node: dict | None = None, **kwargs):
        state = state or valid_state(info=node)
        node = node or state["nodes"][0]
        return parse_queued_request(state, node, now_ms=NOW_MS, **kwargs)

    def assert_gate(self, code: str, state: dict, node: dict | None = None) -> BatchIntakeGateError:
        with self.assertRaises(BatchIntakeGateError) as caught:
            self.parse(state, node)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_valid_request_returns_typed_exact_facts_and_original_sources(self) -> None:
        second = image_node(
            "image-2",
            name="餐具背面.JPG",
            sha256="a" * 64,
            storage_key="image:stored-original-2",
            size=123,
            mime_type="image/jpeg",
        )
        request = self.parse(valid_state(images=[image_node(), second]))

        self.assertEqual(REQUEST_ID, request.request_id)
        self.assertEqual(NOW_MS - 1_000, request.requested_at)
        self.assertEqual("info-1", request.info_node_id)
        self.assertEqual("workflow-1", request.workflow_node_id)
        self.assertEqual("杯类", request.category)
        self.assertEqual(CONTRACT_HASH, request.contract_hash)
        self.assertEqual(FACTS, request.facts.as_dict())
        self.assertEqual(["image-1", "image-2"], [source.node_id for source in request.source_images])
        self.assertEqual(IMAGE_SHA256, request.source_images[0].expected_sha256)
        self.assertEqual("原图一.png", request.source_images[0].name)
        self.assertEqual("image:stored-original-1", request.source_images[0].storage_key)

    def test_route_payload_round_trip_preserves_chinese_and_exact_values(self) -> None:
        request = self.parse()
        encoded = json.dumps(request.route_dict(), ensure_ascii=False).encode("utf-8")
        decoded = json.loads(encoded.decode("utf-8"))

        self.assertIn("杯子".encode("utf-8"), encoded)
        self.assertEqual("杯子", decoded["facts"]["product_type"])
        self.assertEqual("原图一.png", decoded["sourceImages"][0]["name"])
        self.assertEqual(REQUEST_ID, decoded["requestId"])

    def test_queued_info_nodes_selects_only_queued_batch_info_cards(self) -> None:
        queued = info_node(node_id="queued")
        idle = info_node(node_id="idle", status="idle")
        wrong_type = info_node(node_id="text-node")
        wrong_type["type"] = "text"
        state = {"nodes": [idle, wrong_type, queued, workflow_node()], "connections": []}

        self.assertEqual((queued,), queued_info_nodes(state))

    def test_command_requires_one_exact_build_batch_verb(self) -> None:
        invalid_contents = (
            command().replace("build: batch", "run: batch"),
            command().replace("build: batch", "build: images"),
            command() + "\nbuild: batch",
            command() + "\nretry: renders",
            "build: batch",
        )
        for content in invalid_contents:
            with self.subTest(content=content):
                node = info_node(content=content)
                error = self.assert_gate("invalid_command", valid_state(info=node), node)
                self.assertEqual("info-1", error.info_node_id)
                self.assertEqual(REQUEST_ID, error.request_id)

    def test_command_headers_must_match_metadata_without_echoing_values(self) -> None:
        secret = "secret-token-that-must-not-echo"
        mismatches = (
            command(request_id=secret),
            command(requested_at=NOW_MS - 2_000),
        )
        for content in mismatches:
            with self.subTest(content=content):
                node = info_node(content=content)
                error = self.assert_gate("invalid_command", valid_state(info=node), node)
                self.assertNotIn(secret, str(error))

    def test_request_id_has_safe_bounded_format(self) -> None:
        invalid_ids = ("", "short", "含中文请求", "../request-id", "x" * 65, True, 123)
        for request_id in invalid_ids:
            with self.subTest(request_id=request_id):
                node = info_node(request_id=request_id)  # type: ignore[arg-type]
                self.assert_gate("invalid_request", valid_state(info=node), node)

    def test_timestamp_rejects_bool_future_and_stale_boundary(self) -> None:
        invalid_times = (True, "19000", NOW_MS + 1, NOW_MS - 8_000, NOW_MS - 80_000)
        for requested_at in invalid_times:
            with self.subTest(requested_at=requested_at):
                node = info_node(requested_at=requested_at)
                self.assert_gate("expired_request", valid_state(info=node), node)

        fresh = info_node(requested_at=NOW_MS - 7_999)
        self.assertEqual(NOW_MS - 7_999, self.parse(valid_state(info=fresh), fresh).requested_at)

    def test_future_tolerance_is_explicit_and_defaults_to_strict(self) -> None:
        node = info_node(requested_at=NOW_MS + 10)
        with self.assertRaises(BatchIntakeGateError):
            self.parse(valid_state(info=node), node)
        request = self.parse(valid_state(info=node), node, future_tolerance_ms=10)
        self.assertEqual(NOW_MS + 10, request.requested_at)

    def test_facts_require_exact_nine_keys_and_types(self) -> None:
        invalid = (
            {key: value for key, value in FACTS.items() if key != "height_cm"},
            {**FACTS, "extra": "private"},
            {**FACTS, "product_type": "   "},
            {**FACTS, "height_cm": True},
            {**FACTS, "height_cm": 0},
            {**FACTS, "handheld_main": 7},
            {**FACTS, "handheld_detail": 9},
            {**FACTS, "allow_clear_water": 1},
            {**FACTS, "forbid_pouring_and_heating": "是"},
            {**FACTS, "missing_d_no_retake": None},
        )
        for facts in invalid:
            with self.subTest(facts=facts):
                node = info_node(facts=facts)
                error = self.assert_gate("invalid_facts", valid_state(info=node), node)
                self.assertNotIn("private", str(error))

    def test_payload_contract_hash_must_match_backend(self) -> None:
        for value in (None, "", "0" * 64):
            with self.subTest(value=value):
                node = info_node(contract_hash=value)
                self.assert_gate("contract_mismatch", valid_state(info=node), node)

    def test_info_card_must_connect_to_exactly_one_workflow(self) -> None:
        no_workflow = valid_state()
        no_workflow["connections"] = [
            connection for connection in no_workflow["connections"] if connection["fromNodeId"] != "info-1"
        ]
        self.assert_gate("invalid_connection", no_workflow)

        two_workflows = valid_state()
        two_workflows["nodes"].append(workflow_node("workflow-2"))
        two_workflows["connections"].append(
            {"id": "ambiguous", "fromNodeId": "info-1", "toNodeId": "workflow-2"}
        )
        self.assert_gate("invalid_connection", two_workflows)

    def test_workflow_must_have_exactly_one_connected_info_card(self) -> None:
        state = valid_state()
        other = info_node(node_id="info-2", status="idle")
        state["nodes"].append(other)
        state["connections"].append(
            {"id": "other-info", "fromNodeId": "info-2", "toNodeId": "workflow-1"}
        )
        self.assert_gate("invalid_connection", state)

    def test_at_least_one_directly_connected_original_image_is_required(self) -> None:
        state = valid_state()
        state["connections"] = [state["connections"][0]]
        self.assert_gate("missing_images", state)

    def test_connected_derived_image_without_source_file_is_rejected(self) -> None:
        derived = image_node()
        del derived["metadata"]["sourceFile"]
        error = self.assert_gate("derived_image", valid_state(images=[derived]))
        self.assertIn("磁盘原图", error.user_message)

    def test_original_image_requires_storage_and_exact_source_metadata(self) -> None:
        mutations = (
            ("storageKey", ""),
            ("storageKey", "blob:not-browser-storage"),
            ("name", "../原图.png"),
            ("size", True),
            ("size", 0),
            ("type", "text/plain"),
            ("lastModified", True),
            ("lastModified", -1),
            ("sha256", "a" * 63),
            ("sha256", "g" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                image = image_node()
                if key == "storageKey":
                    image["metadata"][key] = value
                else:
                    image["metadata"]["sourceFile"][key] = value
                self.assert_gate("invalid_image", valid_state(images=[image]))

    def test_sha256_is_normalized_to_lowercase(self) -> None:
        image = image_node(sha256=IMAGE_SHA256.upper())
        request = self.parse(valid_state(images=[image]))
        self.assertEqual(IMAGE_SHA256, request.source_images[0].expected_sha256)

    def test_duplicate_node_ids_or_case_insensitive_filenames_are_rejected(self) -> None:
        duplicates = (
            [image_node("same"), image_node("same", name="二.png", storage_key="image:2")],
            [image_node("one", name="原图.PNG"), image_node("two", name="原图.png", storage_key="image:2")],
        )
        for images in duplicates:
            with self.subTest(images=images):
                self.assert_gate("duplicate_image", valid_state(images=images))

    def test_malformed_canvas_state_is_rejected_as_human_safe_error(self) -> None:
        for state in ({}, {"nodes": "private-node-value", "connections": []}, {"nodes": [], "connections": {}}):
            with self.subTest(state=state):
                node = info_node()
                with self.assertRaises(BatchIntakeGateError) as caught:
                    parse_queued_request(state, node, now_ms=NOW_MS)
                self.assertNotIn("private-node-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
