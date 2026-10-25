"""新版元气森林九宫格工作流的主题、方案和用户补充提示。"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class StyleTemplate:
    key: str
    label: str
    activity_index: int
    scheme_index: int


# activity_index 对应新版工作流 3761723474387808 节点的 select 值。
STYLES: dict[str, StyleTemplate] = {
    "夏日冰爽": StyleTemplate(
        key="夏日冰爽",
        label="夏日冰爽",
        activity_index=0,
        scheme_index=1,
    ),
    "运动燃脂": StyleTemplate(
        key="运动燃脂",
        label="运动燃脂",
        activity_index=1,
        scheme_index=1,
    ),
    "户外露营": StyleTemplate(
        key="户外露营",
        label="户外露营",
        activity_index=2,
        scheme_index=1,
    ),
    "节日礼赠": StyleTemplate(
        key="节日礼赠",
        label="节日礼赠",
        activity_index=3,
        scheme_index=1,
    ),
    "办公提神": StyleTemplate(
        key="办公提神",
        label="办公提神",
        activity_index=4,
        scheme_index=1,
    ),
}

SCHEMES: dict[str, int] = {
    "电商主图": 0,
    "TVC动态分镜": 1,
    "卖点海报矩阵": 2,
}

# 节点 705857498768221：0 写实影棚（等同旧版效果）、1 几何品牌大片。
VISUALS: dict[str, int] = {
    "写实影棚": 0,
    "几何品牌大片": 1,
}

# 节点 4105141153397020：0 品牌角标、1 活力气泡、2 书法质感。
FONTS: dict[str, int] = {
    "品牌角标": 0,
    "活力气泡": 1,
    "书法质感": 2,
}

# 兼容旧版飞书表中已经存在的三个选项。
_STYLE_ALIASES = {
    "夏日清爽": "夏日冰爽",
    "健身能量": "运动燃脂",
    "简约电商": "夏日冰爽",
}

DEFAULT_STYLE = "夏日冰爽"
DEFAULT_SCHEME = "TVC动态分镜"
DEFAULT_VISUAL = "写实影棚"
DEFAULT_FONT = "品牌角标"


def build_prompt(
    style_key: str | None,
    requirement: str | None,
    scheme_key: str | None = None,
) -> tuple[str, StyleTemplate]:
    """返回追加到新版工作流固定提示后的用户补充提示和主题模板。"""
    raw_style = (style_key or "").strip()
    style_key = _STYLE_ALIASES.get(raw_style, raw_style)
    template = STYLES.get(style_key, STYLES[DEFAULT_STYLE])

    raw_scheme = (scheme_key or "").strip()
    if not raw_scheme and raw_style == "简约电商":
        raw_scheme = "电商主图"
    scheme_index = SCHEMES.get(raw_scheme, template.scheme_index)
    template = replace(template, scheme_index=scheme_index)

    if requirement and requirement.strip():
        prompt = (
            "用户补充创意要求："
            + requirement.strip()
            + "。请将其作为构图和氛围参考，但不要违背工作流中的产品还原、文字和输出规则。"
        )
    else:
        prompt = "无额外创意要求，请严格遵守工作流中的固定规则。"
    return prompt, template


def resolve_visual(name: str | None) -> int:
    """画面风格 → 节点 705857498768221 的 select，空值走默认写实影棚。"""
    return VISUALS.get((name or "").strip(), VISUALS[DEFAULT_VISUAL])


def resolve_font(name: str | None) -> int:
    """字体风格 → 节点 4105141153397020 的 select，空值走默认品牌角标。"""
    return FONTS.get((name or "").strip(), FONTS[DEFAULT_FONT])
