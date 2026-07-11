---
name: product-identity-archive
description: "Create a single-product identity archive for e-commerce AI image workflows from user-provided product facts and references. Use when Codex needs the upstream product identity artifact before style, angle, variable config, or final prompt work. Do not use for set-product identity, final image prompts, variable configs, or image generation."
---

# Product Identity Archive

## Purpose

Generate the upstream product identity archive for a single product. The archive should stabilize what the product is, what visible facts are known, and what must not be invented before later workflow stages run.

## When to use

- Use when the batch is a single product and no product identity archive exists yet.
- Use when the user provides product photos, product text, SKU notes, visible attributes, or factual constraints and asks to organize them for later image generation.
- Use before style master extraction, angle inventory, variable config generation, or final prompt compilation when product identity is missing or incomplete.

## Required inputs

- User-provided product facts and product images or descriptions.
- Known SKU, material, color, structure, capacity, accessories, packaging, or selling-point information, only when supplied or visible.
- User-declared exclusions or must-keep facts.

## Required references

- `references/产品身份档案提示词.txt`

## Required output

- A structured single-product identity archive.
- Clear separation between confirmed facts, visible inferences, unknowns, and prohibited inventions.
- No final image generation prompt and no image output.

## Hard rules

- Use `产品身份档案提示词.txt` as the upstream prompt rule for this stage.
- Do not treat this upstream prompt file as a final image generation prompt.
- Do not generate style master, angle inventory, variable config, final prompt, or QC output in this Skill.
- Single-product batches do not call set-product Skills by default.
- Do not fabricate product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Mark unknown product facts as unknown instead of filling them creatively.

## Do not use when

- The user explicitly declares this batch is a set product and asks for set-product identity.
- A complete product identity archive already exists and the user is asking for a later stage.
- The task is final prompt compilation, image generation, or post-generation QC.
