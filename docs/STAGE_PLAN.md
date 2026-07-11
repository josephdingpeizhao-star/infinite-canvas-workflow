# Stage Plan

## Stage 0: Source Rule Freeze

Confirm original business rule prompt files exist and identify migration intermediate Skill files without rewriting rule text.

## Stage 1: Skill Tree Normalization

Use `.agents/skills/` as the primary Skill tree. Compare `.codex/skills/` as legacy data and report differences or required manual review.

## Stage 2: References Mapping Validation

Validate every required Skill `references/` mapping against the expected source rule files.

## Stage 3: Templates, Schemas, Scripts, Directories

Confirm template product directories, manifest templates, schemas, baseline scripts, and reports exist.

## Stage 4: Basic Validation Run

Run Skill tree and references validators and ensure JSON and Markdown reports are current.

## Stage 5: Current-State Orchestrator

Maintain `scripts/detect_current_stage.py`, `scripts/workflow_doctor.py`, and `reports/current_state.*` so Codex can determine last completed stage, next stage, blocked reasons, allowed actions, and forbidden actions from repository state.

## Stage 6: Product Batch Intake

Create product-specific batch manifests and input/artifact scaffolds only after a real product ID or product batch request is available. Repository-mode batches may use `inputs/products/<product_id>/` and `artifacts/<product_id>/`; external-workspace batches should use `--workspace-root` and keep product run files under the manifest-declared external run folder.

## Stage 9.5: Final Prompt Integrity Gate

After final prompt compilation and before ComfyUI job preparation or rendering, run `scripts/validate_final_prompt_integrity.py`.

This gate checks compiled prompts, the compiler source, the current batch manifest, product identity archive, variable configs, and job manifest for old product residue, product-specific compiler hardcoding, identity conflicts, low-confidence exact dimension/capacity/weight claims, prop/body boundary errors, and misuse of upstream business prompt files.

The gate writes JSON and Markdown reports to `reports/` and to the manifest-declared external `artifacts/qc_reports/` folder when present. `fail` blocks rendering. `needs_review` records non-blocking warnings.

## Later Stages

Later stages cover upstream artifact readiness, variable configuration, final prompt compilation, render job preparation, rendering, QC, and repair planning. These stages must not run until repository state marks the preceding gates complete.
