"""
消息幂等去重

为什么必须有这一层
------------------
WebSocket 长连接在断线重连、心跳超时、服务端补推的情况下，**一定会**把同一条
买家消息投递多次。如果不去重，后果是：

  - 同一条问询被回复两遍（买家观感差）
  - 同一笔砍价被重复计算，议价阶梯多让一轮（直接亏钱）
  - 订单支付回调重复触发，重复发货（亏卡密 + 亏信誉）

设计：两道防线
--------------
第一道（快）：Redis SET NX EX。毫秒级，扛住 99% 的重复投递。
第二道（稳）：message_log 表上的 (store_id, platform_msg_id) 唯一约束。
             Redis 挂了、被清空、或者 TTL 过期后重投，都由数据库兜住。

两条防线都不依赖调用方的自觉，这是关键。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol, Sequence

# 有平台消息 ID 时，去重窗口可以放长（重投可能跨小时）
DEFAULT_TTL = 24 * 3600
# 退化成内容指纹时，窗口必须放短：否则买家隔一分钟又问了同样一句话会被误杀
FINGERPRINT_TTL = 120


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def fingerprint(*parts: Any) -> str:
    """
    把若干字段揉成一个稳定的指纹。

    用 \\x1f 做分隔符，避免 ("ab", "c") 和 ("a", "bc") 撞成同一个值。
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def dedup_key(
    store_id: str,
    *,
    msg_id: Optional[str] = None,
    buyer_id: str = "",
    content: str = "",
    sent_at: Any = None,
) -> tuple[str, int]:
    """
    生成去重键，并返回该键应该使用的 TTL。

    优先用平台消息 ID；拿不到时才退化成内容指纹。
    返回值形如 ("dedup:8f3a...", 86400)。
    """
    if msg_id:
        raw = f"msg|{store_id}|{msg_id}"
        ttl = DEFAULT_TTL
    else:
        # 时间只取到秒，容忍重投时的毫秒级抖动
        bucket = _to_epoch_second(sent_at)
        raw = f"fp|{store_id}|{buyer_id}|{fingerprint(content)}|{bucket}"
        ttl = FINGERPRINT_TTL
    return "dedup:" + hashlib.sha1(raw.encode("utf-8")).hexdigest(), ttl


def _to_epoch_second(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if isinstance(value, (int, float)):
        # 兼容毫秒时间戳
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)
    return int(time.time())


# ---------------------------------------------------------------------------
# 存储后端
# ---------------------------------------------------------------------------

class IdempotencyStore(Protocol):
    async def claim(self, key: str, ttl: int) -> bool:
        """首次见到该 key 返回 True；已见过返回 False。"""
        ...


class MemoryIdempotencyStore:
    """进程内实现。用于本地开发和单元测试。生产请用 Redis。"""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    async def claim(self, key: str, ttl: int) -> bool:
        now = time.monotonic()
        expires = self._seen.get(key)
        if expires is not None and expires > now:
            return False
        self._seen[key] = now + ttl
        return True

    def sweep(self) -> int:
        """清掉过期条目，避免内存无限增长。建议由定时任务调用。"""
        now = time.monotonic()
        dead = [k for k, exp in self._seen.items() if exp <= now]
        for k in dead:
            self._seen.pop(k, None)
        return len(dead)

    async def claim_many(self, items: Iterable[tuple[str, int]]) -> list[bool]:
        # 进程内实现没有网络往返，逐条就够了
        return [await self.claim(key, ttl) for key, ttl in items]


class RedisIdempotencyStore:
    """
    Upstash / 自建 Redis 实现。

    SET key 1 NX EX ttl 是原子的：并发的两个 worker 里只有一个能拿到 True。
    这正是我们需要的语义，不要用 GET + SET 两步写法。
    """

    def __init__(self, redis_client: Any, prefix: str = "xy") -> None:
        self._redis = redis_client
        self._prefix = prefix

    async def claim(self, key: str, ttl: int) -> bool:
        ok = await self._redis.set(f"{self._prefix}:{key}", "1", nx=True, ex=ttl)
        return bool(ok)

    async def claim_many(self, items: Iterable[tuple[str, int]]) -> list[bool]:
        """
        一次 pipeline 提交多条 SET NX。

        WebSocket 补推时经常一次来十几条消息，逐条 await 就是十几次网络往返。
        用 pipeline 只跑一次 RTT —— 这是最省事的一次优化。
        """
        pairs = list(items)
        if not pairs:
            return []
        pipe = self._redis.pipeline()
        for key, ttl in pairs:
            pipe.set(f"{self._prefix}:{key}", "1", nx=True, ex=ttl)
        results = await pipe.execute()
        return [bool(r) for r in results]


class FailOpenStore:
    """
    Redis 不可用时的降级策略。

    宁可放行（可能重复）也不能卡死（买家收不到回复）。
    重复的风险由数据库唯一约束兜底，这里只需保证服务不中断。
    日志里会记 warn，方便告警。
    """

    def __init__(self, store: IdempotencyStore, on_error=None) -> None:
        self._store = store
        self._on_error = on_error

    async def claim(self, key: str, ttl: int) -> bool:
        try:
            return await self._store.claim(key, ttl)
        except Exception as exc:  # noqa: BLE001 - 降级必须吞掉所有异常
            if self._on_error:
                self._on_error(exc, key)
            return True

    async def claim_many(self, items: Iterable[tuple[str, int]]) -> list[bool]:
        pairs = list(items)
        try:
            inner = getattr(self._store, "claim_many", None)
            if inner is not None:
                return await inner(pairs)
            return [await self.claim(k, t) for k, t in pairs]
        except Exception as exc:  # noqa: BLE001
            if self._on_error:
                self._on_error(exc, f"batch:{len(pairs)}")
            return [True] * len(pairs)   # 整体降级：全部放行


# ---------------------------------------------------------------------------
# 消息过滤
# ---------------------------------------------------------------------------

@dataclass
class IncomingMessage:
    buyer_id: str
    content: str
    msg_id: Optional[str] = None
    sent_at: Any = None
    raw: dict = field(default_factory=dict)


@dataclass
class DedupOutcome:
    fresh: list[IncomingMessage] = field(default_factory=list)
    duplicates: list[IncomingMessage] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.fresh) + len(self.duplicates)

    @property
    def duplicate_rate(self) -> float:
        return len(self.duplicates) / self.total if self.total else 0.0


async def filter_new(
    store_id: str,
    messages: Iterable[IncomingMessage],
    store: IdempotencyStore,
) -> DedupOutcome:
    """
    过滤掉已经处理过的消息，保持原有顺序。

    注意：这里会**先占坑再返回**。也就是说如果调用方拿到 fresh 之后处理失败，
    该消息在本 TTL 内不会被重试。所以调用方必须保证：
        claim → 处理 → 落库
    三步都在同一个可重试单元里，且落库失败时要主动释放（见 release）。
    """
    outcome = DedupOutcome()
    for msg in messages:
        key, ttl = dedup_key(
            store_id,
            msg_id=msg.msg_id,
            buyer_id=msg.buyer_id,
            content=msg.content,
            sent_at=msg.sent_at,
        )
        if await store.claim(key, ttl):
            outcome.fresh.append(msg)
        else:
            outcome.duplicates.append(msg)
    return outcome


async def filter_new_batched(
    store_id: str,
    messages: Sequence[IncomingMessage],
    store: Any,
) -> DedupOutcome:
    """
    批量版 filter_new：一次提交全部去重键（Redis 上只跑一次 RTT）。

    **保持输入顺序**。这一点不能妥协：同一买家的消息一旦乱序，
    议价轮次就会算错，而轮次直接决定让步幅度 —— 那是真金白银。
    """
    items = [
        dedup_key(
            store_id, msg_id=m.msg_id, buyer_id=m.buyer_id,
            content=m.content, sent_at=m.sent_at,
        )
        for m in messages
    ]
    if not items:
        return DedupOutcome()

    results = await store.claim_many(items)
    outcome = DedupOutcome()
    for msg, fresh in zip(messages, results):
        (outcome.fresh if fresh else outcome.duplicates).append(msg)
    return outcome


async def release(
    store_id: str,
    msg: IncomingMessage,
    store: Any,
) -> None:
    """
    处理失败时归还坑位，让这条消息能被重试。

    只有 Redis 实现支持删除；内存实现同样支持 delete 的话需要扩展接口，
    这里按 duck typing 处理。
    """
    key, _ = dedup_key(
        store_id, msg_id=msg.msg_id, buyer_id=msg.buyer_id,
        content=msg.content, sent_at=msg.sent_at,
    )
    inner = getattr(store, "_store", store)
    deleter = getattr(inner, "delete", None)
    if deleter is None:
        return
    result = deleter(key)
    if hasattr(result, "__await__"):
        await result


# ---------------------------------------------------------------------------
# 数据库兜底
# ---------------------------------------------------------------------------

async def persist_once(db: Any, ctx: Any, store_id: str, msg: IncomingMessage, model: Any) -> bool:
    """
    把消息写入 message_log，靠唯一约束挡住重复。

    返回 True 表示确实写入了（首次），False 表示撞了唯一约束（重复）。
    这是第二道防线：即使 Redis 整个挂掉、或者 TTL 已过，重复消息也进不来。

    调用方负责 commit / rollback。
    """
    from sqlalchemy.exc import IntegrityError

    row = model(
        tenant_id=ctx.tenant_id,
        store_id=store_id,
        platform_msg_id=msg.msg_id,
        fingerprint=fingerprint(msg.buyer_id, msg.content),
        buyer_id=msg.buyer_id,
        role="BUYER",
        content=msg.content,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return False
    return True
