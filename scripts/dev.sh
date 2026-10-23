#!/usr/bin/env bash
# 本地开发：一条命令拉起后端 (8000) 与前端 Vite dev (5173)。
# 浏览器打开 http://localhost:5173 即可（Vite 已配置 /api 代理到 8000）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$ROOT/server/.venv" ]; then
  echo "[dev] 创建后端虚拟环境…"
  python3 -m venv "$ROOT/server/.venv"
fi
PY="$ROOT/server/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/server/.venv/Scripts/python.exe"

echo "[dev] 安装后端依赖…"
"$PY" -m pip install -q -r "$ROOT/server/requirements.txt"
echo "[dev] 安装前端依赖…"
(cd "$ROOT/client" && npm install --silent)

"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir "$ROOT/server" &
BACK=$!
(cd "$ROOT/client" && npm run dev) &
FRONT=$!

trap 'kill $BACK $FRONT 2>/dev/null || true' EXIT INT TERM
echo "[dev] 后端 http://127.0.0.1:8000  ·  前端 http://localhost:5173  （Ctrl+C 退出）"
wait
