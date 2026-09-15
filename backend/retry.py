"""
补偿任务与死信队列

为什么必须有
------------
自动发货不是"调用一次就一定成功"的。平台接口会超时、会 502、会限流，而每一次
失败都对应一笔**已经收了钱但没发货**的订单。如果失败只记一个 FAILED 就完事，
这笔订单就永久丢了 —— 买家等不到货，平台判你违约。

所以失败必须能重试。但"无限重试"和"不重试"一样糟：

  - 无限重试 → 卡密池空了还在死磕，把接口打到限流，日志被刷爆
  - 不重试   → 上面那个丢单问题

正确做法是**有限次退避重试 + 死信队列兜底 + 人工可重投**。

两条关键设计
------------
1. **退避要带抖动**。不带抖动的话，一次平台抖动会让所有任务在同一秒一起重试，
   把刚恢复的接口再打挂一次。这就是"惊群"。
2. **错误要分类**。超时/5xx 值得重试；"卡密池已空""参数非法"重试一万次也没用，
   应该直接进死信让人去补货。分不清的时候**默认可重试** —— 因为发货本身是幂等的，
   多试一次的代价远小于丢单。
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence


# ===========================================================================
# 一、错误分类
# ===========================================================================

class ErrorKind:
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


class RetryableError(Exception):
    """调用方明确知道这个错值得重试（超时、限流）。"""


class PermanentError(Exception):
    """调用方明确知道重试没用（库存空了、参数非法）。"""


# 文本兜底：拿不到异常类型时靠关键词猜
_PERMANENT_HINTS = (
    "not found", "invalid", "unauthorized", "forbidden", "permission",
    "已空", "已用尽", "库存不足", "不存在", "参数错误", "非法", "已被使用",
)


def classify(exc: BaseException) -> str:
    """
    判断一个异常该不该重试。

    优先级：显式标记 > 文本关键词 > 默认。
    默认是**可重试** —— 因为发货是幂等的（有 shipment_record 唯一约束兜底），
    多试一次的代价远小于丢单。
    """
    if isinstance(exc, PermanentError):
        return ErrorKind.PERMANENT
    if isinstance(exc, RetryableError):
        return ErrorKind.RETRYABLE

    text = str(exc).casefold()
    if any(h in text for h in _PERMANENT_HINTS):
        return ErrorKind.PERMANENT
    return ErrorKind.RETRYABLE


# ===========================================================================
# 二、退避策略
# ===========================================================================

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 2.0        # 秒
    factor: float = 2.0
    max_delay: float = 300.0       # 5 分钟封顶
    jitter: float = 0.25           # ±25% 抖动，防惊群


DEFAULT_POLICY = RetryPolicy()


def backoff_delay(
    attempt: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    rng: Optional[random.Random] = None,
) -> float:
    """
    第 attempt 次失败后应该等多久（attempt 从 1 开始）。

    2 → 4 → 8 → 16 → 32 ... 到 max_delay 封顶，每档再叠加 ±jitter 的随机抖动。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    raw = policy.base_delay * (policy.factor ** (attempt - 1))
    raw = min(raw, policy.max_delay)
    if policy.jitter:
        r = rng or random
        raw *= 1.0 + r.uniform(-policy.jitter, policy.jitter)
    return round(max(0.0, min(raw, policy.max_delay)), 3)


def should_retry(attempt: int, kind: str, policy: RetryPolicy = DEFAULT_POLICY) -> bool:
    if kind == ErrorKind.PERMANENT:
        return False
    return attempt < policy.max_attempts


def next_attempt_at(
    attempt: int,
    now: datetime,
    policy: RetryPolicy = DEFAULT_POLICY,
    rng: Optional[random.Random] = None,
) -> datetime:
    return now + timedelta(seconds=backoff_delay(attempt, policy, rng))


# ===========================================================================
# 三、任务
# ===========================================================================

class TaskState:
    PENDING = "PENDING"
    DONE = "DONE"
    DEAD = "DEAD"


@dataclass
class Task:
    kind: str
    payload: dict
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    attempts: int = 0
    state: str = TaskState.PENDING
    next_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None

    @property
    def is_due(self) -> bool:
        return self.state == TaskState.PENDING


@dataclass(frozen=True)
class DeadLetter:
    task: Task
    error: str
    kind: str
    attempts: int
    died_at: datetime
    id: str = ""

    @property
    def summary(self) -> str:
        return f"[{self.kind}] {self.task.kind} 重试 {self.attempts} 次后放弃：{self.error}"


# ===========================================================================
# 四、重试队列
# ===========================================================================

class RetryQueue:
    """
    内存版调度器。

    逻辑全部是纯的，可以穷举测试；持久化由 models.TaskRecord + save/load 负责。
    真实部署时把它换成 Celery / ARQ 也只是换个外壳，策略不用动。
    """

    def __init__(
        self,
        policy: RetryPolicy = DEFAULT_POLICY,
        *,
        rng: Optional[random.Random] = None,
        max_dead_letters: int = 1000,
    ) -> None:
        self.policy = policy
        self._rng = rng or random.Random()
        self._tasks: dict[str, Task] = {}
        self._dead: list[DeadLetter] = []
        self._max_dead = max_dead_letters

    # -- 入队 ------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict, now: Optional[datetime] = None) -> Task:
        moment = now or datetime.now(timezone.utc)
        task = Task(kind=kind, payload=dict(payload), created_at=moment, next_run_at=moment)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    # -- 取到期任务 ------------------------------------------------------
    def due(self, now: Optional[datetime] = None) -> list[Task]:
        moment = now or datetime.now(timezone.utc)
        return [
            t for t in self._tasks.values()
            if t.state == TaskState.PENDING and (t.next_run_at is None or t.next_run_at <= moment)
        ]

    # -- 结果上报 --------------------------------------------------------
    def complete(self, task: Task) -> Task:
        task.state = TaskState.DONE
        task.next_run_at = None
        return task

    def fail(
        self,
        task: Task,
        exc: BaseException,
        now: Optional[datetime] = None,
    ) -> Optional[Task]:
        """
        记录一次失败。

        返回同一个 task 表示"已重新排队"，返回 None 表示"进死信了"。
        调用方靠这个返回值决定要不要告警。
        """
        moment = now or datetime.now(timezone.utc)
        task.attempts += 1
        task.last_error = f"{type(exc).__name__}: {exc}"

        kind = classify(exc)
        if should_retry(task.attempts, kind, self.policy):
            task.next_run_at = next_attempt_at(task.attempts, moment, self.policy, self._rng)
            return task

        task.state = TaskState.DEAD
        task.next_run_at = None
        self._to_dead_letter(task, kind, moment)
        return None

    def _to_dead_letter(self, task: Task, kind: str, moment: datetime) -> DeadLetter:
        letter = DeadLetter(
            task=task, error=task.last_error or "", kind=kind,
            attempts=task.attempts, died_at=moment,
            id=str(uuid.uuid4()),
        )
        self._dead.append(letter)
        if len(self._dead) > self._max_dead:
            # 死信本身也要有上限，否则内存会被历史问题撑爆
            self._dead = self._dead[-self._max_dead:]
        return letter

    # -- 死信处理 --------------------------------------------------------
    @property
    def dead_letters(self) -> tuple[DeadLetter, ...]:
        return tuple(self._dead)

    def redeliver(self, letter_id: str, now: Optional[datetime] = None) -> Optional[Task]:
        """
        人工处理完之后重投。

        保留 attempts 计数 —— 否则一个坏任务可以在"重投→失败→重投"之间无限循环，
        看起来每次都是第一次。
        """
        moment = now or datetime.now(timezone.utc)
        for letter in list(self._dead):
            if letter.id != letter_id:
                continue
            self._dead.remove(letter)
            task = letter.task
            task.state = TaskState.PENDING
            task.next_run_at = moment
            self._tasks[task.id] = task
            return task
        return None

    def purge_dead_letters(self) -> int:
        count = len(self._dead)
        self._dead.clear()
        return count

    # -- 统计 ------------------------------------------------------------
    def stats(self, now: Optional[datetime] = None) -> dict:
        moment = now or datetime.now(timezone.utc)
        pending = [t for t in self._tasks.values() if t.state == TaskState.PENDING]
        overdue = [t for t in pending if t.next_run_at and t.next_run_at <= moment]
        by_kind: dict[str, int] = {}
        for letter in self._dead:
            by_kind[letter.task.kind] = by_kind.get(letter.task.kind, 0) + 1
        return {
            "pending": len(pending),
            "due_now": len(overdue),
            "done": sum(1 for t in self._tasks.values() if t.state == TaskState.DONE),
            "dead": len(self._dead),
            "dead_by_kind": by_kind,
            "needs_attention": len(self._dead) > 0,
        }


def render_dead_letters(letters: Sequence[DeadLetter]) -> str:
    if not letters:
        return "死信队列为空"
    lines = [f"死信 {len(letters)} 条，需人工处理："]
    lines.extend("  " + letter.summary for letter in letters[:20])
    if len(letters) > 20:
        lines.append(f"  ...另有 {len(letters) - 20} 条")
    return "\n".join(lines)


# ===========================================================================
# 五、持久化
# ===========================================================================

async def save_task(db: Any, ctx: Any, task: Task, model: Any) -> Any:
    """把任务写进 task_queue 表，进程重启后还能捞回来。调用方负责 commit。"""
    row = model(
        id=task.id,
        tenant_id=ctx.tenant_id,
        kind=task.kind,
        payload=task.payload,
        attempts=task.attempts,
        state=task.state,
        next_run_at=task.next_run_at,
        last_error=task.last_error,
    )
    db.add(row)
    await db.flush()
    return row


async def load_due_tasks(db: Any, ctx: Any, now: datetime, model: Any, limit: int = 100) -> list[Any]:
    from sqlalchemy import select

    from .guardrails import scoped

    stmt = (
        scoped(select(model), ctx, model)
        .where(model.state == TaskState.PENDING, model.next_run_at <= now)
        .order_by(model.next_run_at)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


def task_from_row(row: Any) -> Task:
    return Task(
        id=row.id,
        kind=row.kind,
        payload=dict(row.payload or {}),
        attempts=row.attempts,
        state=row.state,
        next_run_at=row.next_run_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )
