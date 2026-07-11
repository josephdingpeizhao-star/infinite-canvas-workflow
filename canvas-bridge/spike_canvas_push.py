"""Stage-0 spike driver: push workflow projections into infinite-canvas.

Usage examples (run from repository root):

    python canvas-bridge/spike_canvas_push.py --health
    python canvas-bridge/spike_canvas_push.py --push-batch tests/fixtures/external_workspace_batch_manifest.fixture.json
    python canvas-bridge/spike_canvas_push.py --stress 300
    python canvas-bridge/spike_canvas_push.py --status-demo
    python canvas-bridge/spike_canvas_push.py --image-url http://127.0.0.1:8801/spike.svg
    python canvas-bridge/spike_canvas_push.py --get-state --save-layout out.json
    python canvas-bridge/spike_canvas_push.py --clear-mine
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import batch_editor
import ic_client
import layout_store
import projector
import run_controller
import state_reader

STRESS_PREFIX = "wfstress:"
IMAGE_PREFIX = "wfimg:"
MINE_PREFIXES = (
    f"{projector.NODE_ID_PREFIX}:",
    STRESS_PREFIX,
    IMAGE_PREFIX,
    f"{batch_editor.EDITOR_PREFIX}:",
    f"{run_controller.RUN_PREFIX}:",
    f"{run_controller.LOG_PREFIX}:",
)


def build_live_view(manifest_path: Path):
    graph = projector.load_graph()
    batch = projector.load_batch_manifest(manifest_path)
    route = state_reader.read_batch_route(manifest_path)
    integrity = state_reader.integrity_report_status(route)
    view = projector.node_runtime_view(graph, batch, route, integrity)
    return graph, batch, route, view


def resolve_layout(batch: dict, layout_path: Path | None) -> tuple[Path, dict | None]:
    product_id = str(batch.get("product_id") or "unknown")
    target = layout_path or layout_store.default_layout_path(product_id)
    return target, layout_store.load_layout(target)


def build_full_projection(manifest_path: Path, layout_path: Path | None):
    """Full-projection ops: graph nodes + editor node + run console + event log."""
    graph, batch, route, view = build_live_view(manifest_path)
    product_id = str(batch.get("product_id") or "unknown")
    integrity = state_reader.integrity_report_status(route)
    target, layout = resolve_layout(batch, layout_path)
    journal = run_controller.journal_path(manifest_path, product_id)
    ops = projector.project_batch(graph, batch, view=view, layout=layout)
    ops.append(
        {
            "type": "delete_node",
            "ids": [
                batch_editor.editor_node_id(product_id),
                run_controller.run_node_id(product_id),
                run_controller.log_node_id(product_id),
            ],
        }
    )
    ops.append(batch_editor.editor_node_op(product_id, batch))
    ops.append(run_controller.run_node_op(product_id, route, integrity))
    ops.append(run_controller.log_node_op(product_id, run_controller.read_journal_tail(journal)))
    return product_id, batch, route, integrity, view, layout, target, journal, ops


def control_update_ops(
    product_id: str,
    route: dict,
    integrity: dict,
    journal: Path,
    *,
    note: str = "",
    status: str = "idle",
    error: str = "",
) -> list[dict]:
    """update_node refreshes for the run console + event log (position preserved)."""
    return [
        {
            "type": "update_node",
            "id": run_controller.run_node_id(product_id),
            "metadata": {
                "content": run_controller.render_run_content(route, integrity, note),
                "status": status,
                "errorDetails": error,
            },
        },
        {
            "type": "update_node",
            "id": run_controller.log_node_id(product_id),
            "metadata": {"content": run_controller.render_log_content(run_controller.read_journal_tail(journal))},
        },
    ]


def cmd_push_live(manifest_path: Path, layout_path: Path | None = None, restore_viewport: bool = False) -> None:
    product_id, _batch, route, _integrity, _view, layout, target, _journal, ops = build_full_projection(
        manifest_path, layout_path
    )
    ic_client.apply_ops(ops)
    if restore_viewport and layout and layout.get("viewport"):
        ic_client.apply_ops([{"type": "set_viewport", "viewport": layout["viewport"]}])
    print(
        json.dumps(
            {
                "pushed_nodes": sum(1 for op in ops if op["type"] == "add_node"),
                "current_stage": route.get("current_stage"),
                "next_required_skill": route.get("next_required_skill"),
                "blocked_reasons": route.get("blocked_reasons"),
                "available_artifacts": route.get("available_artifacts"),
                "layout_applied": bool(layout),
                "layout_path": str(target),
            },
            ensure_ascii=False,
        )
    )


def cmd_layout_save(manifest_path: Path, layout_path: Path | None = None) -> None:
    graph = projector.load_graph()
    batch = projector.load_batch_manifest(manifest_path)
    product_id = str(batch.get("product_id") or "unknown")
    state = ic_client.call_tool("canvas_get_state")
    layout = layout_store.build_layout(
        product_id,
        str(graph.get("graph_id") or ""),
        state.get("nodes") or [],
        state.get("viewport"),
    )
    target = layout_path or layout_store.default_layout_path(product_id)
    layout_store.save_layout(target, layout)
    print(json.dumps({"layout_saved": str(target), "node_count": len(layout["nodes"])}, ensure_ascii=False))


def cmd_apply_edits(manifest_path: Path, layout_path: Path | None = None, restore_viewport: bool = False) -> None:
    manifest = projector.load_batch_manifest(manifest_path)
    product_id = str(manifest.get("product_id") or "unknown")
    node_id = batch_editor.editor_node_id(product_id)
    state = ic_client.call_tool("canvas_get_state")
    node = next((item for item in state.get("nodes") or [] if item.get("id") == node_id), None)
    if not node:
        print(json.dumps({"applied": False, "error": f"画布上没有批次配置节点 {node_id}，请先 --push-live"}, ensure_ascii=False))
        raise SystemExit(1)
    content = str((node.get("metadata") or {}).get("content") or "")
    try:
        fields = batch_editor.parse_editor_content(content)
        result = batch_editor.apply_edits(manifest_path, fields)
    except batch_editor.EditValidationError as exc:
        ic_client.apply_ops([
            {"type": "update_node", "id": node_id, "metadata": {"status": "error", "errorDetails": str(exc)}}
        ])
        print(json.dumps({"applied": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    cmd_push_live(manifest_path, layout_path, restore_viewport)
    print(json.dumps({"applied": True, **result}, ensure_ascii=False))


def cmd_watch(manifest_path: Path, interval: float, layout_path: Path | None = None) -> None:
    product_id, _batch, route, _integrity, view, _layout, _target, _journal, initial_ops = build_full_projection(
        manifest_path, layout_path
    )
    ic_client.apply_ops(initial_ops)
    print(json.dumps({"watch": "started", "interval": interval, "current_stage": route.get("current_stage")}, ensure_ascii=False), flush=True)
    previous = view
    try:
        while True:
            time.sleep(interval)
            _graph, _batch, route, current = build_live_view(manifest_path)
            ops = projector.runtime_update_ops(product_id, previous, current)
            if ops:
                ic_client.apply_ops(ops)
                print(
                    json.dumps(
                        {
                            "changed_nodes": len(ops),
                            "current_stage": route.get("current_stage"),
                            "next_required_skill": route.get("next_required_skill"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            previous = current
    except KeyboardInterrupt:
        print(json.dumps({"watch": "stopped"}, ensure_ascii=False))


def cmd_serve(
    manifest_path: Path,
    interval: float,
    layout_path: Path | None = None,
    executor_name: str = "demo",
) -> None:
    """Phase 4 daemon: full projection, then poll the run console for commands
    and mirror manifest changes incrementally (three gates per command)."""
    product_id, batch, route, integrity, view, _layout, _target, journal, ops = build_full_projection(
        manifest_path, layout_path
    )
    executor = run_controller.build_executor(executor_name, batch)
    while True:
        try:
            ic_client.apply_ops(ops)
            break
        except ic_client.CanvasAgentError as exc:
            print(json.dumps({"serve": "waiting_canvas", "error": str(exc)[:120]}, ensure_ascii=False), flush=True)
            time.sleep(max(interval, 3.0))
    print(
        json.dumps(
            {
                "serve": "started",
                "interval": interval,
                "executor": executor_name,
                "current_stage": route.get("current_stage"),
                "journal": str(journal),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    previous = view
    consumed_content: str | None = None
    try:
        while True:
            time.sleep(interval)
            try:
                state = ic_client.call_tool("canvas_get_state")
            except ic_client.CanvasAgentError as exc:
                print(json.dumps({"serve": "waiting_canvas", "error": str(exc)[:120]}, ensure_ascii=False), flush=True)
                continue

            run_id = run_controller.run_node_id(product_id)
            node = next((item for item in state.get("nodes") or [] if item.get("id") == run_id), None)
            if node is None:
                # Self-heal: someone deleted the control nodes; re-add them.
                ic_client.apply_ops(
                    [
                        run_controller.run_node_op(product_id, route, integrity),
                        run_controller.log_node_op(product_id, run_controller.read_journal_tail(journal)),
                    ]
                )
                continue

            content = str((node.get("metadata") or {}).get("content") or "")
            command = None
            parse_error: run_controller.RunValidationError | None = None
            if content != consumed_content:
                try:
                    command = run_controller.parse_run_content(content)
                except run_controller.RunValidationError as exc:
                    parse_error = exc
                if command is None and parse_error is None:
                    consumed_content = None  # idle template observed; accept future commands

            if command or parse_error:
                consumed_content = content
                _graph, batch, route, current = build_live_view(manifest_path)
                integrity = state_reader.integrity_report_status(route)
                error = parse_error
                step = None
                if command and not error:
                    try:
                        step = run_controller.resolve_command(command, route, integrity)
                    except run_controller.RunValidationError as exc:
                        error = exc
                if error:
                    detail = str(error)
                    command_text = f"{command[0]}: {command[1]}" if command else ""
                    run_controller.append_event(journal, "gate_rejected", command=command_text, detail=detail)
                    update_ops = projector.runtime_update_ops(product_id, previous, current)
                    update_ops += control_update_ops(
                        product_id, route, integrity, journal, note=f"🚫 {detail}", status="error", error=detail
                    )
                    ic_client.apply_ops(update_ops)
                    previous = current
                    print(json.dumps({"gate_rejected": detail}, ensure_ascii=False), flush=True)
                    continue

                verb, target_name = command  # type: ignore[misc]
                run_controller.append_event(journal, "step_started", step=step, command=f"{verb}: {target_name}")
                stage_canvas_id = projector.canvas_node_id(product_id, run_controller.STEP_GRAPH_NODES[step])
                loading_ops = control_update_ops(
                    product_id, route, integrity, journal, note=f"⏳ 正在执行 {step} …", status="loading"
                )
                loading_ops.append({"type": "update_node", "id": stage_canvas_id, "metadata": {"status": "loading"}})
                ic_client.apply_ops(loading_ops)

                started = time.monotonic()
                try:
                    run_detail = executor.run(step)
                except run_controller.RunExecutionError as exc:
                    run_controller.append_event(journal, "step_failed", step=step, detail=str(exc))
                    note, status, error_text = f"✘ {step} 失败：{exc}", "error", str(exc)
                    result_log = {"step": step, "result": "failed", "detail": str(exc)}
                else:
                    elapsed = f"{time.monotonic() - started:.1f}s"
                    run_controller.append_event(journal, "step_succeeded", step=step, detail=f"{run_detail}（{elapsed}）")
                    note, status, error_text = f"✔ {step} 完成（{elapsed}）", "success", ""
                    result_log = {"step": step, "result": "succeeded", "elapsed": elapsed}

                _graph, batch, route, current = build_live_view(manifest_path)
                integrity = state_reader.integrity_report_status(route)
                update_ops = projector.runtime_update_ops(product_id, previous, current)
                # The stage node was forced to loading outside the view diff;
                # when its view entry is unchanged (e.g. retry of a completed
                # step) the diff is empty, so always restore it explicitly.
                entry = current.get(run_controller.STEP_GRAPH_NODES[step])
                if entry:
                    update_ops.append(
                        {
                            "type": "update_node",
                            "id": stage_canvas_id,
                            "patch": {"title": entry["title"]},
                            "metadata": {
                                "status": entry["status"] or "idle",
                                "content": entry["content"],
                                "errorDetails": entry["errorDetails"],
                            },
                        }
                    )
                update_ops += control_update_ops(
                    product_id, route, integrity, journal, note=note, status=status, error=error_text
                )
                ic_client.apply_ops(update_ops)
                previous = current
                print(
                    json.dumps(
                        {
                            **result_log,
                            "current_stage": route.get("current_stage"),
                            "next_required_skill": route.get("next_required_skill"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue

            # Plain watch tick: mirror external manifest/workspace changes.
            _graph, batch, route, current = build_live_view(manifest_path)
            integrity = state_reader.integrity_report_status(route)
            update_ops = projector.runtime_update_ops(product_id, previous, current)
            if update_ops:
                update_ops += control_update_ops(product_id, route, integrity, journal)
                ic_client.apply_ops(update_ops)
                print(
                    json.dumps(
                        {"changed_nodes": len(update_ops), "current_stage": route.get("current_stage")},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            previous = current
    except KeyboardInterrupt:
        print(json.dumps({"serve": "stopped"}, ensure_ascii=False))


def cmd_health() -> None:
    print(json.dumps(ic_client.health(), ensure_ascii=False))


def cmd_push_batch(manifest_path: Path) -> None:
    graph = projector.load_graph()
    batch = projector.load_batch_manifest(manifest_path)
    ops = projector.project_batch(graph, batch)
    chunks = ic_client.apply_ops(ops)
    adds = sum(1 for op in ops if op["type"] == "add_node")
    connects = sum(1 for op in ops if op["type"] == "connect_nodes")
    print(json.dumps({"pushed_nodes": adds, "pushed_connections": connects, "chunks": chunks}, ensure_ascii=False))


def cmd_stress(count: int, columns: int = 20) -> None:
    started = time.perf_counter()
    ops = [{"type": "delete_node", "ids": [f"{STRESS_PREFIX}{index}" for index in range(count)]}]
    for index in range(count):
        column = index % columns
        row = index // columns
        ops.append(
            {
                "type": "add_node",
                "id": f"{STRESS_PREFIX}{index}",
                "nodeType": "text",
                "title": f"压测节点 {index + 1}",
                "position": {"x": 60 + column * 320, "y": 900 + row * 150},
                "width": 280,
                "height": 100,
                "metadata": {"content": f"stress node {index + 1}/{count}", "fontSize": 12},
            }
        )
    for index in range(count - 1):
        if index % columns != columns - 1:
            ops.append({"type": "connect_nodes", "fromNodeId": f"{STRESS_PREFIX}{index}", "toNodeId": f"{STRESS_PREFIX}{index + 1}"})
    chunks = ic_client.apply_ops(ops)
    elapsed = round(time.perf_counter() - started, 2)
    print(json.dumps({"stress_nodes": count, "chunks": chunks, "push_seconds": elapsed}, ensure_ascii=False))


def cmd_status_demo(manifest_path: Path, hold_seconds: float) -> None:
    graph = projector.load_graph()
    batch = projector.load_batch_manifest(manifest_path)
    for node_id in projector.stage_node_ids(graph, batch):
        ic_client.apply_ops([{"type": "update_node", "id": node_id, "metadata": {"status": "loading"}}])
        time.sleep(hold_seconds)
        ic_client.apply_ops([{"type": "update_node", "id": node_id, "metadata": {"status": "success"}}])
        print(f"status cycled: {node_id}")


def cmd_image_url(url: str, title: str) -> None:
    ops = [
        {"type": "delete_node", "ids": [f"{IMAGE_PREFIX}demo"]},
        {
            "type": "add_node",
            "id": f"{IMAGE_PREFIX}demo",
            "nodeType": "image",
            "title": title,
            "position": {"x": 80, "y": -320},
            "width": 360,
            "height": 240,
            "metadata": {
                "content": url,
                "status": "success",
                "mimeType": "image/svg+xml",
                "naturalWidth": 360,
                "naturalHeight": 240,
            },
        },
    ]
    ic_client.apply_ops(ops)
    print(json.dumps({"image_node": f"{IMAGE_PREFIX}demo", "url": url}, ensure_ascii=False))


def cmd_get_state(save_layout: Path | None) -> None:
    state = ic_client.call_tool("canvas_get_state")
    nodes = state.get("nodes") or []
    connections = state.get("connections") or []
    mine = [node for node in nodes if str(node.get("id", "")).startswith(MINE_PREFIXES)]
    print(
        json.dumps(
            {
                "nodes_total": len(nodes),
                "connections_total": len(connections),
                "nodes_mine": len(mine),
                "viewport": state.get("viewport"),
            },
            ensure_ascii=False,
        )
    )
    if save_layout:
        layout = {
            "layout_version": 1,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes": [
                {
                    "id": node.get("id"),
                    "position": node.get("position"),
                    "width": node.get("width"),
                    "height": node.get("height"),
                }
                for node in mine
            ],
        }
        save_layout.write_text(json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"layout saved: {save_layout} ({len(layout['nodes'])} nodes)")


def cmd_clear_mine() -> None:
    state = ic_client.call_tool("canvas_get_state")
    ids = [str(node.get("id")) for node in state.get("nodes") or [] if str(node.get("id", "")).startswith(MINE_PREFIXES)]
    if ids:
        ic_client.apply_ops([{"type": "delete_node", "ids": ids}])
    print(json.dumps({"deleted": len(ids)}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Spike driver: project workflow state into infinite-canvas.")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--push-batch", type=Path, metavar="MANIFEST")
    parser.add_argument("--push-live", type=Path, metavar="MANIFEST", help="投影真实批次状态（阶段1只读画布）")
    parser.add_argument("--watch", type=Path, metavar="MANIFEST", help="轮询批次状态并增量更新画布")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--apply-edits", type=Path, metavar="MANIFEST", help="读取画布上的批次配置节点，三段校验后写回 manifest（阶段3）")
    parser.add_argument("--serve", type=Path, metavar="MANIFEST", help="常驻：轮询画布运行台命令并执行 + 增量投影（阶段4）")
    parser.add_argument("--executor", default="demo", help="--serve 使用的执行器（阶段4仅内置 demo）")
    parser.add_argument("--layout-save", type=Path, metavar="MANIFEST", help="把当前画布布局保存为 canvas_layout 文件（阶段2）")
    parser.add_argument("--layout-path", type=Path, help="布局文件路径，默认 manifests/<product_id>.canvas_layout.json")
    parser.add_argument("--restore-viewport", action="store_true", help="push-live 时恢复布局文件中的视口")
    parser.add_argument("--stress", type=int, metavar="N")
    parser.add_argument("--status-demo", action="store_true")
    parser.add_argument("--status-manifest", type=Path, default=Path("tests/fixtures/external_workspace_batch_manifest.fixture.json"))
    parser.add_argument("--hold-seconds", type=float, default=1.2)
    parser.add_argument("--image-url", metavar="URL")
    parser.add_argument("--image-title", default="外部引用图片（本地HTTP）")
    parser.add_argument("--get-state", action="store_true")
    parser.add_argument("--save-layout", type=Path)
    parser.add_argument("--clear-mine", action="store_true")
    args = parser.parse_args()

    ran = False
    if args.health:
        cmd_health()
        ran = True
    if args.push_batch:
        cmd_push_batch(args.push_batch)
        ran = True
    if args.push_live:
        cmd_push_live(args.push_live, args.layout_path, args.restore_viewport)
        ran = True
    if args.apply_edits:
        cmd_apply_edits(args.apply_edits, args.layout_path, args.restore_viewport)
        ran = True
    if args.layout_save:
        cmd_layout_save(args.layout_save, args.layout_path)
        ran = True
    if args.watch:
        cmd_watch(args.watch, args.interval, args.layout_path)
        ran = True
    if args.serve:
        cmd_serve(args.serve, args.interval, args.layout_path, args.executor)
        ran = True
    if args.stress:
        cmd_stress(args.stress)
        ran = True
    if args.status_demo:
        cmd_status_demo(args.status_manifest, args.hold_seconds)
        ran = True
    if args.image_url:
        cmd_image_url(args.image_url, args.image_title)
        ran = True
    if args.get_state:
        cmd_get_state(args.save_layout)
        ran = True
    if args.clear_mine:
        cmd_clear_mine()
        ran = True
    if not ran:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
