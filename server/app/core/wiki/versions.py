"""Wiki 版本链：diff（行级 + 并排）与非破坏式回滚。

三条硬约束
----------
1. **只追加**：任何修改都产生新 ``WikiRevision``，历史版本永不删除；
2. **回滚 = 再追加**：回滚到 v3 会产生 v7（内容同 v3），而不是删除 v4-v6；
3. **锁定页不可被编译覆盖**：回滚只改内容，不改锁定状态。
"""

from __future__ import annotations

import difflib
from typing import Any

from sqlalchemy import select

from ...models import WikiPage, WikiRevision
from .compiler import digest_of


def list_revisions(session, page_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(WikiRevision).where(WikiRevision.page_id == page_id).order_by(WikiRevision.version.desc())
    ).scalars().all()
    return [
        {
            "id": item.id,
            "version": item.version,
            "title": item.title,
            "digest": item.digest,
            "author": item.author,
            "message": item.message,
            "created_at": item.created_at.isoformat(sep=" ", timespec="seconds"),
            "size": len(item.body),
        }
        for item in rows
    ]


def get_revision(session, page_id: int, version: int) -> WikiRevision | None:
    return session.execute(
        select(WikiRevision).where(WikiRevision.page_id == page_id, WikiRevision.version == version)
    ).scalars().first()


def diff_versions(session, page_id: int, left: int, right: int) -> dict[str, Any]:
    """行级 unified diff + 并排视图（前端两种视图共用这一份数据）。"""
    left_rev = get_revision(session, page_id, left)
    right_rev = get_revision(session, page_id, right)
    if left_rev is None or right_rev is None:
        return {"status": "missing", "left": left, "right": right}

    left_lines = left_rev.body.splitlines()
    right_lines = right_rev.body.splitlines()
    unified = list(
        difflib.unified_diff(
            left_lines, right_lines, fromfile=f"v{left}", tofile=f"v{right}", lineterm=""
        )
    )
    side_by_side = list(
        difflib.ndiff(left_lines, right_lines)
    )
    added = sum(1 for line in unified if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in unified if line.startswith("-") and not line.startswith("---"))

    return {
        "status": "ok",
        "page_id": page_id,
        "left": {"version": left, "digest": left_rev.digest, "author": left_rev.author,
                 "created_at": left_rev.created_at.isoformat(sep=" ", timespec="seconds")},
        "right": {"version": right, "digest": right_rev.digest, "author": right_rev.author,
                  "created_at": right_rev.created_at.isoformat(sep=" ", timespec="seconds")},
        "added": added,
        "removed": removed,
        "unified": unified,
        "side_by_side": side_by_side,
        "identical": left_rev.body == right_rev.body,
    }


def save_manual_edit(
    session,
    page_id: int,
    body: str,
    *,
    author: str = "manual",
    message: str = "人工编辑",
    title: str | None = None,
) -> dict[str, Any]:
    """人工编辑：同样追加版本，并保留 ``is_locked``。"""
    page = session.get(WikiPage, page_id)
    if page is None:
        return {"status": "missing", "page_id": page_id}
    if page.body == body and (title is None or title == page.title):
        return {"status": "unchanged", "page": page.id, "version": page.version}

    page.version += 1
    page.body = body
    if title is not None and title.strip():
        page.title = title.strip()
    page.is_dirty = False
    page.status = "published"
    revision = WikiRevision(
        page_id=page.id,
        version=page.version,
        title=page.title,
        body=body,
        digest=digest_of(body),
        author=author,
        message=message,
    )
    session.add(revision)
    session.commit()
    return {"status": "saved", "page": page.id, "version": page.version, "revision_id": revision.id}


def rollback(session, page_id: int, target_version: int, *, actor: str = "manual") -> dict[str, Any]:
    """非破坏式回滚：读取目标版本内容，追加为新版本。"""
    page = session.get(WikiPage, page_id)
    if page is None:
        return {"status": "missing", "page_id": page_id}
    target = get_revision(session, page_id, target_version)
    if target is None:
        return {"status": "version_missing", "page_id": page_id, "version": target_version}
    if target.body == page.body:
        return {"status": "unchanged", "page_id": page_id, "version": page.version}

    page.version += 1
    new_version = page.version
    page.body = target.body
    page.title = target.title
    page.is_dirty = False
    revision = WikiRevision(
        page_id=page.id,
        version=new_version,
        title=page.title,
        body=target.body,
        digest=target.digest,
        author="rollback",
        message=f"回滚到 v{target_version}（操作人 {actor}）",
    )
    session.add(revision)
    session.commit()
    return {
        "status": "rolled_back",
        "page_id": page.id,
        "from_version": new_version - 1,
        "target_version": target_version,
        "new_version": new_version,
        "revision_id": revision.id,
    }


def toggle_lock(session, page_id: int, locked: bool) -> dict[str, Any]:
    page = session.get(WikiPage, page_id)
    if page is None:
        return {"status": "missing", "page_id": page_id}
    page.is_locked = bool(locked)
    session.commit()
    return {"status": "locked" if page.is_locked else "unlocked", "page_id": page.id}
