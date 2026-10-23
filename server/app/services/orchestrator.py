"""编排层：把「问答 → 本体草稿 → 提案 → 人工确认 → Wiki → 通知」串成一条可复用的流程。

路由只做 HTTP 编解码，跨模块的顺序与副作用集中在这一层，便于替换成任务队列。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..core.ontology.packs import pack_for_space
from ..core.ontology.service import OntologyService, ProposalResult
from ..core.rag.citations import build_context_block
from ..core.rag.pipeline import RagPipeline
from ..core.wiki.compiler import WikiCompiler
from ..models import ChangeProposal
from ..providers.base import ChatMessage
from ..providers.registry import registry


class OntologyDraftService:
    """问答驱动的提案：从检索证据里归纳候选对象类型，落成待确认提案。"""

    def __init__(self, session: Session, space_id: int) -> None:
        self.session = session
        self.space_id = space_id
        self.service = OntologyService(session)
        self.pack = pack_for_space(session, space_id)

    def _prompt(self, question: str, candidates: list[Any]) -> list[ChatMessage]:
        context_block, _ = build_context_block(candidates)
        existing = "\n".join(
            f"- {row.label}：{row.definition or ''}"
            for row in self.service.list_types(self.space_id, limit=400)
        )
        system = (
            "[[TASK:ontology_draft]]\n"
            f"你是「{self.pack.label}」领域的本体建模助手。只依据给定的资料块归纳对象类型，"
            "不得虚构制度或标准数值；每个候选概念都要指出支撑它的资料编号。\n\n"
            + self.pack.prompt_context()
        )
        user = (
            f"## 目标\n根据以下问题与资料，提出可加入 {self.pack.label} 领域本体的对象类型、属性与关联候选。\n\n"
            f"## 用户问题\n{question}\n\n"
            f"## 已有对象类型\n{existing or '（空）'}\n\n"
            f"## 资料\n{context_block}\n\n"
            '请输出 JSON：{"object_types":[{"label","definition","kind","business_domain",'
            '"confidence","aliases","tags","evidence"}],"relations":[{"source","target","relation",'
            '"description"}],"notes"}'
        )
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _align(self, draft: dict[str, Any]) -> dict[str, Any]:
        """把模型给出的子域与标签收敛到领域包的受控取值上。"""
        for item in draft.get("object_types", []) or []:
            haystack = f"{item.get('label', '')} {item.get('definition', '')}"
            domain = str(item.get("business_domain") or "")
            if domain not in self.pack.business_domains:
                item["business_domain"] = self.pack.guess_domain(haystack)
            if not item.get("tags"):
                item["tags"] = self.pack.guess_tags(haystack)
        return draft

    async def draft(self, question: str, *, final_k: int | None = None) -> dict[str, Any]:
        """检索 → 生成草稿 JSON（不落库），供前端预览后再决定是否提案。"""
        understood, candidates, strategy, web_results = await RagPipeline(self.session).prepare(
            question, self.space_id, final_k=final_k
        )
        if not candidates:
            return {
                "question": question,
                "understood": understood.to_dict(),
                "draft": {"object_types": [], "relations": [], "notes": "本地知识库没有可用资料，无法归纳候选概念。"},
                "evidence": [],
                "strategy": strategy,
                "web": web_results,
            }
        text, _usage = await registry.llm().complete(self._prompt(question, candidates), task="ontology_draft", json_mode=True)
        draft = self._align(_parse_json(text))
        evidence_index = {c.ref: c for c in candidates}
        return {
            "question": question,
            "understood": understood.to_dict(),
            "draft": draft,
            "evidence": [c.as_dict() for c in candidates],
            "evidence_refs": list(evidence_index),
            "strategy": strategy,
            "web": [r.__dict__ for r in web_results],
        }

    async def propose(self, question: str, *, final_k: int | None = None, reviewer_hint: str = "问答驱动生成") -> dict[str, Any]:
        """问答 → 草稿 → ChangeProposal（等待人工确认）。"""
        result = await self.draft(question, final_k=final_k)
        draft = result["draft"]
        proposals: list[dict[str, Any]] = []
        for item in draft.get("object_types", []):
            payload = _type_payload(item)
            issues = self.service.validate_type_payload(payload, self.space_id)
            if any(i.code == "duplicate_label" for i in issues):
                continue
            proposal = self.service.create_proposal(
                self.space_id,
                kind="object_type",
                payload=payload,
                rationale=item.get("rationale") or f"{reviewer_hint}：{result['question']}",
                confidence=float(item.get("confidence") or 0.5),
                evidences=_evidences(item, result["evidence"]),
                title=f"新增对象类型「{payload['label']}」",
            )
            proposals.append(self.service.proposal_view(proposal))
        for link in draft.get("relations", []):
            source = self.service.find_type_by_label(self.space_id, link.get("source", ""))
            target = self.service.find_type_by_label(self.space_id, link.get("target", ""))
            if source is None or target is None:
                continue
            proposal = self.service.create_proposal(
                self.space_id,
                kind="link",
                payload={
                    "source": source.label,
                    "target": target.label,
                    "relation": link.get("relation", "关联"),
                    "cardinality": link.get("cardinality", "n:m"),
                    "description": link.get("description", ""),
                    "category": link.get("category", "association"),
                },
                rationale=f"问答驱动的关联发现：{result['question']}",
                confidence=float(link.get("confidence") or 0.5),
                title=f"新增关联 {source.label} —{link.get('relation', '关联')}→ {target.label}",
            )
            proposals.append(self.service.proposal_view(proposal))
        self.session.commit()
        return {**result, "proposals": proposals}


def after_accept(session: Session, space_id: int, proposal: ChangeProposal, result: ProposalResult, *, notify: bool = True) -> dict[str, Any]:
    """提案确认后的副作用：增量重编译 Wiki，并按配置的 IM 渠道推送变更通知。"""
    compiler = WikiCompiler(session)
    report = compiler.compile_space(space_id, only_dirty=True)
    session.commit()
    notify_result: dict[str, Any] | None = None
    if notify:
        channel = registry.im()
        payload = {
            "proposal_id": proposal.id,
            "title": proposal.title,
            "kind": proposal.kind,
            "created_types": result.created_types,
            "wiki_pages": report.pages,
        }
        try:
            delivered = channel.send("ontology-reviewers", json.dumps(payload, ensure_ascii=False), title="本体提案已确认")
        except Exception as exc:  # noqa: BLE001  通知失败不应回滚已确认的提案
            delivered = {"ok": False, "error": str(exc)[:200]}
        notify_result = {"channel": getattr(channel, "name", "console"), "result": delivered}
    return {
        "compile": {
            "created": report.created,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "skipped_locked": report.skipped_locked,
        },
        "notify": notify_result,
    }


def _type_payload(item: dict[str, Any]) -> dict[str, Any]:
    from ..core.agent.tools import _as_list

    return {
        "label": str(item.get("label", "")).strip(),
        "label_en": item.get("label_en", ""),
        "definition": item.get("definition", ""),
        "kind": item.get("kind", "class"),
        "business_domain": item.get("business_domain", "通用"),
        "parent_label": item.get("parent_label", ""),
        "aliases": _as_list(item.get("aliases")),
        "tags": _as_list(item.get("tags")),
        "annotations": item.get("annotations") or {},
        "properties": item.get("properties") or [],
        "relations": item.get("relations") or [],
        "constraints": item.get("constraints") or [],
    }


def _evidences(item: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把草稿里的 evidence 文本回溯到具体 chunk 定位。"""
    quote = str(item.get("evidence") or item.get("definition") or "")
    if not quote:
        return []
    from ..core.text import overlap

    best = max(candidates, key=lambda c: overlap(quote, c.get("preview", "")), default=None)
    if best is None:
        return []
    return [
        {
            "chunk_id": best["chunk_id"],
            "doc_id": best["doc_id"],
            "locator": best.get("locator", ""),
            "quote": quote[:240],
            "field_path": "definition",
            "score": best.get("final_score", 0.0),
        }
    ]


def _parse_json(text: str) -> dict[str, Any]:
    import re

    fences = re.findall(r"```json\s*(.*?)```", text or "", re.S)
    raw = fences[-1] if fences else ""
    if not raw:
        start, end = (text or "").find("{"), (text or "").rfind("}")
        raw = text[start : end + 1] if start >= 0 and end > start else ""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}
