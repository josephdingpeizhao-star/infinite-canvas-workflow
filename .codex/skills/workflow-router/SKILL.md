---
name: workflow-router
description: "电商 AI 生图工作流总控与路由 Skill。Use when Codex needs to decide the current workflow stage, choose the next single-product or set-product Skill, or check whether upstream artifacts are ready. Do not use when the user asks to execute a specific downstream Skill directly or to generate images."
---

# Workflow Router

## Purpose

Route an e-commerce AI image workflow to the correct stage and Skill. Use the workflow control reference to decide whether the batch is a single product or an explicitly declared set product, what upstream artifacts are required, and which downstream Skill should be called next.

## When to use

- Use when the user asks for workflow planning, stage selection, dependency checking, or next-step routing.
- Use when the request is ambiguous and Codex must determine whether to create a product identity archive, style master, angle inventory, variable configs, final prompts, or QC.
- Use before any set-product Skill unless the user has explicitly declared this batch as a set product.

## Required inputs

- User request and current batch context.
- Product type: single product by default, set product only when explicitly declared by the user.
- List of already generated artifacts, if any.
- Any user constraints for platform, image type, count, style, angle, or scene.

## Required references

- `references/工作流总控规则.txt`

## Required output

- A concise routing decision that names the next Skill to use.
- A dependency check listing missing upstream artifacts.
- A short reason for the routing decision.
- No image prompt, no generated image, and no fabricated product facts.

## Hard rules

- `manifests/workflow_architecture.json` and the generated reports are authoritative over external chat memory and legacy ChatGPT direct-render wording in references.
- ChatGPT is review/discussion only; production progression is decided from repository state, schemas, artifacts, and reports.
- Production rendering is prepared for the ComfyUI execution layer, not decided or executed by ChatGPT.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Use upstream prompt files only for their matching upstream artifact stage.
- For final prompt compilation, call only generated product identity archive, style master, angle inventory, this-image variable config, realism constraints, prop rules, platform rules, and QC checklist.
- Single-product batches do not call set-product Skills by default.
- Call set-product Skills only when the user explicitly declares the batch is a set product or explicitly asks for set-product main/detail variable configs.
- The angle inventory Skill is only for single-product white-background angle recognition.
- The set angle and layout Skill is only for set-product white-background group-shot camera and arrangement recognition.
- Handheld rules are enabled only when this-image variable config explicitly declares a handheld scene.
- QC is only for post-generation inspection and repair guidance, not for adding new creative directions.
- Do not fabricate product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.

## Do not use when

- The user has already named the exact Skill to execute and all required inputs are present.
- The task is to generate or edit images.
- The task is unrelated to the e-commerce AI image workflow.
