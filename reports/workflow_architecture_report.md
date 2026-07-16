# Workflow Architecture Report

- status: pass
- checked_at: 2026-07-16T03:12:49.674651+00:00
- architecture_manifest: manifests/workflow_architecture.json
- schema: schemas/workflow_architecture.schema.json
- error_count: 0

## Checks

- artifact_type: pass - artifact_type must be workflow_architecture
- version: pass - version must be a non-empty string
- chatgpt_review_only: pass - ChatGPT production execution must be false
- codex_uses_repository_state: pass - Codex must use repository state
- external_chat_memory_forbidden: pass - external chat memory must be forbidden as production state
- comfyui_execution_only: pass - ComfyUI decision authority must be false
- artifact_base_path: pass - structured artifacts must use artifacts/{product_id}/
- external_workspace_base_path: pass - external workspace artifacts must be manifest-declared
- external_workspace_manifest_connection: pass - external workspace must be connected through batch manifests
- required_product_artifacts: pass - all required product artifacts must be declared
- reports_gate_progression: pass - JSON and Markdown reports must gate progression
- hard_rule_phrase:ChatGPT is not the primary production execution environment.: pass - hard_rules must include: ChatGPT is not the primary production execution environment.
- hard_rule_phrase:Codex must work from repository state: pass - hard_rules must include: Codex must work from repository state
- hard_rule_phrase:ComfyUI executes image generation: pass - hard_rules must include: ComfyUI executes image generation
- hard_rule_phrase:artifacts/{product_id}/: pass - hard_rules must include: artifacts/{product_id}/
- hard_rule_phrase:manifest-declared external workspace: pass - hard_rules must include: manifest-declared external workspace
- hard_rule_phrase:JSON and Markdown reports: pass - hard_rules must include: JSON and Markdown reports
- hard_rule_phrase:Codex must use reports: pass - hard_rules must include: Codex must use reports
- architecture_manifest_exists: pass - workflow architecture manifest must exist
- schema_available: pass - workflow architecture schema must exist and declare the expected id

## Errors

- None
