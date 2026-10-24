"""海报提示词模板：按风格档位查表，员工不填创意时走默认。

设计原则：
1. 产品必须保持不变 —— 提示词里用最强措辞约束。
2. 顶部留白 —— 给后续叠加中文标题用（扩散模型写中文不可靠）。
3. 画面禁止出现文字 —— 避免模型画出乱码。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StyleTemplate:
    key: str
    label: str
    scene: str
    aspect_ratio: str
    resolution: str


STYLES: dict[str, StyleTemplate] = {
    "夏日清爽": StyleTemplate(
        key="夏日清爽",
        label="夏日清爽",
        scene=(
            "明亮清爽的夏日运动风，健身房环境配冰块与冷色调光影，"
            "柔和侧逆光，产品下方自然投影，画面干净高级"
        ),
        aspect_ratio="3:4",
        resolution="2K",
    ),
    "健身能量": StyleTemplate(
        key="健身能量",
        label="健身能量",
        scene=(
            "高对比度运动能量风，深色背景配蓝色电流光效与薄雾，"
            "产品居中受光，边缘有高光轮廓，强烈运动感"
        ),
        aspect_ratio="3:4",
        resolution="2K",
    ),
    "简约电商": StyleTemplate(
        key="简约电商",
        label="简约电商",
        scene=(
            "简约电商主图风，浅灰渐变背景，产品置于柔和台面，"
            "大面积留白，克制的高级感，商业摄影灯光"
        ),
        aspect_ratio="1:1",
        resolution="2K",
    ),
}

DEFAULT_STYLE = "夏日清爽"

_BASE = (
    "商业海报摄影。以参考图中的产品为唯一主体，"
    "完整保留产品造型、标签文字与 logo 的原始外观和配色，不要改动产品本身。"
)

_NEGATIVE_TAIL = "画面中不要出现任何文字，顶部留出大面积空白用于放置标题。"


def build_prompt(style_key: str | None, requirement: str | None) -> tuple[str, StyleTemplate]:
    """组装最终提示词，返回 (prompt, template)。"""
    template = STYLES.get((style_key or "").strip(), STYLES[DEFAULT_STYLE])
    parts = [_BASE]
    if requirement and requirement.strip():
        parts.append(f"创意要求：{requirement.strip()}。")
    parts.append(f"场景：{template.scene}。")
    parts.append(_NEGATIVE_TAIL)
    return "".join(parts), template
