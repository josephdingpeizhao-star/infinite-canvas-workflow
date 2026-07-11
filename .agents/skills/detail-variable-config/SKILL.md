---
name: detail-variable-config
description: "Generate default eight single-product detail-image variable configurations for e-commerce detail pages. Use after product identity, style master, and single-product angle inventory are ready and the user needs detail-page image planning. Do not use for main-image configs, set-product configs, final prompts, QC, or image generation."
---

# Detail Variable Config

## Purpose

Generate single-product detail-image variable configurations for e-commerce detail-page use. This Skill plans per-image variables for later final prompt compilation; it does not generate images or final image prompts.

## When to use

- Use when the user asks for detail-image variable configuration for a single-product batch.
- Use after product identity archive, style master, and single-product angle inventory exist.
- Use when Codex needs the default 8 detail-image configurations unless the user gives a different count.

## Required inputs

- Generated product identity archive.
- Generated style master.
- Generated single-product angle inventory.
- User requirements for detail-page platform, selling points, modules, scene, sequence, or explicit handheld count/scope, if supplied.
- User-supplied product information supplements, if any.

## Runtime references

- Load `references/runtime_rule_slices/detail-variable-config.runtime_rule_slices.json` first.
- The runtime package may contain only source file names, source hashes, line ranges, and exact original text slices. Do not treat it as a rewritten summary or replacement for the original rules.

## Full audit references

Open these full files only when a runtime slice cites missing context, a user requests a full audit, or validation requires source-file verification:

- `references/详情图单张变量配置提示词生成.txt`
- `references/工作流总控规则.txt`
- `references/真实感约束.txt`
- `references/道具生成规则模块.txt`
- `references/淘宝天猫详情页链路与平台规范模块.txt`
- `references/商品信息补充清单提示词.txt`
- `references/电商图片通用质检清单.txt`

## Required output

- Default output is 8 single-product detail-image variable configurations.
- Each configuration should support one later final detail-image prompt.
- No final image generation prompt and no image output.

## Hard rules

- Use `详情图单张变量配置提示词生成.txt` as the upstream prompt rule for this stage.
- Runtime slices are loading indexes only; if a slice and source file disagree, the full source file wins.
- Detail-image variable configuration defaults to 8 sets unless the user explicitly changes the count.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Use product identity, style master, angle inventory, realism constraints, prop rules, platform rules, product-info supplement rules, and QC checklist to constrain variables.
- Handheld rules are enabled only when this-image variable config explicitly declares a handheld scene; if the user explicitly requests handheld scenes or specifies a handheld count for detail images, carry that requirement into planning and either generate the requested number of handheld configs with clear handheld interaction declarations or stop for missing scope/size/angle/module/safety information. Do not let the default about-3 handheld plan, module-default non-handheld plan, or low-confidence dimension fallback silently override an explicit user handheld count.
- Do not fabricate product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Do not generate images.

## Do not use when

- The user asks for main-image variable configs.
- The user explicitly declares a set-product batch or asks for set-product variable config extensions.
- The user asks for final prompt compilation or post-generation QC.
