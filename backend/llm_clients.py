"""
真实大模型客户端

两个实现：
  - `OpenAICompatClient`  文本议价（DeepSeek-V3 / Qwen / 任何 OpenAI 兼容接口）
  - `QwenVisionVerifier`  多模态鉴真（qwen-vl-max），实现 sourcing.VisionVerifier

都走 httpx，且**接受注入的 transport** —— 这样测试时用 MockTransport 就能覆盖
超时、5xx、返回垃圾 JSON、返回带 markdown 围栏的 JSON 这些真实会发生的场景，
不用碰网络，也不用打桩到函数级别。

三种失败，三种处理
------------------
1. **传输失败**（超时、5xx、限流）→ 按指数退避重试，重试用尽抛 RetryableError，
   交给上层的补偿队列。这类错误重试有意义。
2. **响应不合规**（JSON 解析失败、字段缺失、报价越界）→ 这不是网络问题，
   重试同一个请求多半还是同样的结果。要做的是一次**修复重试**：把校验错误
   回灌给模型，让它自己改。这也是最多一次 —— 再来一次基本就是浪费钱。
3. **模型拒绝或内容为空** → 直接失败，让上层走兜底话术。不要试图"再问一次"，
   那只会把成本翻倍。

刻意不做的事：不在客户端里做价格校验。价格硬校验是 guardrails 的职责，
放在这里会导致同一套规则有两个实现，早晚不一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

import httpx
from pydantic import ValidationError

from .guardrails import AiDecision
from .pipeline import LlmOutcome, ProductContext, Turn, build_system_prompt
from .retry import RetryableError
from .sourcing import Listing, VisionVerdict

# 模型返回的 JSON 经常被包在 markdown 围栏里，这是最常见的"格式错误"
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_REPAIRS = 1


class LlmError(Exception):
    pass


class LlmTimeout(LlmError, RetryableError):
    """继承 RetryableError，这样上层补偿队列会自动把它当成可重试。"""


class LlmServerError(LlmError, RetryableError):
    pass


class LlmClientError(LlmError):
    """4xx —— 请求本身有问题，重试没用。"""


class LlmInvalidResponse(LlmError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_repairs: int = DEFAULT_MAX_REPAIRS
    temperature: float = 0.3

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""


# ===========================================================================
# 一、底层调用
# ===========================================================================

def _build_messages(
    system: str,
    context: Sequence[Turn],
    message: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in context:
        role = "assistant" if turn.role == "AI" else "user"
        if turn.role == "SYSTEM":
            continue                      # 系统消息不重复塞进历史
        messages.append({"role": role, "content": turn.content})
    messages.append({"role": "user", "content": message})
    return messages


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def parse_decision(content: str) -> AiDecision:
    """
    把模型输出解析成 AiDecision。

    先剥 markdown 围栏，再尝试整体 JSON，最后兜底从文本里抠出第一个 {...} ——
    模型偶尔会在 JSON 前后加一句"好的，我的回复是："，这不该导致整次调用失败。
    """
    if not content or not content.strip():
        raise LlmInvalidResponse("模型返回了空内容")

    candidate = _strip_fence(content)
    try:
        return AiDecision.model_validate_json(candidate)
    except ValidationError:
        pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return AiDecision.model_validate_json(candidate[start:end + 1])
        except ValidationError as exc:
            raise LlmInvalidResponse(f"模型输出不符合约定结构：{exc.errors()[:2]}") from exc

    raise LlmInvalidResponse("模型输出里找不到 JSON 对象")


class OpenAICompatClient:
    """文本议价客户端。DeepSeek / Qwen / OpenAI 都兼容这套接口。"""

    def __init__(
        self,
        config: LlmConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        limiter: Any = None,
        limiter_key: str = "llm",
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=config.timeout, transport=transport,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        self._limiter = limiter
        self._limiter_key = limiter_key

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        json_mode: bool = True,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": self.config.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        if self._limiter is not None:
            await self._limiter.get(self._limiter_key).acquire_or_wait("global")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 2):
            try:
                response = await self._client.post(self.config.endpoint(), json=payload)
            except httpx.TimeoutException as exc:
                last_error = LlmTimeout(f"调用大模型超时（第 {attempt} 次）")
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = LlmServerError(f"大模型连接失败：{exc}")
            else:
                if response.status_code >= 500:
                    last_error = LlmServerError(f"大模型返回 {response.status_code}")
                elif response.status_code == 429:
                    last_error = LlmServerError("大模型限流")
                elif response.status_code >= 400:
                    # 4xx 是请求本身的问题，重试无意义
                    raise LlmClientError(
                        f"大模型拒绝请求 {response.status_code}：{response.text[:200]}"
                    )
                else:
                    return self._parse_chat_response(response)

            if attempt <= self.config.max_retries:
                from .retry import backoff_delay

                await _sleep(backoff_delay(attempt))

        raise last_error or LlmServerError("大模型调用失败")

    @staticmethod
    def _parse_chat_response(response: httpx.Response) -> ChatResult:
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmInvalidResponse("大模型返回的不是 JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LlmInvalidResponse("大模型返回里没有 choices")
        choice = choices[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=choice.get("finish_reason", ""),
        )

    async def decide(
        self,
        *,
        system: str,
        context: Sequence[Turn],
        message: str,
        product: Optional[ProductContext] = None,
        round_no: int = 1,
    ) -> LlmOutcome:
        """实现 pipeline.LlmClient 协议。"""
        messages = _build_messages(system, context, message)
        prompt_tokens = completion_tokens = 0

        for repair in range(self.config.max_repairs + 1):
            result = await self.chat(messages)
            prompt_tokens += result.prompt_tokens
            completion_tokens += result.completion_tokens

            try:
                decision = parse_decision(result.content)
            except LlmInvalidResponse as exc:
                if repair >= self.config.max_repairs:
                    raise
                # 把校验错误回灌给模型让它自己改 —— 比原样重试有效得多
                messages = messages + [
                    {"role": "assistant", "content": result.content},
                    {"role": "user", "content": (
                        f"你的输出不符合要求：{exc}。"
                        "请只输出一个 JSON 对象，字段为 reply / offered_price / intent，"
                        "不要加任何解释或 markdown 围栏。"
                    )},
                ]
                continue

            return LlmOutcome(
                decision=decision,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                confidence=1.0 if result.finish_reason != "length" else 0.5,
            )

        raise LlmInvalidResponse("多次修复后仍未得到合规输出")


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# ===========================================================================
# 二、多模态鉴真
# ===========================================================================

VISION_SYSTEM_PROMPT = """你是二手交易的商品鉴真助手。根据商品标题、描述和图片判断是否值得推荐给买家。

重点识别三类风险：
1. 隐性瑕疵：描述或图片里出现"轻微划痕""有磕碰""屏幕有印"这类被淡化的缺陷
2. 非原装：出现"第三方""兼容""非原厂""副厂"等字样
3. 二贩子文案：大量同款堆图、统一话术模板、明显批量铺货特征

只输出 JSON：{"risky": true/false, "labels": ["风险标签"], "confidence": 0~1, "note": "一句话说明"}
判断不了时 risky 传 false，不要凭猜测标记风险。"""

VISION_MAX_IMAGES = 4


class QwenVisionVerifier:
    """
    多模态鉴真，实现 sourcing.VisionVerifier 协议。

    图片只传 URL 不下载 —— 少一份副本就少一个泄露面，也省一次带宽。
    """

    def __init__(
        self,
        config: LlmConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        limiter: Any = None,
        limiter_key: str = "llm",
        max_images: int = VISION_MAX_IMAGES,
    ) -> None:
        self.config = config
        self.max_images = max_images
        self._client = OpenAICompatClient(
            config, transport=transport, limiter=limiter, limiter_key=limiter_key,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def build_content(listing: Listing, max_images: int) -> list[dict[str, Any]]:
        text = (
            f"商品标题：{listing.title}\n"
            f"标价：¥{listing.price:g}\n"
            f"描述：{listing.description or '（无描述）'}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        # 只取前几张。图片越多越贵，而第 5 张之后的边际信息量很低。
        for url in list(listing.image_urls)[:max_images]:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return content

    async def verify(self, listing: Listing) -> VisionVerdict:
        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": self.build_content(listing, self.max_images)},
        ]
        result = await self._client.chat(messages, json_mode=True)

        try:
            payload = json.loads(_strip_fence(result.content))
        except (ValueError, TypeError):
            # 鉴真失败不该阻断推送 —— 返回"无风险"让商品照常推，
            # 而不是因为一次模型抽风就丢掉一个可能的漏。
            return VisionVerdict(False, (), 0.0, "鉴真结果无法解析，按无风险处理")

        labels = payload.get("labels") or []
        return VisionVerdict(
            risky=bool(payload.get("risky")),
            labels=tuple(str(x) for x in labels)[:5],
            confidence=float(payload.get("confidence") or 0.0),
            note=str(payload.get("note") or ""),
        )


# ===========================================================================
# 三、从环境变量装配
# ===========================================================================

@dataclass
class LlmBundle:
    """把装配好的客户端打包，方便挂到 API 的 Container 上。"""

    text: Optional[OpenAICompatClient] = None
    vision: Optional[QwenVisionVerifier] = None

    @property
    def configured(self) -> bool:
        return self.text is not None

    async def aclose(self) -> None:
        if self.text is not None:
            await self.text.aclose()
        if self.vision is not None:
            await self.vision.aclose()


def build_from_env(
    env: Any,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    limiter: Any = None,
) -> LlmBundle:
    """
    按环境变量装配。没配 key 就返回空 bundle，服务照常启动 ——
    FAQ 与规则引擎不依赖大模型，配了才启用，不配就降级。
    """
    text_key = env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY")
    qwen_key = env.get("QWEN_API_KEY")

    text = None
    if text_key:
        text = OpenAICompatClient(
            LlmConfig(
                base_url=env.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
                api_key=text_key,
                model=env.get("LLM_MODEL", "deepseek-chat"),
            ),
            transport=transport, limiter=limiter,
        )

    vision = None
    if qwen_key:
        vision = QwenVisionVerifier(
            LlmConfig(
                base_url=env.get(
                    "VISION_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
                api_key=qwen_key,
                model=env.get("VISION_MODEL", "qwen-vl-max"),
                timeout=45.0,
            ),
            transport=transport, limiter=limiter,
        )

    return LlmBundle(text=text, vision=vision)
