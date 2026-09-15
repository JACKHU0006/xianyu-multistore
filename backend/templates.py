"""
规则模板继承

多店管理最烦的一件事
--------------------
同一个商品在 8 个店都上架了。某天要调议价策略，于是你改了 8 次；下次再调，
又改 8 次。改到第 6 个店的时候手机响了，回来接着改，漏了一家 —— 那家店的
底价保护还是旧的，AI 照旧策略砍价，一天亏几百。

所以需要模板继承。但"所有店共用一套规则"又不行：不同店的客群、成本、竞争
环境都不一样，必须允许单店覆盖。

于是是三层：

    默认值（代码里的兜底）
        ↓ 被覆盖
    租户模板（一改全改，这是"省事"的来源）
        ↓ 被覆盖
    单店覆盖（这家店的特殊情况，这是"灵活"的来源）

**改模板前必须先看会影响谁** —— 这就是 `preview_template_change()` 的作用。
它会把"该店自己覆盖了某字段"这种情况正确排除掉：模板改了，但这个店本来就不
听模板的，那它就不该出现在影响清单里。漏掉这一层判断，预览就是错的。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Iterable, Mapping, Optional

FIELDS: tuple[str, ...] = (
    "auto_reply",
    "auto_bargain",
    "auto_ship",
    "low_stock_threshold",
    "bargain_ladder",
    "quiet_hours",
    "max_bargain_rounds",
    "handoff_threshold",
)

LAYER_NAMES = ("default", "template", "store")


@dataclass(frozen=True)
class RuleSet:
    """
    一层规则。

    **None 表示"这一层不管，交给下层"** —— 这正是继承能工作的前提。
    所以不要用 False 表示"不设置"，False 是明确的"关掉"。
    """

    auto_reply: Optional[bool] = None
    auto_bargain: Optional[bool] = None
    auto_ship: Optional[bool] = None
    low_stock_threshold: Optional[int] = None
    bargain_ladder: Optional[tuple[float, ...]] = None
    quiet_hours: Optional[tuple[int, int]] = None
    max_bargain_rounds: Optional[int] = None
    handoff_threshold: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, tuple):
                out[key] = list(value)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuleSet":
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if value is not None and f.name in ("bargain_ladder", "quiet_hours"):
                value = tuple(value)
            kwargs[f.name] = value
        return cls(**kwargs)


DEFAULTS = RuleSet(
    auto_reply=True,
    auto_bargain=True,
    auto_ship=False,          # 自动发货默认关 —— 这个开关不该默认打开
    low_stock_threshold=10,
    bargain_ladder=(0.05, 0.12, 0.23),
    quiet_hours=(23, 8),
    max_bargain_rounds=4,
    handoff_threshold=60,
)


# ===========================================================================
# 一、解析
# ===========================================================================

def resolve(*layers: RuleSet) -> RuleSet:
    """
    逐字段合并，后面的层覆盖前面的层（只覆盖非 None 的字段）。

    调用顺序固定为 resolve(DEFAULTS, 模板, 单店覆盖)。
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        for name in FIELDS:
            value = getattr(layer, name, None)
            if value is not None:
                merged[name] = value
    return RuleSet(**merged)


def trace(field_name: str, *layers: RuleSet) -> str:
    """
    这个字段最终是谁定的：default / template / store。

    排查"为什么这个店没跟着模板走"的时候，看一眼就清楚了 ——
    比翻三层配置快得多。
    """
    if field_name not in FIELDS:
        raise KeyError(f"未知字段 {field_name}")
    winner = LAYER_NAMES[0]
    for name, layer in zip(LAYER_NAMES, layers):
        if getattr(layer, field_name, None) is not None:
            winner = name
    return winner


def diff(old: RuleSet, new: RuleSet) -> dict[str, tuple[Any, Any]]:
    """两套规则之间真正变化的字段。"""
    out: dict[str, tuple[Any, Any]] = {}
    for name in FIELDS:
        before, after = getattr(old, name), getattr(new, name)
        if before != after:
            out[name] = (before, after)
    return out


# ===========================================================================
# 二、模板变更影响预览
# ===========================================================================

@dataclass(frozen=True)
class TemplateImpact:
    store_id: str
    changed_fields: tuple[str, ...]
    before: RuleSet
    after: RuleSet

    @property
    def summary(self) -> str:
        parts = []
        for name in self.changed_fields:
            b, a = getattr(self.before, name), getattr(self.after, name)
            parts.append(f"{name}: {b} → {a}")
        return f"{self.store_id}：" + "；".join(parts)


def preview_template_change(
    old_template: RuleSet,
    new_template: RuleSet,
    overrides: Mapping[str, RuleSet],
    *,
    defaults: RuleSet = DEFAULTS,
) -> tuple[TemplateImpact, ...]:
    """
    改模板前，先看会影响哪些店、影响哪些字段。

    关键点：**被单店覆盖的字段不算受影响**。模板改了 5 个字段，但某个店
    自己覆盖了其中 3 个，那它只受 2 个字段影响。漏掉这个判断，预览就会虚高，
    然后人就不看预览了 —— 预览一旦不可信就等于没有。
    """
    impacts: list[TemplateImpact] = []
    for store_id, override in overrides.items():
        before = resolve(defaults, old_template, override)
        after = resolve(defaults, new_template, override)
        changed = tuple(name for name in FIELDS if getattr(before, name) != getattr(after, name))
        if changed:
            impacts.append(TemplateImpact(store_id, changed, before, after))
    return tuple(impacts)


def unaffected_stores(
    old_template: RuleSet,
    new_template: RuleSet,
    overrides: Mapping[str, RuleSet],
    *,
    defaults: RuleSet = DEFAULTS,
) -> tuple[str, ...]:
    affected = {i.store_id for i in preview_template_change(
        old_template, new_template, overrides, defaults=defaults)}
    return tuple(sid for sid in overrides if sid not in affected)


# ===========================================================================
# 三、校验
# ===========================================================================

def validate(rules: RuleSet) -> list[str]:
    """
    返回问题清单（空表示合法）。

    在**写入前**校验，而不是等运行时炸 —— 比如阶梯比例必须递增，
    否则第 2 轮的让步会比第 1 轮还小，议价逻辑就反了。
    """
    problems: list[str] = []

    if rules.low_stock_threshold is not None and rules.low_stock_threshold < 0:
        problems.append("low_stock_threshold 不能为负")

    if rules.max_bargain_rounds is not None and rules.max_bargain_rounds < 1:
        problems.append("max_bargain_rounds 至少为 1")

    if rules.handoff_threshold is not None and not 0 < rules.handoff_threshold <= 100:
        problems.append("handoff_threshold 必须在 1-100 之间")

    ladder = rules.bargain_ladder
    if ladder is not None:
        if not ladder:
            problems.append("bargain_ladder 不能为空")
        elif any(p < 0 or p >= 1 for p in ladder):
            problems.append("bargain_ladder 每档必须在 [0, 1) 之间")
        elif list(ladder) != sorted(ladder):
            problems.append("bargain_ladder 必须逐轮递增，否则后面轮的让步比前面还小")

    quiet = rules.quiet_hours
    if quiet is not None:
        start, end = quiet
        if not (0 <= start <= 23 and 0 <= end <= 24):
            problems.append("quiet_hours 必须是 0-23 的小时数")

    return problems


def assert_valid(rules: RuleSet) -> None:
    problems = validate(rules)
    if problems:
        raise ValueError("规则不合法：" + "；".join(problems))


# ===========================================================================
# 四、多店汇总
# ===========================================================================

def effective_rules(
    template: RuleSet,
    overrides: Mapping[str, RuleSet],
    *,
    defaults: RuleSet = DEFAULTS,
) -> dict[str, RuleSet]:
    return {sid: resolve(defaults, template, override) for sid, override in overrides.items()}


def audit_drift(
    template: RuleSet,
    overrides: Mapping[str, RuleSet],
    *,
    defaults: RuleSet = DEFAULTS,
) -> dict[str, tuple[str, ...]]:
    """
    哪些店偏离了模板、偏离在哪几个字段。

    这不是"错误"，而是"需要知道的事"：一个店偏离了 6 个字段，
    多半是当初特殊照顾过，该复核一下是不是还有必要。
    """
    out: dict[str, tuple[str, ...]] = {}
    for store_id, override in overrides.items():
        drifted = tuple(
            name for name in FIELDS if getattr(override, name, None) is not None
        )
        if drifted:
            out[store_id] = drifted
    return out
