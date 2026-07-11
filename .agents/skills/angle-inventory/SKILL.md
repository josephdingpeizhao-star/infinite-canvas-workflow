---
name: angle-inventory
description: "Build a single-product white-background angle slot inventory by recognizing product camera angles and usable views. Use when Codex needs the upstream angle inventory for a single item before variable configs or final prompt compilation. Do not use for set-product group shots, scene design, variable configs, final prompts, or image generation."
---

# Angle Inventory

## Purpose

Recognize and organize single-product white-background source images into an angle slot inventory that later stages can reference for main/detail image planning.

## When to use

- Use when the user provides single-product white-background images for angle recognition.
- Use when a single-product angle inventory is missing before variable config or final prompt compilation.
- Use when the task is to classify camera angle, visible surfaces, orientation, and reuse suitability.

## Required inputs

- Single-product white-background image(s) or image descriptions.
- Existing product identity archive, if available.
- User-supplied angle naming rules or slot requirements, if any.

## Required references

- `references/角度槽位入库表生成与识别提示词.txt`

## Required output

- A single-product angle slot inventory table.
- Each slot should describe angle, visible product surfaces, strengths, limitations, and recommended downstream use.
- No scene design, no variable configs, no final prompts, and no image output.

## Hard rules

- This Skill only handles single-product white-background angle recognition.
- Do not use this Skill for set-product group-shot layout or arrangement recognition.
- Use `角度槽位入库表生成与识别提示词.txt` as the upstream prompt rule for this stage.
- Do not treat the angle inventory prompt as a final image generation prompt.
- Do not fabricate product dimensions, material claims, certifications, performance claims, or review data.

## Do not use when

- The user explicitly declares a set-product batch or asks for set-product group-shot arrangement.
- The input is a lifestyle scene image intended for style extraction rather than white-background angle recognition.
- The user asks for main/detail variable configs, final prompts, or QC.
