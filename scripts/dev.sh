#!/usr/bin/env bash
# FinOnto：一条命令跑起来（macOS / Linux）
#   bash scripts/dev.sh            # 默认 8000
#   PORT=8080 bash scripts/dev.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/server"
PORT="${PORT:-8000}"
PROBE='import fastapi, sqlalchemy, jinja2, numpy'

test_python() {
  [ -n "${1:-}" ] && "$1" -c "$PROBE" >/dev/null 2>&1
}

resolve_python() {
  if test_python "$SERVER/.venv/bin/python"; then echo "$SERVER/.venv/bin/python"; return; fi
  for candidate in python3 python; do
    if test_python "$candidate"; then echo "$candidate"; return; fi
  done
  echo ""
}

echo "FinOnto · 启动中"
echo "  仓库：$ROOT"

cd "$SERVER"
PYTHON="$(resolve_python)"

if [ -z "$PYTHON" ]; then
  if [ ! -x ".venv/bin/python" ]; then
    echo "  创建虚拟环境 server/.venv ..."
    python3 -m venv .venv 2>/dev/null || true
  fi
  echo "  安装依赖 server/requirements.txt ..."
  if [ -x ".venv/bin/python" ]; then
    python3 -m pip --disable-pip-version-check --python .venv/bin/python install -r requirements.txt \
      || .venv/bin/python -m pip install -r requirements.txt || true
  else
    python3 -m pip --disable-pip-version-check install -r requirements.txt || true
  fi
  PYTHON="$(resolve_python)"
fi

if [ -z "$PYTHON" ]; then
  cat <<EOF

  依赖未就绪。请手工执行：
    cd server
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

EOF
  exit 1
fi

echo "  使用 Python：$PYTHON"
export PYTHONPATH="$SERVER"
echo ""
echo "  打开 http://127.0.0.1:$PORT"
echo "  首次启动会自动装入 5 个领域包（金融 / 设备维保 / 人工智能 / 网络 / 通用）"
echo ""
exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
