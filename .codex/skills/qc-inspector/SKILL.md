---
name: qc-inspector
description: "Inspect final generated e-commerce images against the common QC checklist, workflow rules, and realism constraints. Use only after images or final image outputs exist and the user wants quality review or repair notes. Do not use to add new generation directions, create upstream artifacts, compile prompts, or generate images."
---

# QC Inspector

## Purpose

Inspect final generated e-commerce images or final prompt outputs for compliance, realism, product consistency, and repair needs. This Skill reports defects and repair instructions only.

## When to use

- Use after image generation when the user asks for checking, review, QC, audit, or repair feedback.
- Use when comparing final output against product identity, style master, variable config, and platform constraints.
- Use when the task is to identify defects, not create new creative directions.

## Required inputs

- Final generated image(s) or detailed final output descriptions.
- Final prompt text or this-image variable config, if available.
- Product identity archive, style master, and platform constraints, if available.

## Runtime references

- Load `references/runtime_rule_slices/qc-inspector.runtime_rule_slices.json` first.
- The runtime package may contain only source file names, source hashes, line ranges, and exact original text slices. Do not treat it as a rewritten summary or replacement for the original rules.

## Full audit references

Open these full files only when a runtime slice cites missing context, a user requests a full audit, or validation requires source-file verification:

- `references/电商图片通用质检清单.txt`
- `references/工作流总控规则.txt`
- `references/真实感约束.txt`

## Required output

- QC pass/fail or severity-ranked issue list.
- Specific repair notes tied to observed defects.
- No new image direction, no new variable config, no final prompt compilation, and no image output.

## Hard rules

- QC is only for final post-generation inspection and repair guidance.
- Runtime slices are loading indexes only; if a slice and source file disagree, the full source file wins.
- Do not use QC to invent new generation directions or expand the creative brief.
- Do not fabricate product specifications, dimensions, certifications, warranties, after-sales terms, sales, reviews, or test reports.
- Flag unsupported claims, identity drift, unrealistic rendering, platform risk, prop misuse, and text/specification hallucination.
- Handheld rules are evaluated only when the final variable config explicitly declared a handheld scene.

## Do not use when

- No final generated image or final output description exists.
- The user asks to create upstream artifacts, variable configs, or final prompts.
- The user asks to generate or edit images.
