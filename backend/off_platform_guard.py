"""
站外引流拦截

为什么这是"不做会封店"的项
--------------------------
平台最忌讳的不是你服务差，而是**交易脱平台**。一旦买家在会话里被引导到微信/
支付宝成交，平台既拿不到佣金，又失去了纠纷仲裁权，所以对"引导站外交易"的判定
非常严厉：轻则降权限流，重则直接封店。

而对卖家来说，这不是"买家说了什么"的问题——**是你有没有及时拦下来**。
AI 客服如果老老实实回了"好的，我微信是 xxx"，那封店的责任在你。

难点：买家不会乖乖写"微信"两个字
--------------------------------
真实会话里的写法包括但不限于：
    微 信 / 薇信 / 威信 / V信 / vx / wx / ｖｘ / weixin / 加v
    13800138000 / 138-0013-8000 / 138 0013 8000 / 一三八零零一三八零零零
    1 3 8 0 0 1 3 8 0 0 0 / １３８００１３８０００
    微信\u200b号（夹零宽字符）/ 薇❤ / 加我V

所以检测必须先做**归一化对抗**：去零宽字符 → NFKC 兼容归一（全角转半角）→
中文数字转阿拉伯 → 去所有分隔符。归一化之后再匹配，绕过成本就高得多。

三层处置
--------
    ALLOW  放行
    WARN   记录 + 让 AI 委婉引导回平台（意图明显但没给具体联系方式）
    BLOCK  拒绝发送原回复，改用标准话术 + 记一次违规
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

# ===========================================================================
# 一、归一化对抗
# ===========================================================================

# 零宽 / 不可见字符：买家最常用的绕过手段，插在关键词中间破坏匹配
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u2061\u2062\u2063"), None
)

_CN_DIGITS = {
    "零": "0", "〇": "0", "洞": "0",
    "一": "1", "壹": "1", "幺": "1",
    "二": "2", "两": "2", "贰": "2",
    "三": "3", "叁": "3",
    "四": "4", "肆": "4",
    "五": "5", "伍": "5",
    "六": "6", "陆": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}


def normalize_light(text: str) -> str:
    """
    轻度归一：去不可见字符 + NFKC + 大小写折叠。

    NFKC 会顺手把全角字符（ｖｘ、１３８）折成半角，省掉一大段映射表。
    保留空格和标点，因为链接检测需要它们。
    """
    return unicodedata.normalize("NFKC", text.translate(_INVISIBLE)).casefold()


def normalize_digits(text: str) -> str:
    """把中文数字逐字转成阿拉伯数字。只用于扫描，不用于展示。"""
    return "".join(_CN_DIGITS.get(ch, ch) for ch in text)


def strip_symbols(text: str) -> str:
    """
    重度归一：只保留字母、数字、汉字，其余全部丢弃。

    这一步让 "1-3-8 0013_8000" 变成 "13800138000"，
    让 "v 信" 变成 "v信"。
    """
    return "".join(ch for ch in text if ch.isalnum())


def normalize_for_scan(text: str) -> tuple[str, str, str]:
    """返回 (轻度, 数字归一, 重度) 三个版本，供不同规则使用。"""
    light = normalize_light(text)
    digits = normalize_digits(light)
    return light, digits, strip_symbols(digits)


# ===========================================================================
# 二、规则
# ===========================================================================

class Action:
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"


class RuleName:
    URL = "URL"
    PHONE = "PHONE"
    CONTACT = "CONTACT"
    CONTACT_WITH_ID = "CONTACT_WITH_ID"
    PAYMENT = "PAYMENT"
    PAYMENT_WITH_ACCOUNT = "PAYMENT_WITH_ACCOUNT"
    OFFLINE = "OFFLINE"
    IM_WITH_ID = "IM_WITH_ID"
    COMBINED = "COMBINED"


# 即时通讯关键词。放在重度文本上匹配，所以不需要考虑空格变体。
CONTACT_KEYWORDS = (
    "微信", "威信", "薇信", "唯信", "v信", "wx", "vx", "weixin", "wechat", "wecaht",
)
IM_KEYWORDS = ("qq", "扣扣", "企鹅号")
PAYMENT_KEYWORDS = ("支付宝", "银行卡", "转账", "汇款", "打款", "扫码付", "红包转账")
OFFLINE_KEYWORDS = (
    "加我", "私聊", "私下", "面交", "线下", "站外", "留个电话", "打电话",
    "手机号", "联系方式", "加个好友", "走线下",
)

# 含关键词但属于正常商品名的短语。不排除的话，"QQ音乐会员有吗"会被误判成引流，
# 而这类消息在卖卡券的店里占比很高。
BENIGN_PHRASES = ("qq音乐", "qq会员", "qq绿钻", "微信读书")


def _in_benign_phrase(heavy: str, idx: int) -> bool:
    return any(heavy.startswith(p, idx) for p in BENIGN_PHRASES)


_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|[\w-]+\.(?:com|cn|net|org|top|xyz|vip|cc|me|io|shop|store)\b"
)
# 允许分隔符插在数字之间，避免把长订单号里的片段误判成手机号
_DIGIT_RUN_RE = re.compile(r"\d(?:[\s\-._·・]{0,3}\d){5,}")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")


@dataclass(frozen=True)
class GuardHit:
    rule: str
    action: str
    matched: str
    detail: str


@dataclass(frozen=True)
class GuardResult:
    action: str
    hits: tuple[GuardHit, ...] = ()
    normalized: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == Action.BLOCK

    @property
    def warned(self) -> bool:
        return self.action == Action.WARN

    @property
    def clean(self) -> bool:
        return self.action == Action.ALLOW

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(h.rule for h in self.hits)


# ===========================================================================
# 三、扫描
# ===========================================================================

def _find_urls(light: str) -> list[GuardHit]:
    return [
        GuardHit(RuleName.URL, Action.BLOCK, m.group(), "消息包含外部链接")
        for m in _URL_RE.finditer(light)
    ]


def _find_phones(digits: str) -> list[GuardHit]:
    """
    先切出完整数字串再判断长度。

    直接在整串上跑正则会把 15 位订单号的前 11 位当成手机号 —— 这是个很容易
    踩的假阳性，所以必须先按分隔符切段、再逐段校验。
    """
    hits: list[GuardHit] = []
    for raw in _DIGIT_RUN_RE.finditer(digits):
        run = re.sub(r"\D", "", raw.group())
        if len(run) == 11 and _PHONE_RE.fullmatch(run):
            hits.append(GuardHit(RuleName.PHONE, Action.BLOCK, run, "消息包含疑似手机号"))
    return hits


def _keyword_positions(heavy: str, keywords: Sequence[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for kw in keywords:
        start = 0
        while (idx := heavy.find(kw, start)) != -1:
            out.append((kw, idx))
            start = idx + 1
    return out


def _followed_by_identifier(heavy: str, idx: int, kw_len: int, window: int = 14) -> bool:
    """关键词后面是否紧跟着账号标识（字母/数字）。"""
    tail = heavy[idx + kw_len: idx + kw_len + window]
    return any(ch.isascii() and ch.isalnum() for ch in tail)


def _scan_contacts(heavy: str) -> list[GuardHit]:
    hits: list[GuardHit] = []
    for kw, idx in _keyword_positions(heavy, CONTACT_KEYWORDS):
        if _in_benign_phrase(heavy, idx):
            continue
        if _followed_by_identifier(heavy, idx, len(kw)):
            hits.append(GuardHit(RuleName.CONTACT_WITH_ID, Action.BLOCK, kw,
                                 f"'{kw}' 后紧跟账号标识"))
        else:
            hits.append(GuardHit(RuleName.CONTACT, Action.WARN, kw,
                                 f"出现即时通讯关键词 '{kw}'"))
    return hits


def _scan_im(heavy: str) -> list[GuardHit]:
    hits: list[GuardHit] = []
    for kw, idx in _keyword_positions(heavy, IM_KEYWORDS):
        if _in_benign_phrase(heavy, idx):
            continue
        tail = heavy[idx + len(kw): idx + len(kw) + 14]
        run = re.match(r"\d{5,11}", tail)
        if run:
            hits.append(GuardHit(RuleName.IM_WITH_ID, Action.BLOCK, kw + run.group(),
                                 f"'{kw}' 后紧跟疑似账号"))
        else:
            hits.append(GuardHit(RuleName.CONTACT, Action.WARN, kw,
                                 f"出现即时通讯关键词 '{kw}'"))
    return hits


def _scan_payment(heavy: str) -> list[GuardHit]:
    hits: list[GuardHit] = []
    for kw, idx in _keyword_positions(heavy, PAYMENT_KEYWORDS):
        tail = heavy[idx + len(kw): idx + len(kw) + 24]
        if re.search(r"\d{8,}", tail):
            hits.append(GuardHit(RuleName.PAYMENT_WITH_ACCOUNT, Action.BLOCK, kw,
                                 f"'{kw}' 后紧跟长数字账号"))
        else:
            hits.append(GuardHit(RuleName.PAYMENT, Action.WARN, kw,
                                 f"出现站外支付关键词 '{kw}'"))
    return hits


def _scan_offline(heavy: str) -> list[GuardHit]:
    return [
        GuardHit(RuleName.OFFLINE, Action.WARN, kw, f"出现站外交易倾向词 '{kw}'")
        for kw, _ in _keyword_positions(heavy, OFFLINE_KEYWORDS)
    ]


# 多个弱信号叠加 → 升级为拦截。"加我微信"就是典型：单独看都是 WARN，
# 合起来意图已经非常明确。
_COMBINE_THRESHOLD = 2


def scan(text: str) -> GuardResult:
    """扫描一条买家消息，给出处置动作。"""
    if not text or not text.strip():
        return GuardResult(Action.ALLOW, (), "")

    light, digits, heavy = normalize_for_scan(text)

    hits: list[GuardHit] = []
    hits += _find_urls(light)
    hits += _find_phones(digits)
    hits += _scan_contacts(heavy)
    hits += _scan_im(heavy)
    hits += _scan_payment(heavy)
    hits += _scan_offline(heavy)

    # 去重：同一规则同一命中内容只保留一条
    seen: set[tuple[str, str]] = set()
    unique: list[GuardHit] = []
    for h in hits:
        key = (h.rule, h.matched)
        if key not in seen:
            seen.add(key)
            unique.append(h)

    if any(h.action == Action.BLOCK for h in unique):
        action = Action.BLOCK
    elif len([h for h in unique if h.action == Action.WARN]) >= _COMBINE_THRESHOLD:
        action = Action.BLOCK
        unique.append(GuardHit(
            RuleName.COMBINED, Action.BLOCK, "+".join(h.matched for h in unique),
            f"{len(unique)} 个弱信号叠加，判定为明确引流意图",
        ))
    elif unique:
        action = Action.WARN
    else:
        action = Action.ALLOW

    return GuardResult(action, tuple(unique), heavy)


# ===========================================================================
# 四、标准话术
# ===========================================================================

DEFLECT_BLOCK = (
    "亲，平台规定不能引导站外沟通和交易哦～咱们所有沟通和付款都在闲鱼内完成，"
    "这样您的资金和售后才有保障。您有什么需求直接跟我说就行。"
)
DEFLECT_WARN = (
    "亲，建议咱们就在闲鱼内沟通哈，站外交易是不受平台保护的。"
)


def safe_reply(result: GuardResult) -> Optional[str]:
    """
    返回应该发送的替代话术；放行时返回 None（表示可以正常走 AI 回复）。

    注意这里**不解释**为什么拦截、也不复述命中的内容 —— 复述等于把联系方式
    又打了一遍，反而帮买家记住了。
    """
    if result.blocked:
        return DEFLECT_BLOCK
    if result.warned:
        return DEFLECT_WARN
    return None


# ===========================================================================
# 五、买家风险累计
# ===========================================================================

class RiskLevel:
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    HIGH = "HIGH"


STRIKE_DECAY = timedelta(days=30)
HIGH_RISK_STRIKES = 3


@dataclass
class BuyerRisk:
    buyer_id: str
    strikes: int = 0
    last_hit_at: Optional[datetime] = None
    level: str = RiskLevel.NORMAL


def risk_level(strikes: int) -> str:
    if strikes >= HIGH_RISK_STRIKES:
        return RiskLevel.HIGH
    if strikes >= 1:
        return RiskLevel.WATCH
    return RiskLevel.NORMAL


def effective_strikes(risk: BuyerRisk, now: datetime, decay: timedelta = STRIKE_DECAY) -> int:
    """
    超过衰减窗口没有新的违规，历史计数清零。

    不衰减的话，一个买家半年前问过一次微信就会被永久标成高风险，
    后续所有订单都被特殊对待 —— 那是误伤，不是风控。
    """
    if risk.last_hit_at is None:
        return 0
    if now - risk.last_hit_at > decay:
        return 0
    return risk.strikes


def record_strike(risk: BuyerRisk, now: Optional[datetime] = None) -> BuyerRisk:
    """记一次违规并刷新等级。返回同一个对象（原地更新，方便直接落库）。"""
    moment = now or datetime.now(timezone.utc)
    if effective_strikes(risk, moment) == 0:
        risk.strikes = 0
    risk.strikes += 1
    risk.last_hit_at = moment
    risk.level = risk_level(risk.strikes)
    return risk


def observe(risk: BuyerRisk, result: GuardResult, now: Optional[datetime] = None) -> BuyerRisk:
    """把一次扫描结果应用到买家画像上。只有 BLOCK 才计违规。"""
    if result.blocked:
        return record_strike(risk, now)
    return risk


def should_flag_orders(risk: BuyerRisk) -> bool:
    """是否需要在订单层打标（该买家的订单要重点盯）。"""
    return risk.level == RiskLevel.HIGH
