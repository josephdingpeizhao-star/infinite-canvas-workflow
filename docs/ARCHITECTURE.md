# Architecture

This project migrates an e-commerce AI image workflow from manual ChatGPT step confirmation to repository-driven Codex orchestration.

## Responsibility Boundaries

ChatGPT chat is limited to rule discussion, difficult review, exception judgment, and human review assistance.

Codex, Skills, and scripts own repository state, external workspace path resolution, Skill routing, references mapping, manifests, JSON Schemas, validation, current-stage detection, product batch intake, API vision task preparation, variable configuration generation, final prompt compilation, render job preparation, batch execution scripts, QC, repair planning, and versioned reports.

ComfyUI is limited to reference image control, node-based image generation, batch rendering, inpainting, upscaling, and output file saving.

## Repository State Model

- `.agents/skills/` is the canonical Skill tree.
- `.codex/skills/` is legacy compatibility data and must be compared, not deleted.
- Source rule prompt files at repository root are frozen inputs and must not be rewritten by automation.
- `manifests/` defines workflow and batch metadata, including the connection from repository state to any external product run folder.
- `schemas/` defines machine-readable artifact contracts.
- `reports/` records validation, stage, compatibility, and current-state outputs.
- `artifacts/{product_id}/` remains the repository-local product artifact base path for legacy or repository-mode batches.
- Manifest-declared external workspaces may hold product inputs, drafts, structured artifacts, ComfyUI jobs, and generated outputs outside the repository.

## External Workspace Model

The repository should stay clean during product runs. For external batches, a human-created run folder under a desktop `杯类` directory is the runtime workspace, and `manifests/<product_id>.batch_manifest.json` records its absolute paths.

Expected external run layout:

```text
杯类/<product_id>_<run_date>/
  inputs/
    white_bg/
    style_refs/
    set_group/
    component_white_bg/
  drafts/
  artifacts/
    identity/
    style_master/
    angle_inventory/
    variable_configs/
    final_prompts/
    comfyui_jobs/
    qc_reports/
  outputs/
    renders/
    repaired/
  manifests/
```

## Stage Gate

Codex must use repository files and reports to decide the current stage. If Stage 1 through Stage 4 fail, product batch intake and downstream work must not start. When Stage 1 through Stage 4 pass, Stage 5 is limited to current-state orchestration and workflow doctor maintenance.

Final prompt integrity is a fixed post-compilation, pre-render gate. Compiled final prompts and ComfyUI job manifests must pass `scripts/validate_final_prompt_integrity.py` before any ComfyUI submission or rendering entry point may proceed. The gate is report-only and does not generate images, call ComfyUI, or rewrite source business rule prompt files.
