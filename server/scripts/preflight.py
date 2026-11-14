"""上线前检查清单（可执行版）。

    cd server
    .venv/Scripts/python.exe scripts/preflight.py
    .venv/Scripts/python.exe scripts/preflight.py --strict   # 警告也算失败

退出码：0 = 可以上线（或只有 warning），1 = 存在 fail，2 = 有 warning 且 --strict。
同一套检查在界面「运维 · 备份」页与 ``GET /api/system/diagnostics`` 也能看到。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from app.config import get_settings  # noqa: E402
from app.core import diagnostics  # noqa: E402
from app.db import init_db  # noqa: E402

ICONS = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
COLORS = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"}
RESET = "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(description="FinOnto 上线前检查")
    parser.add_argument("--strict", action="store_true", help="warning 也视为失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    settings = get_settings()
    init_db()

    result = diagnostics.run_checks()
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1

    print("=" * 70)
    print("FinOnto 上线前检查")
    print(f"  数据目录：{settings.data_path}")
    print(f"  数据库  ：{settings.resolved_database_url()}")
    print(f"  认证    ：{'开启' if settings.auth_enabled else '关闭（演示模式）'}")
    print("=" * 70)

    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    for item in result["checks"]:
        status = item["status"]
        label = ICONS.get(status, status)
        line = f"{label} {item['title']}：{item['detail']}"
        if color:
            line = f"{COLORS.get(status, '')}{line}{RESET}"
        print(line)
        if item.get("hint"):
            print(f"       → {item['hint']}")

    print("-" * 70)
    counts = result["counts"]
    print(f"结果：ok={counts.get('ok', 0)} warning={counts.get('warn', 0)} fail={counts.get('fail', 0)}")
    if result["ready"]:
        print("结论：主干检查通过，可以上线。" if not args.strict or counts.get("warn", 0) == 0
              else "结论：主干通过，但存在警告项（--strict 下视为不通过）。")
    else:
        print(f"结论：存在阻塞项：{', '.join(result['blocking'])}")

    if not result["ready"]:
        return 1
    if args.strict and counts.get("warn", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
