"""
大模型客户端测试

用 httpx.MockTransport 覆盖真实会发生的坏情况：超时、5xx、限流、4xx、
返回垃圾 JSON、返回带 markdown 围栏的 JSON。不碰网络，也不打桩到函数级别。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend import llm_clients
from backend.llm_clients import (
    LlmClientError,
    LlmConfig,
    LlmInvalidResponse,
    LlmServerError,
    LlmTimeout,
    OpenAICompatClient,
    QwenVisionVerifier,
    build_from_env,
    parse_decision,
)
from backend.pipeline import ProductContext, Turn
from backend.retry import ErrorKind, classify
from backend.sourcing import Listing

run = asyncio.run

CONFIG = LlmConfig(base_url="https://api.example.com/v1", api_key="sk-test",
                   model="test-model", max_retries=2, max_repairs=1)

PRODUCT = ProductContext(title="爱奇艺年卡", listed_price=128.0, min_price=99.0,
                         ladder=(0.05, 0.12), shipping_policy="虚拟发货")


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    """退避是给生产用的，测试里不该真的等 2 秒。"""
    async def _noop(_seconds):
        return None

    monkeypatch.setattr(llm_clients, "_sleep", _noop)


def make_client(handler, **kw):
    return OpenAICompatClient(CONFIG, transport=httpx.MockTransport(handler), **kw)


def ok_body(content: str, prompt=100, completion=20, finish="stop") -> str:
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    })


def decision_json(reply="好的亲～", price=None, intent="ENQUIRY") -> str:
    return json.dumps({"reply": reply, "offered_price": price, "intent": intent})


# ===========================================================================
# 一、输出解析
# ===========================================================================

def test_parses_plain_json():
    d = parse_decision(decision_json("在的", 120.0, "BARGAIN"))
    assert d.reply == "在的"
    assert d.offered_price == 120.0
    assert d.intent == "BARGAIN"


def test_parses_fenced_json():
    raw = "```json\n" + decision_json("好的") + "\n```"
    assert parse_decision(raw).reply == "好的"


def test_parses_json_with_preamble():
    # 模型经常加一句"好的，我的回复是：" —— 这不该导致整次调用失败
    raw = "好的，我的回复是：" + decision_json("在的") + " 希望有帮助"
    assert parse_decision(raw).reply == "在的"


def test_rejects_empty_content():
    with pytest.raises(LlmInvalidResponse):
        parse_decision("   ")


def test_rejects_content_without_json():
    with pytest.raises(LlmInvalidResponse):
        parse_decision("我觉得这个问题很难回答")


def test_rejects_json_with_wrong_shape():
    with pytest.raises(LlmInvalidResponse):
        parse_decision(json.dumps({"answer": "你好", "intent": "ENQUIRY"}))


def test_rejects_off_platform_reply():
    # AiDecision 的校验器在客户端这层也生效
    with pytest.raises(LlmInvalidResponse):
        parse_decision(decision_json("加我微信 abc123"))


# ===========================================================================
# 二、传输层
# ===========================================================================

def test_successful_chat():
    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text=ok_body("hi")))
        result = await client.chat([{"role": "user", "content": "x"}])
        await client.aclose()
        return result

    result = run(scenario())
    assert result.content == "hi"
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20


def test_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, text=ok_body("recovered"))

    async def scenario():
        client = make_client(handler)
        result = await client.chat([{"role": "user", "content": "x"}])
        await client.aclose()
        return result

    assert run(scenario()).content == "recovered"
    assert calls["n"] == 2


def test_gives_up_after_max_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async def scenario():
        client = make_client(handler)
        try:
            await client.chat([{"role": "user", "content": "x"}])
        finally:
            await client.aclose()

    with pytest.raises(LlmServerError):
        run(scenario())
    assert calls["n"] == 3          # 首次 + 2 次重试


def test_timeout_becomes_retryable_error():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    async def scenario():
        client = make_client(handler)
        try:
            await client.chat([{"role": "user", "content": "x"}])
        finally:
            await client.aclose()

    with pytest.raises(LlmTimeout) as exc:
        run(scenario())
    # 关键：它必须是"可重试"的，补偿队列才会接住它
    assert classify(exc.value) == ErrorKind.RETRYABLE


def test_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    async def scenario():
        client = make_client(handler)
        try:
            await client.chat([{"role": "user", "content": "x"}])
        finally:
            await client.aclose()

    with pytest.raises(LlmClientError):
        run(scenario())
    assert calls["n"] == 1          # 请求本身有问题，重试没意义


def test_malformed_response_body():
    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text="<html>not json</html>"))
        try:
            await client.chat([{"role": "user", "content": "x"}])
        finally:
            await client.aclose()

    with pytest.raises(LlmInvalidResponse):
        run(scenario())


def test_response_without_choices():
    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text='{"usage":{}}'))
        try:
            await client.chat([{"role": "user", "content": "x"}])
        finally:
            await client.aclose()

    with pytest.raises(LlmInvalidResponse):
        run(scenario())


def test_json_mode_is_requested():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=ok_body(decision_json()))

    async def scenario():
        client = make_client(handler)
        await client.chat([{"role": "user", "content": "x"}])
        await client.aclose()

    run(scenario())
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["model"] == "test-model"


# ===========================================================================
# 三、decide（pipeline 协议）
# ===========================================================================

def test_decide_returns_outcome_with_usage():
    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text=ok_body(
            decision_json("给您 124", 124.0, "BARGAIN"), prompt=800, completion=60)))
        outcome = await client.decide(
            system="sys", context=[Turn("BUYER", "便宜点")], message="再便宜点",
            product=PRODUCT, round_no=1)
        await client.aclose()
        return outcome

    outcome = run(scenario())
    assert outcome.decision.intent == "BARGAIN"
    assert outcome.decision.offered_price == 124.0
    assert outcome.prompt_tokens == 800
    assert outcome.completion_tokens == 60


def test_decide_sends_system_and_history():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=ok_body(decision_json()))

    async def scenario():
        client = make_client(handler)
        await client.decide(
            system="SYSTEM_PROMPT", context=[Turn("BUYER", "在吗"), Turn("AI", "在的")],
            message="多少钱", product=PRODUCT, round_no=2)
        await client.aclose()

    run(scenario())
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert seen["body"]["messages"][0]["content"] == "SYSTEM_PROMPT"


def test_decide_repairs_invalid_output_once():
    calls = {"n": 0}
    bodies = []

    def handler(request):
        calls["n"] += 1
        bodies.append(json.loads(request.content))
        if calls["n"] == 1:
            return httpx.Response(200, text=ok_body("我不知道怎么回答"))
        return httpx.Response(200, text=ok_body(decision_json("好的")))

    async def scenario():
        client = make_client(handler)
        outcome = await client.decide(system="s", context=(), message="x", product=PRODUCT)
        await client.aclose()
        return outcome

    outcome = run(scenario())
    assert outcome.decision.reply == "好的"
    assert calls["n"] == 2
    # 第二次请求里应该带着校验错误，让模型自己改
    repair_prompt = bodies[1]["messages"][-1]["content"]
    assert "不符合要求" in repair_prompt
    # token 要把两次都算进去，否则成本会少记一半
    assert outcome.prompt_tokens == 200


def test_decide_gives_up_when_repair_also_fails():
    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text=ok_body("还是不行")))
        try:
            await client.decide(system="s", context=(), message="x", product=PRODUCT)
        finally:
            await client.aclose()

    with pytest.raises(LlmInvalidResponse):
        run(scenario())


def test_decide_marks_truncated_output_as_low_confidence():
    async def scenario():
        client = make_client(lambda r: httpx.Response(
            200, text=ok_body(decision_json(), finish="length")))
        outcome = await client.decide(system="s", context=(), message="x", product=PRODUCT)
        await client.aclose()
        return outcome

    assert run(scenario()).confidence == 0.5


def test_limiter_is_consulted():
    class CountingLimiter:
        def __init__(self):
            self.calls = 0

        def get(self, preset):
            return self

        async def acquire_or_wait(self, key):
            self.calls += 1

    limiter = CountingLimiter()

    async def scenario():
        client = make_client(lambda r: httpx.Response(200, text=ok_body(decision_json())),
                             limiter=limiter)
        await client.chat([{"role": "user", "content": "x"}])
        await client.aclose()

    run(scenario())
    assert limiter.calls == 1


# ===========================================================================
# 四、多模态鉴真
# ===========================================================================

def verifier(handler, **kw):
    return QwenVisionVerifier(CONFIG, transport=httpx.MockTransport(handler), **kw)


def test_build_content_includes_images():
    item = Listing(item_id="i1", title="Switch OLED", price=1200.0,
                   description="9成新", image_urls=("u1", "u2"))
    content = QwenVisionVerifier.build_content(item, max_images=4)
    assert content[0]["type"] == "text"
    assert "Switch OLED" in content[0]["text"]
    assert [c["image_url"]["url"] for c in content[1:]] == ["u1", "u2"]


def test_build_content_caps_image_count():
    # 第 5 张之后边际信息量很低，但每张都要花钱
    item = Listing(item_id="i1", title="t", price=1.0,
                   image_urls=tuple(f"u{i}" for i in range(10)))
    content = QwenVisionVerifier.build_content(item, max_images=3)
    assert len(content) == 4          # 1 段文字 + 3 张图


def test_build_content_handles_missing_description():
    item = Listing(item_id="i1", title="t", price=1.0)
    assert "（无描述）" in QwenVisionVerifier.build_content(item, 4)[0]["text"]


def test_verify_flags_risky_item():
    body = ok_body(json.dumps({"risky": True, "labels": ["疑似划痕", "非原装"],
                               "confidence": 0.82, "note": "描述里提到轻微划痕"}))
    async def scenario():
        v = verifier(lambda r: httpx.Response(200, text=body))
        verdict = await v.verify(Listing(item_id="i1", title="t", price=1.0))
        await v.aclose()
        return verdict

    verdict = run(scenario())
    assert verdict.risky is True
    assert "疑似划痕" in verdict.labels
    assert verdict.confidence == pytest.approx(0.82)


def test_verify_passes_clean_item():
    body = ok_body(json.dumps({"risky": False, "labels": [], "confidence": 0.9, "note": ""}))
    async def scenario():
        v = verifier(lambda r: httpx.Response(200, text=body))
        verdict = await v.verify(Listing(item_id="i1", title="t", price=1.0))
        await v.aclose()
        return verdict

    assert run(scenario()).risky is False


def test_verify_failure_defaults_to_safe():
    # 鉴真抽风不该阻断推送 —— 捡漏最怕漏
    async def scenario():
        v = verifier(lambda r: httpx.Response(200, text=ok_body("我判断不出来")))
        verdict = await v.verify(Listing(item_id="i1", title="t", price=1.0))
        await v.aclose()
        return verdict

    verdict = run(scenario())
    assert verdict.risky is False
    assert "无法解析" in verdict.note


def test_verify_caps_label_count():
    body = ok_body(json.dumps({"risky": True, "labels": [f"l{i}" for i in range(20)]}))
    async def scenario():
        v = verifier(lambda r: httpx.Response(200, text=body))
        verdict = await v.verify(Listing(item_id="i1", title="t", price=1.0))
        await v.aclose()
        return verdict

    assert len(run(scenario()).labels) == 5


def test_verifier_satisfies_sourcing_protocol():
    from backend.sourcing import VisionVerifier

    assert isinstance(QwenVisionVerifier(CONFIG), VisionVerifier)


# ===========================================================================
# 五、环境装配
# ===========================================================================

def test_build_from_env_without_keys_is_empty():
    bundle = build_from_env({})
    assert bundle.configured is False
    assert bundle.text is None and bundle.vision is None


def test_build_from_env_with_text_key():
    bundle = build_from_env({"DEEPSEEK_API_KEY": "sk-x", "LLM_MODEL": "deepseek-chat"})
    assert bundle.configured is True
    assert bundle.text.config.model == "deepseek-chat"
    assert bundle.vision is None


def test_build_from_env_with_both():
    bundle = build_from_env({"DEEPSEEK_API_KEY": "sk-x", "QWEN_API_KEY": "sk-y"})
    assert bundle.text is not None and bundle.vision is not None
    assert bundle.vision.config.model == "qwen-vl-max"


def test_build_from_env_honours_overrides():
    bundle = build_from_env({
        "LLM_API_KEY": "sk-x", "LLM_BASE_URL": "https://custom/v1",
        "LLM_MODEL": "custom-model",
    })
    assert bundle.text.config.base_url == "https://custom/v1"
    assert bundle.text.config.endpoint() == "https://custom/v1/chat/completions"


def test_bundle_aclose_is_safe_when_empty():
    run(build_from_env({}).aclose())


def test_build_container_wires_the_bundle(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./wire.db")
    monkeypatch.setenv("MASTER_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("QWEN_API_KEY", "sk-y")

    from backend.main import build_container

    container = build_container()
    assert container.llm is not None
    assert container.vision is not None
    assert container.llm_bundle.configured is True
    assert container.llm is container.llm_bundle.text
    run(container.llm_bundle.aclose())
    run(container.engine.dispose())


def test_build_container_starts_without_llm_keys(monkeypatch):
    # 缺 key 也要能启动 —— 本地开发和 CI 不该被外部服务卡住
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./wire2.db")
    monkeypatch.setenv("MASTER_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)

    from backend.main import build_container

    container = build_container()
    assert container.llm is None
    assert container.vision is None
    assert container.llm_bundle.configured is False
    run(container.engine.dispose())
