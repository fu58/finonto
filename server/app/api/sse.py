"""SSE 工具：手写事件帧，避免额外依赖。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_frame(event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_response(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    """把事件字典流封装成 text/event-stream；每个事件自带 type 字段。"""

    async def body() -> AsyncIterator[str]:
        try:
            async for event in events:
                yield sse_frame(str(event.get("type", "message")), event)
        except Exception as exc:  # noqa: BLE001  流内异常要作为事件送出，否则客户端只能看到连接中断
            yield sse_frame("error", {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
