"""
补偿队列与限流测试

核心断言：可重试的会退避重试、不可重试的直接进死信、重投不会重置计数；
令牌桶的补充速率和突发容量都对得上。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from backend.ratelimit import (
    PRESETS,
    LimiterRegistry,
    RateLimiter,
    RateLimitExceeded,
    TokenBucket,
    build_limiter,
)
from backend.retry import (
    DEFAULT_POLICY,
    DeadLetter,
    ErrorKind,
    PermanentError,
    RetryPolicy,
    RetryQueue,
    RetryableError,
    Task,
    TaskState,
    backoff_delay,
    classify,
    next_attempt_at,
    render_dead_letters,
    should_retry,
)

NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)


# ===========================================================================
# 一、错误分类
# ===========================================================================

def test_explicit_markers_win():
    assert classify(RetryableError("boom")) == ErrorKind.RETRYABLE
    assert classify(PermanentError("boom")) == ErrorKind.PERMANENT


def test_permanent_hints_in_message():
    assert classify(TimeoutError("connection timeout")) == ErrorKind.RETRYABLE
    assert classify(RuntimeError("卡密池已空，需要补货")) == ErrorKind.PERMANENT
    assert classify(RuntimeError("item not found")) == ErrorKind.PERMANENT


def test_unknown_errors_default_to_retryable():
    # 发货是幂等的，多试一次的代价远小于丢单
    assert classify(ValueError("谁知道这是什么")) == ErrorKind.RETRYABLE


# ===========================================================================
# 二、退避
# ===========================================================================

def test_backoff_grows_exponentially():
    p = RetryPolicy(base_delay=2, factor=2, jitter=0)
    assert [backoff_delay(i, p) for i in range(1, 5)] == [2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped():
    p = RetryPolicy(base_delay=2, factor=2, max_delay=10, jitter=0)
    assert backoff_delay(10, p) == 10.0


def test_jitter_stays_within_bounds():
    p = RetryPolicy(base_delay=100, factor=1, max_delay=1000, jitter=0.25)
    rng = random.Random(42)
    values = [backoff_delay(1, p, rng) for _ in range(200)]
    assert min(values) >= 75.0
    assert max(values) <= 125.0
    assert len(set(values)) > 1        # 确实是抖动的


def test_jitter_never_exceeds_max_delay():
    p = RetryPolicy(base_delay=100, factor=1, max_delay=100, jitter=0.5)
    for seed in range(50):
        assert backoff_delay(1, p, random.Random(seed)) <= 100.0


def test_backoff_rejects_bad_attempt():
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_should_retry_respects_kind_and_limit():
    p = RetryPolicy(max_attempts=3)
    assert should_retry(1, ErrorKind.RETRYABLE, p)
    assert not should_retry(3, ErrorKind.RETRYABLE, p)
    assert not should_retry(1, ErrorKind.PERMANENT, p)


def test_next_attempt_at_is_in_the_future():
    at = next_attempt_at(1, NOW, RetryPolicy(base_delay=5, jitter=0))
    assert at == NOW + timedelta(seconds=5)


# ===========================================================================
# 三、重试队列
# ===========================================================================

def _queue(max_attempts=3):
    return RetryQueue(RetryPolicy(max_attempts=max_attempts, base_delay=10, jitter=0),
                      rng=random.Random(0))


def test_enqueue_is_immediately_due():
    q = _queue()
    task = q.enqueue("ship_order", {"order_id": "o1"}, now=NOW)
    assert task in q.due(NOW)


def test_complete_removes_from_due():
    q = _queue()
    task = q.enqueue("ship_order", {}, now=NOW)
    q.complete(task)
    assert q.due(NOW) == []
    assert q.stats(NOW)["done"] == 1


def test_retryable_failure_requeues_with_delay():
    q = _queue()
    task = q.enqueue("ship_order", {}, now=NOW)
    again = q.fail(task, RetryableError("timeout"), now=NOW)

    assert again is task
    assert task.attempts == 1
    assert task.state == TaskState.PENDING
    assert task.next_run_at == NOW + timedelta(seconds=10)
    assert q.due(NOW) == []                       # 还没到时间
    assert q.due(NOW + timedelta(seconds=10)) == [task]


def test_permanent_failure_goes_straight_to_dead_letter():
    q = _queue()
    task = q.enqueue("ship_order", {}, now=NOW)
    assert q.fail(task, PermanentError("卡密池已空"), now=NOW) is None
    assert task.state == TaskState.DEAD
    assert len(q.dead_letters) == 1
    assert "卡密池已空" in q.dead_letters[0].error


def test_exhausting_attempts_goes_to_dead_letter():
    q = _queue(max_attempts=3)
    task = q.enqueue("ship_order", {}, now=NOW)
    for _ in range(3):
        q.fail(task, RetryableError("timeout"), now=NOW)
    assert task.state == TaskState.DEAD
    assert q.dead_letters[0].attempts == 3


def test_dead_letter_redelivery_keeps_attempt_count():
    # 不保留计数的话，坏任务能在"重投→失败→重投"之间无限循环
    q = _queue(max_attempts=3)
    task = q.enqueue("ship_order", {}, now=NOW)
    for _ in range(3):
        q.fail(task, RetryableError("x"), now=NOW)

    letter_id = q.dead_letters[0].id
    revived = q.redeliver(letter_id, now=NOW)

    assert revived is task
    assert task.attempts == 3
    assert task.state == TaskState.PENDING
    assert q.dead_letters == ()


def test_redeliver_unknown_id_returns_none():
    assert _queue().redeliver("nope") is None


def test_stats_report_health():
    q = _queue(max_attempts=2)
    q.enqueue("a", {}, now=NOW)
    doomed = q.enqueue("b", {}, now=NOW)
    q.fail(doomed, PermanentError("库存不足"), now=NOW)

    stats = q.stats(NOW)
    assert stats["pending"] == 1          # 进死信的那个不算 pending
    assert stats["dead"] == 1
    assert stats["dead_by_kind"] == {"b": 1}
    assert stats["needs_attention"] is True


def test_dead_letter_buffer_is_bounded():
    q = RetryQueue(RetryPolicy(max_attempts=1), max_dead_letters=3)
    for i in range(6):
        q.fail(q.enqueue(f"k{i}", {}, now=NOW), PermanentError("nope"), now=NOW)
    assert len(q.dead_letters) == 3


def test_purge_dead_letters():
    q = _queue()
    q.fail(q.enqueue("k", {}, now=NOW), PermanentError("x"), now=NOW)
    assert q.purge_dead_letters() == 1
    assert q.dead_letters == ()


def test_render_dead_letters():
    q = _queue()
    q.fail(q.enqueue("ship_order", {}, now=NOW), PermanentError("卡密池已空"), now=NOW)
    text = render_dead_letters(q.dead_letters)
    assert "死信 1 条" in text and "ship_order" in text
    assert render_dead_letters([]) == "死信队列为空"


def test_task_from_dead_letter_summary():
    q = _queue()
    q.fail(q.enqueue("ship_order", {}, now=NOW), PermanentError("boom"), now=NOW)
    assert "重试 1 次后放弃" in q.dead_letters[0].summary


# ===========================================================================
# 四、令牌桶
# ===========================================================================

def test_bucket_starts_full():
    bucket = TokenBucket(capacity=5, refill_per_second=1)
    assert bucket.available == 5
    assert bucket.try_acquire(5, now=0.0)
    assert not bucket.try_acquire(1, now=0.0)


def test_bucket_refills_over_time():
    bucket = TokenBucket(capacity=5, refill_per_second=1)
    bucket.try_acquire(5, now=0.0)
    assert bucket.available == 0
    assert bucket.try_acquire(1, now=1.0)
    assert bucket.try_acquire(1, now=2.0)
    assert not bucket.try_acquire(1, now=2.0)


def test_bucket_does_not_overfill():
    bucket = TokenBucket(capacity=3, refill_per_second=1)
    bucket.try_acquire(3, now=0.0)
    # 等 100 秒也只能回到容量 3，不会攒出 100 个
    assert bucket.try_acquire(4, now=100.0) is False
    assert bucket.try_acquire(3, now=100.0) is True


def test_time_until_reports_wait():
    bucket = TokenBucket(capacity=2, refill_per_second=0.5)
    bucket.try_acquire(2, now=0.0)
    assert bucket.time_until(1, now=0.0) == pytest.approx(2.0)
    assert bucket.time_until(2, now=0.0) == pytest.approx(4.0)


def test_time_until_is_zero_when_available():
    bucket = TokenBucket(capacity=2, refill_per_second=1)
    assert bucket.time_until(1, now=0.0) == 0.0


def test_bucket_rejects_bad_config():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_per_second=1)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_per_second=0)


# ===========================================================================
# 五、限流器
# ===========================================================================

def test_limiter_isolates_keys():
    # 一个店打满不影响别的店 —— 这是按 key 分桶的全部意义
    limiter = RateLimiter(capacity=1, refill_per_second=1)
    assert limiter.try_acquire("s1", now=0.0)
    assert not limiter.try_acquire("s1", now=0.0)
    assert limiter.try_acquire("s2", now=0.0)


def test_limiter_raises_with_retry_after():
    limiter = RateLimiter(capacity=1, refill_per_second=0.5)
    limiter.acquire("s1", now=0.0)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.acquire("s1", now=0.0)
    assert exc.value.retry_after == pytest.approx(2.0)
    assert "s1" in str(exc.value)


def test_limiter_snapshot_and_reset():
    limiter = RateLimiter(capacity=3, refill_per_second=1)
    limiter.try_acquire("s1", 2, now=0.0)
    assert limiter.snapshot() == {"s1": 1.0}
    limiter.reset()
    assert limiter.snapshot() == {}


def test_build_limiter_from_preset():
    limiter = build_limiter("platform_api")
    capacity, refill = PRESETS["platform_api"]
    assert limiter.bucket("k").capacity == capacity
    assert limiter.bucket("k").refill_per_second == refill


def test_unknown_preset_is_rejected():
    with pytest.raises(KeyError):
        build_limiter("nope")


def test_registry_shares_limiters_by_preset():
    registry = LimiterRegistry()
    registry.try_acquire("notify", "s1")
    assert registry.get("notify") is registry.get("notify")
    assert "notify" in registry.snapshot()


def test_registry_keeps_presets_separate():
    registry = LimiterRegistry()
    registry.try_acquire("llm", "s1")
    assert set(registry.snapshot()) == {"llm"}


def test_platform_preset_is_conservative():
    # 对平台接口要克制：容量 5、每秒 1 次
    assert PRESETS["platform_api"] == (5, 1.0)
    assert PRESETS["scrape"][1] < PRESETS["notify"][1]
