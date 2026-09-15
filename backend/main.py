"""
应用装配

启动：uvicorn backend.main:create_app --factory --host 0.0.0.0 --port 8000

关于 CORS 的一处刻意偏离原方案
------------------------------
原方案的模块 A 写的是"配置 CORS 中间件，允许任意前端域名访问"。这个不要照做。

`allow_origins=["*"]` 配上 `allow_credentials=True` 时，浏览器会拒绝携带凭证，
很多人为了"跑通"就把凭证关掉 —— 于是控制台变成了任何人打开网页都能读的接口。
而如果不带凭证，这套系统的鉴权就只能靠自定义头，那等于没有 CSRF 防护。

所以这里默认只允许白名单里的域名，通过 CORS_ORIGINS 环境变量配置。
开发时设成 http://localhost:5173 就行。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .api import Container, build_router
from .crypto import KeyMaterialMissing, configure_provider, provider_from_env
from .llm_clients import build_from_env
from .ratelimit import LimiterRegistry, RateLimitExceeded
from .retry import RetryQueue

DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def parse_origins(raw: Optional[str]) -> list[str]:
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


def build_container(
    database_url: Optional[str] = None,
    *,
    configure_crypto: bool = True,
    **overrides,
) -> Container:
    url = database_url or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("未配置 DATABASE_URL")

    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    if configure_crypto:
        # 主密钥不在环境里就直接失败启动 —— 带着没配密钥的状态跑起来，
        # 第一次发货才会炸，那时候已经在收钱了。
        configure_provider(provider_from_env())

    # 限流器要先建，因为大模型客户端会拿它做调用节流
    limiters = overrides.pop("limiters", None) or LimiterRegistry()

    # 大模型没配 key 也能启动：FAQ 与规则引擎不依赖它，配了才启用。
    # 这比"缺 key 就起不来"务实得多 —— 本地开发和 CI 都不该被外部服务卡住。
    bundle = overrides.pop("llm_bundle", None)
    if bundle is None:
        bundle = build_from_env(os.environ, limiter=limiters)

    # 平台 webhook 签名密钥等配置，从环境变量收集（键名原样保留）
    settings = overrides.pop("settings", None)
    if settings is None:
        settings = {
            k: v for k, v in os.environ.items() if k.endswith("_WEBHOOK_SECRET") and v
        }

    # 鉴权：配了 JWT_SECRET 就默认进入 jwt 模式（安全默认），否则退回 dev 模式。
    jwt_secret = overrides.pop("jwt_secret", None) or os.environ.get("JWT_SECRET", "")
    auth_mode = (
        overrides.pop("auth_mode", None)
        or os.environ.get("AUTH_MODE", "").lower()
        or ("jwt" if jwt_secret else "dev")
    )
    if auth_mode == "dev":
        print(
            "[warn] AUTH_MODE=dev：正在使用可伪造的 X-* 身份头，仅供本地开发。"
            " 生产环境请设置 JWT_SECRET（或 AUTH_MODE=jwt）。"
        )

    return Container(
        session_factory=factory,
        engine=engine,
        limiters=limiters,
        retries=overrides.pop("retries", None) or RetryQueue(),
        llm=overrides.pop("llm", bundle.text),
        vision=overrides.pop("vision", bundle.vision),
        llm_bundle=bundle,
        settings=settings,
        jwt_secret=jwt_secret,
        auth_mode=auth_mode,
        **overrides,
    )


def create_app(
    container: Optional[Container] = None,
    *,
    cors_origins: Optional[str] = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        container = getattr(app.state, "container", None)
        if container is None:
            return
        # 大模型客户端持有 httpx 连接池，不关会留下未回收的 socket
        bundle = getattr(container, "llm_bundle", None)
        if bundle is not None:
            await bundle.aclose()
        engine = getattr(container, "engine", None)
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="多店铺智控平台",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=parse_origins(cors_origins or os.environ.get("CORS_ORIGINS")),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc), "retry_after": exc.retry_after},
            headers={"Retry-After": str(int(exc.retry_after) + 1)},
        )

    @app.exception_handler(KeyMaterialMissing)
    async def _missing_key(request: Request, exc: KeyMaterialMissing) -> JSONResponse:
        # 密钥没配是配置问题，不是用户的问题 —— 但也不能把细节吐给调用方
        return JSONResponse(status_code=503, content={"detail": "密钥服务不可用"})

    @app.exception_handler(RequestValidationError)
    async def _invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        """校验失败时**不回显原始输入值**。

        FastAPI 默认会把出错的原值整段放进 `input` 字段返回。但我们卡长度限制的
        那几个字段（buyer_id/msg_id/order_id）本身就可能长得离谱 —— 平台推一个
        5000 字的 ID 进来，默认行为会把它原样塞进响应和访问日志，一个小问题被
        放大成大问题（日志刷屏、响应体膨胀）。

        这里只保留字段路径和原因，去掉 `input`。
        """
        errors = []
        for err in exc.errors():
            item = {"loc": list(err.get("loc", ())), "msg": err.get("msg", "不合法"),
                    "type": err.get("type", "value_error")}
            limit = (err.get("ctx") or {}).get("max_length")
            if limit is not None:
                item["max_length"] = limit
            errors.append(item)
        return JSONResponse(status_code=422, content={"detail": errors})

    # 作为 uvicorn --factory 入口时，容器不会被传进来，这里按环境变量装配一个。
    #
    # 这一步曾经漏掉，结果是 `uvicorn backend.main:create_app --factory` 起来的
    # 服务能正常监听端口、日志也正常，但**一条路由都没注册**，所有请求都返回
    # 404 —— 属于"启动成功但完全不可用"的隐蔽故障，光看日志发现不了。
    if container is None:
        container = build_container()

    app.state.container = container
    app.include_router(build_router(container))
    return app


def main() -> None:  # pragma: no cover - 入口
    import uvicorn

    uvicorn.run(create_app(build_container()), host="0.0.0.0", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
