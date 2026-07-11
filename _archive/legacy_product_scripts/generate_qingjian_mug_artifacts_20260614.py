from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from submit_comfy_cloud_jobs import DEFAULT_API_KEY_ENV, load_local_env


PROJECT = Path(r"D:\onedrive\OneDrive\Desktop\杯类6.08.0版本代码仓库")
RUN = Path(r"D:\onedrive\OneDrive\Desktop\杯类\qingjian_mug_20260614")
PRODUCT_ID = "qingjian_mug"
PRODUCT_NAME = "手绘蝴蝶咖啡杯"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_md(path: Path, title: str, data: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = data if isinstance(data, str) else "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")


def report(name: str, payload: dict) -> None:
    write_json(PROJECT / "reports" / f"{name}.json", payload)
    write_md(PROJECT / "reports" / f"{name}.md", name, payload)


def stable_json_sha256(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_variable_configs(configs: list[dict]) -> tuple[dict, list[dict]]:
    variable_configs = [item["variable_config"] for item in configs]
    common_keys = set(variable_configs[0])
    for item in variable_configs[1:]:
        common_keys &= set(item)

    common_constraints = {}
    for key in variable_configs[0]:
        if key in common_keys and all(item[key] == variable_configs[0][key] for item in variable_configs[1:]):
            common_constraints[key] = variable_configs[0][key]

    compact_configs = []
    for item in configs:
        overrides = {key: value for key, value in item["variable_config"].items() if key not in common_constraints}
        compact_configs.append({
            "config_id": item["config_id"],
            "output_type": item["output_type"],
            "per_image_overrides": overrides,
            "resolved_variable_config_sha256": stable_json_sha256(item["variable_config"]),
        })
    return common_constraints, compact_configs


def config_reference_lookup(doc: dict, path: Path, path_sha256: str) -> dict[str, dict]:
    refs = {}
    for index, item in enumerate(doc["configs"]):
        refs[item["config_id"]] = {
            "config_id": item["config_id"],
            "output_type": item["output_type"],
            "source_path": str(path),
            "source_sha256": path_sha256,
            "source_schema": "common_constraints + per_image_overrides",
            "common_constraints_ref": {
                "path": str(path),
                "json_pointer": "/common_constraints",
            },
            "per_image_overrides_ref": {
                "path": str(path),
                "json_pointer": f"/configs/{index}/per_image_overrides",
            },
            "resolved_variable_config_sha256": item["resolved_variable_config_sha256"],
        }
    return refs


def report_status(name: str) -> str | None:
    path = PROJECT / "reports" / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None


def report_unless_preserved(name: str, payload: dict) -> None:
    preserved_statuses = {"pass", "pass_with_manual_review_recommended", "complete", "completed"}
    if report_status(name) in preserved_statuses:
        return
    report(name, payload)


def cfg(
    config_id: str,
    module: str,
    slot_label: str,
    asset_id: str,
    file_name: str,
    composition: str,
    text: str,
    handheld: bool,
    output_type: str,
    extra: dict | None = None,
) -> dict:
    color_basis = (
        "商品本体颜色以绑定白底图为唯一颜色参照；风格母版、道具和环境光不得改变暖白陶瓷、"
        "绿色浮雕带、深棕描边和蓝色蝶形杯柄/小蝴蝶装饰。"
    )
    size_lock = (
        "尺寸来源为用户提供文本，置信度高：杯口直径 8cm，高度 8.5cm，杯口含杯柄最大宽度 13cm，"
        "容量 200ml；杯碟尺寸未提供，不得标注或推断。"
    )
    common_negative = (
        "不要改形、改色、删除杯碟或蝶形杯柄；不要新增金边、盖子、吸管、勺、托盘为商品本体；"
        "不要生成多个销售件数承诺；不要让道具遮挡杯口、蝶形杯柄、杯碟外圈或绿色浮雕带；"
        "不要乱码、错字、英文替代中文；不要出现产品污渍、裂纹、破损、做旧或明显 AI 融化边缘。"
    )
    data = {
        "页面任务": module,
        "绑定角度槽位": f"{slot_label}；对应白底图 {asset_id}，{file_name}",
        "角度适配原则": "以绑定白底图的朝向、俯仰、杯口可见程度、杯身透视和杯碟空间关系完成本张任务，不改变产品角度。",
        "产品角度依据": "本张产品角度以绑定角度槽位对应的白底产品图为唯一依据；其他图片只用于确认身份和风格。",
        "产品颜色依据": color_basis,
        "辅助参考图调用": "无多角度单件合并参考图；白底图组只作角度入库和结构辅助，不改变本张绑定角度与颜色依据。",
        "展示重点": "保持暖白陶瓷杯身、绿色浮雕边带、深棕描边、蓝色立体蝴蝶杯柄和匹配杯碟清晰可识别。",
        "构图方式": composition,
        "镜头距离": "中近景；产品清晰，非产品元素低干扰并有真实景深。",
        "产品位置": "按构图任务定位，但产品始终为视觉中心。",
        "产品占比": "主图约 55%-72%；详情图按模块可调整，产品占比只代表构图不代表实物尺寸改变。",
        "尺寸比例锁定": size_lock,
        "风格贴合锚点调用": ["暖奶油色桌面/背景", "柔化绿色植物层", "白色或浅蓝小花", "柔和侧上方自然光", "真实接触阴影"],
        "道具密度等级": "丰富生活场景" if ("生活" in module or "礼物" in module) else "克制",
        "背景层次配置": "前景少量花材或布料纹理，中景产品清晰，背景为奶油色空间与柔化绿植/花束；结构说明可简化但不退化为纯白棚拍。",
        "内容物状态": "默认空杯；仅使用场景任务可加入浅色茶/咖啡液面，且液面服从真实重力并不得遮挡杯口结构。",
        "手持交互声明": (
            "启用手持：成人手真实自然有血色，轻握蝶形杯柄或托住杯碟边缘，单手为主；不遮挡杯口、"
            "蝶形杯柄主体、绿色浮雕边带和杯碟装饰；产品角度仍服从绑定白底图。"
            if handheld
            else "本张图不启用手持场景"
        ),
        "动态手持样式参考图调用": "未提供，不调用" if handheld else "无",
        "背景与光线": "延续暖奶油生活化商业摄影；柔和侧上方自然光，接触阴影真实，背景/道具不抢主体。",
        "文字信息": "需要小面积中文文字，服务本张任务。" if text != "无" else "本张图不设置文字信息",
        "中文营销文案": text,
        "文字渲染要求": "只渲染【中文营销文案】列出的简体中文，深灰或灰褐色，清晰无乱码，不遮挡产品。" if text != "无" else "无",
        "真实感要求": "真实商业摄影质感；陶瓷釉面、高光、接触阴影和景深可信；避免塑料感、漂浮感、手部畸形、文字漂浮和 AI 融化边缘。",
        "风格防退化检查": "保留暖奶油背景、前/中/后景层次、柔化绿植/花材和真实接触阴影；若因产品识别简化道具，须保留至少两层背景关系。",
        "禁止事项": common_negative,
    }
    data["道具生成" if output_type == "main" else "道具关系"] = (
        "非产品元素控制在页面任务需要范围内，可用米白布料、白花、绿植虚化、少量咖啡豆或浅色纸张；"
        "不得遮挡杯口、蝶形杯柄、杯碟外圈和绿色浮雕纹理。非产品元素可有轻微真实纹理和接触阴影，不作用于产品本体。"
    )
    if extra:
        data.update(extra)
    return {"config_id": config_id, "output_type": output_type, "variable_config": data}


def main() -> int:
    load_local_env()
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    paths = {
        "manifests": RUN / "manifests",
        "inputs": RUN / "inputs",
        "drafts": RUN / "drafts",
        "artifacts": RUN / "artifacts",
        "outputs": RUN / "outputs",
    }
    for directory in [
        paths["manifests"],
        paths["drafts"],
        paths["inputs"] / "white_bg",
        paths["inputs"] / "style_refs",
        paths["inputs"] / "set_group",
        paths["inputs"] / "component_white_bg",
        paths["inputs"] / "product_info",
        paths["artifacts"] / "identity",
        paths["artifacts"] / "style_master",
        paths["artifacts"] / "angle_inventory",
        paths["artifacts"] / "variable_configs",
        paths["artifacts"] / "final_prompts",
        paths["artifacts"] / "comfyui_jobs",
        paths["artifacts"] / "qc_reports",
        paths["outputs"] / "renders",
        paths["outputs"] / "repaired",
        paths["outputs"] / "qc",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    white_bg = sorted((paths["inputs"] / "white_bg").glob("*.JPG"))
    style_ref = paths["inputs"] / "style_refs" / "风格参考图.png"
    product_info = paths["inputs"] / "product_info" / "商品信息补充清单提示词.txt"

    manifest = {
        "batch_id": PRODUCT_ID,
        "product_id": PRODUCT_ID,
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": ["main", "detail", "final_prompts"],
        "current_stage": "codex_standard_artifacts_ready",
        "next_skill": None,
        "workspace": {
            "mode": "external",
            "root": str(RUN),
            "layout": "external_run_folder_v1",
            "manifests_root": str(paths["manifests"]),
            "inputs_root": str(paths["inputs"]),
            "drafts_root": str(paths["drafts"]),
            "artifacts_root": str(paths["artifacts"]),
            "outputs_root": str(paths["outputs"]),
        },
        "inputs": {
            "white_bg_images": [str(paths["inputs"] / "white_bg")],
            "style_reference_images": [str(paths["inputs"] / "style_refs")],
            "set_group_images": [str(paths["inputs"] / "set_group")],
            "component_white_bg_images": [str(paths["inputs"] / "component_white_bg")],
            "product_info_text": [str(product_info)],
        },
        "drafts": {
            "product_identity_draft": str(paths["drafts"] / "product_identity_draft.md"),
            "style_master_draft": str(paths["drafts"] / "style_master_draft.md"),
        },
        "artifacts": {
            "asset_manifest": str(paths["manifests"] / "asset_manifest.json"),
            "product_identity_archive": str(paths["artifacts"] / "identity"),
            "style_master": str(paths["artifacts"] / "style_master"),
            "angle_inventory": str(paths["artifacts"] / "angle_inventory"),
            "main_variable_configs": [str(paths["artifacts"] / "variable_configs")],
            "detail_variable_configs": [str(paths["artifacts"] / "variable_configs")],
            "set_product_identity": "",
            "set_angle_layout_inventory": "",
            "final_prompts": [str(paths["artifacts"] / "final_prompts")],
            "comfyui_jobs": [str(paths["artifacts"] / "comfyui_jobs")],
            "qc_reports": [str(paths["artifacts"] / "qc_reports")],
        },
        "outputs": {"renders": [str(paths["outputs"] / "renders")], "repaired": [str(paths["outputs"] / "repaired")]},
        "missing_required_artifacts": [],
        "blocked_reasons": [],
        "notes": "2026-06-14 Codex rebuild from desktop 杯类 inputs. Rendering not executed because COMFY_CLOUD_API_KEY and workflow template are unavailable.",
    }
    write_json(PROJECT / "manifests" / f"{PRODUCT_ID}.batch_manifest.json", manifest)
    write_json(paths["manifests"] / "batch_manifest.json", manifest)

    slot_names = {
        "A": "角度槽位 A：正面微俯视",
        "B": "角度槽位 B：45°斜侧视",
        "C": "角度槽位 C：顶部俯视",
        "D": "角度槽位 D：侧面低角度",
    }
    angle_map = {
        "1S0A1232.JPG": ("B", "45°斜侧视，双件重复陈列，杯柄和杯碟空间关系可见"),
        "1S0A1233.JPG": ("B", "45°斜侧视，单杯碟组合，杯口、蝶形柄和杯碟关系清晰"),
        "1S0A1234.JPG": ("B", "多件斜侧陈列，适合作结构辅助，不作为套装件数依据"),
        "1S0A1235.JPG": ("A", "正面微俯视，杯口和杯碟外缘可见，主体识别清楚"),
        "1S0A1237.JPG": ("B", "重复杯碟斜侧陈列，能观察前后空间和蝶形柄"),
        "1S0A1238.JPG": ("A", "双件正面微俯视，适合主视觉第一印象"),
        "1S0A1240.JPG": ("A", "单杯碟正面微俯视，杯身轮廓、杯口与蝶形柄清楚"),
        "1S0A1241.JPG": ("B", "杯碟与分离杯碟的斜侧空间关系，适合配件关系说明"),
        "1S0A1242.JPG": ("D", "侧面低角度倾向，杯身高度、杯柄轮廓和分离杯碟可见"),
        "1S0A1243.JPG": ("D", "侧面低角度，杯身高度、蝶形柄和底部支撑关系突出"),
        "1S0A1244.JPG": ("C", "顶部俯视，杯碟平面陈列关系、杯口和盘面装饰清楚"),
        "1S0A1245.JPG": ("C", "俯视偏斜，杯碟、杯口和盘面蝶形装饰可见"),
        "1S0A1246.JPG": ("D", "侧后低角度，底部和杯碟关系可辅助观察，主体识别风险较高"),
        "1S0A1249.JPG": ("C", "顶部偏斜，分离杯碟与杯底/蝶柄辅助结构可见"),
        "1S0A1250.JPG": ("C", "顶部俯视，多杯碟平面陈列，适合桌面布局参考"),
    }
    slot_detail = {
        "A": (["整体第一印象", "杯身轮廓", "波浪杯口", "杯碟外圈", "蝶形杯柄侧部"], ["完整杯内壁", "完整顶部平面", "底部细节"], ["主图第一印象", "产品识别", "杯碟成组装饰感"]),
        "B": (["杯身体积", "杯口厚度", "蝶形杯柄", "杯碟空间关系", "陶瓷釉面"], ["正面无透视完整图案", "完整俯视杯内"], ["生活场景主图", "使用感", "立体结构说明", "材质质感说明"]),
        "C": (["杯口与杯内区域", "杯碟平面关系", "桌面陈列", "杯碟装饰分布"], ["杯身高度", "蝶形杯柄侧面厚度", "底足支撑"], ["俯视陈列", "规格标注辅助", "桌面搭配关系", "详情页模块信息承载"]),
        "D": (["杯身高度", "侧面线条", "蝶形杯柄轮廓", "底部/杯碟支撑关系"], ["完整顶部平面", "完整杯内壁", "正面图案全貌"], ["侧面结构展示", "拿起/手持使用感", "杯柄轮廓说明"]),
    }

    assets = []
    image_assets = []
    for index, image in enumerate(white_bg, start=1):
        slot, note = angle_map[image.name]
        asset_id = f"white_bg_{index:02d}"
        assets.append({
            "asset_id": asset_id,
            "file_path": str(image),
            "asset_role": "single_product_white_bg_angle_reference",
            "is_single_product_white_bg": True,
            "is_set_group_shot": False,
            "is_style_reference": False,
            "bound_angle_slot": slot_names[slot],
            "component_id": "cup_and_saucer_single_product",
            "notes": note,
        })
        image_assets.append({"asset_id": asset_id, "file_path": str(image), "notes": note})
    assets.extend([
        {
            "asset_id": "style_ref_01",
            "file_path": str(style_ref),
            "asset_role": "style_reference",
            "is_single_product_white_bg": False,
            "is_set_group_shot": False,
            "is_style_reference": True,
            "bound_angle_slot": "",
            "component_id": "",
            "notes": "Warm cream lifestyle e-commerce reference with soft sunlight, plants, flowers, and restrained Chinese typography.",
        },
        {
            "asset_id": "product_info_01",
            "file_path": str(product_info),
            "asset_role": "product_info_supplement",
            "is_single_product_white_bg": False,
            "is_set_group_shot": False,
            "is_style_reference": False,
            "bound_angle_slot": "",
            "component_id": "",
            "notes": "User-provided four-field product text supplement: product name, size info, capacity info, and Chinese marketing copy.",
        },
    ])
    asset_manifest = {"product_id": PRODUCT_ID, "artifact_type": "asset_manifest", "generated_at": now, "assets": assets}
    write_json(paths["manifests"] / "asset_manifest.json", asset_manifest)
    write_md(paths["manifests"] / "asset_manifest.md", "Asset Manifest", asset_manifest)

    identity = {
        "product_id": PRODUCT_ID,
        "artifact_type": "product_identity_archive",
        "source_inputs": [str(p) for p in white_bg] + [str(product_info)],
        "identity": {
            "product_name": PRODUCT_NAME,
            "product_category": "陶瓷咖啡杯 / 杯碟组合型杯具。按单品批次处理；白底图中重复出现的同款杯碟仅作为角度和结构参考，不作为套装件数承诺。",
            "batch_type_judgment": "用户未显式声明套装产品，按 single 执行；商品本体按一套杯身与匹配杯碟的组合建档。",
            "components": ["杯身", "杯口与波浪杯沿", "杯壁", "杯底/底足", "立体蓝色蝴蝶杯柄", "匹配杯碟", "杯碟边缘立体小蝴蝶装饰"],
            "core_shape": "杯身为偏直筒并轻微外扩的矮杯形，杯口呈波浪花瓣状边缘；杯碟为浅盘，外圈同样呈波浪花瓣形。杯柄为立体展开的蓝色蝴蝶造型，从杯身侧面连接。",
            "true_dimensions": {"source": "用户提供商品信息文本", "confidence": "高", "height_cm": "8.5", "maximum_width_or_outer_diameter_cm": "13（杯口含杯柄）", "opening_diameter_cm": "8", "bottom_diameter_cm": "无法确认", "capacity_ml": "200", "weight_g": "无法确认", "accessory_dimensions": "杯碟尺寸未提供，不得推断"},
            "color_and_material": "陶瓷材质；杯身主体为暖白/象牙白釉面；杯口及杯碟外圈有绿色浮雕纹理带，边缘带深棕色手绘釉线；蝶形杯柄和杯碟小蝴蝶装饰为蓝色至青蓝色釉面。",
            "texture_and_surface": "整体为亮面陶瓷釉，绿色装饰带具有凹凸浮雕纹理，蓝色蝶形部分有翅脉压纹；深棕边线有手绘不完全均匀感。",
            "must_keep": ["暖白陶瓷杯身与亮面釉质", "波浪花瓣状杯口和杯碟外沿", "绿色浮雕纹理带", "深棕色描边", "蓝色立体蝴蝶杯柄", "杯碟上的蓝色小蝴蝶装饰", "杯身与杯碟的匹配关系", "用户提供的尺寸与容量字段"],
            "allowed_changes": ["背景", "桌面", "光线", "道具", "文字位置", "镜头距离", "产品在画面中的位置和占比", "在角度入库表允许范围内切换绑定白底角度"],
            "prohibited_inventions": ["不得把单件杯碟组合擅自改成多件套销售承诺", "不得删除杯碟", "不得删除或改形蓝色蝴蝶杯柄", "不得把杯柄改成普通圆耳把手", "不得增加金边、盖子、吸管、勺子或托盘为商品本体", "不得改变绿色纹理带和深棕描边", "不得把陶瓷改成玻璃、金属或塑料", "不得根据图片推断杯碟尺寸、重量、认证、质保或售后", "不得让产品出现原图不存在的污渍、裂纹、划痕或做旧痕迹"],
            "product_lock_description": "请严格保持手绘蝴蝶咖啡杯本身不变：暖白亮面陶瓷杯身、波浪花瓣状杯口、绿色浮雕纹理边带、深棕手绘描边、蓝色立体蝴蝶杯柄、匹配杯碟及杯碟蓝色小蝴蝶装饰均不得改变。产品角度以本张绑定白底图为唯一依据，产品颜色以本张绑定白底图为唯一颜色参照。只允许改变背景、光线、道具、构图和文字排版，不允许重新设计产品或补出当前角度不可见结构。",
        },
        "missing_information": ["杯碟直径", "底径", "重量", "包装清单", "品牌资质", "检测报告", "认证", "专利", "销量", "评价摘录", "质保", "售后流程", "退换边界", "执行标准"],
        "blocked_reasons": [],
        "notes": "Identity generated from user text plus visual inspection. Set-product Skills intentionally not enabled.",
    }
    write_json(paths["artifacts"] / "identity" / "product_identity_archive.json", identity)
    write_md(paths["artifacts"] / "identity" / "product_identity_archive.md", "产品身份档案", identity)
    write_md(paths["drafts"] / "product_identity_draft.md", "Product Identity Draft", identity)

    style = {
        "product_id": PRODUCT_ID,
        "artifact_type": "style_master",
        "source_references": [str(style_ref)],
        "style_master": {
            "overall_positioning": "适合陶瓷杯类主图、详情页首屏和生活化场景图的柔和暖调商业摄影风格。",
            "layout": "方形主视觉，产品在下半部偏中间形成强主体，上方与右上方留文字区域；详情图可变体为左右信息分区。",
            "background_rules": "米白、奶油白、浅暖灰背景；真实桌面/织物承托，背景有柔化植物、白色花材和自然光影层次，不退化为纯白棚拍。",
            "color_rules": "整体暖奶油色为主，低饱和绿植和浅蓝花材作辅助；对比度中低，产品本体颜色不被环境色重染。",
            "lighting_rules": "柔和自然侧逆光或侧上方日光，阴影柔但有方向；桌面和杯碟下方保留真实接触阴影。",
            "style_anchors": ["暖奶油色桌面和背景", "左后方或上方柔化绿色植物层", "白色或浅蓝小花作为低干扰前景/背景", "柔和侧上方自然光与真实接触阴影", "产品下半部强主体，右上或侧边留文字空间", "真实布料/桌面细微纹理", "低饱和、浅景深生活化层次"],
            "background_layers": {"foreground": "少量白花、布料褶皱或浅色桌面纹理，可轻微虚化", "midground": "产品与杯碟，保持清晰主体", "background": "柔化绿植、花束、奶油色布景和自然光斑"},
            "prop_density_level": "丰富生活场景",
            "content_state": "参考图为空杯，无咖啡/茶/奶泡内容物；后续仅在使用场景任务需要时加入内容物。",
            "text_presence_inheritance": "参考图含文字；可按页面任务添加小面积中文文字，不复制原文。",
            "anti_degradation_rules": ["不得退化为纯白/纯灰背景", "不得删除主要前中后景层次", "不得把生活化桌面变成孤立棚拍", "不得用大标题压住产品或删除主要风格锚点"],
        },
        "missing_information": [],
        "notes": "Style-only extraction; reference product appearance does not override product identity.",
    }
    write_json(paths["artifacts"] / "style_master" / "style_master.json", style)
    write_md(paths["artifacts"] / "style_master" / "style_master.md", "风格母版", style)
    write_md(paths["drafts"] / "style_master_draft.md", "Style Master Draft", style)

    angle_slots = []
    for index, image in enumerate(white_bg, start=1):
        slot, note = angle_map[image.name]
        natural, forbidden, tasks = slot_detail[slot]
        angle_slots.append({
            "angle_slot": slot_names[slot],
            "source_asset_id": f"white_bg_{index:02d}",
            "file_name": image.name,
            "camera_angle": note,
            "natural_visible_content": natural,
            "do_not_force": forbidden,
            "usable_for": tasks,
            "suitable_for_main": "适合" if slot in {"A", "B"} else "勉强适合",
            "suitable_for_detail": "适合",
            "risk_notes": "白底图中存在重复杯碟或多个同款角度时，仅作结构/角度辅助，不推断套装件数。" if image.name in {"1S0A1232.JPG", "1S0A1234.JPG", "1S0A1237.JPG", "1S0A1238.JPG", "1S0A1250.JPG"} else "无明显风险",
            "inventory_result": "勉强可用，但建议重拍" if image.name == "1S0A1246.JPG" else "合格，可进入对应槽位",
            "notes": note,
        })
    angle_inventory = {"product_id": PRODUCT_ID, "artifact_type": "angle_inventory", "image_assets": image_assets, "angle_slots": angle_slots, "missing_angle_slots": [], "notes": "All A/B/C/D slots are represented. Repeated same-product arrangements do not activate set-product workflow."}
    write_json(paths["artifacts"] / "angle_inventory" / "angle_inventory.json", angle_inventory)
    write_md(paths["artifacts"] / "angle_inventory" / "angle_inventory.md", "角度入库表", angle_inventory)

    main_defs = [
        ("main_01", "主图第一印象", "角度槽位 A：正面微俯视", "white_bg_07", "1S0A1240.JPG", "产品居中偏下完整展示，右上保留小面积中文标题区域", "主标题：立体蝶柄；副标题：杯碟成组更有装饰感", False),
        ("main_02", "生活化柔光主视觉", "角度槽位 B：45°斜侧视", "white_bg_02", "1S0A1233.JPG", "产品偏左前景，米白布料与绿植虚化形成层次", "无", True),
        ("main_03", "杯碟组合价值", "角度槽位 B：45°斜侧视", "white_bg_08", "1S0A1241.JPG", "斜侧中景，杯与碟关系清楚，少量小花和咖啡豆分布在边缘", "主标题：杯碟成组；副标题：一杯一碟完整搭配", False),
        ("main_04", "俯视桌面陈列", "角度槽位 C：顶部俯视", "white_bg_11", "1S0A1244.JPG", "顶部俯视，产品与杯碟平面关系清楚，文字沿右侧留白排布", "主标题：手绘蝴蝶咖啡杯", False),
        ("main_05", "手持使用感", "角度槽位 D：侧面低角度", "white_bg_10", "1S0A1243.JPG", "侧面低角度，成人手轻握蝶形杯柄或托住杯碟边缘，产品仍为视觉中心", "无", True),
        ("main_06", "礼物感收尾主视觉", "角度槽位 C：顶部俯视", "white_bg_12", "1S0A1245.JPG", "俯视偏斜构图，产品偏右下，前景白花和浅色纸张营造礼物感", "主标题：手绘蝴蝶杯；副标题：日常饮用与陈列皆宜", True),
    ]
    main_configs = [cfg(*item, output_type="main") for item in main_defs]
    main_common_constraints, main_compact_configs = compact_variable_configs(main_configs)
    main_doc = {
        "product_id": PRODUCT_ID,
        "artifact_type": "main_variable_config",
        "config_count": 6,
        "upstream_artifacts": {
            "product_identity_archive": str(paths["artifacts"] / "identity" / "product_identity_archive.json"),
            "style_master": str(paths["artifacts"] / "style_master" / "style_master.json"),
            "angle_inventory": str(paths["artifacts"] / "angle_inventory" / "angle_inventory.json"),
        },
        "common_constraints": main_common_constraints,
        "configs": main_compact_configs,
        "notes": "Single-product main-image variable configs. Handheld enabled in 3/6 because dimensions have high confidence and selected angles support safe hand interaction.",
    }
    main_variable_config_path = paths["artifacts"] / "variable_configs" / "main_variable_configs.json"
    write_json(main_variable_config_path, main_doc)
    write_md(paths["artifacts"] / "variable_configs" / "main_variable_configs.md", "主图变量配置", main_doc)
    main_refs = config_reference_lookup(main_doc, main_variable_config_path, file_sha256(main_variable_config_path))

    detail_defs = [
        ("detail_01", "模块01 首屏 · 主视觉与卖点承接", "角度槽位 B：45°斜侧视", "white_bg_02", "1S0A1233.JPG", "左右轻分区，产品大面积清晰展示，右侧承接主图核心卖点", "主标题：手绘蝴蝶咖啡杯；副标题：杯碟成组更有装饰感", False, "回答买家：这是不是我想点进来的杯子"),
        ("detail_02", "模块02 核心卖点证明", "角度槽位 A：正面微俯视", "white_bg_07", "1S0A1240.JPG", "近中景强调立体蓝色蝴蝶杯柄、绿色浮雕边带和深棕描边", "主标题：立体蝴蝶杯柄；卖点：可见立体装饰 / 杯碟呼应", False, "回答买家：装饰点具体在哪里"),
        ("detail_03", "模块03 使用场景与方法", "角度槽位 B：45°斜侧视", "white_bg_03", "1S0A1234.JPG", "生活化桌面中景，手轻扶杯柄或托杯碟边缘，表达日常饮用拿取感", "主标题：日常一杯；副标题：咖啡与茶饮皆可入镜表达", True, "回答买家：日常使用感如何"),
        ("detail_04", "模块04 细节实拍与材质工艺", "角度槽位 D：侧面低角度", "white_bg_10", "1S0A1243.JPG", "局部近景但保留识别信息，强调釉面反光、浮雕纹理和翅脉压纹", "主标题：浮雕纹理可见；卖点：亮面陶瓷釉 / 手绘描边", False, "回答买家：质感和细节是否清楚"),
        ("detail_05", "模块05 规格尺寸与容量", "角度槽位 A：正面微俯视", "white_bg_04", "1S0A1235.JPG", "产品居左，右侧克制参数区，标注已确认尺寸，不标注杯碟尺寸", "主标题：规格尺寸；卖点：杯口直径8cm / 高度8.5cm / 容量200ml", False, "回答买家：尺寸和容量是多少"),
        ("detail_06", "模块06 质感可信视觉呈现", "角度槽位 C：顶部俯视", "white_bg_11", "1S0A1244.JPG", "俯视桌面陈列，利用真实釉面高光、杯碟接触阴影和细节清晰度表达可信质感", "主标题：亮面陶瓷质感；副标题：杯碟细节清晰可见", False, "回答买家：实物质感是否可信"),
        ("detail_07", "模块07 决策辅助", "角度槽位 C：顶部俯视", "white_bg_15", "1S0A1250.JPG", "俯视陈列结合生活场景，辅助说明装饰陈列与日常饮用两类使用画面", "主标题：装饰与日用；副标题：一杯一碟的桌面存在感", False, "回答买家：适合怎样摆放和使用"),
        ("detail_08", "模块08 收尾氛围与风险克制", "角度槽位 B：45°斜侧视", "white_bg_08", "1S0A1241.JPG", "克制收尾图，产品清晰完整，生活化背景形成温和记忆点，不生成售后、退换、质保或资料缺失说明", "主标题：温柔桌面一角；副标题：杯碟成组更有装饰感", True, "回答买家：整体摆放效果是否自然"),
    ]
    detail_configs = []
    for item in detail_defs:
        config_id, module, *_rest, question = item
        is_size = config_id == "detail_05"
        extra = {
            "标准模块归属": module,
            "买家疑问": question,
            "信息来源与可用证据": "产品身份档案、绑定白底图、商品信息补充清单四项字段；不把未提供或资料缺失说明渲染到画面中。",
            "平台硬约束检查": "商品名称、尺寸、容量和卖点描述需与主图、产品身份档案和商品信息补充清单四项字段一致；图片建议宽度 1440px，单文件不超过 20MB；AI 场景化不得造成货不对板、材质/颜色/大小失真或抠图贴图感。",
            "尺寸标注信息": "尺寸来源：用户提供文本；尺寸置信度：高；允许标注：杯口直径8cm、高度8.5cm、杯口含杯柄13cm、容量200ml、材质陶瓷；禁止标注：杯碟尺寸、重量、底径。" if is_size else "非尺寸标注图，不启用尺寸标注信息",
            "尺寸标注图规则": "标注线和参数框属于信息标注层，不计入道具；只标注已确认字段，不遮挡杯口、蝶形杯柄、杯碟外圈或绿色浮雕带；不得根据白底图推断杯碟尺寸。" if is_size else "非尺寸标注图，不启用",
        }
        detail_configs.append(cfg(*item[:-1], output_type="detail", extra=extra))
    detail_common_constraints, detail_compact_configs = compact_variable_configs(detail_configs)
    detail_doc = {
        "product_id": PRODUCT_ID,
        "artifact_type": "detail_variable_config",
        "config_count": 8,
        "upstream_artifacts": {
            "product_identity_archive": str(paths["artifacts"] / "identity" / "product_identity_archive.json"),
            "style_master": str(paths["artifacts"] / "style_master" / "style_master.json"),
            "angle_inventory": str(paths["artifacts"] / "angle_inventory" / "angle_inventory.json"),
            "product_info_supplement": str(product_info),
        },
        "common_constraints": detail_common_constraints,
        "configs": detail_compact_configs,
        "notes": "Standard complete detail-page mode with modules 01-08. Unsupported certificates, reviews, warranty and return promises are not generated.",
    }
    detail_variable_config_path = paths["artifacts"] / "variable_configs" / "detail_variable_configs.json"
    write_json(detail_variable_config_path, detail_doc)
    write_md(paths["artifacts"] / "variable_configs" / "detail_variable_configs.md", "详情图变量配置", detail_doc)
    detail_refs = config_reference_lookup(detail_doc, detail_variable_config_path, file_sha256(detail_variable_config_path))
    variable_config_refs = {**main_refs, **detail_refs}

    asset_lookup = {f"white_bg_{i:02d}": str(p) for i, p in enumerate(white_bg, start=1)}
    product_lock = identity["identity"]["product_lock_description"]
    size_lock = "杯口直径 8cm，高度 8.5cm，杯口含杯柄最大宽度 13cm，容量 200ml；杯碟尺寸未提供，不得标注或推断。"
    common_negative = "不要改形、改色、删除杯碟或蝶形杯柄；不要新增金边、盖子、吸管、勺、托盘为商品本体；不要生成多个销售件数承诺；不要乱码、错字或明显 AI 融化边缘。"
    jobs = []
    index_items = []
    for item in main_configs + detail_configs:
        config_id = item["config_id"]
        output_type = item["output_type"]
        vc = item["variable_config"]
        ref_asset = vc["绑定角度槽位"].split("对应白底图 ")[1].split("，")[0]
        reference = asset_lookup[ref_asset]
        text = vc.get("中文营销文案", "无")
        prompt = "\n".join([
            f"生成一张淘宝天猫电商{'主图' if output_type == 'main' else '详情图'}，任务：{vc['页面任务']}。",
            f"产品锁定：{product_lock}",
            f"绑定白底参考图：{vc['绑定角度槽位']}。必须保持该图的产品角度、杯身透视、杯碟空间关系和商品本体颜色。",
            f"真实尺寸锁定：{size_lock}",
            f"构图：{vc['构图方式']}；镜头距离：{vc['镜头距离']}。",
            "风格：暖奶油色生活化商业摄影，柔和侧上方自然光，米白桌面/布料，柔化绿植和白色/浅蓝花材形成前中后景层次；真实接触阴影，不退化为纯白棚拍。",
            f"道具与背景：{vc.get('道具生成', vc.get('道具关系', ''))}",
            f"手持：{vc['手持交互声明']}",
            f"文字：{text}。如果为“无”，不要渲染任何文字；如有文字，仅渲染列出的简体中文，深灰/灰褐色，清晰无乱码，不遮挡产品。",
            "画面必须像真实商业摄影，陶瓷釉面、高光、阴影、接触关系和景深可信。",
        ])
        if output_type == "detail" and vc.get("尺寸标注信息", "").startswith("尺寸来源"):
            prompt += f"\n尺寸标注：{vc['尺寸标注信息']}；{vc['尺寸标注图规则']}"
        final_doc = {
            "product_id": PRODUCT_ID,
            "artifact_type": "final_prompt",
            "upstream_artifacts": {
                "product_identity_archive": str(paths["artifacts"] / "identity" / "product_identity_archive.json"),
                "style_master": str(paths["artifacts"] / "style_master" / "style_master.json"),
                "angle_inventory": str(paths["artifacts"] / "angle_inventory" / "angle_inventory.json"),
                "variable_config": str(main_variable_config_path if output_type == "main" else detail_variable_config_path),
                "realism_constraints": str(PROJECT / "真实感约束.txt"),
                "prop_rules": str(PROJECT / "道具生成规则模块.txt"),
                "platform_rules": str(PROJECT / "淘宝天猫详情页链路与平台规范模块.txt"),
                "qc_checklist": str(PROJECT / "电商图片通用质检清单.txt"),
            },
            "variable_config": variable_config_refs[config_id],
            "uses_upstream_prompt_files_as_visual_requirements": False,
            "final_prompt": prompt,
            "negative_prompt": common_negative,
            "notes": "Compiled from generated upstream artifacts and this-image variable config only. The render entry is final_prompt plus negative_prompt; variable_config is a resolvable reference.",
        }
        prompt_json = paths["artifacts"] / "final_prompts" / f"{config_id}_final_prompt.json"
        write_json(prompt_json, final_doc)
        write_md(paths["artifacts"] / "final_prompts" / f"{config_id}_final_prompt.md", f"{config_id} Final Prompt", final_doc)
        index_items.append({"job_id": config_id, "output_type": output_type, "final_prompt_path": str(prompt_json), "bound_reference": reference})
        jobs.append({
            "job_id": config_id,
            "output_type": output_type,
            "final_prompt_path": str(prompt_json),
            "required_product_reference": reference,
            "style_reference": str(style_ref),
            "output_target_dir": str(paths["outputs"] / "renders"),
            "width": 1440,
            "height": 1440 if output_type == "main" else 1920,
            "notes": "Prepared for ComfyUI/Comfy Cloud execution; not submitted by this artifact generation step.",
        })

    final_index = {"product_id": PRODUCT_ID, "artifact_type": "final_prompt_index", "prompt_count": len(index_items), "uses_upstream_prompt_files_as_visual_requirements": False, "items": index_items}
    write_json(paths["artifacts"] / "final_prompts" / "final_prompt_index.json", final_index)
    write_md(paths["artifacts"] / "final_prompts" / "final_prompt_index.md", "Final Prompt Index", final_index)

    comfy_blocked_reasons = ["No ComfyUI API workflow template JSON was found in the repository"]
    if not os.environ.get(DEFAULT_API_KEY_ENV):
        comfy_blocked_reasons.insert(0, "COMFY_CLOUD_API_KEY is not set")

    comfy = {
        "product_id": PRODUCT_ID,
        "artifact_type": "comfyui_job",
        "generated_at": now,
        "job_count": len(jobs),
        "execution_layer": "ComfyUI / Comfy Cloud",
        "execution_status": "prepared_not_submitted",
        "blocked_reasons": comfy_blocked_reasons,
        "jobs": jobs,
    }
    write_json(paths["artifacts"] / "comfyui_jobs" / "comfyui_job_manifest.json", comfy)
    write_md(paths["artifacts"] / "comfyui_jobs" / "comfyui_job_manifest.md", "ComfyUI Job Manifest", comfy)

    qc_boundary = {
        "product_id": PRODUCT_ID,
        "artifact_type": "qc_report",
        "checked_assets": [str(paths["artifacts"] / "final_prompts" / "final_prompt_index.json"), str(paths["artifacts"] / "comfyui_jobs" / "comfyui_job_manifest.json")],
        "results": [
            {"check_item": "final prompts use generated upstream artifacts, not upstream prompt files", "status": "pass"},
            {"check_item": "single-product routing; set-product Skills not enabled", "status": "pass"},
            {"check_item": "ComfyUI render outputs available for post-generation QC", "status": "not_applicable", "notes": "No rendered outputs exist yet."},
        ],
        "issues": [{"issue_id": "render_execution_missing", "severity": "needs_review", "description": "Rendering is not executed because no ComfyUI API workflow template is available."}],
        "repair_targets": [],
        "adds_new_generation_direction": False,
        "notes": "Pre-render boundary report only. Full QC must run after rendered images exist.",
    }
    write_json(paths["artifacts"] / "qc_reports" / "pre_render_qc_boundary_report.json", qc_boundary)
    write_md(paths["artifacts"] / "qc_reports" / "pre_render_qc_boundary_report.md", "Pre-render QC Boundary Report", qc_boundary)

    report("qingjian_mug_routing_decision", {
        "product_id": PRODUCT_ID,
        "status": "pass",
        "checked_at": now,
        "routing_decision": "single-product workflow; set-product Skills disabled",
        "next_skill_sequence": ["product-identity-archive", "style-master-extractor", "angle-inventory", "main-variable-config", "detail-variable-config", "final-prompt-compiler"],
        "missing_upstream_artifacts": [],
        "reason": "User did not explicitly declare a set product. Cup-and-saucer compositions are handled as one product with components.",
    })
    report("qingjian_mug_stage_6_product_batch_intake_report", {"product_id": PRODUCT_ID, "stage": 6, "stage_name": "Product Batch Intake", "status": "pass", "checked_at": now, "workspace_root": str(RUN), "manifest": str(PROJECT / "manifests" / f"{PRODUCT_ID}.batch_manifest.json"), "workspace_manifest": str(paths["manifests"] / "batch_manifest.json"), "asset_manifest": str(paths["manifests"] / "asset_manifest.json"), "input_counts": {"white_bg_images": len(white_bg), "style_refs": 1, "product_info_text": 1}, "image_generation_performed": False, "comfyui_execution_performed": False})
    report("qingjian_mug_stage_7_upstream_artifact_readiness_report", {"product_id": PRODUCT_ID, "stage": 7, "stage_name": "Upstream Artifact Readiness", "status": "pass", "checked_at": now, "outputs": [str(paths["artifacts"] / "identity" / "product_identity_archive.json"), str(paths["artifacts"] / "style_master" / "style_master.json"), str(paths["artifacts"] / "angle_inventory" / "angle_inventory.json")], "missing_required_artifacts": [], "image_generation_performed": False, "comfyui_execution_performed": False})
    report("qingjian_mug_stage_8_variable_config_generation_report", {"product_id": PRODUCT_ID, "stage": 8, "stage_name": "Variable Config Generation", "status": "pass", "checked_at": now, "outputs": [str(paths["artifacts"] / "variable_configs" / "main_variable_configs.json"), str(paths["artifacts"] / "variable_configs" / "detail_variable_configs.json")], "main_config_count": 6, "detail_config_count": 8, "image_generation_performed": False, "comfyui_execution_performed": False})
    report("qingjian_mug_stage_9_final_prompt_compilation_report", {"product_id": PRODUCT_ID, "stage": 9, "stage_name": "Final Prompt Compilation", "status": "pass", "checked_at": now, "outputs": [str(paths["artifacts"] / "final_prompts" / "final_prompt_index.json")], "prompt_count": len(index_items), "uses_upstream_prompt_files_as_visual_requirements": False, "image_generation_performed": False, "comfyui_execution_performed": False})
    report("qingjian_mug_stage_10_comfyui_render_job_preparation_report", {"product_id": PRODUCT_ID, "stage": 10, "stage_name": "ComfyUI Render Job Preparation", "status": "pass", "checked_at": now, "outputs": [str(paths["artifacts"] / "comfyui_jobs" / "comfyui_job_manifest.json")], "job_count": len(jobs), "notes": "Prepared ComfyUI job manifest only. No ComfyUI call was made.", "image_generation_performed": False, "comfyui_execution_performed": False})
    report_unless_preserved("qingjian_mug_stage_11_rendering_report", {"product_id": PRODUCT_ID, "stage": 11, "stage_name": "Rendering", "status": "blocked_missing_execution_layer", "checked_at": now, "blocked_reasons": comfy_blocked_reasons, "generated_output_count": 0, "outputs": [], "comfyui_execution_performed": False})
    report_unless_preserved("qingjian_mug_stage_12_qc_report", {"product_id": PRODUCT_ID, "stage": 12, "stage_name": "QC and Retry Planning", "status": "not_started_no_render_outputs", "checked_at": now, "qc_report": str(paths["artifacts"] / "qc_reports" / "pre_render_qc_boundary_report.json"), "checked_output_count": 0, "notes": "Full generated-image QC must run after rendering."})

    print(json.dumps({"status": "created", "workspace_root": str(RUN), "white_bg_count": len(white_bg), "final_prompt_count": len(index_items), "comfyui_job_count": len(jobs), "manifest": str(PROJECT / "manifests" / f"{PRODUCT_ID}.batch_manifest.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
