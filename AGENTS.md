# Codex Collaboration Guide

This repository is the source of truth for the e-commerce AI image workflow migration. Do not rely on external chat memory as project state.

## Before Every Task

1. Read this file first.
2. Read `docs/index.md` to find the right project document.
3. Read `reports/current_state.md` and, when machine-readable state is needed, `reports/current_state.json`.
4. For workflow-stage work, also read `docs/STAGE_PLAN.md`, `docs/CURRENT_PROGRESS.md`, and the relevant files under `reports/`.
5. For product-batch work, read `manifests/<product_id>.batch_manifest.json` before using any input, artifact, or external workspace path.
6. For Skill work, use `.agents/skills/` as the canonical Skill tree and read the relevant `SKILL.md` before acting.
7. For canvas work (anything touching `canvas-bridge/`, the workflow projection, or the infinite-canvas fork), read `docs/CANVAS_PROJECT_STATE.md` first — it is the authoritative state ledger for that sub-project. Fork work additionally requires reading `FORK_NOTES.md` at the fork root (`D:\dev\infinite-canvas`).

## Operating Rules

- Use `.agents/skills/` as the canonical Skill tree.
- Treat `.codex/skills/` as legacy compatibility data when present.
- Do not delete legacy Skill files unless a later explicit migration step requires it.
- Do not rewrite original business rule prompt files in the repository root.
- Do not treat upstream prompt files as final image-generation prompts.
- Do not infer external workspace state from chat history; read paths from the batch manifest.
- The canvas is a projection only: repository files stay the single source of truth, canvas writes go exclusively through the whitelisted gates in `canvas-bridge/` (see `docs/CANVAS_PROJECT_STATE.md`), and infinite-canvas fork changes must follow the anchor discipline in the fork's `FORK_NOTES.md`.
- Default `batch_type` is `single`.
- Enable set-product Skills only when a user explicitly declares a set product or asks for set-product main/detail variable configuration.
- Do not invent product facts, dimensions, materials, capacities, or visual details that are not present in approved inputs.

## Files That Need Explicit User Approval

Do not modify these unless the user clearly asks for that exact kind of change:

- Business rule prompt files at the repository root, such as `工作流总控规则.txt`, `主图单张变量配置提示词生成.txt`, and similar `.txt` rule files.
- Workflow JSON files, including `GPT-image 2 图生图工作流（初始）【api】.json`.
- Files under `scripts/`, `schemas/`, and `manifests/`.
- Product artifacts, generated reports, or external workspace outputs.
- Legacy files under `.codex/skills/`.

Documentation-only tasks may update `AGENTS.md`, `ARCHITECTURE.md`, and files under `docs/` when requested.

## Required State Files

- Human-readable project docs live in `AGENTS.md`, `ARCHITECTURE.md`, and `docs/`.
- Machine-readable state lives in `reports/current_state.json`.
- Human-readable current state lives in `reports/current_state.md`.
- Stage reports must be written as both JSON and Markdown under `reports/`.

## Self-Check Entry Points

Run these before advancing workflow stages:

```powershell
python scripts/validate_skill_tree.py
python scripts/validate_references.py
python scripts/detect_current_stage.py
python scripts/workflow_doctor.py
```

`workflow_doctor.py` refreshes validation reports and rewrites `reports/current_state.json` and `reports/current_state.md`. Do not run it for a documentation-only task unless the user asks to refresh state.

## Final Prompt Integrity Gate

After `scripts/compile_final_prompts.py` writes compiled final prompts and the ComfyUI job manifest, it must run `scripts/validate_final_prompt_integrity.py`.

The gate writes JSON and Markdown reports to `reports/` and, for manifest-declared external workspaces, to the batch `artifacts/qc_reports/` folder. `fail` or `render_blocked=true` blocks ComfyUI submission and rendering. `needs_review` is allowed to continue only for non-blocking warnings.

## External Workspace Rules

- The repository remains the source of truth for rules, Skills, Schemas, Scripts, manifests, and reports.
- Product batch input files and generated runtime outputs may live in a manifest-declared external workspace.
- The preferred external workspace layout is one run folder under a top-level desktop `杯类` directory.
- Keep product batch files connected through `manifests/<product_id>.batch_manifest.json`.

## Stop And Report When

Stop work and explain the blocker before changing files if:

- The requested action conflicts with `reports/current_state.md` forbidden actions.
- A required manifest, schema, Skill, input folder, or artifact is missing.
- The user request would require changing business rules, workflow JSON, scripts, schemas, or manifests but did not explicitly ask for that.
- Final prompt integrity reports are missing or failing before a render-related task.
- Product type is unclear, especially whether the product is single or set.
- Repository documents disagree with each other and the correct rule is not obvious.
- A change would alter existing workflow behavior when the user requested documentation, review, or planning only.
