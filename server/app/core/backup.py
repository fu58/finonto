"""数据备份与恢复。

为什么要自己做而不是 ``cp var/finonto.db``
--------------------------------------------
SQLite 在 WAL 模式下，主库文件里可能缺最近提交的事务（数据还在 -wal 里）。
``sqlite3.Connection.backup()`` 是官方在线备份 API，会做一致性快照，**不用停服务**。

备份内容：``finonto.db`` + ``objects/``（原始文档）+ ``components.json``（组件选择）。
恢复方式写进 ``restore_hint()``，并附在备份包里，避免「半年后没人记得怎么还原」。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..models import utcnow

RESTORE_HINT = """FinOnto 备份恢复步骤
====================

1. 停止服务（Ctrl+C 或 systemctl stop finonto）；
2. 备份当前数据目录（防止覆盖后无法回退）：
     mv server/var server/var.before-restore
3. 解压备份包到 server/var（保持目录结构）：
     mkdir -p server/var
     unzip finonto-backup-YYYYmmdd-HHMMSS.zip -d server/var
4. 确认 server/var/finonto.db 与 server/var/objects/ 存在；
5. 启动服务，访问 /api/ready 应返回 ok；
6. 抽查：/api/spaces 应返回恢复时的空间数量，Wiki 页面版本号连续。

注意
----
* 若换过 embedding 模型，恢复后需要到「文档入库」逐篇重建索引；
* 组件选择保存在 components.json，恢复后组件组合会回到备份时状态；
* 恢复前请确认没有其他进程在写同一个数据目录（SQLite 单写者）。
"""


def _sqlite_backup(source: Path, target: Path) -> bool:
    """用官方在线备份 API 做一致性快照（WAL 下也安全）。

    注意：``with sqlite3.connect(...)`` 只提交事务、**不关闭连接**，
    Windows 上未关闭的连接会让后续删除临时文件失败（WinError 32）。
    """
    if not source.is_file():
        return False
    src = dst = None
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        src.backup(dst)
        dst.commit()
        return target.is_file() and target.stat().st_size > 0
    except sqlite3.Error:
        return False
    finally:
        for conn in (src, dst):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


def create_backup(*, note: str = "", session=None) -> dict[str, Any]:
    settings = get_settings()
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"finonto-backup-{stamp}.zip"
    target = settings.backups_dir / name

    db_path = settings.db_path
    is_sqlite = settings.resolved_database_url().startswith("sqlite")
    temp_db = settings.backups_dir / f".{stamp}.db"

    files: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if is_sqlite and _sqlite_backup(db_path, temp_db):
            archive.write(temp_db, "finonto.db")
            files.append("finonto.db")
        objects = settings.objects_dir
        if objects.is_dir():
            for file in objects.rglob("*"):
                if file.is_file():
                    archive.write(file, str(Path("objects") / file.relative_to(objects)))
                    files.append("objects/…")
        components = settings.components_path
        if components.is_file():
            archive.write(components, "components.json")
            files.append("components.json")
        archive.writestr("RESTORE.md", RESTORE_HINT)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "created_at": utcnow().isoformat(timespec="seconds"),
                    "note": note,
                    "database_url": settings.resolved_database_url(),
                    "files": sorted(set(files)),
                    "app_version": "0.1.0",
                    "llm": settings.llm_provider,
                    "embedding": settings.embedding_provider,
                    "vector_store": settings.vector_store,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    if temp_db.exists():
        temp_db.unlink(missing_ok=True)

    size = target.stat().st_size
    return {
        "status": "created",
        "name": name,
        "path": str(target),
        "bytes": size,
        "files": sorted(set(files)),
        "restore_hint": "备份包内含 RESTORE.md",
    }


def list_backups() -> list[dict[str, Any]]:
    settings = get_settings()
    directory = settings.backups_dir
    if not directory.is_dir():
        return []
    items = []
    for file in sorted(directory.glob("finonto-backup-*.zip"), reverse=True):
        stat = file.stat()
        items.append(
            {
                "name": file.name,
                "bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
            }
        )
    return items


def delete_backup(name: str) -> dict[str, Any]:
    settings = get_settings()
    safe_name = Path(name).name  # 防目录穿越
    target = settings.backups_dir / safe_name
    if not target.is_file() or not safe_name.endswith(".zip"):
        return {"status": "missing", "name": safe_name}
    target.unlink()
    return {"status": "deleted", "name": safe_name}


def restore_hint() -> str:
    return RESTORE_HINT


def cleanup_dir() -> int:
    """把备份目录里的临时文件清掉（异常退出可能残留）。"""
    settings = get_settings()
    removed = 0
    for file in settings.backups_dir.glob(".*.db"):
        try:
            file.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def copy_tree(src: Path, dst: Path) -> int:  # pragma: no cover - 运维小工具
    if not src.is_dir():
        return 0
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return sum(1 for _ in dst.rglob("*") if _.is_file())
