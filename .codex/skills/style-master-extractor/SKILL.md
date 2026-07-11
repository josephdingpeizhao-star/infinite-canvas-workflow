---
name: style-master-extractor
description: "Extract a reusable e-commerce image style master from reference images or existing successful outputs. Use when Codex needs to reverse-engineer composition, lighting, material feel, color mood, and platform visual style for later prompts. Do not use for product identity, angle inventory, variable config, final prompts, or image generation."
---

# Style Master Extractor

## Purpose

Reverse-extract a style master from reference images or benchmark outputs so later workflow stages can reuse stable visual rules without copying unrelated product facts.

## When to use

- Use when the user provides one or more style reference images or existing image descriptions.
- Use when a style master is missing before variable config or final prompt compilation.
- Use when the task is to describe lighting, camera feel, environment, composition, color, texture, and visual constraints as reusable style rules.

## Required inputs

- Reference image(s), benchmark image(s), or detailed descriptions supplied by the user.
- Existing product identity archive, if available, to avoid mixing style with product facts.
- User-declared platform or brand style constraints, if any.

## Required references

- `references/反向提取风格母版提示词.txt`

## Required output

- A structured style master for later workflow stages.
- Style rules separated from product identity facts.
- No final image generation prompt and no image output.

## Hard rules

- Use `反向提取风格母版提示词.txt` as the upstream prompt rule for this stage.
- Do not treat the style extraction prompt as a final image generation prompt.
- Do not invent product specifications, product functions, certifications, platform claims, or sales data from style references.
- Do not create variable configs or final prompts in this Skill.
- If a reference image contains props, treat them as style context unless the user confirms they belong to the product.

## Do not use when

- The user asks to identify product facts rather than extract visual style.
- The user asks for angle slot recognition from white-background product images.
- The user asks to compile final output prompts or inspect generated images.
