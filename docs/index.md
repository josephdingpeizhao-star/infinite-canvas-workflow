# Documentation Index

Use this page as the starting point for understanding the project.

## Start Here

- `AGENTS.md`: Codex operating rules. Read this before asking Codex to modify anything.
- `ARCHITECTURE.md`: plain-language explanation of the project structure.
- `reports/current_state.md`: current human-readable status, allowed actions, forbidden actions, and active batch summary.
- `reports/current_state.json`: same current status in machine-readable form.

## Project Structure

- `ARCHITECTURE.md`: simple overview for non-programmers.
- `docs/ARCHITECTURE.md`: more technical architecture notes from the migration work.
- `manifests/workflow_architecture.json`: machine-readable workflow architecture.
- `manifests/workflow_graph.template.json`: machine-readable pipeline graph (stages, artifacts, dependencies), validated against `schemas/workflow_graph.schema.json` and kept consistent with `scripts/detect_current_state.py` routing by `tests/test_workflow_graph_projection.py`.
- `docs/RUNTIME_OPTIMIZATION.md`: notes about runtime efficiency and boundaries.

## Task Flow And Stages

- `docs/STAGE_PLAN.md`: what each workflow stage means.
- `docs/CURRENT_PROGRESS.md`: latest summarized progress and restrictions.
- `reports/current_state.md`: best first stop before deciding the next action.
- `reports/*_stage_*.md`: product-specific stage reports.
- `reports/*_final_prompt_integrity_report.md`: render-blocking or render-allowing final prompt integrity reports.

## Product Batches

- `manifests/<product_id>.batch_manifest.json`: source of truth for a product batch's paths and workspace connection.
- `inputs/products/`: repository-local input templates.
- `artifacts/`: repository-local artifact templates and outputs when not using an external workspace.
- Manifest-declared external workspaces may hold real product inputs, drafts, artifacts, renders, and repaired outputs.

## Skills And Rules

- `.agents/skills/`: active Codex Skills. Use these first.
- `.codex/skills/`: legacy compatibility Skills. Do not edit unless explicitly asked.
- Root `.txt` files: original business rule prompt files. They explain the business rules, but they are not final image-generation prompts.

## Scripts, Schemas, And Checks

- `scripts/validate_skill_tree.py`: checks Skill tree structure.
- `scripts/validate_references.py`: checks Skill reference files.
- `scripts/detect_current_stage.py`: detects workflow stage status.
- `scripts/workflow_doctor.py`: refreshes validation reports and current state.
- `scripts/compile_final_prompts.py`: compiles final prompts from approved upstream artifacts.
- `scripts/validate_final_prompt_integrity.py`: required gate before rendering.
- `schemas/`: JSON file shape rules used by validation scripts.

## History And Evidence

- `reports/`: validation reports, stage reports, routing decisions, rendering reports, and QC reports.
- `_archive/`: stale manifests and migrated files kept for history.
- `docs/CURRENT_PROGRESS.md`: short human summary of completed gates and current restrictions.

## How To Ask GPT And Codex Safely

When planning with GPT, include `reports/current_state.md`, the relevant manifest, and the relevant document from this index.

When asking Codex to execute, say whether it may edit documentation only, may edit scripts, may edit manifests, or must inspect without changing files. If the task affects rendering, ask Codex to verify the final prompt integrity gate before any ComfyUI-related action.
