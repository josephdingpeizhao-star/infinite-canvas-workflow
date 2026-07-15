from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_executor import (  # noqa: E402
    CanvasAgentCodexTransport,
    CanvasAgentTransportError,
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
)
from executor_contract import ExecutionRequest, ExecutorContext, ExecutorExecutionError  # noqa: E402
from executor_factory import build_executor, build_registry  # noqa: E402
from codex_dev_downstream import stable_json_sha256  # noqa: E402


VALID_IDENTITY = {
    "artifact_type": "product_identity_archive",
    "identity": {
        "confirmed_facts": ["已确认事实"],
        "visible_inferences": ["可见推断"],
        "unknowns": ["无法确认"],
        "prohibited_inventions": ["禁止虚构尺寸"],
        "product_lock_description": "保持可见结构不变。",
    },
    "missing_information": ["容量无法确认"],
    "blocked_reasons": [],
    "notes": "",
}

VALID_STYLE_MASTER = {
    "artifact_type": "style_master",
    "style_master": {
        "visual_positioning": "适合生活方式电商主视觉与品牌首屏。",
        "composition_and_layout": "竖幅，主体偏下居中，左上留出文字区。",
        "background_rules": "暖米色真实空间背景，前中后景清楚。",
        "color_rules": "低饱和米色基底，绿色与粉色作辅助色。",
        "lighting_rules": "左前方柔和自然光，保留真实接触阴影。",
        "subject_presentation_rules": "主体完整展示，视觉权重最高。",
        "prop_rules": "使用克制的花叶与布面环境元素。",
        "typography_rules": "小面积低干扰中文标题，不复制原文案。",
        "negative_space_rules": "左上留白承载标题并形成呼吸感。",
        "visual_mood": "明亮、柔和、生活化，由暖背景和自然光形成。",
        "reusable_rules": ["暖米色多层背景", "左前方柔光", "花叶虚化层次", "克制文字区", "真实接触阴影"],
        "fidelity_enhancements": {
            "style_anchors": ["右后方花叶虚化背景", "左上文字留白", "浅色布面承托"],
            "reusable_prop_clusters": {
                "must_keep": ["虚化花叶"],
                "replaceable": ["浅色布面"],
                "optional": [],
            },
            "background_layers": {
                "foreground": "少量虚化枝叶",
                "midground": "浅色布面",
                "background": "花叶与暖色空间",
            },
            "prop_density_level": "常规",
            "contents_and_usage_state": "仅记录参考图可见使用状态，不固化具体产品内容物。",
            "text_inheritance": "参考图含小面积中文标题，只继承排版气质。",
            "anti_degradation_rules": ["不得退化为纯白背景", "不得删除前中后景层次"],
        },
        "forbidden_elements": [
            "不得改变产品身份",
            "不得改变产品角度",
            "不得复制品牌或具体文案",
            "不得退化为白底棚拍",
            "不得删除主要层次",
            "不得用硬阴影",
            "不得堆叠杂乱道具",
            "不得让文字压住主体",
        ],
        "concise_style_master": (
            "本风格只约束非产品视觉风格，不覆盖产品身份、角度、尺寸比例和单张页面任务。"
            "采用暖米色真实空间、多层花叶虚化背景、浅色布面中景、少量枝叶前景和左前方柔和自然光；"
            "保留真实接触阴影、左上留白及小面积低干扰中文标题气质，不复制具体文案。"
            "道具密度为常规，关键锚点为花叶虚化层、浅色布面承托和暖色空间层次。"
            "不得退化为纯白或纯灰背景，不得删除主要前中后景，也不得为适配风格改变商品结构、颜色、"
            "材质、图案或配件关系。"
        ),
    },
    "missing_information": [],
    "notes": "",
}


def valid_angle_inventory(asset_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "artifact_type": "angle_inventory",
        "angle_slots": [
            {
                "angle_slot": "B",
                "source_asset_id": asset_id,
                "camera_angle": "45°斜侧视",
                "decision_basis": "可见壶口、侧前轮廓和壶身立体关系。",
                "naturally_visible_content": ["壶身立体感", "壶口边缘"],
                "must_not_force_content": ["完整顶部俯视", "背面不可见结构"],
                "suitable_page_tasks": ["整体识别", "立体感展示"],
                "unsuitable_page_tasks": ["完整壶口俯视"],
                "main_image_suitability": "适合：立体关系清楚。",
                "detail_image_suitability": "适合：可说明侧前结构。",
                "risk_notes": "无明显风险",
                "recommended_task_binding": "生活场景主图",
                "admission_result": "合格，可进入对应槽位",
                "merged_reference_note": "无",
                "usable_for": ["主图", "详情图"],
                "notes": "",
            }
            for asset_id in asset_ids
        ],
        "missing_angle_slots": ["A", "C", "D"],
        "retake_recommendations": [],
        "notes": "",
    }


DOWNSTREAM_NOTES = (
    "用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | "
    "主图手持数量: 2 | 详情图手持数量: 1 | "
    "允许清水场景: 是 | 禁止倾倒与加热: 是 | D槽位不补拍: 是"
)

MAIN_REQUIRED_OVERRIDE_FIELDS = (
    "主图核心承诺",
    "绑定角度槽位",
    "角度适配原则",
    "产品角度依据",
    "产品颜色依据",
    "辅助参考图调用",
    "页面任务",
    "展示重点",
    "构图方式",
    "镜头距离",
    "产品位置",
    "产品占比",
    "尺寸比例锁定",
    "输出画布比例",
    "风格贴合锚点调用",
    "道具密度等级",
    "背景层次配置",
    "内容物状态",
    "道具生成",
    "手持交互声明",
    "动态手持样式参考图调用",
    "背景与光线",
    "文字信息",
)

DETAIL_REQUIRED_OVERRIDE_FIELDS = (
    "标准模块归属",
    "买家疑问",
    "信息来源与可用证据",
    "平台硬约束检查",
    "绑定角度槽位",
    "角度适配原则",
    "产品角度依据",
    "产品颜色依据",
    "辅助参考图调用",
    "页面任务",
    "展示重点",
    "镜头距离",
    "产品位置",
    "产品占比",
    "尺寸比例锁定",
    "输出画布比例",
    "尺寸标注信息",
    "尺寸标注图规则",
    "风格贴合锚点调用",
    "道具密度等级",
    "背景层次配置",
    "内容物状态",
    "构图方式",
    "文字信息",
    "中文营销文案",
    "文字渲染要求",
    "道具关系",
    "手持交互声明",
    "动态手持样式参考图调用",
    "背景与光线",
    "真实感要求",
    "风格防退化检查",
    "禁止事项",
)


def valid_main_variable_response() -> dict[str, object]:
    common = {
        "产品类型": "家居盛水水壶",
        "已确认高度": "约 25 厘米",
        "事实边界": "不得虚构容量、其他尺寸、重量、具体材质、耐热、认证、品牌或型号",
        "动作边界": "允许清水静置；禁止倾倒、加热、沸腾或热水动作",
    }
    configs: list[dict[str, object]] = []
    assets = (("img_001", "A"), ("img_006", "B"), ("img_007", "C"))
    for index in range(1, 7):
        asset_id, slot = assets[(index - 1) % len(assets)]
        overrides = {
            "主图核心承诺": f"主图{index}清楚识别水壶完整轮廓",
            "绑定角度槽位": f"{asset_id}；{slot} 槽位",
            "角度适配原则": "只使用绑定白底图的实际角度，不旋转或补出不可见结构",
            "产品角度依据": f"以 {asset_id} 的 {slot} 槽位记录为准",
            "产品颜色依据": f"只以 {asset_id} 的产品本色为准",
            "辅助参考图调用": "仅调用风格母版的非产品氛围规则",
            "页面任务": f"形成与其他主图不同的第{index}个识别任务",
            "展示重点": "完整壶身、壶口和把手可见关系",
            "构图方式": "单品居中，保留克制留白",
            "镜头距离": "中近景",
            "产品位置": "画面中央略偏下",
            "产品占比": "约占画面六成，不改变真实尺寸关系",
            "尺寸比例锁定": "产品高度约 25 厘米，手部和道具按现实比例配合",
            "输出画布比例": "1:1",
            "风格贴合锚点调用": "暖米色生活空间、浅色布面和柔和自然光",
            "道具密度等级": "克制",
            "背景层次配置": "前景少量虚化枝叶，中景浅色布面，背景暖色空间",
            "内容物状态": "空壶" if index % 2 else "少量清水静置，不倾倒",
            "道具生成": "使用 1 至 2 个克制生活道具，不遮挡主体",
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：静态握持。"
                "单手自然握住把手，不离桌，不倾倒"
                if index in {1, 2}
                else "本张图不启用手持场景"
            ),
            "动态手持样式参考图调用": (
                "无，仅动态拿起场景可调用"
                if index in {1, 2}
                else "无"
            ),
            "背景与光线": "左前方柔和自然光，保留真实接触阴影",
            "文字信息": "小面积中文任务标题，不含价格、促销或未确认参数",
        }
        configs.append(
            {
                "config_id": f"main_{index:02d}",
                "per_image_overrides": overrides,
                "notes": "仅生成变量配置",
            }
        )
    return {
        "common_constraints": common,
        "configs": configs,
        "handheld_count_summary": {
            "用户要求主图手持数量": 2,
            "实际启用手持数量": 2,
            "未启用手持数量": 4,
            "启用手持配置": ["main_01", "main_02"],
            "是否完全满足用户数量": "是",
        },
        "notes": "不生成图片、最终提示词或质检产物",
    }


def valid_detail_variable_response() -> dict[str, object]:
    common = {
        "产品类型": "家居盛水水壶",
        "已确认高度": "约 25 厘米",
        "事实边界": "不得虚构容量、其他尺寸、重量、具体材质、耐热、认证、品牌或型号",
        "动作边界": "允许清水静置；禁止倾倒、加热、沸腾或热水动作",
        "页面链路": "模块01至模块08依次回答识别、结构、细节、场景、尺寸、手持比例、质感和收尾问题",
    }
    assets = (("img_001", "A"), ("img_006", "B"), ("img_007", "C"))
    configs: list[dict[str, object]] = []
    for index in range(1, 9):
        asset_id, slot = assets[(index - 1) % len(assets)]
        is_size = index == 5
        is_handheld = index == 6
        overrides = {
            "标准模块归属": f"模块{index:02d}",
            "买家疑问": f"第{index}个详情问题如何从现有证据得到回答",
            "信息来源与可用证据": "产品身份档案、绑定白底图、风格母版和角度槽位入库表",
            "平台硬约束检查": "详情图比例固定 3:4；不改变商品颜色、结构、大小和物理关系",
            "绑定角度槽位": f"{asset_id}；{slot} 槽位",
            "角度适配原则": "只利用绑定角度完成当前信息任务，不改变角度",
            "产品角度依据": f"以 {asset_id} 的 {slot} 槽位白底图为唯一角度依据",
            "产品颜色依据": f"以 {asset_id} 的商品本色为唯一颜色参照",
            "辅助参考图调用": "仅调用风格母版的非产品环境规则",
            "页面任务": f"详情模块{index:02d}的独立信息解释任务",
            "展示重点": "当前角度自然可见的壶身、壶口和把手关系",
            "镜头距离": "中景" if index % 2 else "中近景",
            "产品位置": "画面中央偏下并保留信息区",
            "产品占比": "按模块任务设定，不改变真实大小关系",
            "尺寸比例锁定": "产品高度约 25 厘米，手部和道具保持现实比例",
            "输出画布比例": "3:4",
            "尺寸标注信息": (
                "高度约 25 厘米；仅允许标注此高度；禁止容量、宽度、直径、重量和材质参数"
                if is_size
                else "非尺寸标注图，不启用尺寸标注信息"
            ),
            "尺寸标注图规则": (
                "只用一条贴近产品可见高度边界的标注线和‘高度约 25 厘米’，不新增其他参数"
                if is_size
                else "非尺寸标注图，不启用"
            ),
            "风格贴合锚点调用": "暖米色空间、浅色布面与柔和自然光",
            "道具密度等级": "克制" if index != 8 else "常规",
            "背景层次配置": "前景少量虚化枝叶，中景浅色布面，背景暖色空间",
            "内容物状态": "空壶" if index in {2, 3, 5} else "少量清水静置，不倾倒",
            "构图方式": f"第{index}种纵向详情构图，与其他模块明显不同",
            "文字信息": "使用小面积中文信息，不遮挡主体",
            "中文营销文案": "看得见的自然轮廓" if not is_size else "高度约 25 厘米",
            "文字渲染要求": "只渲染本栏和尺寸标注信息列出的文字，清晰无乱码",
            "道具关系": "使用 0 至 2 个克制场景元素，服务信息解释且不遮挡主体",
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：动态拿起。"
                "单手自然握住把手，轻微拿起展示比例，不倾倒"
                if is_handheld
                else "本张图不启用手持场景"
            ),
            "动态手持样式参考图调用": "未提供，不调用" if is_handheld else "无",
            "背景与光线": "左前方柔和自然光，产品与承托面有真实接触阴影",
            "真实感要求": "真实商业摄影质感，手部、接触、透视和阴影符合物理规律",
            "风格防退化检查": "保留至少两层空间和主要风格锚点，不退化为白底",
            "禁止事项": "禁止改变产品身份、角度、颜色、结构；禁止倾倒、加热和虚构商品参数",
        }
        configs.append(
            {
                "config_id": f"detail_{index:02d}",
                "per_image_overrides": overrides,
                "notes": "仅生成详情变量配置",
            }
        )
    return {
        "common_constraints": common,
        "configs": configs,
        "handheld_count_summary": {
            "用户要求详情图手持数量": 1,
            "实际启用手持数量": 1,
            "未启用手持数量": 7,
            "启用手持配置": ["detail_06"],
            "是否完全满足用户数量": "是",
        },
        "notes": "不生成图片、最终提示词或质检产物",
    }


def valid_detail_chunk_responses(
    response: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    value = copy.deepcopy(response or valid_detail_variable_response())
    configs = value["configs"]
    chunks: list[dict[str, object]] = []
    for chunk_index, start in enumerate(range(0, 8, 2), start=1):
        chunk: dict[str, object] = {
            "chunk_index": chunk_index,
            "chunk_count": 4,
            "configs": copy.deepcopy(configs[start : start + 2]),
        }
        if chunk_index == 1:
            chunk["common_constraints"] = copy.deepcopy(value["common_constraints"])
            chunk["notes"] = value["notes"]
        if chunk_index == 4:
            chunk["handheld_count_summary"] = copy.deepcopy(value["handheld_count_summary"])
        chunks.append(chunk)
    return chunks


def detail_chunk_turns(
    chunks: list[dict[str, object]],
    *,
    thread_id: str = "thread-detail-vc",
) -> list[CodexTurnResult]:
    return [
        CodexTurnResult(text=json.dumps(chunk, ensure_ascii=False), thread_id=thread_id)
        for chunk in chunks
    ]


def valid_final_prompt_response(mode: str) -> dict[str, object]:
    if mode == "main":
        config_response = valid_main_variable_response()
        ratio = "1:1"
    else:
        config_response = valid_detail_variable_response()
        ratio = "3:4"
    prompts = []
    for config in config_response["configs"]:
        overrides = config["per_image_overrides"]
        handheld = overrides["手持交互声明"]
        prompts.append(
            {
                "config_id": config["config_id"],
                "final_prompt": (
                    f"生成一张淘宝天猫{'主图' if mode == 'main' else '详情图'}；"
                    f"配置 {config['config_id']}；画布比例 {ratio}；"
                    f"绑定 {overrides['绑定角度槽位']}；产品高度约 25 厘米；"
                    f"页面任务：{overrides['页面任务']}；构图：{overrides['构图方式']}；"
                    f"手持：{handheld}；内容物仅为空壶或清水静置，禁止倾倒、加热、沸腾和热水动作；"
                    "保持产品身份、角度、颜色、结构和现实比例，画面像真实商业摄影。"
                ),
                "negative_prompt": (
                    "不要改变产品身份、角度、颜色或结构；不得虚构容量、其他尺寸、重量、具体材质、"
                    "耐热、认证、品牌或型号；不要乱码、畸形手部、漂浮阴影或 AI 融化边缘。"
                ),
            }
        )
    return {"prompts": prompts}


def handheld_count(artifact: dict[str, object]) -> int:
    return sum(
        "本张图不启用手持场景" not in config["per_image_overrides"]["手持交互声明"]
        for config in artifact["configs"]
    )


def valid_resolved_hash(artifact: dict[str, object], config: dict[str, object]) -> bool:
    resolved = dict(artifact["common_constraints"])
    resolved.update(config["per_image_overrides"])
    return stable_json_sha256(resolved) == config["resolved_variable_config_sha256"]


def jpeg_with_exif_orientation(orientation: int) -> bytes:
    tiff = (
        b"MM\x00\x2a\x00\x00\x00\x08"
        b"\x00\x01"
        b"\x01\x12\x00\x03\x00\x00\x00\x01"
        + orientation.to_bytes(2, "big")
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    app1 = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1 + b"\xff\xd9"


class FakeTransport:
    def __init__(
        self,
        result: CodexTurnResult | list[CodexTurnResult] | tuple[CodexTurnResult, ...] | None = None,
        error: Exception | None = None,
    ):
        self.results = list(result) if isinstance(result, (list, tuple)) else ([result] if result else [])
        self.error = error
        self.calls: list[tuple[str, tuple[CodexAttachment, ...]]] = []
        self.continuation_calls: list[tuple[str, str, tuple[CodexAttachment, ...]]] = []

    def run_turn(self, prompt: str, attachments: tuple[CodexAttachment, ...]) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        if self.error:
            raise self.error
        assert self.results
        return self.results.pop(0)

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.continuation_calls.append((thread_id, prompt, attachments))
        if self.error:
            raise self.error
        assert self.results
        return self.results.pop(0)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.stream = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body

    def readline(self) -> bytes:
        return self.stream.readline()


class CodexDevFixture(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[ExecutorContext, Path]:
        skill_dir = root / ".agents" / "skills" / "product-identity-archive"
        reference_dir = skill_dir / "references"
        reference_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("SKILL_MARKER: 只做产品身份档案", encoding="utf-8")
        (reference_dir / "产品身份档案提示词.txt").write_text("REFERENCE_MARKER: 不得虚构", encoding="utf-8")

        inputs = root / "workspace" / "inputs" / "white_bg"
        inputs.mkdir(parents=True)
        (inputs / "front.jpg").write_bytes(b"offline-jpeg")
        output_dir = root / "workspace" / "artifacts" / "identity"
        manifest_path = root / "manifests" / "p1.batch_manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest = {
            "product_id": "p1",
            "batch_type": "single",
            "notes": "只使用可见信息",
            "workspace": {"artifacts_root": str(root / "workspace" / "artifacts")},
            "inputs": {"white_bg_images": [str(inputs)]},
            "artifacts": {"product_identity_archive": str(output_dir)},
        }
        return ExecutorContext(
            manifest=manifest,
            manifest_path=manifest_path,
            environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
        ), output_dir

    def make_style_fixture(
        self,
        root: Path,
        context: ExecutorContext,
    ) -> tuple[ExecutorContext, Path, Path, Path]:
        skill_dir = root / ".agents" / "skills" / "style-master-extractor"
        reference_dir = skill_dir / "references"
        reference_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("STYLE_SKILL_MARKER: 只提取视觉风格", encoding="utf-8")
        (reference_dir / "反向提取风格母版提示词.txt").write_text(
            "STYLE_REFERENCE_MARKER: 不得覆盖产品身份",
            encoding="utf-8",
        )

        style_inputs = root / "workspace" / "inputs" / "style_refs"
        style_inputs.mkdir(parents=True)
        style_image = style_inputs / "style.png"
        style_image.write_bytes(b"offline-png")

        identity_path = root / "workspace" / "artifacts" / "identity" / "product_identity_archive.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_archive = dict(VALID_IDENTITY)
        identity_archive["product_id"] = "p1"
        identity_path.write_text(json.dumps(identity_archive, ensure_ascii=False), encoding="utf-8")

        style_output_dir = root / "workspace" / "artifacts" / "style_master"
        manifest = json.loads(json.dumps(context.manifest))
        manifest["inputs"]["style_reference_images"] = [str(style_inputs)]
        manifest["artifacts"]["style_master"] = str(style_output_dir)
        return (
            ExecutorContext(
                manifest=manifest,
                manifest_path=context.manifest_path,
                environment=context.environment,
            ),
            style_output_dir,
            style_image,
            identity_path,
        )

    def make_angle_fixture(
        self,
        root: Path,
        context: ExecutorContext,
    ) -> tuple[ExecutorContext, Path, tuple[Path, ...], Path]:
        skill_dir = root / ".agents" / "skills" / "angle-inventory"
        reference_dir = skill_dir / "references"
        reference_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "ANGLE_SKILL_MARKER: 只做单品角度槽位入库",
            encoding="utf-8",
        )
        (reference_dir / "角度槽位入库表生成与识别提示词.txt").write_text(
            "ANGLE_REFERENCE_MARKER: A/B/C/D；末尾套装字段不适用于本批次",
            encoding="utf-8",
        )

        white_bg_dir = Path(context.manifest["inputs"]["white_bg_images"][0])
        for existing in white_bg_dir.iterdir():
            existing.unlink()
        image_paths = tuple(white_bg_dir / f"image{index:02d}.jpg" for index in range(1, 13))
        for image_path in image_paths:
            image_path.write_bytes(b"offline-jpeg")

        identity_path = root / "workspace" / "artifacts" / "identity" / "product_identity_archive.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_archive = dict(VALID_IDENTITY)
        identity_archive["product_id"] = "p1"
        identity_path.write_text(json.dumps(identity_archive, ensure_ascii=False), encoding="utf-8")

        style_inputs = root / "workspace" / "inputs" / "style_refs"
        style_inputs.mkdir(parents=True)
        (style_inputs / "must-not-be-attached.png").write_bytes(b"offline-png")

        angle_output_dir = root / "workspace" / "artifacts" / "angle_inventory"
        manifest = json.loads(json.dumps(context.manifest))
        manifest["batch_type"] = "single"
        manifest["user_declared_set_product"] = False
        manifest["inputs"]["style_reference_images"] = [str(style_inputs)]
        manifest["artifacts"]["angle_inventory"] = str(angle_output_dir)
        return (
            ExecutorContext(
                manifest=manifest,
                manifest_path=context.manifest_path,
                environment=context.environment,
            ),
            angle_output_dir,
            image_paths,
            identity_path,
        )

    def make_downstream_fixture(self, root: Path) -> tuple[ExecutorContext, Path]:
        skill_root = root / ".agents" / "skills" / "main-variable-config"
        runtime_path = (
            skill_root
            / "references"
            / "runtime_rule_slices"
            / "main-variable-config.runtime_rule_slices.json"
        )
        runtime_path.parent.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "MAIN_VARIABLE_SKILL_MARKER: 生成六套单品主图变量配置",
            encoding="utf-8",
        )
        runtime_path.write_text(
            json.dumps(
                {
                    "artifact_type": "runtime_rule_slice_package",
                    "skill": "main-variable-config",
                    "slices": [{"slice_id": "main", "text": "MAIN_RUNTIME_MARKER: 主图固定 1:1"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        artifacts_root = root / "workspace" / "artifacts"
        identity_path = artifacts_root / "identity" / "product_identity_archive.json"
        style_path = artifacts_root / "style_master" / "style_master.json"
        angle_path = artifacts_root / "angle_inventory" / "angle_inventory.json"
        for path in (identity_path, style_path, angle_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        identity = dict(VALID_IDENTITY)
        identity["product_id"] = "p1"
        identity_path.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
        style = dict(VALID_STYLE_MASTER)
        style["product_id"] = "p1"
        style_path.write_text(json.dumps(style, ensure_ascii=False), encoding="utf-8")
        angle = {
            "product_id": "p1",
            "artifact_type": "angle_inventory",
            "image_assets": [
                {"asset_id": asset_id, "file_path": f"{asset_id}.jpg"}
                for asset_id in ("img_001", "img_005", "img_006", "img_007", "img_004")
            ],
            "angle_slots": [
                {
                    "source_asset_id": "img_001",
                    "angle_slot": "A",
                    "admission_result": "合格，可进入对应槽位",
                    "camera_angle": "正面",
                },
                {
                    "source_asset_id": "img_006",
                    "angle_slot": "B",
                    "admission_result": "合格，可进入对应槽位",
                    "camera_angle": "斜侧面",
                },
                {
                    "source_asset_id": "img_007",
                    "angle_slot": "C",
                    "admission_result": "勉强可用，但建议重拍",
                    "camera_angle": "侧面",
                },
                {
                    "source_asset_id": "img_004",
                    "angle_slot": "D",
                    "admission_result": "合格，可进入对应槽位",
                    "camera_angle": "俯视",
                },
                {
                    "source_asset_id": "img_005",
                    "angle_slot": "不适合归入现有槽位",
                    "admission_result": "不适合入库，需重拍",
                    "camera_angle": "横放",
                },
            ],
            "missing_angle_slots": ["D"],
            "retake_recommendations": [],
            "notes": "D 不补拍",
        }
        angle_path.write_text(json.dumps(angle, ensure_ascii=False), encoding="utf-8")

        output_dir = artifacts_root / "variable_configs"
        manifest_path = root / "manifests" / "p1.batch_manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest = {
            "product_id": "p1",
            "batch_type": "single",
            "user_declared_set_product": False,
            "requested_outputs": ["main", "detail", "final_prompts"],
            "notes": DOWNSTREAM_NOTES,
            "workspace": {"artifacts_root": str(artifacts_root)},
            "artifacts": {
                "product_identity_archive": str(identity_path.parent),
                "style_master": str(style_path.parent),
                "angle_inventory": str(angle_path.parent),
                "main_variable_configs": [str(output_dir)],
                "detail_variable_configs": [str(output_dir)],
                "final_prompts": [str(artifacts_root / "final_prompts")],
            },
        }
        return (
            ExecutorContext(
                manifest=manifest,
                manifest_path=manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            ),
            output_dir / "main_variable_configs.json",
        )

    def make_detail_fixture(self, root: Path) -> tuple[ExecutorContext, Path, Path]:
        context, main_output = self.make_downstream_fixture(root)
        skill_root = root / ".agents" / "skills" / "detail-variable-config"
        runtime_path = (
            skill_root
            / "references"
            / "runtime_rule_slices"
            / "detail-variable-config.runtime_rule_slices.json"
        )
        runtime_path.parent.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "DETAIL_VARIABLE_SKILL_MARKER: 生成八套单品详情图变量配置",
            encoding="utf-8",
        )
        runtime_path.write_text(
            json.dumps(
                {
                    "artifact_type": "runtime_rule_slice_package",
                    "skill": "detail-variable-config",
                    "slices": [{"slice_id": "detail", "text": "DETAIL_RUNTIME_MARKER: 详情图固定 3:4"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = valid_main_variable_response()
        common = response["common_constraints"]
        configs = []
        for raw in response["configs"]:
            resolved = dict(common)
            resolved.update(raw["per_image_overrides"])
            configs.append(
                {
                    "config_id": raw["config_id"],
                    "output_type": "main",
                    "per_image_overrides": raw["per_image_overrides"],
                    "resolved_variable_config_sha256": stable_json_sha256(resolved),
                    "notes": raw["notes"],
                }
            )
        main_artifact = {
            "product_id": "p1",
            "artifact_type": "main_variable_config",
            "config_count": 6,
            "upstream_artifacts": {
                "product_identity_archive": "identity/product_identity_archive.json",
                "style_master": "style_master/style_master.json",
                "angle_inventory": "angle_inventory/angle_inventory.json",
            },
            "common_constraints": common,
            "configs": configs,
            "notes": "formal main fixture",
        }
        main_output.parent.mkdir(parents=True, exist_ok=True)
        main_output.write_text(json.dumps(main_artifact, ensure_ascii=False), encoding="utf-8")
        detail_output = main_output.parent / "detail_variable_configs.json"
        return context, detail_output, main_output

    def make_final_prompt_fixture(self, root: Path) -> tuple[ExecutorContext, Path, Path, Path]:
        context, detail_output, main_output = self.make_detail_fixture(root)
        skill_root = root / ".agents" / "skills" / "final-prompt-compiler"
        runtime_path = (
            skill_root
            / "references"
            / "runtime_rule_slices"
            / "final-prompt-compiler.runtime_rule_slices.json"
        )
        runtime_path.parent.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "FINAL_PROMPT_SKILL_MARKER: 只编译已确认变量配置",
            encoding="utf-8",
        )
        runtime_path.write_text(
            json.dumps(
                {
                    "artifact_type": "runtime_rule_slice_package",
                    "skill": "final-prompt-compiler",
                    "slices": [{"slice_id": "final", "text": "FINAL_RUNTIME_MARKER: 不改变变量配置"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = valid_detail_variable_response()
        common = response["common_constraints"]
        configs = []
        for raw in response["configs"]:
            resolved = dict(common)
            resolved.update(raw["per_image_overrides"])
            configs.append(
                {
                    "config_id": raw["config_id"],
                    "output_type": "detail",
                    "per_image_overrides": raw["per_image_overrides"],
                    "resolved_variable_config_sha256": stable_json_sha256(resolved),
                    "notes": raw["notes"],
                }
            )
        detail_artifact = {
            "product_id": "p1",
            "artifact_type": "detail_variable_config",
            "config_count": 8,
            "upstream_artifacts": {
                "product_identity_archive": "identity/product_identity_archive.json",
                "style_master": "style_master/style_master.json",
                "angle_inventory": "angle_inventory/angle_inventory.json",
                "main_variable_configs": str(main_output),
            },
            "common_constraints": common,
            "configs": configs,
            "notes": "formal detail fixture",
        }
        detail_output.write_text(json.dumps(detail_artifact, ensure_ascii=False), encoding="utf-8")
        final_dir = Path(context.manifest["artifacts"]["final_prompts"][0])
        return context, final_dir, main_output, detail_output


class CodexDevExecutorTest(CodexDevFixture):
    def test_registered_without_replacing_existing_executors(self) -> None:
        self.assertEqual(("codex-dev", "demo", "openai-image"), build_registry().names())
        executor = build_executor("codex-dev", {"product_id": "p1", "inputs": {}, "artifacts": {}})
        self.assertEqual("codex-dev", executor.name)

    def test_main_vc_writes_six_configs_with_two_handheld_and_fixed_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.make_downstream_fixture(root)
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                    thread_id="thread-main-vc",
                )
            )
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            result = executor.execute(ExecutionRequest(step="main_vc"))

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("main_variable_config", artifact["artifact_type"])
            self.assertEqual(6, artifact["config_count"])
            self.assertEqual(
                [f"main_{index:02d}" for index in range(1, 7)],
                [config["config_id"] for config in artifact["configs"]],
            )
            self.assertEqual(2, handheld_count(artifact))
            self.assertTrue(all(valid_resolved_hash(artifact, config) for config in artifact["configs"]))
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("主图变量配置已生成", result.detail)
            self.assertEqual("thread-main-vc", result.metadata["thread_id"])
            prompt, attachments = transport.calls[0]
            self.assertIn("MAIN_VARIABLE_SKILL_MARKER", prompt)
            self.assertIn("MAIN_RUNTIME_MARKER", prompt)
            self.assertIn("img_001", prompt)
            self.assertNotIn("img_005.jpg", prompt)
            self.assertEqual((), attachments)

    def test_main_vc_accepts_canonical_handheld_reference_values_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.make_downstream_fixture(root)
            response = valid_main_variable_response()
            enabled_ids = []
            for config in response["configs"]:
                overrides = config["per_image_overrides"]
                if "本张图不启用手持场景" in overrides["手持交互声明"]:
                    overrides["动态手持样式参考图调用"] = "无"
                else:
                    overrides["手持交互声明"] = (
                        "本张图启用手持场景。手持子场景类型：静态握持。"
                        + overrides["手持交互声明"]
                    )
                    overrides["动态手持样式参考图调用"] = "无，仅动态拿起场景可调用"
                    enabled_ids.append(config["config_id"])
            response["handheld_count_summary"] = {
                "用户要求主图手持数量": 2,
                "实际启用手持数量": 2,
                "未启用手持数量": 4,
                "启用手持配置": enabled_ids,
                "是否完全满足用户数量": "是",
            }
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(response, ensure_ascii=False),
                    thread_id="thread-main-canonical-handheld",
                )
            )

            result = CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                ExecutionRequest(step="main_vc")
            )

            self.assertEqual((output_path,), result.outputs)
            self.assertEqual(2, handheld_count(json.loads(output_path.read_text(encoding="utf-8"))))

    def test_main_vc_rejects_invalid_responses_before_formal_write(self) -> None:
        def five_configs(value: dict[str, object]) -> None:
            value["configs"].pop()

        def duplicate_id(value: dict[str, object]) -> None:
            value["configs"][1]["config_id"] = "main_01"

        def missing_d(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["绑定角度槽位"] = "img_004；D 槽位"

        def rejected_asset(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["绑定角度槽位"] = "img_005；A 槽位"

        def one_handheld(value: dict[str, object]) -> None:
            value["configs"][1]["per_image_overrides"]["手持交互声明"] = "本张图不启用手持场景"
            value["configs"][1]["per_image_overrides"]["动态手持样式参考图调用"] = "无"

        def three_handheld(value: dict[str, object]) -> None:
            value["configs"][2]["per_image_overrides"]["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：静态握持。轻扶壶身"
            )
            value["configs"][2]["per_image_overrides"]["动态手持样式参考图调用"] = (
                "无，仅动态拿起场景可调用"
            )

        def missing_field(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"].pop("页面任务")

        def wrong_ratio(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["输出画布比例"] = "3:4"

        def invented_capacity(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["展示重点"] = "明确展示容量 2 L"

        def invented_material(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["展示重点"] = "突出陶瓷材质"

        def downstream_field(value: dict[str, object]) -> None:
            value["final_prompt"] = "PRIVATE_FINAL_PROMPT"

        def damaged_unicode(value: dict[str, object]) -> None:
            value["notes"] = "损坏\ufffd内容"

        cases = {
            "five_configs": five_configs,
            "duplicate_id": duplicate_id,
            "missing_d": missing_d,
            "rejected_asset": rejected_asset,
            "one_handheld": one_handheld,
            "three_handheld": three_handheld,
            "missing_field": missing_field,
            "wrong_ratio": wrong_ratio,
            "invented_capacity": invented_capacity,
            "invented_material": invented_material,
            "downstream_field": downstream_field,
            "damaged_unicode": damaged_unicode,
        }
        for case_name, mutate in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path = self.make_downstream_fixture(root)
                response = copy.deepcopy(valid_main_variable_response())
                mutate(response)
                transport = FakeTransport(
                    CodexTurnResult(
                        text=json.dumps(response, ensure_ascii=False),
                        thread_id=f"thread-{case_name}",
                    )
                )
                executor = CodexDevExecutor(context, transport=transport, repository_root=root)

                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="main_vc"))

                self.assertFalse(output_path.exists())
                self.assertNotIn("PRIVATE_FINAL_PROMPT", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_main_vc_existing_output_and_path_escape_are_rejected_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.make_downstream_fixture(root)
            output_path.parent.mkdir(parents=True)
            output_path.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(
                CodexTurnResult(text=json.dumps(valid_main_variable_response()), thread_id="unused")
            )

            with self.assertRaises(ExecutorExecutionError):
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="main_vc")
                )

            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, _output_path = self.make_downstream_fixture(root)
            manifest = copy.deepcopy(context.manifest)
            manifest["artifacts"]["main_variable_configs"] = [str(root / "outside")]
            unsafe_context = ExecutorContext(
                manifest=manifest,
                manifest_path=context.manifest_path,
                environment=context.environment,
            )
            transport = FakeTransport(
                CodexTurnResult(text=json.dumps(valid_main_variable_response()), thread_id="unused")
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(unsafe_context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="main_vc")
                )

            self.assertIn("artifacts_root", str(caught.exception))
            self.assertEqual([], transport.calls)

    def test_detail_vc_covers_eight_modules_with_one_handheld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            transport = FakeTransport(detail_chunk_turns(valid_detail_chunk_responses()))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            result = executor.execute(ExecutionRequest(step="detail_vc"))

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("detail_variable_config", artifact["artifact_type"])
            self.assertEqual(8, artifact["config_count"])
            self.assertEqual(
                [f"detail_{index:02d}" for index in range(1, 9)],
                [config["config_id"] for config in artifact["configs"]],
            )
            self.assertEqual(
                [f"模块{index:02d}" for index in range(1, 9)],
                [config["per_image_overrides"]["标准模块归属"] for config in artifact["configs"]],
            )
            self.assertEqual(1, handheld_count(artifact))
            self.assertEqual(
                "本张图不启用手持场景",
                artifact["configs"][4]["per_image_overrides"]["手持交互声明"],
            )
            self.assertTrue(
                all(
                    config["per_image_overrides"]["输出画布比例"] == "3:4"
                    for config in artifact["configs"]
                )
            )
            self.assertTrue(all(valid_resolved_hash(artifact, config) for config in artifact["configs"]))
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("详情图变量配置已生成", result.detail)
            prompt, attachments = transport.calls[0]
            self.assertIn("DETAIL_VARIABLE_SKILL_MARKER", prompt)
            self.assertIn("DETAIL_RUNTIME_MARKER", prompt)
            self.assertIn("main_01", prompt)
            self.assertIn("第 1/4 段", prompt)
            self.assertIn("notes 必须是 JSON 字符串", prompt)
            self.assertEqual((), attachments)
            self.assertEqual(3, len(transport.continuation_calls))
            self.assertTrue(
                all(call[0] == "thread-detail-vc" for call in transport.continuation_calls)
            )
            self.assertIn("第 2/4 段", transport.continuation_calls[0][1])
            self.assertIn("第 3/4 段", transport.continuation_calls[1][1])
            self.assertIn("第 4/4 段", transport.continuation_calls[2][1])

    def test_detail_vc_accepts_confirmed_height_in_canonical_size_lock_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            response = valid_detail_variable_response()
            for config in response["configs"]:
                config["per_image_overrides"]["尺寸比例锁定"] = "约 25 厘米"
            response["common_constraints"]["尺寸比例"] = (
                "所有配置的尺寸比例锁定均写约 25 厘米；不得补写容量、宽度、直径、重量、"
                "具体材质、耐热性能、认证、品牌或型号。"
            )
            transport = FakeTransport(
                detail_chunk_turns(
                    valid_detail_chunk_responses(response),
                    thread_id="thread-detail-canonical-size-lock",
                )
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="detail_vc"))

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(
                all(
                    config["per_image_overrides"]["尺寸比例锁定"] == "约 25 厘米"
                    for config in artifact["configs"]
                )
            )
            self.assertEqual(
                response["common_constraints"]["尺寸比例"],
                artifact["common_constraints"]["尺寸比例"],
            )
            self.assertEqual("详情图变量配置已生成", result.detail)

    def test_detail_vc_accepts_safe_wide_composition_with_explicit_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            response = valid_detail_variable_response()
            response["configs"][0]["per_image_overrides"]["尺寸比例锁定"] = (
                "宽松构图下产品高度约 25 厘米"
            )
            transport = FakeTransport(
                detail_chunk_turns(
                    valid_detail_chunk_responses(response),
                    thread_id="thread-detail-safe-wide-composition",
                )
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="detail_vc"))

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "宽松构图下产品高度约 25 厘米",
                artifact["configs"][0]["per_image_overrides"]["尺寸比例锁定"],
            )
            self.assertEqual("详情图变量配置已生成", result.detail)

    def test_detail_vc_accepts_explicit_negative_material_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            response = valid_detail_variable_response()
            response["configs"][0]["per_image_overrides"]["真实感要求"] = (
                "真实商业摄影，不出现塑料感、蜡感或明显 AI 痕迹"
            )
            response["configs"][0]["per_image_overrides"]["禁止事项"] = (
                "不要把亮面硬质观感写死为陶瓷、玻璃或塑料"
            )
            response["configs"][1]["per_image_overrides"]["信息来源与可用证据"] = (
                "真实感约束用于避免塑料感、漂浮和过度磨皮"
            )
            response["configs"][1]["per_image_overrides"]["平台硬约束检查"] = (
                "不生成证书、报告、评价、销量、认证编号或虚假截图"
            )
            module_names = [
                "模块01 首屏 · 主视觉与卖点承接",
                "模块02 核心卖点证明",
                "模块03 使用场景与方法",
                "模块04 细节实拍与材质工艺",
                "模块05 规格尺寸与容量",
                "模块06 质感可信视觉呈现",
                "模块07 决策辅助与场景想象",
                "模块08 收尾氛围与风险克制",
            ]
            for config, module_name in zip(response["configs"], module_names, strict=True):
                config["per_image_overrides"]["标准模块归属"] = module_name
            transport = FakeTransport(
                detail_chunk_turns(
                    valid_detail_chunk_responses(response),
                    thread_id="thread-detail-negative-guardrails",
                )
            )

            result = CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                ExecutionRequest(step="detail_vc")
            )

            self.assertTrue(output_path.exists())
            self.assertEqual("详情图变量配置已生成", result.detail)

    def test_detail_vc_rejects_invalid_modules_handheld_angles_and_facts(self) -> None:
        def duplicate_module(value: dict[str, object]) -> None:
            value["configs"][7]["per_image_overrides"]["标准模块归属"] = "模块07"

        def wrong_module_order(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["标准模块归属"] = "模块02"

        def handheld_on_module05(value: dict[str, object]) -> None:
            value["configs"][4]["per_image_overrides"]["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：静态握持。轻扶壶身"
            )
            value["configs"][4]["per_image_overrides"]["动态手持样式参考图调用"] = (
                "无，仅动态拿起场景可调用"
            )
            value["configs"][5]["per_image_overrides"]["手持交互声明"] = "本张图不启用手持场景"
            value["configs"][5]["per_image_overrides"]["动态手持样式参考图调用"] = "无"

        def zero_handheld(value: dict[str, object]) -> None:
            value["configs"][5]["per_image_overrides"]["手持交互声明"] = "本张图不启用手持场景"
            value["configs"][5]["per_image_overrides"]["动态手持样式参考图调用"] = "无"

        def two_handheld(value: dict[str, object]) -> None:
            value["configs"][6]["per_image_overrides"]["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：静态握持。自然握住把手"
            )
            value["configs"][6]["per_image_overrides"]["动态手持样式参考图调用"] = (
                "无，仅动态拿起场景可调用"
            )

        def missing_d(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["绑定角度槽位"] = "img_004；D 槽位"

        def rejected_asset(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["绑定角度槽位"] = "img_005；A 槽位"

        def invented_weight(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["中文营销文案"] = "轻巧仅重 500 g"

        def wrong_ratio(value: dict[str, object]) -> None:
            value["configs"][0]["per_image_overrides"]["输出画布比例"] = "1:1"

        def final_field(value: dict[str, object]) -> None:
            value["configs"][0]["final_prompt"] = "PRIVATE_DETAIL_PROMPT"

        cases = {
            "duplicate_module": duplicate_module,
            "wrong_module_order": wrong_module_order,
            "handheld_on_module05": handheld_on_module05,
            "zero_handheld": zero_handheld,
            "two_handheld": two_handheld,
            "missing_d": missing_d,
            "rejected_asset": rejected_asset,
            "invented_weight": invented_weight,
            "wrong_ratio": wrong_ratio,
            "final_field": final_field,
        }
        for case_name, mutate in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                response = copy.deepcopy(valid_detail_variable_response())
                mutate(response)
                transport = FakeTransport(
                    detail_chunk_turns(
                        valid_detail_chunk_responses(response),
                        thread_id=f"detail-{case_name}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                        ExecutionRequest(step="detail_vc")
                    )

                self.assertFalse(output_path.exists())
                self.assertNotIn("PRIVATE_DETAIL_PROMPT", str(caught.exception))

    def test_detail_vc_requires_formal_main_and_refuses_existing_output_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, main_path = self.make_detail_fixture(root)
            main_path.unlink()
            transport = FakeTransport(
                CodexTurnResult(text=json.dumps(valid_detail_variable_response()), thread_id="unused")
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="detail_vc")
                )

            self.assertIn("正式主图变量配置", str(caught.exception))
            self.assertEqual([], transport.calls)
            self.assertFalse(output_path.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            output_path.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(
                CodexTurnResult(text=json.dumps(valid_detail_variable_response()), thread_id="unused")
            )

            with self.assertRaises(ExecutorExecutionError):
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="detail_vc")
                )

            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_detail_vc_corrects_one_safe_envelope_error_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            malformed = copy.deepcopy(chunks[0])
            malformed["notes"] = {
                "handheld_count_summary": {"actual": 1},
                "chunk_notes": "PRIVATE_WRONG_ENVELOPE",
            }
            transport = FakeTransport(
                detail_chunk_turns(
                    [malformed, chunks[0], chunks[1], chunks[2], chunks[3]],
                    thread_id="thread-detail-envelope-correction",
                )
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="detail_vc"))

            artifact_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_WRONG_ENVELOPE", artifact_text)
            self.assertEqual(
                "详情图变量配置已生成（格式纠正 1 次）",
                result.detail,
            )
            self.assertEqual(0, result.metadata["recovery_attempts"])
            self.assertEqual(1, result.metadata["structure_correction_attempts"])
            self.assertEqual(4, len(transport.continuation_calls))
            correction_prompt = transport.continuation_calls[0][1]
            self.assertIn("完整重发第 1/4 段", correction_prompt)
            self.assertIn("包装格式", correction_prompt)
            self.assertNotIn("PRIVATE_WRONG_ENVELOPE", correction_prompt)

    def test_detail_vc_stops_after_one_envelope_correction_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            first = copy.deepcopy(chunks[0])
            first["notes"] = {"chunk_notes": "PRIVATE_FIRST_ENVELOPE"}
            second = copy.deepcopy(chunks[0])
            second["notes"] = {"chunk_notes": "PRIVATE_SECOND_ENVELOPE"}
            transport = FakeTransport(
                detail_chunk_turns(
                    [first, second],
                    thread_id="thread-detail-envelope-limit",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertEqual(
                "codex-dev 详情图变量配置格式纠正已达到上限",
                str(caught.exception),
            )
            self.assertNotIn("PRIVATE_", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(1, len(transport.continuation_calls))

    def test_detail_vc_does_not_correct_envelope_with_invalid_business_content(self) -> None:
        cases = {
            "ratio": ("输出画布比例", "1:1", "画布比例异常"),
            "unsupported_fact": ("真实感要求", "本产品为食品级塑料", "未确认商品事实"),
            "same_number_wrong_dimension": (
                "中文营销文案",
                "壶身宽度约 25 厘米",
                "未确认参数",
            ),
        }
        for case_name, (field, value, expected_error) in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                chunks = valid_detail_chunk_responses()
                malformed = copy.deepcopy(chunks[0])
                malformed["notes"] = {"chunk_notes": "PRIVATE_ENVELOPE"}
                malformed["configs"][0]["per_image_overrides"][field] = value
                transport = FakeTransport(
                    detail_chunk_turns(
                        [malformed],
                        thread_id=f"thread-detail-envelope-{case_name}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(
                        context,
                        transport=transport,
                        repository_root=root,
                    ).execute(ExecutionRequest(step="detail_vc"))

                self.assertIn(expected_error, str(caught.exception))
                self.assertFalse(output_path.exists())
                self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_rejects_height_number_when_used_as_unconfirmed_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            chunks[0]["configs"][0]["per_image_overrides"]["中文营销文案"] = (
                "壶身宽度约 25 厘米"
            )
            transport = FakeTransport(
                detail_chunk_turns(
                    chunks,
                    thread_id="thread-detail-unconfirmed-width",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertIn("未确认参数", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_rejects_width_suffix_in_size_lock_context(self) -> None:
        cases = (
            "约 25 厘米宽度",
            "尺寸比例锁定为约 25 厘米宽度",
            "尺寸比例锁定为约 25 厘米宽（含把手）",
            "尺寸比例锁定为约 25 厘米宽×25 厘米高",
            "尺寸比例锁定为约 25 厘米宽x25 厘米高",
            "尺寸比例锁定为约 25 厘米宽X25 厘米高",
            "尺寸比例锁定为约 25 厘米宽*25 厘米高",
        )
        for index, value in enumerate(cases, start=1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                response = valid_detail_variable_response()
                response["configs"][0]["per_image_overrides"]["尺寸比例锁定"] = value
                transport = FakeTransport(
                    detail_chunk_turns(
                        valid_detail_chunk_responses(response),
                        thread_id=f"thread-detail-width-suffix-{index}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(
                        context,
                        transport=transport,
                        repository_root=root,
                    ).execute(ExecutionRequest(step="detail_vc"))

                self.assertIn("未确认参数", str(caught.exception))
                self.assertFalse(output_path.exists())
                self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_rejects_ambiguous_size_lock_measurements(self) -> None:
        cases = (
            "尺寸比例锁定为壶身宽约 25 厘米",
            "尺寸比例锁定：可装约 25 cm³",
            "尺寸比例锁定：可装约 25 cm ³",
            "尺寸比例锁定为约 24-25 厘米",
            "尺寸比例锁定为约 24/25 厘米",
            "尺寸比例锁定为约 -25 厘米",
        )
        for index, value in enumerate(cases, start=1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                response = valid_detail_variable_response()
                response["configs"][0]["per_image_overrides"]["尺寸比例锁定"] = value
                transport = FakeTransport(
                    detail_chunk_turns(
                        valid_detail_chunk_responses(response),
                        thread_id=f"thread-detail-ambiguous-size-lock-{index}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(
                        context,
                        transport=transport,
                        repository_root=root,
                    ).execute(ExecutionRequest(step="detail_vc"))

                self.assertIn("未确认参数", str(caught.exception))
                self.assertFalse(output_path.exists())
                self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_does_not_globally_accept_bare_confirmed_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            response = valid_detail_variable_response()
            response["configs"][0]["per_image_overrides"]["中文营销文案"] = "约 25 厘米"
            transport = FakeTransport(
                detail_chunk_turns(
                    valid_detail_chunk_responses(response),
                    thread_id="thread-detail-bare-height",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertIn("未确认参数", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_rejects_unconfirmed_width_nested_under_height_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            chunks[0]["common_constraints"]["已确认高度"] = {"宽度": "25 厘米"}
            transport = FakeTransport(
                detail_chunk_turns(
                    chunks,
                    thread_id="thread-detail-nested-unconfirmed-width",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertIn("未确认参数", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_rejects_natural_width_nested_under_height_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            chunks[0]["common_constraints"]["已确认高度"] = {"壶身宽": "25 厘米"}
            transport = FakeTransport(
                detail_chunk_turns(
                    chunks,
                    thread_id="thread-detail-nested-natural-width",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertIn("未确认参数", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_does_not_correct_envelope_with_invalid_handheld_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            malformed = copy.deepcopy(chunks[3])
            malformed["notes"] = "PRIVATE_EXTRA_WRAPPER"
            malformed["handheld_count_summary"]["实际启用手持数量"] = 2
            transport = FakeTransport(
                detail_chunk_turns(
                    [chunks[0], chunks[1], chunks[2], malformed],
                    thread_id="thread-detail-invalid-handheld-summary",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertIn("手持数量说明异常", str(caught.exception))
            self.assertNotIn("PRIVATE_", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual(3, len(transport.continuation_calls))

    def test_detail_vc_envelope_correction_cannot_change_business_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            malformed = copy.deepcopy(chunks[0])
            malformed["notes"] = {"chunk_notes": "PRIVATE_WRONG_ENVELOPE"}
            changed = copy.deepcopy(chunks[0])
            changed["configs"][0]["per_image_overrides"]["页面任务"] = "另一项安全但不同的详情任务"
            transport = FakeTransport(
                detail_chunk_turns(
                    [malformed, changed],
                    thread_id="thread-detail-envelope-content-change",
                )
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="detail_vc"))

            self.assertEqual(
                "codex-dev 详情图变量配置格式纠正改变了业务内容",
                str(caught.exception),
            )
            self.assertNotIn("PRIVATE_", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual(1, len(transport.continuation_calls))

    def test_detail_vc_tracks_transport_and_envelope_recovery_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            damaged = copy.deepcopy(chunks[0])
            damaged["configs"][0]["notes"] = "PRIVATE_TRANSPORT\ufffd"
            malformed = copy.deepcopy(chunks[0])
            malformed["notes"] = {"chunk_notes": "PRIVATE_ENVELOPE"}
            transport = FakeTransport(
                detail_chunk_turns(
                    [damaged, malformed, chunks[0], chunks[1], chunks[2], chunks[3]],
                    thread_id="thread-detail-two-recovery-kinds",
                )
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="detail_vc"))

            artifact_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_", artifact_text)
            self.assertEqual(
                "详情图变量配置已生成（受控恢复 1 次，格式纠正 1 次）",
                result.detail,
            )
            self.assertEqual(1, result.metadata["recovery_attempts"])
            self.assertEqual(1, result.metadata["structure_correction_attempts"])
            self.assertEqual(5, len(transport.continuation_calls))
            self.assertIn("传输完整性门禁", transport.continuation_calls[0][1])
            self.assertIn("包装格式", transport.continuation_calls[1][1])

    def test_detail_vc_recovers_unicode_damaged_chunk_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            damaged_chunk = copy.deepcopy(chunks[1])
            damaged_chunk["configs"][0]["notes"] = "PRIVATE_BROKEN\ufffdBODY"
            results = detail_chunk_turns(
                [chunks[0], damaged_chunk, chunks[1], chunks[2], chunks[3]],
                thread_id="thread-detail-repair",
            )
            transport = FakeTransport(results)

            result = CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                ExecutionRequest(step="detail_vc")
            )

            artifact_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", artifact_text)
            self.assertNotIn("PRIVATE_BROKEN", artifact_text)
            self.assertEqual("详情图变量配置已生成（受控恢复 1 次）", result.detail)
            self.assertEqual(1, result.metadata["recovery_attempts"])
            self.assertEqual(4, len(transport.continuation_calls))
            self.assertTrue(
                all(call[0] == "thread-detail-repair" for call in transport.continuation_calls)
            )
            self.assertIn("完整重发第 2/4 段", transport.continuation_calls[1][1])

    def test_detail_vc_recovers_truncated_json_chunk_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            results = [
                detail_chunk_turns([chunks[0]], thread_id="thread-detail-truncated")[0],
                CodexTurnResult(text='{"chunk_index": 2', thread_id="thread-detail-truncated"),
                *detail_chunk_turns(chunks[1:], thread_id="thread-detail-truncated"),
            ]
            transport = FakeTransport(results)

            result = CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                ExecutionRequest(step="detail_vc")
            )

            self.assertTrue(output_path.exists())
            self.assertEqual("详情图变量配置已生成（受控恢复 1 次）", result.detail)
            self.assertIn("完整重发第 2/4 段", transport.continuation_calls[1][1])

    def test_detail_vc_recovers_json_truncated_inside_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            results = [
                detail_chunk_turns([chunks[0]], thread_id="thread-detail-string-truncated")[0],
                CodexTurnResult(
                    text=(
                        '{"chunk_index": 2, "chunk_count": 4, "configs": '
                        '[{"config_id": "detail_03'
                    ),
                    thread_id="thread-detail-string-truncated",
                ),
                *detail_chunk_turns(chunks[1:], thread_id="thread-detail-string-truncated"),
            ]
            transport = FakeTransport(results)

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="detail_vc"))

            self.assertTrue(output_path.exists())
            self.assertEqual(1, result.metadata["recovery_attempts"])
            self.assertEqual(4, len(transport.continuation_calls))

    def test_detail_vc_does_not_retry_json_that_is_not_a_valid_object_prefix(self) -> None:
        cases = {
            "closed_trailing_comma": '{"chunk_index": 1, "chunk_count": 4, "configs": [],}',
            "truncated_wrong_root": '[{"chunk_index": 1',
            "unclosed_but_already_invalid": '{"chunk_index":,',
            "earlier_error_then_dangling_escape": '{"chunk_index":, "x":"abc' + "\\",
            "earlier_error_then_partial_unicode": '{"chunk_index":, "x":"' + "\\u12",
            "empty": "",
        }
        for case_name, response_text in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                transport = FakeTransport(
                    CodexTurnResult(
                        text=response_text,
                        thread_id=f"thread-detail-complete-invalid-{case_name}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(
                        context,
                        transport=transport,
                        repository_root=root,
                    ).execute(ExecutionRequest(step="detail_vc"))

                self.assertEqual(
                    "codex-dev 收到的详情图变量配置分段不是有效 JSON",
                    str(caught.exception),
                )
                self.assertFalse(output_path.exists())
                self.assertEqual([], transport.continuation_calls)

    def test_detail_vc_stops_after_two_transport_recoveries_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()

            def damaged(index: int) -> CodexTurnResult:
                value = copy.deepcopy(chunks[index - 1])
                value["configs"][0]["notes"] = f"PRIVATE_DAMAGE_{index}\ufffd"
                return CodexTurnResult(
                    text=json.dumps(value, ensure_ascii=False),
                    thread_id="thread-detail-limit",
                )

            results = [
                damaged(1),
                detail_chunk_turns([chunks[0]], thread_id="thread-detail-limit")[0],
                damaged(2),
                detail_chunk_turns([chunks[1]], thread_id="thread-detail-limit")[0],
                damaged(3),
            ]
            transport = FakeTransport(results)

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="detail_vc")
                )

            self.assertEqual("codex-dev 详情图变量配置传输恢复已达到上限", str(caught.exception))
            self.assertNotIn("PRIVATE_DAMAGE", str(caught.exception))
            self.assertFalse(output_path.exists())
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(4, len(transport.continuation_calls))

    def test_detail_vc_business_or_scope_error_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _main_path = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            chunks[0]["configs"][0]["final_prompt"] = "PRIVATE_DETAIL_PROMPT"
            transport = FakeTransport(
                detail_chunk_turns(chunks, thread_id="thread-detail-scope-error")
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="detail_vc")
                )

            self.assertFalse(output_path.exists())
            self.assertEqual([], transport.continuation_calls)
            self.assertNotIn("PRIVATE_DETAIL_PROMPT", str(caught.exception))

    def test_detail_vc_rejects_wrong_chunk_identity_or_config_coverage_without_retry(self) -> None:
        cases = {}
        wrong_index = valid_detail_chunk_responses()
        wrong_index[0]["chunk_index"] = 2
        cases["wrong_index"] = wrong_index
        duplicate_id = valid_detail_chunk_responses()
        duplicate_id[0]["configs"][1]["config_id"] = "detail_01"
        cases["duplicate_id"] = duplicate_id
        missing_config = valid_detail_chunk_responses()
        missing_config[0]["configs"].pop()
        cases["missing_config"] = missing_config

        for case_name, chunks in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_path, _main_path = self.make_detail_fixture(root)
                transport = FakeTransport(
                    detail_chunk_turns(chunks, thread_id=f"thread-detail-{case_name}")
                )

                with self.assertRaises(ExecutorExecutionError):
                    CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                        ExecutionRequest(step="detail_vc")
                    )

                self.assertFalse(output_path.exists())
                self.assertEqual([], transport.continuation_calls)

    def test_final_prompts_write_fourteen_prompt_only_artifacts_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, main_path, detail_path = self.make_final_prompt_fixture(root)
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("main"), ensure_ascii=False),
                        thread_id="thread-final-main",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("detail"), ensure_ascii=False),
                        thread_id="thread-final-detail",
                    ),
                ]
            )

            result = CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                ExecutionRequest(step="final_prompts")
            )

            ids = [f"main_{index:02d}" for index in range(1, 7)] + [
                f"detail_{index:02d}" for index in range(1, 9)
            ]
            expected_names = {"final_prompt_index.json", "final_prompt_index.md"}
            for config_id in ids:
                expected_names.add(f"{config_id}_final_prompt.json")
                expected_names.add(f"{config_id}_final_prompt.md")
            self.assertEqual(expected_names, {path.name for path in final_dir.iterdir()})

            index = json.loads((final_dir / "final_prompt_index.json").read_text(encoding="utf-8"))
            self.assertEqual(14, index["prompt_count"])
            self.assertEqual(ids, [item["config_id"] for item in index["items"]])
            self.assertFalse(index["uses_upstream_prompt_files_as_visual_requirements"])

            for config_id in ids:
                doc = json.loads(
                    (final_dir / f"{config_id}_final_prompt.json").read_text(encoding="utf-8")
                )
                output_type = "main" if config_id.startswith("main_") else "detail"
                source_path = main_path if output_type == "main" else detail_path
                self.assertEqual("final_prompt", doc["artifact_type"])
                self.assertEqual(output_type, doc["variable_config"]["output_type"])
                self.assertEqual(str(source_path), doc["variable_config"]["source_path"])
                self.assertEqual(64, len(doc["variable_config"]["source_sha256"]))
                self.assertEqual("/common_constraints", doc["variable_config"]["common_constraints_ref"]["json_pointer"])
                self.assertIn("/per_image_overrides", doc["variable_config"]["per_image_overrides_ref"]["json_pointer"])
                self.assertIn("约 25 厘米", doc["final_prompt"])
                self.assertIn("1:1" if output_type == "main" else "3:4", doc["final_prompt"])
                self.assertFalse(doc["uses_upstream_prompt_files_as_visual_requirements"])

            self.assertEqual((final_dir / "final_prompt_index.json",), result.outputs)
            self.assertEqual("最终提示词已生成", result.detail)
            self.assertEqual(2, len(transport.calls))
            self.assertTrue(all(attachments == () for _prompt, attachments in transport.calls))
            self.assertIn("FINAL_PROMPT_SKILL_MARKER", transport.calls[0][0])
            self.assertIn("FINAL_RUNTIME_MARKER", transport.calls[1][0])
            self.assertFalse((root / "workspace" / "artifacts" / "comfyui_jobs").exists())
            self.assertFalse((root / "workspace" / "artifacts" / "qc_reports").exists())

    def test_final_prompts_invalid_second_batch_leaves_no_formal_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_detail = valid_final_prompt_response("detail")
            invalid_detail["prompts"].pop()
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("main"), ensure_ascii=False),
                        thread_id="thread-final-main",
                    ),
                    CodexTurnResult(
                        text=json.dumps(invalid_detail, ensure_ascii=False),
                        thread_id="thread-final-detail-invalid",
                    ),
                ]
            )

            with self.assertRaises(ExecutorExecutionError):
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="final_prompts")
                )

            self.assertEqual(2, len(transport.calls))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_final_prompts_reject_changed_config_decisions_and_unknown_fields(self) -> None:
        def duplicate_id(value: dict[str, object]) -> None:
            value["prompts"][1]["config_id"] = "main_01"

        def wrong_ratio(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] = value["prompts"][0]["final_prompt"].replace(
                "1:1", "3:4"
            )

        def rejected_asset(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] = value["prompts"][0]["final_prompt"].replace(
                "img_001；A 槽位", "img_005；A 槽位"
            )

        def missing_d(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] = value["prompts"][0]["final_prompt"].replace(
                "img_001；A 槽位", "img_004；D 槽位"
            )

        def missing_height(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] = value["prompts"][0]["final_prompt"].replace(
                "产品高度约 25 厘米", "产品高度未知"
            )

        def changed_handheld(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] = value["prompts"][0]["final_prompt"].replace(
                "启用手持场景", "本张图不启用手持场景"
            )

        def invented_capacity(value: dict[str, object]) -> None:
            value["prompts"][0]["final_prompt"] += "；产品容量 2 L。"

        def damaged_unicode(value: dict[str, object]) -> None:
            value["prompts"][0]["negative_prompt"] += "\ufffd"

        def unknown_field(value: dict[str, object]) -> None:
            value["prompts"][0]["image"] = "PRIVATE_IMAGE_BODY"

        cases = {
            "duplicate_id": duplicate_id,
            "wrong_ratio": wrong_ratio,
            "rejected_asset": rejected_asset,
            "missing_d": missing_d,
            "missing_height": missing_height,
            "changed_handheld": changed_handheld,
            "invented_capacity": invented_capacity,
            "damaged_unicode": damaged_unicode,
            "unknown_field": unknown_field,
        }
        for case_name, mutate in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
                main_response = copy.deepcopy(valid_final_prompt_response("main"))
                mutate(main_response)
                transport = FakeTransport(
                    CodexTurnResult(
                        text=json.dumps(main_response, ensure_ascii=False),
                        thread_id=f"final-{case_name}",
                    )
                )

                with self.assertRaises(ExecutorExecutionError) as caught:
                    CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                        ExecutionRequest(step="final_prompts")
                    )

                self.assertEqual(1, len(transport.calls))
                self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))
                self.assertNotIn("PRIVATE_IMAGE_BODY", str(caught.exception))

    def test_final_prompts_existing_formal_target_is_rejected_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            final_dir.mkdir(parents=True)
            existing = final_dir / "main_01_final_prompt.json"
            existing.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(
                [
                    CodexTurnResult(text=json.dumps(valid_final_prompt_response("main")), thread_id="unused"),
                    CodexTurnResult(text=json.dumps(valid_final_prompt_response("detail")), thread_id="unused"),
                ]
            )

            with self.assertRaises(ExecutorExecutionError):
                CodexDevExecutor(context, transport=transport, repository_root=root).execute(
                    ExecutionRequest(step="final_prompts")
                )

            self.assertEqual('{"preserve": true}', existing.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_identity_uses_required_rules_and_writes_validated_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_dir = self.make_fixture(root)
            response = f"```json\n{json.dumps(VALID_IDENTITY, ensure_ascii=False)}\n```"
            transport = FakeTransport(CodexTurnResult(text=response, thread_id="thread-1"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            result = executor.execute(ExecutionRequest(step="identity"))

            output_path = output_dir / "product_identity_archive.json"
            archive = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("p1", archive["product_id"])
            self.assertEqual(["front.jpg"], archive["source_inputs"])
            self.assertEqual("codex-dev", result.provider)
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("thread-1", result.metadata["thread_id"])
            self.assertEqual("产品身份档案已生成", result.detail)

            prompt, attachments = transport.calls[0]
            self.assertIn("SKILL_MARKER", prompt)
            self.assertIn("REFERENCE_MARKER", prompt)
            self.assertIn("只处理 identity", prompt)
            self.assertEqual(("front.jpg",), tuple(item.name for item in attachments))
            self.assertTrue(attachments[0].data_url.startswith("data:image/jpeg;base64,"))

    def test_style_master_uses_required_rules_identity_lock_and_writes_validated_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, _style_image, _identity_path = self.make_style_fixture(root, identity_context)
            response = f"```json\n{json.dumps(VALID_STYLE_MASTER, ensure_ascii=False)}\n```"
            transport = FakeTransport(CodexTurnResult(text=response, thread_id="thread-style"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            result = executor.execute(ExecutionRequest(step="style_master"))

            output_path = output_dir / "style_master.json"
            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("p1", artifact["product_id"])
            self.assertEqual("style_master", artifact["artifact_type"])
            self.assertEqual(["style.png"], artifact["source_references"])
            self.assertEqual("codex-dev", result.provider)
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("thread-style", result.metadata["thread_id"])
            self.assertEqual("风格母版已生成", result.detail)

            prompt, attachments = transport.calls[0]
            self.assertIn("STYLE_SKILL_MARKER", prompt)
            self.assertIn("STYLE_REFERENCE_MARKER", prompt)
            self.assertIn("product_lock_description", prompt)
            self.assertIn("只处理 style_master", prompt)
            self.assertIn(
                "forbidden_elements 和 concise_style_master 必须位于 style_master 对象内部",
                prompt,
            )
            self.assertIn(
                "missing_information 和 notes 必须在 style_master 对象关闭后、根对象关闭前",
                prompt,
            )
            self.assertEqual(("style.png",), tuple(item.name for item in attachments))
            self.assertTrue(attachments[0].data_url.startswith("data:image/png;base64,"))

    def test_style_master_requires_reference_image_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, style_image, _identity_path = self.make_style_fixture(root, identity_context)
            style_image.unlink()
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_STYLE_MASTER), thread_id="unused"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="style_master"))

            self.assertIn("风格参考图", str(caught.exception))
            self.assertEqual([], transport.calls)
            self.assertFalse((output_dir / "style_master.json").exists())

    def test_style_master_requires_product_identity_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, _style_image, identity_path = self.make_style_fixture(root, identity_context)
            identity_path.unlink()
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_STYLE_MASTER), thread_id="unused"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="style_master"))

            self.assertIn("产品身份档案", str(caught.exception))
            self.assertEqual([], transport.calls)
            self.assertFalse((output_dir / "style_master.json").exists())

    def test_style_master_rejects_out_of_scope_response_without_raw_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, _style_image, _identity_path = self.make_style_fixture(root, identity_context)
            response = dict(VALID_STYLE_MASTER)
            response["final_prompt"] = "PRIVATE_STYLE_BODY"
            transport = FakeTransport(CodexTurnResult(text=json.dumps(response), thread_id="thread-style-invalid"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="style_master"))

            self.assertIn("返回格式异常", str(caught.exception))
            self.assertNotIn("PRIVATE_STYLE_BODY", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertFalse((output_dir / "style_master.json").exists())

    def test_existing_style_master_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, _style_image, _identity_path = self.make_style_fixture(root, identity_context)
            output_dir.mkdir(parents=True)
            output_path = output_dir / "style_master.json"
            output_path.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_STYLE_MASTER), thread_id="unused"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="style_master"))

            self.assertIn("已存在", str(caught.exception))
            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_angle_inventory_uses_single_product_rules_and_writes_one_entry_per_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, image_paths, _identity_path = self.make_angle_fixture(
                root,
                identity_context,
            )
            asset_ids = tuple(f"img_{index:03d}" for index in range(1, 13))
            response = f"```json\n{json.dumps(valid_angle_inventory(asset_ids), ensure_ascii=False)}\n```"
            transport = FakeTransport(CodexTurnResult(text=response, thread_id="thread-angle"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            result = executor.execute(ExecutionRequest(step="angle_inventory"))

            output_path = output_dir / "angle_inventory.json"
            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("p1", artifact["product_id"])
            self.assertEqual("angle_inventory", artifact["artifact_type"])
            self.assertEqual(12, len(artifact["image_assets"]))
            self.assertEqual(12, len(artifact["angle_slots"]))
            self.assertEqual(
                {item["asset_id"] for item in artifact["image_assets"]},
                {item["source_asset_id"] for item in artifact["angle_slots"]},
            )
            self.assertEqual([path.name for path in image_paths], [item["file_path"] for item in artifact["image_assets"]])
            self.assertEqual("codex-dev", result.provider)
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("thread-angle", result.metadata["thread_id"])
            self.assertEqual("角度槽位入库表已生成", result.detail)

            prompt, attachments = transport.calls[0]
            self.assertIn("ANGLE_SKILL_MARKER", prompt)
            self.assertIn("ANGLE_REFERENCE_MARKER", prompt)
            self.assertIn("product_lock_description", prompt)
            self.assertIn("只处理 angle_inventory", prompt)
            self.assertIn("single", prompt)
            self.assertIn("A、B、C、D", prompt)
            self.assertIn("忽略末尾误植的套装字段", prompt)
            self.assertEqual(tuple(path.name for path in image_paths), tuple(item.name for item in attachments))
            self.assertNotIn("must-not-be-attached.png", tuple(item.name for item in attachments))

    def test_angle_inventory_prompt_clarifies_a_b_and_upright_d_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, _output_dir, _image_paths, _identity_path = self.make_angle_fixture(
                root,
                identity_context,
            )
            asset_ids = tuple(f"img_{index:03d}" for index in range(1, 13))
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_angle_inventory(asset_ids), ensure_ascii=False),
                    thread_id="thread-angle-boundaries",
                )
            )
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            executor.execute(ExecutionRequest(step="angle_inventory"))

            prompt, _attachments = transport.calls[0]
            self.assertIn("正面主体面占主导", prompt)
            self.assertIn("B 必须有明显侧面展开", prompt)
            self.assertIn("D 只允许产品直立", prompt)
            self.assertIn("横放、侧躺、倒置或仅拍底部", prompt)

    def test_angle_inventory_prompt_applies_exif_display_orientation_before_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, _output_dir, image_paths, _identity_path = self.make_angle_fixture(
                root,
                identity_context,
            )
            image_paths[0].write_bytes(jpeg_with_exif_orientation(8))
            asset_ids = tuple(f"img_{index:03d}" for index in range(1, 13))
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_angle_inventory(asset_ids), ensure_ascii=False),
                    thread_id="thread-angle-orientation",
                )
            )
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            executor.execute(ExecutionRequest(step="angle_inventory"))

            prompt, _attachments = transport.calls[0]
            self.assertIn('"source_asset_id": "img_001"', prompt)
            self.assertIn('"exif_orientation": 8', prompt)
            self.assertIn('"display_rotation": "逆时针旋转90°"', prompt)
            self.assertIn("先按 EXIF 方向纠正显示，再判断产品是否直立", prompt)
            self.assertIn("不可辨认文字统一写“无法辨认”", prompt)

    def test_angle_inventory_requires_images_and_identity_before_transport(self) -> None:
        for missing in ("images", "identity"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                identity_context, _identity_output = self.make_fixture(root)
                context, output_dir, image_paths, identity_path = self.make_angle_fixture(
                    root,
                    identity_context,
                )
                if missing == "images":
                    for image_path in image_paths:
                        image_path.unlink()
                else:
                    identity_path.unlink()
                transport = FakeTransport(
                    CodexTurnResult(text=json.dumps(valid_angle_inventory(())), thread_id="unused")
                )
                executor = CodexDevExecutor(context, transport=transport, repository_root=root)

                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))

                self.assertIn("白底图" if missing == "images" else "产品身份档案", str(caught.exception))
                self.assertEqual([], transport.calls)
                self.assertFalse((output_dir / "angle_inventory.json").exists())

    def test_angle_inventory_rejects_set_product_before_transport(self) -> None:
        for batch_type, declared in (("set", False), ("single", True)):
            with self.subTest(batch_type=batch_type, declared=declared), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                identity_context, _identity_output = self.make_fixture(root)
                context, output_dir, _image_paths, _identity_path = self.make_angle_fixture(
                    root,
                    identity_context,
                )
                manifest = json.loads(json.dumps(context.manifest))
                manifest["batch_type"] = batch_type
                manifest["user_declared_set_product"] = declared
                unsafe_context = ExecutorContext(
                    manifest=manifest,
                    manifest_path=context.manifest_path,
                    environment=context.environment,
                )
                transport = FakeTransport(CodexTurnResult(text="{}", thread_id="unused"))
                executor = CodexDevExecutor(unsafe_context, transport=transport, repository_root=root)

                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))

                self.assertIn("单品", str(caught.exception))
                self.assertEqual([], transport.calls)
                self.assertFalse((output_dir / "angle_inventory.json").exists())

    def test_angle_inventory_rejects_invalid_or_out_of_scope_response(self) -> None:
        cases: dict[str, object] = {}
        asset_ids = tuple(f"img_{index:03d}" for index in range(1, 13))

        duplicate = valid_angle_inventory(asset_ids)
        duplicate["angle_slots"][1]["source_asset_id"] = "img_001"
        cases["duplicate_source"] = duplicate

        unknown = valid_angle_inventory(asset_ids)
        unknown["angle_slots"][0]["source_asset_id"] = "img_999"
        cases["unknown_source"] = unknown

        illegal_slot = valid_angle_inventory(asset_ids)
        illegal_slot["angle_slots"][0]["angle_slot"] = "E"
        cases["illegal_slot"] = illegal_slot

        illegal_admission = valid_angle_inventory(asset_ids)
        illegal_admission["angle_slots"][0]["admission_result"] = "可以"
        cases["illegal_admission"] = illegal_admission

        illegal_suitability = valid_angle_inventory(asset_ids)
        illegal_suitability["angle_slots"][0]["main_image_suitability"] = "很好"
        cases["illegal_suitability"] = illegal_suitability

        inconsistent_missing_slots = valid_angle_inventory(asset_ids)
        inconsistent_missing_slots["angle_slots"][0]["angle_slot"] = "A"
        cases["inconsistent_missing_slots"] = inconsistent_missing_slots

        replacement_character = valid_angle_inventory(asset_ids)
        replacement_character["angle_slots"][0]["risk_notes"] = "底部文字不得解读品牌��认证"
        cases["replacement_character"] = replacement_character

        missing_field = valid_angle_inventory(asset_ids)
        missing_field["angle_slots"][0].pop("decision_basis")
        cases["missing_field"] = missing_field

        out_of_scope = valid_angle_inventory(asset_ids)
        out_of_scope["set_layouts"] = "PRIVATE_SET_BODY"
        cases["out_of_scope"] = out_of_scope

        unexpected = valid_angle_inventory(asset_ids)
        unexpected["unexpected_private_field"] = "PRIVATE_UNDECLARED_BODY"
        cases["unexpected_top_level"] = unexpected

        for name, response in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                identity_context, _identity_output = self.make_fixture(root)
                context, output_dir, _image_paths, _identity_path = self.make_angle_fixture(
                    root,
                    identity_context,
                )
                transport = FakeTransport(
                    CodexTurnResult(text=json.dumps(response, ensure_ascii=False), thread_id="invalid")
                )
                executor = CodexDevExecutor(context, transport=transport, repository_root=root)

                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))

                self.assertIn("返回格式异常", str(caught.exception))
                self.assertNotIn("PRIVATE_SET_BODY", str(caught.exception))
                self.assertNotIn("PRIVATE_UNDECLARED_BODY", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertFalse((output_dir / "angle_inventory.json").exists())

    def test_existing_angle_inventory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, output_dir, _image_paths, _identity_path = self.make_angle_fixture(root, identity_context)
            output_dir.mkdir(parents=True)
            output_path = output_dir / "angle_inventory.json"
            output_path.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(CodexTurnResult(text="{}", thread_id="unused"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="angle_inventory"))

            self.assertIn("已存在", str(caught.exception))
            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_angle_inventory_output_must_stay_under_artifacts_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_context, _identity_output = self.make_fixture(root)
            context, _output_dir, _image_paths, _identity_path = self.make_angle_fixture(root, identity_context)
            manifest = json.loads(json.dumps(context.manifest))
            manifest["artifacts"]["angle_inventory"] = str(root / "outside")
            unsafe_context = ExecutorContext(
                manifest=manifest,
                manifest_path=context.manifest_path,
                environment=context.environment,
            )
            transport = FakeTransport(CodexTurnResult(text="{}", thread_id="unused"))
            executor = CodexDevExecutor(unsafe_context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="angle_inventory"))

            self.assertIn("artifacts_root", str(caught.exception))
            self.assertEqual([], transport.calls)

    def test_unsupported_step_is_rejected_before_transport_or_file_access(self) -> None:
        context = ExecutorContext(manifest={}, manifest_path=None, environment={})
        transport = FakeTransport(CodexTurnResult(text="{}", thread_id="unused"))
        executor = CodexDevExecutor(context, transport=transport, repository_root=Path("Z:/missing"))

        with self.assertRaises(ExecutorExecutionError) as caught:
            executor.execute(ExecutionRequest(step="render"))

        self.assertIn(
            "仅支持 identity、style_master、angle_inventory、main_vc，detail_vc、final_prompts",
            str(caught.exception),
        )
        self.assertEqual([], transport.calls)

    def test_missing_canvas_agent_config_becomes_unified_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_dir = self.make_fixture(root)
            transport = CanvasAgentCodexTransport(config_path=root / "missing-config.json")
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            self.assertIn("canvas-agent 配置缺失", str(caught.exception))
            self.assertFalse((output_dir / "product_identity_archive.json").exists())

    def test_transport_failures_are_sanitized_and_do_not_write_output(self) -> None:
        cases = {
            "connection": "无法连接 canvas-agent",
            "thread": "Codex 线程执行失败",
        }
        for code, expected in cases.items():
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, output_dir = self.make_fixture(root)
                transport = FakeTransport(error=CanvasAgentTransportError(code, "token=secret FULL_PRODUCT_BODY"))
                executor = CodexDevExecutor(context, transport=transport, repository_root=root)

                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="identity"))

                message = str(caught.exception)
                self.assertIn(expected, message)
                self.assertNotIn("secret", message)
                self.assertNotIn("FULL_PRODUCT_BODY", message)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertFalse((output_dir / "product_identity_archive.json").exists())

    def test_malformed_or_out_of_scope_response_is_rejected_without_raw_body(self) -> None:
        malformed = {
            "artifact_type": "product_identity_archive",
            "identity": {"confirmed_facts": ["PRIVATE_DETAIL"]},
            "final_prompt": "out of scope",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_dir = self.make_fixture(root)
            transport = FakeTransport(CodexTurnResult(text=json.dumps(malformed), thread_id="thread-2"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            message = str(caught.exception)
            self.assertIn("返回格式异常", message)
            self.assertNotIn("PRIVATE_DETAIL", message)
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertFalse((output_dir / "product_identity_archive.json").exists())

    def test_invalid_json_does_not_remain_in_exception_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, _output_dir = self.make_fixture(root)
            transport = FakeTransport(CodexTurnResult(text='{"private":"FULL_PRODUCT_BODY"', thread_id="thread-json"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            self.assertNotIn("FULL_PRODUCT_BODY", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)

    def test_existing_archive_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_dir = self.make_fixture(root)
            output_dir.mkdir(parents=True)
            output_path = output_dir / "product_identity_archive.json"
            output_path.write_text('{"preserve": true}', encoding="utf-8")
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_IDENTITY), thread_id="unused"))
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            self.assertIn("已存在", str(caught.exception))
            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_output_must_stay_under_manifest_artifacts_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, _output_dir = self.make_fixture(root)
            manifest = dict(context.manifest)
            manifest["artifacts"] = {"product_identity_archive": str(root / "outside")}
            unsafe_context = ExecutorContext(
                manifest=manifest,
                manifest_path=context.manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            )
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_IDENTITY), thread_id="unused"))
            executor = CodexDevExecutor(unsafe_context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            self.assertIn("artifacts_root", str(caught.exception))
            self.assertEqual([], transport.calls)

    def test_real_execution_requires_explicit_process_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_dir = self.make_fixture(root)
            disabled_context = ExecutorContext(
                manifest=context.manifest,
                manifest_path=context.manifest_path,
                environment={},
            )
            transport = FakeTransport(CodexTurnResult(text=json.dumps(VALID_IDENTITY), thread_id="unused"))
            executor = CodexDevExecutor(disabled_context, transport=transport, repository_root=root)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="identity"))

            self.assertIn("未获准真实执行", str(caught.exception))
            self.assertEqual([], transport.calls)
            self.assertFalse((output_dir / "product_identity_archive.json").exists())


class CanvasAgentCodexTransportTest(unittest.TestCase):
    def test_http_and_sse_are_wrapped_without_real_network(self) -> None:
        sse = b"".join(
            [
                b'event: hello\ndata: {"ok":true}\n\n',
                b'event: agent_event\ndata: {"agent":"codex","type":"turn.started"}\n\n',
                b'event: agent_event\ndata: {"agent":"codex","type":"item.updated","item":{"type":"agent_message","text":"{\\"artifact_type\\":\\"product_identity_archive\\"}"}}\n\n',
                b'event: agent_done\ndata: {"agent":"codex"}\n\n',
            ]
        )
        responses = [
            FakeResponse(sse),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-3"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-3"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"assistant","text":"{\\"artifact_type\\":\\"product_identity_archive\\",\\"identity\\":{}}"}]}'),
        ]
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        result = transport.run_turn("offline prompt", ())

        self.assertEqual("thread-3", result.thread_id)
        self.assertIn("product_identity_archive", result.text)
        self.assertEqual(
            ["/events", "/agent/codex/threads/new", "/agent/codex/turn", "/agent/codex/threads/thread-3"],
            [urllib.parse.urlparse(item[0].full_url).path for item in requests],
        )
        new_thread_request = next(
            item[0]
            for item in requests
            if urllib.parse.urlparse(item[0].full_url).path == "/agent/codex/threads/new"
        )
        self.assertEqual({"model": "gpt-5.5"}, json.loads(new_thread_request.data.decode("utf-8")))
        self.assertTrue(all(item[0].get_header("X-canvas-agent-token") == "test-token" for item in requests))

    def test_existing_thread_continuation_reuses_thread_without_creating_another(self) -> None:
        sse = b'event: agent_done\ndata: {"agent":"codex","status":"completed"}\n\n'
        responses = [
            FakeResponse(sse),
            FakeResponse(
                b'{"ok":true,"thread":{"id":"thread-existing"},"messages":['
                b'{"role":"user","text":"original"},'
                b'{"role":"assistant","text":"OLD_RESULT"}]}'
            ),
            FakeResponse(b'{"ok":true,"threadId":"thread-existing"}'),
            FakeResponse(
                b'{"ok":true,"thread":{"id":"thread-existing"},"messages":['
                b'{"role":"user","text":"original"},'
                b'{"role":"assistant","text":"OLD_RESULT"},'
                b'{"role":"user","text":"repair"},'
                b'{"role":"assistant","text":"NEW_REPAIRED_RESULT"}]}'
            ),
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        result = transport.continue_turn("thread-existing", "repair", ())

        paths = [urllib.parse.urlparse(request.full_url).path for request in requests]
        self.assertEqual("thread-existing", result.thread_id)
        self.assertEqual("NEW_REPAIRED_RESULT", result.text)
        self.assertEqual(
            [
                "/events",
                "/agent/codex/threads/thread-existing",
                "/agent/codex/turn",
                "/agent/codex/threads/thread-existing",
            ],
            paths,
        )
        self.assertNotIn("/agent/codex/threads/new", paths)
        turn_request = requests[2]
        self.assertEqual(
            {
                "threadId": "thread-existing",
                "prompt": "repair",
                "attachments": [],
            },
            json.loads(turn_request.data.decode("utf-8")),
        )

    def test_failed_agent_done_is_rejected_before_thread_result_read(self) -> None:
        sse = b'event: agent_done\ndata: {"agent":"codex","status":"failed"}\n\n'
        responses = [
            FakeResponse(sse),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-failed"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-failed"}'),
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("offline prompt", ())

        self.assertEqual("thread", caught.exception.code)
        self.assertNotIn(
            "/agent/codex/threads/thread-failed",
            [urllib.parse.urlparse(request.full_url).path for request in requests],
        )

    def test_sse_message_from_another_turn_is_not_used_as_result(self) -> None:
        sse = b"".join(
            [
                b'event: agent_event\ndata: {"agent":"codex","type":"turn.started"}\n\n',
                b'event: agent_event\ndata: {"agent":"codex","type":"item.updated","item":{"type":"agent_message","text":"WRONG_THREAD_PRIVATE_BODY"}}\n\n',
                b'event: agent_done\ndata: {"agent":"codex"}\n\n',
                b'event: agent_event\ndata: {"agent":"codex","type":"turn.started"}\n\n',
                b'event: agent_done\ndata: {"agent":"codex"}\n\n',
            ]
        )
        responses = [
            FakeResponse(sse),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-own"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-own"}'),
            FakeResponse(b'{"ok":true,"messages":[]}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"assistant","text":"OWN_THREAD_RESULT"}]}'),
        ]

        def opener(_request, timeout):
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        result = transport.run_turn("offline prompt", ())

        self.assertEqual("OWN_THREAD_RESULT", result.text)
        self.assertNotEqual("WRONG_THREAD_PRIVATE_BODY", result.text)

    def test_large_attachment_set_is_split_across_one_thread_before_final_synthesis(self) -> None:
        sse = b"".join(
            b'event: agent_done\ndata: {"agent":"codex"}\n\n' for _ in range(3)
        )
        responses = [
            FakeResponse(sse),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-batch"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-batch"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"assistant","text":"RECORDED_1"}]}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-batch"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"assistant","text":"RECORDED_1"},{"role":"assistant","text":"RECORDED_2"}]}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-batch"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"assistant","text":"RECORDED_1"},{"role":"assistant","text":"RECORDED_2"},{"role":"assistant","text":"FINAL_IDENTITY_JSON"}]}'),
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
            max_attachment_payload_bytes=40,
        )
        attachments = (
            CodexAttachment("first.jpg", "image/jpeg", "data:image/jpeg;base64,AAAAAAAAAAAA"),
            CodexAttachment("second.jpg", "image/jpeg", "data:image/jpeg;base64,BBBBBBBBBBBB"),
        )

        result = transport.run_turn("IDENTITY_RULES", attachments)

        turn_bodies = [
            json.loads(request.data.decode("utf-8"))
            for request in requests
            if urllib.parse.urlparse(request.full_url).path == "/agent/codex/turn"
        ]
        self.assertEqual("FINAL_IDENTITY_JSON", result.text)
        self.assertEqual(3, len(turn_bodies))
        self.assertEqual([1, 1, 0], [len(body["attachments"]) for body in turn_bodies])
        self.assertTrue(all(body["threadId"] == "thread-batch" for body in turn_bodies))
        self.assertIn("第 1/2 批", turn_bodies[0]["prompt"])
        self.assertIn("第 2/2 批", turn_bodies[1]["prompt"])
        self.assertTrue(all("必须返回非空 JSON" in body["prompt"] for body in turn_bodies[:2]))
        self.assertTrue(all("batch_observation" in body["prompt"] for body in turn_bodies[:2]))
        self.assertIn("综合本线程全部", turn_bodies[2]["prompt"])

    def test_single_attachment_over_chunk_limit_is_rejected_before_network(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise AssertionError("network must not be attempted")

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
            max_attachment_payload_bytes=40,
        )
        attachment = CodexAttachment(
            "oversized.jpg",
            "image/jpeg",
            "data:image/jpeg;base64," + "A" * 50,
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("private prompt", (attachment,))

        self.assertEqual("response", caught.exception.code)
        self.assertEqual([], calls)

    def test_complete_json_request_body_limit_is_enforced_before_turn_post(self) -> None:
        responses = [
            FakeResponse(b'event: hello\ndata: {"ok":true}\n\n'),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-body-limit"}}'),
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
            max_request_body_bytes=120,
        )
        attachment = CodexAttachment("one.jpg", "image/jpeg", "data:image/jpeg;base64,AAAA")

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("prompt with enough text to exceed the deliberately tiny body limit", (attachment,))

        self.assertEqual("response", caught.exception.code)
        self.assertNotIn(
            "/agent/codex/turn",
            [urllib.parse.urlparse(request.full_url).path for request in requests],
        )

    def test_empty_second_turn_detected_when_first_turn_had_multiple_assistants(self) -> None:
        sse = b"".join(
            b'event: agent_done\ndata: {"agent":"codex"}\n\n' for _ in range(2)
        )
        responses = [
            FakeResponse(sse),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-counts"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-counts"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"user","text":"batch 1"},{"role":"assistant","text":"A1"},{"role":"assistant","text":"A2"}]}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-counts"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"user","text":"batch 1"},{"role":"assistant","text":"A1"},{"role":"assistant","text":"A2"},{"role":"user","text":"batch 2"}]}'),
        ]

        def opener(_request, timeout):
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
            max_attachment_payload_bytes=40,
        )
        attachments = (
            CodexAttachment("first.jpg", "image/jpeg", "data:image/jpeg;base64,AAAAAAAAAAAA"),
            CodexAttachment("second.jpg", "image/jpeg", "data:image/jpeg;base64,BBBBBBBBBBBB"),
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("IDENTITY_RULES", attachments)

        self.assertEqual("response", caught.exception.code)

    def test_non_loopback_canvas_agent_url_is_refused_before_network(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise AssertionError("network must not be attempted")

        transport = CanvasAgentCodexTransport(
            config={"url": "https://example.com:17371", "token": "test-token"},
            opener=opener,
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("private prompt", ())

        self.assertEqual("unsafe_config", caught.exception.code)
        self.assertEqual([], calls)

    def test_default_transport_explicitly_disables_system_proxies(self) -> None:
        fake_opener = mock.Mock()
        with mock.patch("codex_dev_executor.urllib.request.build_opener", return_value=fake_opener) as build:
            transport = CanvasAgentCodexTransport()

        build.assert_called_once()
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, urllib.request.ProxyHandler)
        self.assertEqual({}, handler.proxies)
        self.assertEqual(fake_opener.open, transport.opener)

    def test_thread_http_failure_is_classified_without_response_body(self) -> None:
        responses = [FakeResponse(b'event: hello\ndata: {"ok":true}\n\n')]

        def opener(request, timeout):
            if responses:
                return responses.pop(0)
            raise urllib.error.HTTPError(request.full_url, 500, "server error", {}, io.BytesIO(b"token=secret FULL_PRODUCT_BODY"))

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("offline prompt", ())

        self.assertEqual("thread", caught.exception.code)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("FULL_PRODUCT_BODY", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_completed_own_turn_without_assistant_is_response_error(self) -> None:
        responses = [
            FakeResponse(b'event: agent_done\ndata: {"agent":"codex"}\n\n'),
            FakeResponse(b'{"ok":true,"thread":{"id":"thread-empty"}}'),
            FakeResponse(b'{"ok":true,"threadId":"thread-empty"}'),
            FakeResponse(b'{"ok":true,"messages":[{"role":"user","text":"batch input"}]}'),
        ]

        def opener(_request, timeout):
            return responses.pop(0)

        transport = CanvasAgentCodexTransport(
            config={"url": "http://127.0.0.1:17371", "token": "test-token"},
            opener=opener,
        )

        with self.assertRaises(CanvasAgentTransportError) as caught:
            transport.run_turn("offline prompt", ())

        self.assertEqual("response", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
