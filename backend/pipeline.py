"""
消息处理管线

把前面几个模块串成一条主流程，顺便把三个成本/质量优化做进去：

    收到消息
      ↓
    ① 幂等去重        idempotency      —— 重复的直接丢，不花任何成本
      ↓
    ② 人工接管检查     handoff          —— 排队中/人工中，AI 闭嘴
      ↓
    ③ 站外引流拦截     off_platform_guard —— 命中就发标准话术，不调用大模型
      ↓
    ④ FAQ 优先匹配     pipeline         —— 命中直接回预设答案，不调用大模型
      ↓
    ⑤ 大模型决策       guardrails       —— 报价过服务端硬校验
      ↓
    ⑥ 转人工判定       handoff          —— 信号汇总，够阈值就开工单
      ↓
    ⑦ 成本核算         pipeline

三个优化点，按性价比排序：

  **优化一 · FAQ 优先命中**：FAQ 命中率每提高 10 个点，大模型调用量就降 10%，
  而且响应从秒级降到毫秒级。这是最划算的优化，没有之一。

  **优化二 · 上下文压缩**：长会话只保留最近 N 轮原文，更早的压成一句事实摘要。
  避免"聊了 30 轮还在把前 30 轮全塞进 prompt"这种烧钱行为。

  **优化三 · 成本核算**：按店铺/按天统计 token 与费用。免费额度是会被烧穿的，
  不记账就不知道什么时候烧穿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Protocol, Sequence, runtime_checkable

from .guardrails import AiDecision, FALLBACK_REPLY, clamp_offer, offer_band
from .handoff import (
    ConvState,
    Decision,
    Ticket,
    ai_should_stay_silent,
    build_signals,
    decide,
    open_ticket,
)
from .idempotency import IdempotencyStore, IncomingMessage, dedup_key
from .off_platform_guard import (
    BuyerRisk,
    GuardResult,
    observe as observe_risk,
    safe_reply,
    scan,
)

# ===========================================================================
# 一、FAQ 匹配（优化一）
# ===========================================================================

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str


@dataclass(frozen=True)
class FaqMatch:
    faq: Faq
    score: float


def tokenize(text: str) -> frozenset[str]:
    """
    中英混排的粗粒度分词：中文切 2-gram，英文数字按词切。

    刻意不上分词器 —— FAQ 问题都很短（"多久发货""支持哪些设备"），
    2-gram 的召回已经足够，而且没有依赖、没有加载开销。
    """
    from .off_platform_guard import normalize_light

    t = normalize_light(text)
    tokens: set[str] = set()
    for m in _CJK_RUN.finditer(t):
        run = m.group()
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    tokens.update(_ASCII_WORD.findall(t))
    return frozenset(tokens)


def _squash(text: str) -> str:
    from .off_platform_guard import normalize_digits, normalize_light, strip_symbols

    return strip_symbols(normalize_digits(normalize_light(text)))


FAQ_THRESHOLD = 0.6


def match_faq(
    text: str,
    faqs: Sequence[Faq],
    threshold: float = FAQ_THRESHOLD,
) -> Optional[FaqMatch]:
    """
    在 FAQ 库里找最匹配的一条。

    两种命中方式：
      1. 问题原文是消息的子串 → 直接 1.0。用户基本是照抄问题在问。
      2. 否则算"问题 token 被消息覆盖的比例"（containment，不是 Jaccard）。

    用 containment 而不是 Jaccard 的原因：买家消息通常比 FAQ 问题长
    （"你好，我想问一下这个大概多久能发货呀"），Jaccard 会被长文本稀释到很低，
    导致永远命中不了。
    """
    if not text or not faqs:
        return None

    squashed = _squash(text)
    msg_tokens = tokenize(text)
    best: Optional[FaqMatch] = None

    for faq in faqs:
        faq_squashed = _squash(faq.question)
        if faq_squashed and faq_squashed in squashed:
            score = 1.0
        else:
            q_tokens = tokenize(faq.question)
            # 问题太短（只有一个 token）时统计上不可靠，跳过
            if len(q_tokens) < 2:
                continue
            score = len(q_tokens & msg_tokens) / len(q_tokens)

        if score >= threshold and (best is None or score > best.score):
            best = FaqMatch(faq, round(score, 4))

    return best


# ===========================================================================
# 二、上下文压缩（优化二）
# ===========================================================================

@dataclass(frozen=True)
class Turn:
    role: str                     # BUYER / AI / SYSTEM
    content: str
    intent: str = ""
    offered_price: Optional[float] = None


DEFAULT_KEEP_RECENT = 6


def digest_turns(turns: Sequence[Turn]) -> str:
    """
    把早期对话压成一句事实摘要。

    刻意用规则而不是让大模型总结：总结本身要花钱、要延迟，而这里需要的
    只是"谈过几轮、砍到多少、什么意图"这几个确定的事实。
    """
    if not turns:
        return ""

    buyer_turns = [t for t in turns if t.role == "BUYER"]
    prices = [t.offered_price for t in turns if t.offered_price is not None]
    intents = sorted({t.intent for t in turns if t.intent})

    parts = [f"此前共 {len(turns)} 轮对话"]
    if buyer_turns:
        parts.append(f"买家发言 {len(buyer_turns)} 次")
    if prices:
        parts.append(f"已报价 {len(prices)} 次，最低 ¥{min(prices):g}")
    if intents:
        parts.append(f"涉及意图 {'/'.join(intents)}")
    return "；".join(parts) + "。"


def compress_context(
    turns: Sequence[Turn],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    max_chars: int = 1800,
) -> tuple[tuple[Turn, ...], str]:
    """
    返回 (保留的近期轮次, 早期轮次摘要)。

    先按轮数截断，再按字符数兜底 —— 单条消息可能是篇小作文，
    只按轮数截断挡不住。
    """
    if not turns:
        return (), ""

    if len(turns) <= keep_recent:
        recent, summary = list(turns), ""
    else:
        older, recent = turns[:-keep_recent], list(turns[-keep_recent:])
        summary = digest_turns(older)

    # 字符数兜底：从最旧的一条开始丢。
    # 注意这一步**不能**因为"轮数没超"就跳过 —— 买家发一篇小作文的时候，
    # 6 条消息也照样能把 prompt 撑爆。
    total = sum(len(t.content) for t in recent)
    while len(recent) > 1 and total > max_chars:
        total -= len(recent[0].content)
        recent.pop(0)

    return tuple(recent), summary


# ===========================================================================
# 三、成本核算（优化三）
# ===========================================================================

# DeepSeek-V3 量级的参考价（元 / 百万 token）。换模型改这里就行。
PRICE_IN_PER_MILLION = 1.0
PRICE_OUT_PER_MILLION = 2.0


def estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    price_in: float = PRICE_IN_PER_MILLION,
    price_out: float = PRICE_OUT_PER_MILLION,
) -> float:
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


@dataclass
class Usage:
    turns: int = 0
    faq_hits: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    @property
    def faq_hit_rate(self) -> float:
        return self.faq_hits / self.turns if self.turns else 0.0

    @property
    def avg_cost_per_turn(self) -> float:
        return self.cost / self.turns if self.turns else 0.0


class UsageTracker:
    """
    按 (日期, 店铺) 记账。

    按店铺分开是为了能定位"哪个店在烧钱"；按天分开是为了对齐免费额度
    （Upstash 是每天 10k 请求，大模型是每月账单）。
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[date, str], Usage] = {}

    def bucket(self, store_id: str, when: Optional[datetime] = None) -> Usage:
        day = (when or datetime.now(timezone.utc)).date()
        return self._buckets.setdefault((day, store_id), Usage())

    def record_turn(self, store_id: str, *, when: Optional[datetime] = None) -> Usage:
        b = self.bucket(store_id, when)
        b.turns += 1
        return b

    def record_faq_hit(self, store_id: str, *, when: Optional[datetime] = None) -> Usage:
        b = self.bucket(store_id, when)
        b.faq_hits += 1
        return b

    def record_llm(
        self,
        store_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        when: Optional[datetime] = None,
    ) -> Usage:
        b = self.bucket(store_id, when)
        b.llm_calls += 1
        b.prompt_tokens += prompt_tokens
        b.completion_tokens += completion_tokens
        b.cost += estimate_cost(prompt_tokens, completion_tokens)
        return b

    def report(self, store_id: str, when: Optional[datetime] = None) -> dict:
        b = self.bucket(store_id, when)
        return self._render(b)

    def aggregate(self, when: Optional[datetime] = None) -> Usage:
        """跨店铺汇总当天数据，供全局看板使用（不传 store_id 时）。"""
        day = (when or datetime.now(timezone.utc)).date()
        acc = Usage()
        for (d, _sid), b in self._buckets.items():
            if d == day:
                acc.turns += b.turns
                acc.llm_calls += b.llm_calls
                acc.faq_hits += b.faq_hits
                acc.prompt_tokens += b.prompt_tokens
                acc.completion_tokens += b.completion_tokens
                acc.cost += b.cost
        return acc

    def aggregate_report(self, when: Optional[datetime] = None) -> dict:
        return self._render(self.aggregate(when))

    @staticmethod
    def _render(b: "Usage") -> dict:
        return {
            "turns": b.turns,
            "llm_calls": b.llm_calls,
            "faq_hits": b.faq_hits,
            "faq_hit_rate": round(b.faq_hit_rate, 4),
            "cost": round(b.cost, 6),
            "avg_cost_per_turn": round(b.avg_cost_per_turn, 6),
            "saved_calls": b.faq_hits,   # FAQ 命中省下的大模型调用次数
        }


# ===========================================================================
# 四、大模型接口
# ===========================================================================

@dataclass(frozen=True)
class ProductContext:
    title: str
    listed_price: float
    min_price: float
    ladder: tuple[float, ...] = ()
    shipping_policy: str = ""


@dataclass(frozen=True)
class LlmOutcome:
    decision: AiDecision
    prompt_tokens: int = 0
    completion_tokens: int = 0
    confidence: float = 1.0


@runtime_checkable
class LlmClient(Protocol):
    """runtime_checkable 是为了能在装配时断言实现确实满足协议。"""

    async def decide(
        self,
        *,
        system: str,
        context: Sequence[Turn],
        message: str,
        product: Optional[ProductContext] = None,
        round_no: int = 1,
    ) -> LlmOutcome: ...


def build_system_prompt(product: Optional[ProductContext], round_no: int) -> str:
    """
    拼系统提示词。注意这里**只告诉模型本轮的允许区间**，不做"请不要低于底价"
    这种软约束 —— 软约束靠不住，硬校验在 clamp_offer 里。
    """
    lines = [
        "你是闲鱼店铺的客服，用简短口语化的中文回复买家。",
        "禁止提及任何站外联系方式（微信/QQ/电话/链接）。",
        "输出必须是 JSON：{reply, offered_price, intent}。",
    ]
    if product is not None:
        lines.append(f"当前商品：{product.title}")
        lines.append(f"挂牌价 ¥{product.listed_price:g}，包邮政策：{product.shipping_policy or '见商品页'}")
        band = offer_band(
            listed_price=product.listed_price,
            min_price=product.min_price,
            ladder=list(product.ladder),
            round_no=round_no,
        )
        lines.append(
            f"这是第 {round_no} 轮议价，本轮你只能报 ¥{band.low:g} 到 ¥{band.high:g} 之间的价格；"
            f"若不涉及议价则 offered_price 传 null。"
        )
    return "\n".join(lines)


# ===========================================================================
# 五、主流程
# ===========================================================================

class Action:
    DROP = "DROP_DUPLICATE"     # 重复消息，直接丢弃
    SILENT = "SILENT"           # 人工接管中，AI 不发言
    DEFLECT = "DEFLECT"         # 站外引流拦截，发标准话术
    FAQ = "FAQ_HIT"             # FAQ 命中，不调用大模型
    AI = "AI_REPLY"             # 大模型正常回复
    HANDOFF = "HANDOFF"         # 转人工


HANDOFF_HOLD_REPLY = "好的，正在为您转接人工客服，请稍等片刻～"


@dataclass
class HandleResult:
    action: str
    reply: Optional[str] = None
    draft_reply: Optional[str] = None      # 转人工时留给人工参考的草稿
    intent: str = "UNKNOWN"
    offered_price: Optional[float] = None
    guard: Optional[GuardResult] = None
    handoff: Optional[Decision] = None
    ticket: Optional[Ticket] = None
    faq_score: float = 0.0
    cost: float = 0.0
    note: str = ""


async def handle_turn(
    turn: IncomingMessage,
    *,
    store_id: str,
    conv_state: str = ConvState.AI,
    faqs: Sequence[Faq] = (),
    product: Optional[ProductContext] = None,
    context: Sequence[Turn] = (),
    llm: Optional[LlmClient] = None,
    dedup_store: Optional[IdempotencyStore] = None,
    usage: Optional[UsageTracker] = None,
    risk: Optional[BuyerRisk] = None,
    round_no: int = 1,
    unresolved_turns: int = 0,
    now: Optional[datetime] = None,
) -> HandleResult:
    moment = now or datetime.now(timezone.utc)

    # ---- ① 幂等去重：重复消息在花钱之前就被挡住 ----
    if dedup_store is not None:
        key, ttl = dedup_key(
            store_id, msg_id=turn.msg_id, buyer_id=turn.buyer_id,
            content=turn.content, sent_at=turn.sent_at,
        )
        if not await dedup_store.claim(key, ttl):
            return HandleResult(Action.DROP, note="重复消息，已丢弃")

    # ---- ② 人工接管中，AI 必须闭嘴 ----
    if ai_should_stay_silent(conv_state):
        return HandleResult(Action.SILENT, note="会话已由人工接管，AI 不发言")

    # ---- ③ 站外引流拦截 ----
    guard = scan(turn.content)
    if risk is not None:
        observe_risk(risk, guard, moment)

    action: str
    reply: Optional[str]
    draft: Optional[str] = None
    intent = "UNKNOWN"
    offered_price: Optional[float] = None
    confidence: Optional[float] = None
    faq_score = 0.0
    cost = 0.0
    note = ""

    if guard.blocked:
        action, reply = Action.DEFLECT, safe_reply(guard)
        note = "命中站外引流规则：" + "、".join(guard.rules)
    else:
        # ---- ④ FAQ 优先匹配：命中就不调用大模型 ----
        hit = match_faq(turn.content, faqs)
        if hit is not None:
            action, reply, faq_score = Action.FAQ, hit.faq.answer, hit.score
            intent = "ENQUIRY"
            note = f"FAQ 命中（相似度 {hit.score:.2f}），未调用大模型"
            if usage is not None:
                usage.record_faq_hit(store_id, when=moment)
        # ---- ⑤ 大模型决策 + 服务端硬校验 ----
        elif llm is not None:
            kept, _summary = compress_context(context)
            outcome = await llm.decide(
                system=build_system_prompt(product, round_no),
                context=kept,
                message=turn.content,
                product=product,
                round_no=round_no,
            )
            cost = estimate_cost(outcome.prompt_tokens, outcome.completion_tokens)
            confidence = outcome.confidence
            if usage is not None:
                usage.record_llm(
                    store_id,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    when=moment,
                )
            intent = outcome.decision.intent
            reply = outcome.decision.reply
            action = Action.AI

            # 报价必须过服务端校验；越界就丢掉报价，不冒"文本与价格不一致"的险
            #
            # 注意这里**不能**写成 `if product is not None and ...`：拿不到商品上下文时
            # 若直接跳过校验，AI 的任意报价就会被原样发给买家 —— 平台推送的 item_id
            # 与本地登记不一致、商品被删、或消息不在商品上下文里，都会走到这条路径，
            # 结果是底价防护被静默绕过。所以只要模型给出了报价，就必须有东西兜住它。
            if outcome.decision.intent == "BARGAIN" or outcome.decision.offered_price is not None:
                if product is None:
                    # 无商品上下文 → 无从判断报价是否合法，一律不报价
                    reply = FALLBACK_REPLY
                    note = "缺少商品上下文，无法校验报价，已丢弃报价并改用兜底话术"
                else:
                    safe_price = clamp_offer(
                        listed_price=product.listed_price,
                        min_price=product.min_price,
                        ladder=list(product.ladder),
                        round_no=round_no,
                        ai_offered=outcome.decision.offered_price,
                    )
                    if safe_price is None:
                        reply = FALLBACK_REPLY
                        note = "AI 报价越界，已丢弃报价并改用兜底话术"
                    else:
                        offered_price = safe_price
        else:
            action, reply = Action.AI, None
            note = "未配置大模型客户端"

    # ---- ⑥ 转人工判定 ----
    signals = build_signals(
        text=turn.content,
        off_platform_blocked=guard.blocked,
        confidence=confidence,
        unresolved_turns=unresolved_turns,
        bargain_rounds=round_no if intent == "BARGAIN" else 0,
    )
    decision = decide(signals)
    ticket: Optional[Ticket] = None

    if decision.should_handoff:
        ticket = open_ticket(decision, store_id=store_id, buyer_id=turn.buyer_id, now=moment)
        if action == Action.DEFLECT:
            # 拦截优先：话术照发，同时开工单。止损比转接更紧急。
            note += "；同时已转人工"
        else:
            draft = reply                      # 先留住草稿，再覆盖 reply
            action = Action.HANDOFF
            reply = HANDOFF_HOLD_REPLY
            note = "已转人工：" + "、".join(decision.reasons)

    if usage is not None:
        usage.record_turn(store_id, when=moment)

    return HandleResult(
        action=action,
        reply=reply,
        draft_reply=draft,
        intent=intent,
        offered_price=offered_price,
        guard=guard,
        handoff=decision if decision.should_handoff else None,
        ticket=ticket,
        faq_score=faq_score,
        cost=cost,
        note=note,
    )
