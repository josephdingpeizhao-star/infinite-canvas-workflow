---
name: set-product-identity
description: "Create a set-product identity archive for explicitly declared e-commerce set batches. Use when the user states the batch is a set product and set-level identity rules are needed before set layout or variable configs. Do not use for default single-product batches, final prompts, QC, or image generation."
---

# Set Product Identity

## Purpose

Generate the upstream product identity archive for an explicitly declared set product, including set components, relationships, shared identity, and component boundaries.

## When to use

- Use only when the user explicitly declares the batch is a set product.
- Use when the user provides set-product photos, component descriptions, packaging notes, or component-level facts.
- Use before set angle/layout inventory or set variable configuration extension when set identity is missing.

## Required inputs

- User-declared set-product context.
- Set-product images, component facts, bundle composition, or packaging facts supplied by the user.
- Component-level exclusions or must-keep facts, if any.

## Required references

- `references/套装产品身份档案提示词.txt`
- `references/套装产品工作流补充规则.txt`

## Required output

- A structured set-product identity archive.
- Component list, confirmed component relationships, unknowns, and prohibited inventions.
- No final image generation prompt and no image output.

## Hard rules

- Use set-product Skills only when the user explicitly declares a set-product batch or asks for set-product main/detail variable configs.
- Single-product batches do not call set-product Skills by default.
- Use `套装产品身份档案提示词.txt` and `套装产品工作流补充规则.txt` as upstream rules for this stage.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Do not fabricate component count, product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.

## Do not use when

- The user has not explicitly declared a set-product batch.
- The user asks for single-product identity, single-product angle inventory, or single-product variable configs.
- The user asks for final prompt compilation, QC, or image generation.
