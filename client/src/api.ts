/** 与本地 FastAPI 服务通信的统一入口：JSON 请求 + SSE 事件流。 */

export const API_BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail ?? parsed);
    } catch {
      /* 保留原始文本 */
    }
    throw new Error(`HTTP ${response.status} · ${detail.slice(0, 400)}`);
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? "{}" : JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T>(path: string, form: FormData) =>
    fetch(`${API_BASE}${path}`, { method: "POST", body: form }).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status} · ${(await r.text()).slice(0, 300)}`);
      return (await r.json()) as T;
    }),
};

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export type StreamEvent = Record<string, unknown> & { type: string };

/** 逐帧解析 SSE：帧以空行分隔，data 行是带 type 字段的 JSON。 */
export async function* streamSSE(path: string, signal?: AbortSignal): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status} · ${await response.text()}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    for (;;) {
      const boundary = buffer.indexOf("\n\n");
      if (boundary < 0) break;
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      try {
        yield JSON.parse(dataLine.slice(5).trim()) as StreamEvent;
      } catch {
        /* 忽略不完整帧 */
      }
    }
  }
}
