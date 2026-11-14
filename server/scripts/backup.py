"""命令行备份 / 恢复提示。

    cd server
    .venv/Scripts/python.exe scripts/backup.py                 # 立即备份
    .venv/Scripts/python.exe scripts/backup.py --list          # 列出备份
    .venv/Scripts/python.exe scripts/backup.py --note "上线前"  # 带备注
    .venv/Scripts/python.exe scripts/backup.py --restore-hint  # 打印恢复步骤

建议配成计划任务（Windows）或 cron：每天 02:00 跑一次，保留最近 14 份。
注意：本脚本不需要服务在跑，但**恢复时**必须停服（SQLite 单写者）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from app.config import get_settings  # noqa: E402
from app.core import backup  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="FinOnto 备份工具")
    parser.add_argument("--note", default="", help="备份备注")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--restore-hint", action="store_true", help="打印恢复步骤")
    parser.add_argument("--keep", type=int, default=0, help="只保留最近 N 份，其余删除")
    args = parser.parse_args()

    if args.restore_hint:
        print(backup.restore_hint())
        return 0

    if args.list:
        items = backup.list_backups()
        if not items:
            print("还没有备份。")
            return 0
        for item in items:
            print(f"{item['name']:<42} {item['bytes'] / 1024:>9.1f} KB  {item['created_at']}")
        return 0

    settings = get_settings()
    init_db()
    result = backup.create_backup(note=args.note or "cli")
    print(f"已创建备份：{result['name']}")
    print(f"  路径：{result['path']}")
    print(f"  大小：{result['bytes'] / 1024:.1f} KB")
    print(f"  内容：{', '.join(result['files']) or '（空）'}")
    print(f"  恢复：{settings.backups_dir}\\{result['name']} 内附 RESTORE.md")

    if args.keep > 0:
        items = backup.list_backups()
        for item in items[args.keep:]:
            backup.delete_backup(item["name"])
            print(f"  已清理旧备份：{item['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
