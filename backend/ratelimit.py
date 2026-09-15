"""
本地令牌桶限流

为什么要有
----------
不是为了"防攻击"，而是为了**别把自己的账号搞出问题**。

对平台接口的调用如果毫无节制 —— 比如卡密池空了触发 200 个补偿任务，每个都去
调一次平台接口 —— 结果就是账号被限流甚至被风控。自己把自己打挂，很常见。

所以每个店铺、每类接口都要有一个本地令牌桶兜住。这是"礼貌"层面的自我约束，
和绕过平台风控没有任何关系 —— 恰恰相反，它是**避免触发**平台风控的。

为什么用令牌桶而不是固定窗口
----------------------------
固定窗口（每分钟最多 N 次）会在窗口边界上放行 2N 次：第 59 秒发 N 次，
第 61 秒又发 N 次。令牌桶用"匀速补充"天然没有这个问题，而且允许小幅突发
（桶容量），更贴合真实调用模式。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class RateLimitExceeded(Exception):
    def __init__(self, key: str, retry_after: float) -> None:
        self.key = key
        self.retry_after = retry_after
        super().__init__(f"{key} 触发限流，{retry_after:.2f} 秒后可重试")


@dataclass
class TokenBucket:
    """
    容量 capacity，每秒补 refill_per_second 个令牌。

    capacity 决定能突发多少，refill 决定长期平均速率。
    比如 capacity=10, refill=1 → 平时每秒 1 次，攒够了可以瞬间发 10 次。
    """

    capacity: float
    refill_per_second: float
    tokens: float = -1.0
    updated_at: float = -1.0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity 必须为正数")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second 必须为正数")
        if self.tokens < 0:
            self.tokens = float(self.capacity)   # 初始满桶

    def _refill(self, now: float) -> None:
        if self.updated_at < 0:
            self.updated_at = now
            return
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

    def try_acquire(self, n: float = 1.0, now: Optional[float] = None) -> bool:
        moment = time.monotonic() if now is None else now
        self._refill(moment)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def time_until(self, n: float = 1.0, now: Optional[float] = None) -> float:
        """还需要等多少秒才能拿到 n 个令牌。"""
        moment = time.monotonic() if now is None else now
        self._refill(moment)
        if self.tokens >= n:
            return 0.0
        deficit = n - self.tokens
        return round(deficit / self.refill_per_second, 4)

    @property
    def available(self) -> float:
        return round(self.tokens, 4)


class RateLimiter:
    """按 key 分桶。key 通常是 "店铺ID:接口名"，这样一个店打满不会影响别的店。"""

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._capacity = capacity
        self._refill = refill_per_second
        self._buckets: dict[str, TokenBucket] = {}
        self._clock = clock or time.monotonic

    def bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity=self._capacity, refill_per_second=self._refill)
            self._buckets[key] = bucket
        return bucket

    def try_acquire(self, key: str, n: float = 1.0, now: Optional[float] = None) -> bool:
        return self.bucket(key).try_acquire(n, self._now(now))

    def time_until(self, key: str, n: float = 1.0, now: Optional[float] = None) -> float:
        return self.bucket(key).time_until(n, self._now(now))

    def acquire(self, key: str, n: float = 1.0, now: Optional[float] = None) -> None:
        """拿不到就抛异常，让调用方去排队而不是硬闯。"""
        moment = self._now(now)
        if not self.bucket(key).try_acquire(n, moment):
            raise RateLimitExceeded(key, self.bucket(key).time_until(n, moment))

    async def acquire_or_wait(self, key: str, n: float = 1.0, *, max_wait: float = 30.0) -> None:
        """
        拿不到就等。等太久（超过 max_wait）宁可失败 ——
        无限等会把补偿队列堵死，比直接失败更糟。
        """
        wait = self.time_until(key, n)
        if wait > max_wait:
            raise RateLimitExceeded(key, wait)
        if wait > 0:
            await asyncio.sleep(wait)
        self.acquire(key, n)

    def snapshot(self) -> dict[str, float]:
        return {key: bucket.available for key, bucket in self._buckets.items()}

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)

    def _now(self, override: Optional[float]) -> float:
        return self._clock() if override is None else override


# ===========================================================================
# 预设
# ===========================================================================

# (容量, 每秒补充)
PRESETS: dict[str, tuple[float, float]] = {
    # 平台接口：宁可慢也别触发风控。平时 1 次/秒，最多攒 5 次突发
    "platform_api": (5, 1.0),
    # 发消息：节奏要像人，2 秒一条
    "send_message": (3, 0.5),
    # 大模型：并发别太高，容易被限流
    "llm": (4, 1.0),
    # 通知推送：可以快一点
    "notify": (20, 5.0),
    # 商品抓取：更要克制
    "scrape": (2, 0.2),
}


def build_limiter(preset: str, **kwargs) -> RateLimiter:
    if preset not in PRESETS:
        raise KeyError(f"未知预设 {preset}，可用：{', '.join(sorted(PRESETS))}")
    capacity, refill = PRESETS[preset]
    return RateLimiter(capacity, refill, **kwargs)


@dataclass
class LimiterRegistry:
    """一个进程里按用途持有多个限流器，省得各处各建一套。"""

    clock: Optional[Callable[[], float]] = None
    _limiters: dict[str, RateLimiter] = field(default_factory=dict)

    def get(self, preset: str) -> RateLimiter:
        limiter = self._limiters.get(preset)
        if limiter is None:
            limiter = build_limiter(preset, clock=self.clock)
            self._limiters[preset] = limiter
        return limiter

    def try_acquire(self, preset: str, key: str, n: float = 1.0) -> bool:
        return self.get(preset).try_acquire(key, n)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {name: limiter.snapshot() for name, limiter in self._limiters.items()}
