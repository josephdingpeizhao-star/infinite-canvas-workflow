---
name: final-prompt-compiler
description: "Compile final e-commerce image generation prompts from already generated upstream artifacts and this-image variable config. Use when product identity, style master, angle inventory, variable config, realism constraints, prop rules, platform rules, and QC checklist are ready. Do not use to create upstream artifacts, inspect finished images, or generate images."
---

# Final Prompt Compiler

## Purpose

Compile final image generation prompt text for one or more e-commerce images using only approved upstream artifacts and constraint references. This Skill turns a completed per-image variable config into final prompt wording.

## When to use

- Use when the user asks to compile final image prompts after upstream artifacts are ready.
- Use when there is a generated product identity archive, style master, angle inventory, and this-image variable configuration.
- Use when the task is prompt assembly rather than upstream planning or post-generation QC.

## Required inputs

- Generated product identity archive.
- Generated style master.
- Generated angle inventory.
- This-image variable configuration.
- User-specified platform, output constraints, or explicit handheld count/scope, if any.

## Runtime references

- Load `references/runtime_rule_slices/final-prompt-compiler.runtime_rule_slices.json` first.
- The runtime package may contain only source file names, source hashes, line ranges, and exact original text slices. Do not treat it as a rewritten summary or replacement for the original rules.

## Full audit references

Open these full files only when a runtime slice cites missing context, a user requests a full audit, or validation requires source-file verification:

- `references/工作流总控规则.txt`
- `references/真实感约束.txt`
- `references/道具生成规则模块.txt`
- `references/淘宝天猫详情页链路与平台规范模块.txt`
- `references/电商图片通用质检清单.txt`

## Required output

- Final image generation prompt text for the requested image(s).
- Negative constraints or repair-risk notes when required by the references.
- No upstream variable config generation, no QC report, and no generated image.

## Hard rules

- Final prompts are structured production artifacts for downstream ComfyUI job preparation, not ChatGPT production-render requests.
- Runtime slices are loading indexes only; if a slice and source file disagree, the full source file wins.
- Do not use upstream prompt-generation files as final image generation prompts.
- Final prompt compilation may call only already generated product identity archive, style master, angle inventory, this-image variable config, realism constraints, prop rules, platform rules, and QC checklist.
- Do not invent product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Handheld rules are enabled only when this-image variable config explicitly declares a handheld scene. When the approved variable configs carry an explicit user handheld count or enabled handheld declarations, preserve them into final prompts and run the integrity gate with the expected handheld count/scope when supplied; do not remove, rewrite, or dilute handheld declarations during final prompt assembly.
- Preserve confirmed product identity and do not overwrite it with style or prop assumptions.
- Do not generate images.

## Do not use when

- Product identity archive, style master, angle inventory, or this-image variable config is missing.
- The user asks to create product identity, style master, angle inventory, or variable configs.
- The user asks for post-generation image QC or repair inspection.
