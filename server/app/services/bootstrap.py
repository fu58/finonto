"""冷启动编排：领域包安装 + 示例文档入库 + Wiki 首次编译，供 HTTP 接口与冒烟脚本共用。

全部步骤幂等：已存在的空间/类型/同名文档会被跳过，可以反复调用；
`pack_keys` 决定装哪几个领域包，省略时装入全部内置领域包（金融 / 设备维保 / 人工智能 / 计算机网络 / 通用）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.ontology.packs import all_packs, install_pack, require_pack, sample_manifest
from ..models import AgentRun, ChangeProposal, Chunk, DocumentRecord, MemoryItem, ObjectType, Space, WikiPage


def sample_manifest_all(server_root: Path) -> list[dict[str, Any]]:
    """所有内置领域包示例语料的合并清单，前端「载入示例」直接消费。"""
    rows: list[dict[str, Any]] = []
    for pack in all_packs():
        rows.extend(sample_manifest(pack, server_root))
    return rows


def pack_keys_for(pack_keys: list[str] | None) -> list[str]:
    """None 表示全部内置领域包；否则校验每个 key 都已注册。"""
    if pack_keys is None:
        return [pack.key for pack in all_packs()]
    return [require_pack(key).key for key in pack_keys]


def counters(session: Session) -> dict[str, Any]:
    def count(model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(session.scalar(stmt) or 0)

    return {
        "spaces": count(Space),
        "object_types": count(ObjectType),
        "documents": count(DocumentRecord),
        "chunks": count(Chunk),
        "proposals": count(ChangeProposal),
        "pending_proposals": count(ChangeProposal, ChangeProposal.status == "pending"),
        "wiki_pages": count(WikiPage),
        "agent_runs": count(AgentRun),
        "memory_items": count(MemoryItem),
    }


def bootstrap(
    session: Session,
    root: Path,
    *,
    pack_keys: list[str] | None = None,
    seed_ontology: bool = True,
    load_samples: bool = True,
    compile_wiki: bool = True,
) -> dict[str, Any]:
    """把指定领域包装进各自的本体空间，返回逐包报告；`pack_keys=None` 表示全部内置领域包。"""
    reports: list[dict[str, Any]] = []
    for key in pack_keys_for(pack_keys):
        pack = require_pack(key)
        report = install_pack(
            session,
            pack,
            root,
            load_samples=load_samples,
            compile_wiki=compile_wiki,
            seed_ontology=seed_ontology,
        )
        session.commit()
        reports.append(report)
    return {
        "packs": [
            {k: r[k] for k in ("pack", "pack_label", "space_id", "space_name") if k in r} for r in reports
        ],
        "reports": reports,
        "counters": counters(session),
    }
