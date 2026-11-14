"""SSE 编码工具。

格式：每条事件一行 ``data: <json>\\n\\n``（不自定义 event 名，前端只需一个 onmessage）。
为什么用 SSE 而不是 WebSocket：本项目是单向推送（服务端→浏览器），SSE 走普通 HTTP，
代理/网关配置简单，断线由浏览器自动重连。
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterable, Iterator

from starlette.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲，否则 token 会被攒着一次性下发
}


def encode(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def sse_response(events: Iterable[dict]) -> StreamingResponse:
    def generator() -> Iterator[str]:
        try:
            for event in events:
                yield encode(event)
        except Exception as exc:  # noqa: BLE001 - 流中异常也要变成一条事件
            yield encode({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)
