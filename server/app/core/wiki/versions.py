"""Wiki 版本管理：列表、读取、diff 与回滚。

回滚采用「非破坏式」策略：把历史版本内容作为一个新版本追加，保留完整审计链，
因此任何一次误编辑都可以再次回滚。
"""

from __future__ import annotations

import difflib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import WikiPage, WikiRevision
from .compiler import _revision_view


class VersionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def revisions(self, page_id: int) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(WikiRevision).where(WikiRevision.page_id == page_id).order_by(WikiRevision.version_no.desc())
        ).all()
        return [_revision_view(r) for r in rows]

    def get_revision(self, page_id: int, version_no: int) -> WikiRevision | None:
        return self.session.scalar(
            select(WikiRevision).where(
                WikiRevision.page_id == page_id, WikiRevision.version_no == version_no
            )
        )

    def diff(self, page_id: int, left: int, right: int | None = None) -> dict[str, Any]:
        page = self.session.get(WikiPage, page_id)
        if page is None:
            raise KeyError(str(page_id))
        left_rev = self.get_revision(page_id, left)
        right_rev = self.get_revision(page_id, right) if right else self.get_revision(page_id, page.current_version)
        if left_rev is None or right_rev is None:
            raise KeyError("版本不存在")
        left_lines = (left_rev.body_md or "").splitlines(keepends=True)
        right_lines = (right_rev.body_md or "").splitlines(keepends=True)
        unified = list(difflib.unified_diff(left_lines, right_lines, fromfile=f"v{left}", tofile=f"v{right_rev.version_no}", n=2))
        added = sum(1 for line in unified if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in unified if line.startswith("-") and not line.startswith("---"))
        return {
            "page_slug": page.slug,
            "left": _revision_view(left_rev),
            "right": _revision_view(right_rev),
            "unified_diff": "".join(unified),
            "added_lines": added,
            "removed_lines": removed,
            "side_by_side": _side_by_side(left_lines, right_lines),
        }

    def rollback(self, page_id: int, to_version: int, *, actor: str = "human", note: str = "") -> dict[str, Any]:
        page = self.session.get(WikiPage, page_id)
        if page is None:
            raise KeyError(str(page_id))
        target = self.get_revision(page_id, to_version)
        if target is None:
            raise KeyError(f"版本 v{to_version} 不存在")
        latest = self.session.scalar(
            select(WikiRevision).where(WikiRevision.page_id == page_id).order_by(WikiRevision.version_no.desc())
        )
        if latest and latest.body_md == target.body_md:
            return {"rolled_back": False, "message": f"当前内容已与 v{to_version} 一致，无需回滚", "page_id": page_id}
        revision = WikiRevision(
            page_id=page_id,
            version_no=(latest.version_no + 1) if latest else 1,
            body_md=target.body_md,
            summary=target.summary,
            author=actor,
            change_note=note or f"回滚至 v{to_version}",
        )
        self.session.add(revision)
        page.current_version = revision.version_no
        page.dirty = False
        self.session.flush()
        return {
            "rolled_back": True,
            "page_id": page_id,
            "from_version": latest.version_no if latest else 0,
            "to_content_version": to_version,
            "new_version": revision.version_no,
            "revision": _revision_view(revision),
        }

    def delete_revision(self, page_id: int, version_no: int) -> bool:
        """仅允许删除非当前版本，防止破坏审计链。"""
        page = self.session.get(WikiPage, page_id)
        revision = self.get_revision(page_id, version_no)
        if page is None or revision is None:
            return False
        if version_no == page.current_version:
            raise ValueError("不能删除当前版本，请先回滚到其他版本")
        self.session.delete(revision)
        self.session.flush()
        return True


def _side_by_side(left: list[str], right: list[str], *, limit: int = 240) -> list[dict[str, str]]:
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    rows: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append({"kind": "equal", "left": left[i1 + offset].rstrip("\n"), "right": right[j1 + offset].rstrip("\n")})
                if len(rows) >= limit:
                    return rows
            continue
        max_len = max(i2 - i1, j2 - j1)
        for offset in range(max_len):
            rows.append(
                {
                    "kind": tag,
                    "left": left[i1 + offset].rstrip("\n") if i1 + offset < i2 else "",
                    "right": right[j1 + offset].rstrip("\n") if j1 + offset < j2 else "",
                }
            )
            if len(rows) >= limit:
                return rows
    return rows
