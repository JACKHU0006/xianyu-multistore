FROM python:3.11-slim

WORKDIR /app

# 刻意不安装 xvfb / chromium。
#
# 原方案打算在云端用无头浏览器跑平台操作，但 Render 免费实例只有 512MB 内存，
# 一个 Chromium 实例就要 300-500MB，多店并行必然 OOM。所以浏览器相关操作已经
# 下沉到店主本机，云端只做 AI 决策、数据与调度 —— 镜像因此能保持很小，
# 冷启动也快得多。

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
