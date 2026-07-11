from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
PRODUCT_ID = "mist_blue_mug"
PRODUCT_NAME = "青见马克杯"
MANIFEST_PATH = PROJECT / "manifests" / f"{PRODUCT_ID}.batch_manifest.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_md(path: Path, title: str, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = data if isinstance(data, str) else "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")


def stable_json_sha256(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report(name: str, payload: dict[str, Any]) -> None:
    write_json(PROJECT / "reports" / f"{name}.json", payload)
    write_md(PROJECT / "reports" / f"{name}.md", name, payload)


def compact_variable_configs(configs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    variable_configs = [item["variable_config"] for item in configs]
    common_keys = set(variable_configs[0])
    for item in variable_configs[1:]:
        common_keys &= set(item)

    common_constraints: dict[str, Any] = {}
    for key in variable_configs[0]:
        if key in common_keys and all(item[key] == variable_configs[0][key] for item in variable_configs[1:]):
            common_constraints[key] = variable_configs[0][key]

    compact_configs: list[dict[str, Any]] = []
    for item in configs:
        overrides = {key: value for key, value in item["variable_config"].items() if key not in common_constraints}
        compact_configs.append(
            {
                "config_id": item["config_id"],
                "output_type": item["output_type"],
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(item["variable_config"]),
                "notes": item.get("notes", ""),
            }
        )
    return common_constraints, compact_configs


def variable_config(
    *,
    config_id: str,
    output_type: str,
    page_task: str,
    bound_angle: str,
    composition: str,
    product_position: str,
    product_ratio: str,
    text: str,
    prop_direction: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "页面任务": page_task,
        "绑定角度槽位": bound_angle,
        "角度适配原则": "以绑定白底图的产品朝向、俯仰关系、杯口可见程度、杯身外壁透视、底足可见关系和杯柄空间关系完成本张任务，不改变产品角度。",
        "产品角度依据": "本张产品角度以绑定角度槽位对应的白底产品图为唯一依据；其他图片只用于确认身份、结构和风格。",
        "产品颜色依据": "商品本体颜色以绑定白底图为唯一颜色参照；风格母版、背景、道具和环境光只允许造成真实局部明暗，不得改变青绿色、草绿色、乳白渐变釉及绿色喷点关系。",
        "产品结构关系依据": "外扩宽口、矮宽杯身、向底部收窄的杯壁、窄底足、圆润环形杯柄、釉面喷点和流釉纹理。",
        "辅助参考图调用": "无多角度单件合并参考图；白底图组只作角度入库和结构辅助，不改变本张绑定角度与颜色依据。",
        "展示重点": "保持青绿色到乳白的渐变釉、绿色喷点、杯内深绿色釉、外扩杯口、矮宽杯身和圆环杯柄清晰可识别。",
        "构图方式": composition,
        "镜头距离": "中近景；产品清晰，非产品元素低干扰并有真实景深。",
        "产品位置": product_position,
        "产品占比": product_ratio,
        "尺寸比例锁定": "尺寸来源：用户未提供实测、厂家或详情页参数；尺寸置信度：低；高度、最大宽度、口径、底径、容量和重量均无法确认。不得输出精确厘米、毫升或克数标注，不启用手持和强比例参照道具。",
        "风格贴合锚点调用": ["浅米白桌面与浅暖背景", "柔和侧上方自然光", "绿植虚化背景", "浅色布料自然褶皱", "柠檬/白花/浅色石板等清新生活化弱道具"],
        "风格精简描述": "清新自然光杯具电商摄影：浅米白桌面和背景，柔和侧上方自然光，产品置于布料或浅色石板中景，前景可有少量虚化绿叶/柠檬，背景保留柔化绿植和白花层次；真实接触阴影，不退化为纯白棚拍。",
        "道具密度等级": "丰富生活场景",
        "背景层次配置": "前景少量虚化绿叶、白花或柠檬，中景产品主体与浅色布料/石板，背景为柔化绿植与浅暖墙面；结构说明图可简化但至少保留两层空间。",
        "内容物状态": "默认空杯；使用场景图可加入茶、柠檬茶或浅色饮品液面，但不得改变杯体结构，不得生成溢出或违背重力的液体。",
        "手持交互声明": "本张图不启用手持场景",
        "动态手持样式参考图调用": "无",
        "背景与光线": "延续清新自然光生活化商业摄影；柔和侧上方自然光，釉面高光真实，杯底与桌面接触阴影明确，背景/道具不抢主体。",
        "文字信息": "本张图不设置文字信息" if text == "无" else "小面积中文文字，服务本张页面任务，不遮挡杯口、杯沿、杯身渐变釉或杯柄。",
        "中文营销文案": text,
        "文字渲染要求": "无" if text == "无" else "只渲染【中文营销文案】列出的简体中文，深灰或墨绿色，清晰无乱码，不遮挡产品。",
        "道具生成" if output_type == "main" else "道具关系": prop_direction,
        "真实感要求": "真实商业摄影质感；陶瓷釉面、高光、颗粒纹理、接触阴影和景深可信；避免塑料感、漂浮感、文字漂浮和 AI 融化边缘。",
        "风格防退化检查": "保留浅米白桌面、浅色布料/石板、绿植/白花/柠檬等前中后景层次和真实接触阴影；不得退化为孤立白底棚拍。",
        "禁止事项": "不要改变杯型、颜色、釉面、喷点、杯口外扩关系、杯柄形态或底足；不要添加杯盖、吸管、杯碟、勺子、托盘为商品本体；不要生成多个杯作为销售件数承诺；不要标注未提供的尺寸容量。",
    }
    if extra:
        base.update(extra)
    return {"config_id": config_id, "output_type": output_type, "variable_config": base}


def main() -> int:
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = load_json(MANIFEST_PATH)
    run_root = Path(manifest["workspace"]["root"])
    paths = {
        "manifests": run_root / "manifests",
        "inputs": run_root / "inputs",
        "drafts": run_root / "drafts",
        "artifacts": run_root / "artifacts",
        "outputs": run_root / "outputs",
    }
    for directory in [
        paths["manifests"],
        paths["drafts"],
        paths["inputs"] / "white_bg",
        paths["inputs"] / "style_refs",
        paths["inputs"] / "set_group",
        paths["inputs"] / "component_white_bg",
        paths["artifacts"] / "identity",
        paths["artifacts"] / "style_master",
        paths["artifacts"] / "angle_inventory",
        paths["artifacts"] / "variable_configs",
        paths["artifacts"] / "final_prompts",
        paths["artifacts"] / "comfyui_jobs",
        paths["artifacts"] / "qc_reports",
        paths["outputs"] / "renders",
        paths["outputs"] / "repaired",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    white_bg = sorted((paths["inputs"] / "white_bg").glob("*.JPG"))
    style_refs = sorted(list((paths["inputs"] / "style_refs").glob("*.png")) + list((paths["inputs"] / "style_refs").glob("*.JPG")))
    if len(white_bg) != 8:
        raise SystemExit(f"expected 8 white-bg JPG files for current batch, found {len(white_bg)}")
    if not style_refs:
        raise SystemExit("missing style reference image")
    style_ref = style_refs[0]
    product_info_source = paths["drafts"] / "商品信息补充清单提示词.txt"
    if not product_info_source.is_file():
        raise SystemExit(f"missing product info source: {product_info_source}")

    manifest["requested_outputs"] = ["main", "detail", "final_prompts", "renders", "qc"]
    manifest["current_stage"] = "upstream_and_variable_configs_ready"
    manifest["next_skill"] = "final-prompt-compiler"
    manifest["inputs"]["product_info_text"] = [str(product_info_source)]
    manifest["notes"] = "Current product inputs rebuilt from desktop 杯类 folder on 2026-06-15. Batch remains single-product. Upstream artifacts regenerated for 8 current white-background images."
    write_json(MANIFEST_PATH, manifest)
    write_json(paths["manifests"] / "batch_manifest.json", manifest)

    slot_map = {
        "1S0A9939.JPG": ("white_bg_01", "角度槽位 B：45°斜侧视", "侧前方中低机位，杯柄在左侧，杯口、外扩杯沿、杯身渐变和底足关系清楚。"),
        "1S0A9944.JPG": ("white_bg_02", "角度槽位 A：正面微俯视", "高位微俯视，杯柄在左后侧，杯口和深绿色杯内区域明显可见，杯身前侧可见。"),
        "1S0A9945.JPG": ("white_bg_03", "角度槽位 A：正面微俯视", "高位微俯视，杯口椭圆、杯内空间、杯身前方渐变釉和左侧杯柄同时可见。"),
        "1S0A9948.JPG": ("white_bg_04", "角度槽位 C：顶部俯视", "接近垂直俯视，杯口和杯内底部为主要可见面，杯柄仅从左侧伸出。"),
        "1S0A9951.JPG": ("white_bg_05", "角度槽位 D：侧面低角度", "低位侧前方，杯身高度、外扩杯沿、圆环杯柄和底足支撑关系突出。"),
        "1S0A9970.JPG": ("white_bg_06", "角度槽位 D：侧面低角度", "低位侧前方，杯柄在右侧，杯口弱可见，侧面轮廓和底足清楚。"),
        "1S0A9972.JPG": ("white_bg_07", "角度槽位 B：45°斜侧视", "侧前方中景，杯柄在右侧，杯口内侧、杯身外壁和底足透视关系清楚。"),
        "1S0A9974.JPG": ("white_bg_08", "角度槽位 A：正面微俯视", "高位微俯视，杯柄在右后侧，杯口、杯内釉面和杯身正面渐变同时可见。"),
    }

    assets = []
    image_assets = []
    slot_lookup: dict[str, dict[str, Any]] = {}
    for image in white_bg:
        asset_id, slot, note = slot_map[image.name]
        slot_lookup[asset_id] = {"slot": slot, "note": note, "file": image}
        asset = {
            "asset_id": asset_id,
            "file_path": str(image),
            "asset_role": "single_product_white_bg_angle_reference",
            "is_single_product_white_bg": True,
            "is_set_group_shot": False,
            "is_style_reference": False,
            "bound_angle_slot": slot,
            "component_id": "single_mug",
            "notes": note,
        }
        assets.append(asset)
        image_assets.append({"asset_id": asset_id, "file_path": str(image), "notes": note})
    assets.extend(
        [
            {
                "asset_id": "style_ref_01",
                "file_path": str(style_ref),
                "asset_role": "style_reference",
                "is_single_product_white_bg": False,
                "is_set_group_shot": False,
                "is_style_reference": True,
                "bound_angle_slot": "",
                "component_id": "",
                "notes": "Fresh natural-light lifestyle reference with greenery, lemon, white flowers, pale stone, soft cloth and warm white background.",
            },
            {
                "asset_id": "product_info_01",
                "file_path": str(product_info_source),
                "asset_role": "product_info_supplement_source",
                "is_single_product_white_bg": False,
                "is_set_group_shot": False,
                "is_style_reference": False,
                "bound_angle_slot": "",
                "component_id": "",
                "notes": "User-provided product-info supplement source. It confirms product name only; size, capacity and marketing copy are blank.",
            },
        ]
    )
    asset_manifest = {"product_id": PRODUCT_ID, "artifact_type": "asset_manifest", "generated_at": checked_at, "assets": assets}
    write_json(paths["manifests"] / "asset_manifest.json", asset_manifest)
    write_md(paths["manifests"] / "asset_manifest.md", "Asset Manifest", asset_manifest)

    product_info = {
        "product_id": PRODUCT_ID,
        "artifact_type": "product_info_supplement",
        "missing_questions": ["尺寸信息未提供", "容量信息未提供", "中文营销文案未提供"],
        "confirmed_facts": {
            "商品名称": PRODUCT_NAME,
            "尺寸信息": "",
            "容量信息": "",
            "中文营销文案": "",
        },
        "unconfirmed_items": ["价格", "促销", "认证", "销量", "评价", "质保", "售后承诺", "退换政策", "执行标准", "包装清单", "产地", "适用人群"],
        "notes": "Only the four allowed product text fields are recorded. Blank fields must not be rendered as '未提供' or '资料缺失' in detail images.",
    }
    product_info_path = paths["artifacts"] / "identity" / "product_info_supplement.json"
    write_json(product_info_path, product_info)
    write_md(product_info_path.with_suffix(".md"), "商品信息补充清单", product_info)

    identity = {
        "product_id": PRODUCT_ID,
        "artifact_type": "product_identity_archive",
        "source_inputs": [str(path) for path in white_bg] + [str(product_info_source)],
        "identity": {
            "product_name": PRODUCT_NAME,
            "product_category": "陶瓷马克杯 / 咖啡杯 / 茶饮杯，按单品批次处理。",
            "batch_type_judgment": "用户未显式声明套装产品，按 single 执行；白底图只显示同一只带把马克杯，不启用套装 Skill。",
            "components": ["杯身", "杯口", "杯沿", "杯壁", "杯底/窄底足", "圆环杯柄"],
            "auxiliary_reference_usage": {
                "multi_angle_combined_reference_provided": "未提供",
                "confirmed_scope": "无",
                "unconfirmed_content": "背面完整细节、完整底面结构、真实尺寸、容量、重量无法确认。",
                "conflict_or_risk": "未发现明显冲突；白底图组仅用于单件结构辅助理解和角度入库，不固定后续所有出图角度。",
            },
            "core_shape": "矮宽杯身，杯口明显外扩，杯壁向底部轻微收窄，底部为较窄平底/底足；杯柄为圆润环形把手，上下连接点贴近杯身，整体没有杯盖、吸管、杯碟、勺或托盘。",
            "visual_proportions": "杯口宽于底部，杯身高度低于常见直筒马克杯，整体更接近浅口宽杯；圆环杯柄高度接近杯身高度的中上段，杯柄厚度圆润。",
            "true_dimensions": {
                "source": "无法确认",
                "confidence": "低",
                "height_cm": "无法确认",
                "maximum_width_or_outer_diameter_cm": "无法确认",
                "opening_diameter_cm": "无法确认",
                "bottom_diameter_cm": "无法确认",
                "capacity_ml": "无法确认",
                "weight_g": "无法确认",
                "accessory_dimensions": "无配件；尺寸无法确认",
                "usage_rule": "后续不得从白底图、手部或道具反推精确尺寸；不得输出厘米、毫升、克数等精确标注。",
            },
            "color_and_material": "亮面陶瓷釉。杯内和上半部为草绿色、青绿色、橄榄绿混合釉色，向下逐渐过渡为乳白/浅灰白釉面；表面分布绿色喷点、细小斑点和手作流釉颗粒。产品颜色最终以每张绑定白底图为唯一视觉参照。",
            "texture_and_surface": "杯体釉面有明显手作流釉感、颗粒喷点、局部垂流痕迹和烧制不均匀的自然过渡；表面整体为亮面陶瓷反光，不是塑料、玻璃或金属。",
            "pattern_and_decoration": "无独立具象图案、文字、品牌标识或金边装饰；装饰来自釉色渐变、喷点和流釉纹理。",
            "structural_details": "杯口外扩且边缘略有手作不完全规则感；杯内壁深绿色釉色明显；杯柄为圆润环形，与杯身连接处有釉色连续过渡；底部为较窄平底，完整底面不可见。",
            "angle_usage_rule": "本档案不固定产品展示角度。后续生成主图、详情图或场景图时，产品角度和颜色均以当次绑定白底图或角度入库表中绑定槽位对应白底图为唯一依据。",
            "must_keep": ["矮宽外扩杯身", "外扩杯口和厚实杯沿", "圆环杯柄", "窄底足/平底支撑", "青绿色/草绿色到乳白渐变釉", "绿色喷点和流釉颗粒", "亮面陶瓷材质", "无杯盖、无吸管、无杯碟、无金边、无独立图案"],
            "allowed_changes": ["背景", "桌面", "光线", "道具", "文字位置", "构图方式", "产品在画面中的位置和占比", "在角度入库表允许范围内切换绑定白底角度"],
            "prohibited_inventions": ["不要添加杯盖、吸管、杯碟、托盘、勺子或金边为商品本体", "不要把圆环杯柄改成方形或细长把手", "不要把矮宽杯身改成高直筒杯", "不要把青绿色渐变釉改成纯白、纯蓝、纯黄或其他颜色", "不要删除喷点和流釉纹理", "不要添加品牌字母、图案或花纹", "不要生成多个杯作为销售套装承诺", "不要从白底图推断厘米尺寸、容量或重量", "不要在尺寸无法确认时启用手持或强比例道具", "不要为了风格复现把产品重绘成参考图里的其他杯型"],
            "product_lock_description": "请严格保持产品本身不变：单只青见陶瓷马克杯，矮宽杯身、外扩杯口、厚实杯沿、圆环杯柄、较窄平底/底足，亮面陶瓷釉面呈青绿色/草绿色到乳白的自然渐变，并带绿色喷点与流釉颗粒。原图没有杯盖、吸管、杯碟、勺、托盘、金边、品牌字母或独立图案，不得增加。产品展示角度和商品本体颜色以当次绑定白底图为唯一依据；只允许改变背景、光线、道具、构图和文字排版，不允许重新设计产品，不允许根据白底图推断精确尺寸或容量。",
            "negative_prompt_constraints": "不要改形、改色、改材质；不要把矮宽外扩杯身改成高直筒杯；不要改变圆环杯柄、外扩杯口、窄底足、青绿色到乳白渐变釉、喷点和流釉颗粒；不要新增杯盖、吸管、杯碟、勺、托盘、金边、品牌字母或图案为商品本体；不要生成多个杯作为套装承诺；不要标注未提供的尺寸、容量或重量；不要乱码、错字、手部畸形、漂浮、穿模、边缘融化或塑料质感。",
        },
        "missing_information": ["真实高度", "最大宽度或外径", "口径", "底径", "容量", "重量", "完整底部细节"],
        "blocked_reasons": [],
        "notes": "Visual facts are based on the eight white-background product images. Real dimensions and capacity were not provided and are not inferred.",
    }
    identity_path = paths["artifacts"] / "identity" / "product_identity_archive.json"
    write_json(identity_path, identity)
    write_md(identity_path.with_suffix(".md"), "产品身份档案", identity)
    write_md(paths["drafts"] / "product_identity_draft.md", "Product Identity Draft", identity)

    style_master = {
        "product_id": PRODUCT_ID,
        "artifact_type": "style_master",
        "source_references": [str(style_ref)],
        "style_master": {
            "overall_positioning": "适合淘宝天猫杯具主图、详情页首屏和生活场景模块的清新自然光生活化商业摄影风格。",
            "layout": "主体位于下半区或中下区域，背景留出柔和空间；可做居中式、轻左右分区或场景式构图，具体构图服从单张页面任务。",
            "background_rules": "浅米白桌面、浅色石板或浅暖背景墙，真实空间背景而非纯色棚拍；可加入绿植虚化、柠檬、白花和浅色布料，保留自然褶皱、桌面/石板细微纹理和真实接触阴影。",
            "color_rules": "整体为浅米白、柔和绿、柠檬黄和低饱和自然光色；明度偏高、对比中低，不能用高饱和霓虹色或强烈冷暖滤镜影响商品本体颜色。",
            "lighting_rules": "柔和侧上方自然光，阴影边缘柔，产品釉面高光真实但不过曝；背景有植物投影和轻微明暗层次。",
            "subject_rules": "商品保持视觉中心，主图产品占比约 55%-72%，详情图可按任务做中近景或局部；不得因风格而改变产品角度、结构或颜色。",
            "prop_rules": "生活化弱道具可包括浅色布料、绿植枝条、柠檬片、白花、浅色石板、玻璃杯或木质小物；道具服务层次和清新饮用场景，不得被误认为商品组成。",
            "text_rules": "参考图无可读商品标题。后续如页面任务需要文字，可用深灰/墨绿色小面积中文文字；具体文案由变量配置决定，不复制未确认承诺。",
            "negative_rules": ["不要纯白棚拍退化", "不要复杂拼贴或多宫格", "不要强霓虹色", "不要硬阴影", "不要杂乱堆道具", "不要文字遮挡主体", "不要复制未确认的认证/销量/售后承诺", "不要为了风格改变产品身份或角度"],
            "style_fidelity_anchors": ["浅米白桌面/浅色石板", "柔化绿色枝叶形成背景层", "柠檬和白花作为清新生活化弱道具", "浅色布料自然褶皱", "柔和侧上方自然光与植物影", "高明度低对比的自然清新色调"],
            "reusable_prop_clusters": {
                "must_keep_for_high_fidelity": ["浅米白桌面/浅色石板", "柔化绿植", "浅色布料", "柔和自然光"],
                "replaceable_same_class": ["柠檬片", "白花", "玻璃杯", "浅色盘/板"],
                "optional": ["前景虚化叶片", "少量植物投影"],
            },
            "background_layers": {
                "foreground": "可有少量虚化绿叶、白花或柠檬，低干扰。",
                "midground": "产品主体清晰，位于浅色石板、布料或桌面上。",
                "background": "浅暖墙面、柔化绿植和自然投影形成生活空间。",
            },
            "prop_density_level": "丰富生活场景",
            "content_and_usage_state": "参考图表现空杯或杯内深色釉面；使用场景图可加入茶、柠檬茶或浅色饮品液面，内容物属于使用场景元素，不属于产品身份。",
            "text_inheritance": "参考图无明显可读标题文字。风格复现图默认不加大标题；如页面任务需要，只允许小面积低干扰中文文字。",
            "anti_degradation_rules": "不得退化为纯白或纯灰背景；不得删除主要前中后景层次；不得把生活化桌面改成孤立白底棚拍；不得用大标题遮挡产品。",
            "compact_style_master": "清新自然光杯具电商摄影风格：浅米白桌面或浅色石板，柔和侧上方自然光，产品置于布料或桌面中景，前景可有少量虚化绿叶、柠檬或白花，背景保留柔化绿植和浅暖墙面层次。道具密度为丰富生活场景，但不得堆叠或遮挡主体。参考图无明显标题，默认不加大标题；如需文字，只用小面积深灰/墨绿色中文。该风格只约束背景、光线、留白、文字气质和道具密度，不覆盖产品身份、角度、颜色和尺寸规则。",
        },
        "missing_information": [],
        "notes": "Style rules are separated from product identity. The reference image product is not copied as product fact.",
    }
    style_path = paths["artifacts"] / "style_master" / "style_master.json"
    write_json(style_path, style_master)
    write_md(style_path.with_suffix(".md"), "风格母版", style_master)
    write_md(paths["drafts"] / "style_master_draft.md", "Style Master Draft", style_master)

    angle_slots = []
    for image in white_bg:
        asset_id, slot, note = slot_map[image.name]
        if "角度槽位 A" in slot:
            natural = ["杯口外扩关系", "杯内深绿色釉面", "杯身前侧渐变釉", "杯柄连接位置"]
            avoid = ["完整垂直俯视布局", "完整底部结构", "背面细节"]
            tasks = ["主图第一印象", "杯口与杯内结构详情", "釉面纹理说明"]
        elif "角度槽位 C" in slot:
            natural = ["杯口圆形/椭圆形", "杯内底部和内壁釉色", "桌面平面摆放关系", "杯柄俯视轮廓"]
            avoid = ["杯身高度", "侧面线条", "底足支撑", "杯柄侧面结构"]
            tasks = ["杯口与内壁详情", "顶部饮品/液面场景", "桌面陈列"]
        elif "角度槽位 D" in slot:
            natural = ["杯身高度", "杯柄轮廓", "底足支撑", "侧面渐变釉"]
            avoid = ["完整杯内平面", "顶部桌面布局", "正面无透视展示"]
            tasks = ["侧面结构展示", "杯柄轮廓说明", "材质质感详情"]
        else:
            natural = ["杯身体积", "杯柄空间关系", "杯口厚度", "侧前方渐变釉"]
            avoid = ["完整顶部俯视", "完整底面", "背面结构"]
            tasks = ["生活场景主图", "整体识别", "杯柄与杯身关系", "釉面质感说明"]
        angle_slots.append(
            {
                "angle_slot": slot,
                "source_asset_id": asset_id,
                "file_name": image.name,
                "camera_angle": note,
                "visible_surfaces": natural,
                "not_to_force": avoid,
                "usable_for": tasks,
                "main_image_fit": "适合" if ("角度槽位 B" in slot or "角度槽位 A" in slot) else "勉强适合",
                "detail_image_fit": "适合",
                "risk_notes": "无明显风险" if image.name != "1S0A9948.JPG" else "顶部俯视图不适合展示杯身高度或杯柄侧面结构。",
                "recommended_binding_tasks": tasks,
                "inventory_result": "合格，可进入对应槽位",
                "notes": "无多角度合并参考图。",
            }
        )
    angle_inventory = {
        "product_id": PRODUCT_ID,
        "artifact_type": "angle_inventory",
        "image_assets": image_assets,
        "angle_slots": angle_slots,
        "missing_angle_slots": [],
        "notes": "All provided white-background images are single-product mug references. No set-product workflow is enabled. Slots A/B/C/D are represented.",
    }
    angle_path = paths["artifacts"] / "angle_inventory" / "angle_inventory.json"
    write_json(angle_path, angle_inventory)
    write_md(angle_path.with_suffix(".md"), "角度入库表", angle_inventory)

    def ba(asset_id: str) -> str:
        item = slot_lookup[asset_id]
        return f"{item['slot']}；对应白底图 {asset_id}，{item['file'].name}"

    main_configs = [
        variable_config(config_id="main_01", output_type="main", page_task="主图第一印象与商品识别", bound_angle=ba("white_bg_03"), composition="产品居中偏下完整展示，左上保留小面积标题区，背景延续浅米白清新自然光场景。", product_position="居中偏下，杯柄朝左后侧，杯口和杯身完整可见。", product_ratio="约 62%-68%，突出单只马克杯主体。", text="青见马克杯", prop_direction="1-3 个弱道具：浅色布料、虚化绿植或柠檬片；不遮挡杯口、杯沿、杯身渐变釉和杯柄。"),
        variable_config(config_id="main_02", output_type="main", page_task="生活化饮用氛围主图", bound_angle=ba("white_bg_07"), composition="产品偏右下中景，左侧和上方保留柔和留白，前景少量柠檬或绿叶虚化，背景可有白花与绿植。", product_position="偏右下，杯柄仍按绑定白底图朝右，不旋转。", product_ratio="约 55%-62%，保留桌面生活层次。", text="无", prop_direction="3-5 个生活化弱道具：布料、柠檬、白花、浅色石板、绿植；道具分布在前景/背景，不堆在杯身周围。"),
        variable_config(config_id="main_03", output_type="main", page_task="釉色纹理主视觉", bound_angle=ba("white_bg_01"), composition="中近景强调杯身青绿色到乳白渐变、喷点和圆环杯柄，背景简化为浅米白桌面与虚化绿植。", product_position="居中略偏右，杯身上半部釉色为视觉重点。", product_ratio="约 65%-72%，允许局部接近但不裁掉杯柄连接点。", text="自然流釉纹理", prop_direction="0-2 个弱道具：浅色布料或背景绿植；不得遮挡杯口、杯柄和喷点纹理。"),
        variable_config(config_id="main_04", output_type="main", page_task="顶部杯口与清新饮用感主图", bound_angle=ba("white_bg_04"), composition="顶部俯视桌面构图，杯口和杯内深绿色釉面清晰，可在周围留出柠檬/绿叶风格层次。", product_position="居中，杯柄按白底图朝左伸出，杯口为视觉中心。", product_ratio="约 50%-58%，保留俯视桌面空间。", text="宽口浅饮", prop_direction="2-4 个俯视弱道具：柠檬片、白花、浅色布料、绿叶；不得遮挡杯口或把道具误认为商品配件。"),
        variable_config(config_id="main_05", output_type="main", page_task="杯柄与侧面轮廓展示", bound_angle=ba("white_bg_06"), composition="侧面低角度生活化主图，杯身侧面与圆环杯柄清晰，右上保留小面积文字。", product_position="居中偏左，杯柄完整可见，底足与桌面接触清晰。", product_ratio="约 60%-68%，完整保留杯口、杯柄和底足。", text="宽口矮身", prop_direction="1-3 个弱道具：浅布、绿植、背景白花；全部低存在感，不形成尺寸暗示。"),
        variable_config(config_id="main_06", output_type="main", page_task="风格复现型品牌氛围主图", bound_angle=ba("white_bg_08"), composition="贴合参考图的自然光桌面场景，产品置于浅色石板/布料上，背景含柔化绿植、柠檬和白花。", product_position="偏右下，杯口和杯柄完整可见。", product_ratio="约 55%-62%，保留前中后景层次。", text="清新一杯", prop_direction="4-6 个非产品元素：布料、绿植、柠檬、白花、浅色石板、玻璃杯；作为风格锚点分层出现，不遮挡产品。"),
    ]
    main_common, main_compact = compact_variable_configs(main_configs)
    main_doc = {
        "product_id": PRODUCT_ID,
        "artifact_type": "main_variable_config",
        "config_count": 6,
        "upstream_artifacts": {
            "product_identity_archive": str(identity_path),
            "style_master": str(style_path),
            "angle_inventory": str(angle_path),
        },
        "common_constraints": main_common,
        "configs": main_compact,
        "notes": "Single-product main-image variable configs. Handheld count is 0 because true dimensions and capacity were not provided and dimension confidence is low.",
    }
    main_path = paths["artifacts"] / "variable_configs" / "main_variable_configs.json"
    write_json(main_path, main_doc)
    write_md(main_path.with_suffix(".md"), "主图变量配置", main_doc)

    detail_configs = [
        variable_config(config_id="detail_01", output_type="detail", page_task="模块01 首屏 · 主视觉与卖点承接", bound_angle=ba("white_bg_03"), composition="首屏左右轻分区，产品在右下中景完整展示，左上使用商品名与短副标题承接主图。", product_position="右下主体，杯柄朝左后侧，杯口和杯身完整可见。", product_ratio="约 58%-65%。", text="青见马克杯｜自然流釉", prop_direction="2-4 个生活化弱道具：布料、绿植、柠檬、浅色石板；不得遮挡主体。", extra={"标准模块归属": "模块01 首屏 · 主视觉与卖点承接", "买家疑问": "这是不是我刚才点进来的青见马克杯", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_02", output_type="detail", page_task="模块02 核心卖点证明", bound_angle=ba("white_bg_01"), composition="中近景突出杯口外扩、青绿色渐变和喷点纹理，文字在上方留白。", product_position="居中偏下，杯身上半部釉色清晰。", product_ratio="约 66%-72%。", text="青绿渐变釉", prop_direction="0-2 个弱道具，优先保留浅色布料或虚化绿植；细节不被道具干扰。", extra={"标准模块归属": "模块02 核心卖点证明", "买家疑问": "核心视觉特点在哪里", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_03", output_type="detail", page_task="模块03 使用场景与方法", bound_angle=ba("white_bg_07"), composition="生活化饮品桌面场景，可加入真实液面但不改变杯体；产品为中景主体，背景柔化。", product_position="居中偏右，杯柄朝右，杯口可见。", product_ratio="约 52%-60%。", text="茶饮与日常桌面", prop_direction="3-5 个使用场景道具：柠檬、白花、绿植、布料、玻璃杯；均为非商品道具，不暗示套装。", extra={"标准模块归属": "模块03 使用场景与方法", "买家疑问": "日常使用场景是否自然", "内容物状态": "可加入茶或柠檬茶的真实液面，液面服从杯口角度和重力；不标注容量。", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_04", output_type="detail", page_task="模块04 细节实拍与材质工艺", bound_angle=ba("white_bg_08"), composition="细节近景但保留足够杯型识别，强调杯内深绿色釉、喷点、流釉颗粒、杯沿厚度和亮面陶瓷高光。", product_position="居中，杯口和杯身上半部为重点，杯柄连接点不被裁掉。", product_ratio="约 68%-74%。", text="喷点与流釉颗粒", prop_direction="0-1 个极弱背景道具；材质说明图以产品细节为主，不堆道具。", extra={"标准模块归属": "模块04 细节实拍与材质工艺", "买家疑问": "釉面与细节是否清楚可信", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_05", output_type="detail", page_task="模块05 规格尺寸与容量", bound_angle=ba("white_bg_05"), composition="非精确尺寸模块：用完整杯身和杯口展示器型比例，不输出厘米、毫升或克数；文字只说明未含数值的器型信息。", product_position="居中偏左，右侧留出小面积说明区域。", product_ratio="约 58%-64%。", text="宽口矮身｜尺寸以实物为准", prop_direction="0-2 个弱道具，避免书本、手机、手部、小勺等强比例参照；不得暗示具体大小。", extra={"标准模块归属": "模块05 规格尺寸与容量", "买家疑问": "杯型比例大致如何", "尺寸标注信息": "尺寸来源无法确认；不允许渲染任何厘米、毫升或克数标注。", "尺寸标注图规则": "不画尺寸线、不画箭头、不输出精确数值；只可做非数值器型比例展示。"}),
        variable_config(config_id="detail_06", output_type="detail", page_task="模块06 质感可信视觉呈现", bound_angle=ba("white_bg_06"), composition="侧面低角度中近景，强调杯身侧面渐变釉、杯柄轮廓、底足接触阴影和真实陶瓷反光。", product_position="居中略偏左，底足与桌面接触清晰。", product_ratio="约 62%-70%。", text="亮面陶瓷釉感", prop_direction="1-3 个材质对比弱道具：布料、浅色石板、玻璃杯背景；不得遮挡杯柄和底足。", extra={"标准模块归属": "模块06 质感可信视觉呈现", "买家疑问": "实物质感是否可信", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_07", output_type="detail", page_task="模块07 决策辅助与场景想象", bound_angle=ba("white_bg_04"), composition="顶部俯视桌面氛围图，杯口与周边柠檬、白花、绿植形成清新摆放想象，文字小面积靠上。", product_position="居中，杯口清晰，杯柄朝左。", product_ratio="约 48%-56%。", text="清新桌面一角", prop_direction="3-5 个氛围道具：绿植、柠檬、白花、浅色布料、玻璃杯；不形成商品组成误解。", extra={"标准模块归属": "模块07 决策辅助与场景想象", "买家疑问": "它放在日常桌面中是否协调", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
        variable_config(config_id="detail_08", output_type="detail", page_task="模块08 收尾氛围与风险克制", bound_angle=ba("white_bg_03"), composition="克制收尾图，产品完整清晰，背景保留柔化绿植和暖米白空间，文字可无或极小。", product_position="居中偏下，保留完整轮廓。", product_ratio="约 55%-62%。", text="无", prop_direction="2-4 个低干扰道具：布料、绿植、柠檬、白花；不生成售后、质保、价格、促销或资料缺失说明。", extra={"标准模块归属": "模块08 收尾氛围与风险克制", "买家疑问": "最后的整体视觉印象是否真实克制", "尺寸标注信息": "非尺寸标注图，不启用尺寸标注信息", "尺寸标注图规则": "非尺寸标注图，不启用"}),
    ]
    detail_common, detail_compact = compact_variable_configs(detail_configs)
    detail_doc = {
        "product_id": PRODUCT_ID,
        "artifact_type": "detail_variable_config",
        "config_count": 8,
        "upstream_artifacts": {
            "product_identity_archive": str(identity_path),
            "style_master": str(style_path),
            "angle_inventory": str(angle_path),
            "product_info_supplement": str(product_info_path),
        },
        "detail_module_coverage_plan": [
            "模块01 首屏 · 主视觉与卖点承接",
            "模块02 核心卖点证明",
            "模块03 使用场景与方法",
            "模块04 细节实拍与材质工艺",
            "模块05 规格尺寸与容量（无精确数值标注）",
            "模块06 质感可信视觉呈现",
            "模块07 决策辅助与场景想象",
            "模块08 收尾氛围与风险克制",
        ],
        "common_constraints": detail_common,
        "configs": detail_compact,
        "notes": "Standard complete detail-page mode with modules 01-08. Size and capacity were not provided, so module 05 does not render exact numeric labels.",
    }
    detail_path = paths["artifacts"] / "variable_configs" / "detail_variable_configs.json"
    write_json(detail_path, detail_doc)
    write_md(detail_path.with_suffix(".md"), "详情图变量配置", detail_doc)

    routing = {
        "product_id": PRODUCT_ID,
        "artifact_type": "routing_decision",
        "status": "pass",
        "checked_at": checked_at,
        "routing_decision": "single-product workflow; set-product Skills disabled",
        "next_skill_sequence": ["product-identity-archive", "style-master-extractor", "angle-inventory", "main-variable-config", "detail-variable-config", "final-prompt-compiler", "qc-inspector"],
        "missing_upstream_artifacts": [],
        "reason": "User did not explicitly declare a set product. All provided white-background images show one mug.",
    }
    report(f"{PRODUCT_ID}_routing_decision", routing)
    report(
        f"{PRODUCT_ID}_stage_6_product_batch_intake_report",
        {
            "product_id": PRODUCT_ID,
            "stage": 6,
            "stage_name": "Product Batch Intake",
            "status": "pass",
            "checked_at": checked_at,
            "workspace_root": str(run_root),
            "manifest": str(MANIFEST_PATH),
            "workspace_manifest": str(paths["manifests"] / "batch_manifest.json"),
            "asset_manifest": str(paths["manifests"] / "asset_manifest.json"),
            "input_counts": {"white_bg_images": len(white_bg), "style_refs": len(style_refs), "product_info_text": 1},
            "image_generation_performed": False,
            "comfyui_execution_performed": False,
        },
    )
    report(
        f"{PRODUCT_ID}_stage_7_upstream_artifact_readiness_report",
        {
            "product_id": PRODUCT_ID,
            "stage": 7,
            "stage_name": "Upstream Artifact Readiness",
            "status": "pass",
            "checked_at": checked_at,
            "outputs": [str(identity_path), str(product_info_path), str(style_path), str(angle_path)],
            "missing_required_artifacts": [],
            "image_generation_performed": False,
            "comfyui_execution_performed": False,
        },
    )
    report(
        f"{PRODUCT_ID}_stage_8_variable_config_generation_report",
        {
            "product_id": PRODUCT_ID,
            "stage": 8,
            "stage_name": "Variable Config Generation",
            "status": "pass",
            "checked_at": checked_at,
            "outputs": [str(main_path), str(detail_path)],
            "main_config_count": 6,
            "detail_config_count": 8,
            "handheld_config_count": 0,
            "reason_no_handheld": "Product true dimensions and capacity were not provided; dimension confidence is low.",
            "image_generation_performed": False,
            "comfyui_execution_performed": False,
        },
    )

    print(
        json.dumps(
            {
                "status": "created",
                "product_id": PRODUCT_ID,
                "product_name": PRODUCT_NAME,
                "workspace_root": str(run_root),
                "white_bg_count": len(white_bg),
                "main_config_count": 6,
                "detail_config_count": 8,
                "identity": str(identity_path),
                "style_master": str(style_path),
                "angle_inventory": str(angle_path),
                "main_variable_configs": str(main_path),
                "detail_variable_configs": str(detail_path),
                "asset_manifest_sha256": file_sha256(paths["manifests"] / "asset_manifest.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
