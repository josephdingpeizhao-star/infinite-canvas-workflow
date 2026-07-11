# Project Architecture

This project is a controlled workspace for producing e-commerce AI image prompts and related production reports. In simple terms, it turns product information and reference images into structured files, checks those files, prepares final image prompts, and only then allows rendering work to continue.

The repository is the rule book and control center. Product photos, temporary outputs, and rendered images may live outside the repository when a batch manifest points to an external workspace.

## Main Folders

- `.agents/skills/` is the main Codex Skill library. Each Skill is a task recipe, such as product identity, angle inventory, variable configuration, final prompt compilation, and QC inspection.
- `.codex/skills/` is an older compatibility copy of the Skill library. It should be compared when needed, but not treated as the primary source.
- `docs/` holds human-readable explanations, progress notes, stage plans, and this project's document index.
- `inputs/` holds template input folders for product photos and style references when a batch is kept inside the repository.
- `artifacts/` holds template output folders for structured product files, final prompts, ComfyUI job files, and QC reports when a batch is kept inside the repository.
- `manifests/` connects a product batch to its inputs, artifacts, external workspace paths, and workflow metadata.
- `schemas/` defines the expected shape of JSON files. These are the checklists scripts use to catch malformed outputs.
- `scripts/` contains Python tools that validate the repo, build manifests, detect the current stage, compile final prompts, prepare or submit render jobs, and write reports.
- `reports/` stores current state, validation results, stage reports, final prompt integrity reports, rendering reports, and QC reports. For most questions about "where are we now?", start here.
- `tests/` contains test fixtures used to check script behavior.
- `_archive/` keeps stale or migrated files for history. It is not the active working area.

## Important Root Files

- `AGENTS.md` tells Codex how to work safely in this repository.
- `ARCHITECTURE.md` is this plain-language structure overview.
- Root `.txt` files are original business rule prompts and rule modules. They are source material, not final image prompts, and should not be rewritten casually.
- `GPT-image 2 图生图工作流（初始）【api】.json` is a workflow JSON file for image-generation execution. It should not be edited without an explicit workflow change request.
- `.env.local` is local environment configuration.
- `ecommerce_ai_image_workflow_state.json` and `stage_4_basic_validation.json` are older or side state files; prefer `reports/current_state.*` for current status.

## Main Workflow

1. A human or GPT defines the task and constraints.
2. Codex reads `AGENTS.md`, `docs/index.md`, and `reports/current_state.md`.
3. If a product batch is involved, Codex reads the matching file in `manifests/`.
4. Codex uses the relevant Skill from `.agents/skills/`.
5. Scripts and schemas check whether generated files are complete and valid.
6. Final prompts are compiled only after upstream files are ready.
7. The final prompt integrity gate must pass before any ComfyUI submission or rendering.
8. Reports are written as both JSON and Markdown so humans and scripts can review the same result.

## Safe Collaboration Model

For a non-programmer workflow, use GPT for planning and review, then ask Codex to execute a narrow task. A safe request usually says:

- what you want changed,
- which product or stage it applies to,
- whether code/script/workflow changes are allowed,
- and whether Codex should only inspect, only document, or actually modify files.

When in doubt, Codex should stop and report instead of guessing.
