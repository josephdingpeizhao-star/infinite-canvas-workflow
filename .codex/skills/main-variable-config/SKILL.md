---
name: main-variable-config
description: "Generate exactly six single-product main-image variable configurations for e-commerce AI image workflows. Use after product identity, style master, and single-product angle inventory are ready. Do not use for detail-image configs, set-product configs, final image prompts, QC, or image generation."
---

# Main Variable Config

## Purpose

Generate six single-product main-image variable configurations that define per-image variables for later final prompt compilation. This Skill plans image directions only; it does not generate images or final image prompts.

## When to use

- Use when the user asks for main-image variable configuration for a single-product batch.
- Use after product identity archive, style master, and single-product angle inventory exist.
- Use when Codex needs exactly 6 main-image configurations for later final prompt compilation.

## Required inputs

- Generated product identity archive.
- Generated style master.
- Generated single-product angle inventory.
- User requirements for main image platform, size, priority, scene, selling point, or explicit handheld count/scope, if supplied.

## Runtime references

- Load `references/runtime_rule_slices/main-variable-config.runtime_rule_slices.json` first.
- The runtime package may contain only source file names, source hashes, line ranges, and exact original text slices. Do not treat it as a rewritten summary or replacement for the original rules.

## Full audit references

Open these full files only when a runtime slice cites missing context, a user requests a full audit, or validation requires source-file verification:

- `references/主图单张变量配置提示词生成.txt`
- `references/工作流总控规则.txt`
- `references/真实感约束.txt`
- `references/道具生成规则模块.txt`
- `references/电商图片通用质检清单.txt`

## Required output

- Exactly 6 single-product main-image variable configurations.
- Each configuration should be suitable for one later final prompt.
- No final image generation prompt and no image output.

## Hard rules

- Use `主图单张变量配置提示词生成.txt` as the upstream prompt rule for this stage.
- Runtime slices are loading indexes only; if a slice and source file disagree, the full source file wins.
- Main-image variable configuration only generates 6 sets of main-image variables.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Use product identity, style master, angle inventory, realism constraints, prop rules, and QC checklist to constrain variables.
- Handheld rules are enabled only when this-image variable config explicitly declares a handheld scene; if the user explicitly requests handheld scenes or specifies a handheld count for main images, carry that requirement into planning and either generate the requested number of handheld configs with clear handheld interaction declarations or stop for missing scope/size/angle/safety information. Do not let the default about-3 handheld plan or low-confidence dimension fallback silently override an explicit user handheld count.
- Do not fabricate product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Do not generate images.

## Do not use when

- The user asks for detail-image variable configs.
- The user explicitly declares a set-product batch or asks for set-product variable config extensions.
- The user asks for final prompt compilation or post-generation QC.
