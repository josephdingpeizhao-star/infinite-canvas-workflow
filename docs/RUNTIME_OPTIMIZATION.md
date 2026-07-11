# Runtime Optimization Notes

This repository keeps original business rule prompt files as immutable source material. Runtime optimization is limited to execution cost, repeated reads, and reportable gates.

## Implemented

- Runtime rule packages are slim slices. They keep original text slices plus `audit_ref`; full source paths and SHA-256 values remain in `reports/runtime_rule_index.json`.
- `scripts/validate_references.py` verifies each slim slice against the audit index and the original source lines.
- `scripts/compile_final_prompts.py` compiles final prompts from manifest-declared artifacts and writes JSON plus Markdown reports without reading root prompt files as final visual requirements.
- `scripts/run_comfy_cloud_batch_robust.py` keeps default serial execution, but records recommended small-batch concurrency. Start Comfy Cloud runs with `--concurrency 3` or `4` only after quota and workflow-template checks.
- `scripts/pre_render_reference_gate.py` flags risky single-product references before rendering. It never removes jobs, rewrites prompts, downsamples render references, or calls ComfyUI.

## Boundaries

- Delivery count remains 6 main images plus 8 detail images unless a user explicitly requests fewer outputs.
- QC, repair reports, and manual review suggestions remain part of the workflow.
- Low-resolution working copies, if added later, may be used only for Codex recognition or preview. Final Comfy render references must continue to use original images.
- Set-product logic remains disabled unless the user explicitly declares a set product or requests set-product variable configuration.
