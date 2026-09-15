"""
本地大模型 Mock 服务（仅用于开发/验收，不需要真实 API key）
==========================================================

模拟一个 OpenAI 兼容的 /chat/completions 接口。它会从买家最后一条消息里
**提取对方报出的数字**，原样当成 AI 的报价返回 —— 这样就能在本地验证：

  - 议价管线真的会调大模型（usage.llm_calls > 0、cost > 0）；
  - 服务端底价硬校验生效：买家报低于地板价的数，后端会把整条决策
    判无效并回退到无报价兜底话术，AI 无法击穿底价。

用法：
  python mock_llm_server.py            # 默认 :9099
  LLM_API_KEY=dummy LLM_BASE_URL=http://127.0.0.1:9099 \
  python -m uvicorn backend.main:create_app --factory --port 8000

注意：这只是回显买家数字的玩具，真实部署请换成 DeepSeek / Qwen 等。
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _extract_price(text: str) -> float | None:
    matches = PRICE_RE.findall(text or "")
    for m in matches:
        val = float(m)
        if val > 0:
            return val
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib 命名
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send({"error": "not found"}, status=404)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            self._send({"error": "bad json"}, status=400)
            return

        # 取最后一条 user 消息
        last_user = ""
        system_text = ""
        for msg in req.get("messages", []):
            if msg.get("role") == "user":
                last_user = msg.get("content", "") or ""
            elif msg.get("role") == "system":
                system_text = msg.get("content", "") or ""

        # 多模态鉴真请求：系统提示里带"鉴真"，返回鉴真结构
        if "鉴真" in system_text or "risky" in system_text:
            # 标题里带"瑕疵/划痕/非原装"等词就判风险，否则无风险
            risky = any(w in str(last_user) for w in ("划痕", "磕碰", "非原装", "副厂", "兼容"))
            verdict = {
                "risky": risky,
                "labels": ["隐性瑕疵"] if risky else [],
                "confidence": 0.8 if risky else 0.9,
                "note": "检测到被淡化的瑕疵" if risky else "未见明显风险",
            }
            self._send({
                "choices": [{
                    "message": {"role": "assistant",
                                "content": json.dumps(verdict, ensure_ascii=False)},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 800, "completion_tokens": 40},
            })
            return

        price = _extract_price(last_user)

        if price is not None:
            decision = {
                "reply": f"亲，¥{price:g} 可以给您，直接拍就行～",
                "offered_price": price,
                "intent": "BARGAIN",
            }
        else:
            decision = {
                "reply": "这款随时能发，您拍下我马上给您卡密～",
                "offered_price": None,
                "intent": "ENQUIRY",
            }

        self._send({
            "choices": [{
                "message": {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })

    def log_message(self, *args) -> None:  # 静音
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[mock-llm] listening on http://127.0.0.1:{port}/chat/completions")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
