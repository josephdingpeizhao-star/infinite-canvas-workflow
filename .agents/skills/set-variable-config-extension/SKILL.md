---
name: set-variable-config-extension
description: "Apply set-product supplemental rules to main-image or detail-image variable configuration for explicitly declared set-product batches. Use when the user asks for set-product main/detail variable configs and set identity plus set angle/layout inventory are ready. Do not use for default single-product batches, final prompts, QC, or image generation."
---

# Set Variable Config Extension

## Purpose

Extend variable configuration work for explicitly declared set products by applying set identity, component relationships, set arrangement rules, and set workflow supplements.

## When to use

- Use only when the user explicitly declares the batch is a set product or explicitly asks for set-product main/detail variable configs.
- Use after set-product identity and set angle/layout inventory exist.
- Use alongside the relevant main/detail variable configuration logic to ensure set-product component relationships and arrangement constraints are respected.

## Required inputs

- User-declared set-product context.
- Generated set-product identity archive.
- Generated set angle/layout inventory.
- Main-image or detail-image variable config requirement from the user.
- Any user-supplied component priority, selling point, platform, or scene constraints.

## Required references

- `references/套装产品工作流补充规则.txt`
- `references/套装变量配置补充模块.txt`
- `references/套装编排规则.txt`
- `references/套装产品身份档案提示词.txt`
- `references/套装角度与编排入库表提示词.txt`

## Required output

- Set-product variable configuration supplement or set-aware variable configs for the requested main/detail image stage.
- Component relationship constraints, arrangement constraints, and prohibited inventions.
- No final image generation prompt and no image output.

## Hard rules

- Use set-product Skills only when the user explicitly declares a set-product batch or asks for set-product main/detail variable configs.
- Single-product batches do not call set-product Skills by default.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Preserve set component identity and arrangement relationships from upstream artifacts.
- Handheld rules are enabled only when this-image variable config explicitly declares a handheld scene.
- Do not fabricate component count, product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Do not generate images.

## Do not use when

- The user has not explicitly declared a set-product batch and has not asked for set-product variable configs.
- Set-product identity or set angle/layout inventory is missing.
- The user asks for final prompt compilation, QC, or image generation.
