# Current Progress

Last repository-state checkpoint: 2026-06-15.

## Completed Gates

- Stage 0: source rule files are present.
- Stage 1: `.agents/skills/` exists as the canonical Skill tree.
- Stage 2: required references mappings are present.
- Stage 3: template directories, manifest templates, schemas, scripts, and report paths are present.
- Stage 4: baseline validation reports exist and pass in the latest checked state.
- Stage 5: current-state orchestration scripts and reports exist.
- Final prompt integrity gate has been added as a fixed post-compilation, pre-render control.

## Current Gate

Use `reports/current_state.json` and `reports/current_state.md` as the machine and human-readable current state. `workflow_doctor.py` refreshes these reports from the repository and manifest-declared external workspace.

## Current Restrictions

- Do not generate images.
- Do not call ComfyUI.
- Do not submit or render any ComfyUI job unless `final_prompt_integrity_report` exists for the current batch and is not `fail`.
- Do not treat `needs_review` warnings as product repair instructions; they are review notes unless they become blocking issues.
- Do not enable set-product Skills unless the product is explicitly declared as a set product or a set-product output is explicitly requested.
