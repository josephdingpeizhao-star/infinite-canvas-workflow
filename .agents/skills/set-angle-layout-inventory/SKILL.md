---
name: set-angle-layout-inventory
description: "Build a set-product white-background group-shot camera and arrangement inventory. Use only for explicitly declared set-product batches where the user needs overall angle, layout, and component relationship recognition. Do not use for single-product angle recognition, scene design, final prompts, QC, or image generation."
---

# Set Angle Layout Inventory

## Purpose

Recognize the overall camera position, component arrangement, visual hierarchy, and layout relationship in set-product white-background group shots.

## When to use

- Use only when the user explicitly declares the batch is a set product.
- Use when set-product white-background group-shot images need overall camera and arrangement recognition.
- Use before set variable configuration extension when set angle/layout inventory is missing.

## Required inputs

- User-declared set-product context.
- Set-product white-background group-shot image(s) or descriptions.
- Set-product identity archive, if available.

## Required references

- `references/套装角度与编排入库表提示词.txt`
- `references/套装编排规则.txt`
- `references/套装产品工作流补充规则.txt`

## Required output

- A set-product angle and layout inventory table.
- Overall camera angle, component positions, front/back relationships, occlusion risks, layout strengths, and downstream use notes.
- No single-product angle table, no variable config, no final prompt, and no image output.

## Hard rules

- This Skill only handles set-product white-background group-shot camera and arrangement recognition.
- Do not use this Skill for single-product white-background angle recognition.
- Use set-product Skills only when the user explicitly declares a set-product batch or asks for set-product main/detail variable configs.
- Do not treat upstream prompt-generation files as final image generation prompts.
- Do not fabricate component count, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.

## Do not use when

- The user has not explicitly declared a set-product batch.
- The input is a single-product white-background angle recognition task.
- The user asks for final prompt compilation, QC, or image generation.
