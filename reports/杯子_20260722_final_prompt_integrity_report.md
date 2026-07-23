# Final Prompt Integrity Report

- product_id: 杯子_20260722
- mode: prompts-only
- status: pass
- render_blocked: false
- checked_at: 2026-07-22T12:09:03+00:00
- checked_prompt_count: 14
- blocking_issue_count: 0
- warning_count: 0
- image_generation_performed: false
- comfyui_execution_performed: false

## Results

- prompt_count_schema_and_sequence: pass (expected=14, indexed=14.)
- source_and_resolved_fingerprint_chain: pass (Source file SHA-256 and compact stable JSON fingerprints are recomputed.)
- ratio_and_confirmed_height_literals: pass (invalid_ratios=0, invalid_heights=0.)
- handheld_counts: pass ({"expected_detail": 1, "expected_main": 2, "final_prompt_detail": 1, "final_prompt_main": 2, "variable_config_detail": 1, "variable_config_main": 2})
- unicode_integrity: pass (unicode_issue_count=0.)

## Blocking Issues

- None

## Warnings

- None

## Skipped Checks

- comfyui_job_manifest: prompts-only 在 ComfyUI 作业生成前运行，因此不读取或要求作业清单。
- index_job_path_set_comparison: 没有 ComfyUI 作业清单；索引集合改由变量配置确定性序列核对。
- job_dimensions_and_ratio: 没有作业尺寸层；改为核对每份最终提示词中的 1:1 或 3:4 字面约束。
- job_layer_handheld_count: 没有作业层；改为同时核对变量配置、最终提示词与 manifest notes。
- legacy_content_heuristics: 为避免真实批次误报，跳过旧 must_keep、禁用语境、道具本体和低置信单位扫描；改用 Schema、指纹、比例和已确认高度语义检查。
- legacy_compiler_literal_scan: prompts-only 验证已编译产物，不扫描编译器源码，以避免真实批次误报。

## Checked Assets

- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_01_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_02_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_03_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_04_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_05_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\main_06_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_01_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_02_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_03_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_04_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_05_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_06_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_07_final_prompt.json
- D:\onedrive\OneDrive\Desktop\杯类\杯子_20260722\artifacts\final_prompts\detail_08_final_prompt.json
