"""工具注册表：Agent 的可执行动作面。

工具分四类风险：`read`（只读）、`write`（写提案，不直接改正式本体）、
`exec`（沙箱执行）、`network`（出网）。所有写操作只产生 ChangeProposal，
由人工确认后入库 —— 这是「引导 Agent」和「自动落库」之间的安全边界。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AgentRun, Chunk, DocumentRecord, ObjectType, WikiPage
from ...providers.base import ChatMessage
from ...providers.registry import registry
from ...providers.search.web import NoopWebSearch
from ..ontology.owl import export_bundle, to_json_schema, to_skos, to_turtle
from ..ontology.packs import lexicon_for
from ..ontology.service import OntologyService
from ..rag.generator import answer_once
from ..rag.query_understanding import understand
from ..rag.retriever import Candidate, HybridRetriever
from . import sandbox
from .mcp_client import mcp_manager
from .memory import MemoryService
from .skills import get_skill

Handler = Callable[["ToolContext", dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolContext:
    session: Session
    space_id: int
    run_id: int | None = None
    skill: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    evidence_by_chunk: dict[int, Candidate] = field(default_factory=dict)
    scratchpad: list[dict[str, Any]] = field(default_factory=list)

    def evidence_for(self, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """从最近检索到的片段中挑出能支撑该结论的证据。"""
        from ..text import overlap

        ranked = sorted(self.candidates, key=lambda c: -overlap(text, c.text))
        out: list[dict[str, Any]] = []
        for candidate in ranked[:limit]:
            if overlap(text, candidate.text) < 0.08 and not candidate.reasons:
                continue
            out.append(
                {
                    "chunk_id": candidate.chunk_id,
                    "doc_id": candidate.doc_id,
                    "locator": candidate.locator,
                    "quote": candidate.text[:240],
                    "score": candidate.final_score or candidate.fused_score,
                    "answer_id": f"run:{self.run_id}" if self.run_id else "",
                }
            )
        return out


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    handler: Handler
    category: str
    risk: str = "read"

    def render(self) -> str:
        params = ", ".join(
            f"{key}({spec.get('type', 'string')}{'/必填' if spec.get('required') else ''})"
            for key, spec in self.parameters.items()
        )
        return f"- {self.name}: {self.description} 参数: {params or '无'} [风险:{self.risk}]"

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk,
            "parameters": self.parameters,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, dict[str, Any]],
        handler: Handler,
        *,
        category: str,
        risk: str = "read",
    ) -> None:
        self._tools[name] = ToolSpec(name, description, parameters, handler, category, risk)

    def tools(self, allowed: list[str] | None = None) -> list[ToolSpec]:
        rows = list(self._tools.values())
        if allowed:
            expanded = set(allowed) | {name for name in self._tools if name.startswith("mcp.") and any(a.startswith("mcp.") for a in allowed)}
            rows = [t for t in rows if t.name in expanded]
        return rows

    def describe(self, allowed: list[str] | None = None) -> str:
        return "\n".join(tool.render() for tool in self.tools(allowed))

    def schemas(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools(allowed)]

    async def execute(self, ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            if name.startswith("mcp."):
                return await _mcp_fallback(ctx, name, arguments)
            return {"ok": False, "error": f"未知工具 {name}，请从可用工具列表中选择"}
        skill = get_skill(ctx.skill)
        if skill and tool.name not in skill.allowed_tools:
            return {"ok": False, "error": f"技能 {skill.name} 不允许调用 {tool.name}"}
        missing = [k for k, spec in tool.parameters.items() if spec.get("required") and not str(arguments.get(k, "")).strip()]
        if missing:
            return {"ok": False, "error": f"缺少必填参数: {', '.join(missing)}"}
        try:
            result = await tool.handler(ctx, arguments)
        except Exception as exc:  # noqa: BLE001  工具异常要变成 observation 而非中断整轮推理
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        return result

    def has(self, name: str) -> bool:
        return name in self._tools


registry_tools = ToolRegistry()
P = {"required": True}


def _param(**kwargs: Any) -> dict[str, Any]:
    base = {"type": "string", "required": False, "description": ""}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- 检索类工具
async def _rag_search(ctx: ToolContext, args: dict) -> dict:
    query = args["query"]
    top_k = int(args.get("top_k") or 6)
    lexicon = lexicon_for(ctx.session, ctx.space_id)
    understood = understand(
        query,
        known_types=[(t.id, t.label, t.definition or "") for t in ctx.session.scalars(select(ObjectType).where(ObjectType.space_id == ctx.space_id))],
        synonyms=lexicon["synonyms"],
        extra_suffixes=lexicon["term_suffixes"],
    )
    candidates = HybridRetriever(ctx.session).search(understood, ctx.space_id, final_k=top_k)
    ctx.candidates = candidates
    ctx.evidence_by_chunk = {c.chunk_id: c for c in candidates}
    return {
        "ok": True,
        "summary": f"检索到 {len(candidates)} 条片段",
        "intent": understood.intent,
        "expansions": understood.expansions[:6],
        "results": [
            {
                "ref": c.ref,
                "chunk_id": c.chunk_id,
                "doc": c.meta.get("doc_name", ""),
                "locator": c.locator,
                "score": round(c.final_score or c.fused_score, 3),
                "text": c.text[:280],
            }
            for c in candidates
        ],
        "empty": not candidates,
        "reasoning_hint": (
            "无结果：本地知识库缺少相关材料，可调用 search.web 或先上传文档"
            if not candidates
            else "可基于以上片段调用 ontology.propose_type 生成候选对象类型"
        ),
    }


async def _rag_read_chunk(ctx: ToolContext, args: dict) -> dict:
    chunk = ctx.session.get(Chunk, int(args["chunk_id"]))
    if chunk is None:
        return {"ok": False, "error": "片段不存在"}
    doc = ctx.session.get(DocumentRecord, chunk.doc_id)
    return {
        "ok": True,
        "chunk_id": chunk.id,
        "doc_id": chunk.doc_id,
        "doc_name": doc.name if doc else "",
        "locator": chunk.locator,
        "ordinal": chunk.ordinal,
        "text": chunk.text,
    }


async def _rag_ask(ctx: ToolContext, args: dict) -> dict:
    from ..rag.pipeline import RagPipeline

    pipeline = RagPipeline(ctx.session)
    understood, candidates, _strategy, _web = await pipeline.prepare(args["question"], ctx.space_id, final_k=int(args.get("top_k") or 6))
    payload = await answer_once(
        understood,
        candidates,
        session=ctx.session,
        answer_id=f"tool-{ctx.run_id}",
        domain_label=lexicon_for(ctx.session, ctx.space_id)["label"],
    )
    ctx.candidates = candidates
    ctx.evidence_by_chunk = {c.chunk_id: c for c in candidates}
    return {"ok": True, "answer": payload["answer"][:3000], "citations": payload["citations"][:10], "coverage": payload["coverage"]}


# ---------------------------------------------------------------- 文档类工具
async def _doc_list(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.session.scalars(select(DocumentRecord).order_by(DocumentRecord.id.desc()).limit(int(args.get("limit") or 30))).all()
    out = []
    for doc in rows:
        chunk_count = ctx.session.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc.id)) or 0
        out.append(
            {
                "doc_id": doc.id,
                "name": doc.name,
                "format": doc.doc_format,
                "doc_type": doc.doc_type,
                "chunks": chunk_count,
                "status": doc.status,
                "authority": doc.authority,
                "space_id": doc.space_id,
            }
        )
    return {"ok": True, "summary": f"共 {len(out)} 份文档", "documents": out}


async def _doc_read(ctx: ToolContext, args: dict) -> dict:
    doc_id = int(args["doc_id"])
    limit = int(args.get("max_chars") or 4000)
    chunks = ctx.session.scalars(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.ordinal)).all()
    if not chunks:
        return {"ok": False, "error": "该文档没有已索引片段"}
    body: list[str] = []
    used = 0
    picked: list[Candidate] = []
    for chunk in chunks:
        piece = f"[{chunk.locator}] {chunk.text}"
        if used + len(piece) > limit:
            break
        body.append(piece)
        used += len(piece)
        candidate = Candidate(chunk_id=chunk.id, doc_id=doc_id, text=chunk.text, locator=chunk.locator, meta=chunk.meta or {}, reasons=["文档直读"])
        picked.append(candidate)
    ctx.candidates = picked
    ctx.evidence_by_chunk = {c.chunk_id: c for c in picked}
    doc = ctx.session.get(DocumentRecord, doc_id)
    return {"ok": True, "doc_id": doc_id, "doc_name": doc.name if doc else "", "chars": used, "text": "\n".join(body), "chunk_count": len(chunks)}


# ---------------------------------------------------------------- 本体类工具
async def _onto_list(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    rows = service.list_types(
        ctx.space_id,
        q=args.get("q") or None,
        status=args.get("status") or None,
        domain=args.get("domain") or None,
        limit=int(args.get("limit") or 60),
    )
    return {
        "ok": True,
        "summary": f"当前空间有 {len(rows)} 个对象类型",
        "stats": service.space_stats(ctx.space_id),
        "types": [
            {"id": t.id, "label": t.label, "label_en": t.label_en, "kind": t.kind, "status": t.status, "domain": t.business_domain, "definition": (t.definition or "")[:120]}
            for t in rows
        ],
    }


async def _onto_get(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    type_id = int(args["type_id"])
    if not service.get_type(type_id):
        return {"ok": False, "error": f"对象类型 {type_id} 不存在"}
    return {"ok": True, "detail": service.detail(type_id)}


async def _onto_find_similar(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    duplicates = service.similar_types(ctx.space_id, args["label"])
    return {
        "ok": True,
        "label": args["label"],
        "duplicates": duplicates,
        "recommendation": "复用已有类型（调用 ontology.propose_update）" if any(d["reason"] == "同名" for d in duplicates) else ("确认区分度后可新增" if duplicates else "可以新增"),
    }


async def _onto_propose_type(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    payload: dict[str, Any] = {
        "label": args["label"].strip(),
        "label_en": args.get("label_en", ""),
        "definition": args.get("definition", ""),
        "kind": args.get("kind", "class"),
        "business_domain": args.get("business_domain", "通用"),
        "parent_label": args.get("parent_label", ""),
        "aliases": _as_list(args.get("aliases")),
        "tags": _as_list(args.get("tags")),
        "annotations": args.get("annotations") or {},
        "properties": args.get("properties") or [],
        "relations": args.get("relations") or [],
        "constraints": args.get("constraints") or [],
    }
    issues = service.validate_type_payload(payload, ctx.space_id)
    blocking = [i for i in issues if i.level == "violation"]
    if any(i.code == "duplicate_label" for i in blocking):
        return {
            "ok": False,
            "error": f"「{payload['label']}」已存在同名对象类型，请改用 ontology.propose_update 或换一个标签",
            "duplicates": service.similar_types(ctx.space_id, payload["label"]),
        }
    confidence = float(args.get("confidence") or 0.5)
    proposal = service.create_proposal(
        ctx.space_id,
        kind="object_type",
        payload=payload,
        rationale=args.get("rationale", "") or "由 Agent 依据检索到的资料生成",
        confidence=confidence,
        evidences=ctx.evidence_for(payload["label"] + " " + payload["definition"]),
        run_id=ctx.run_id,
        title=f"新增对象类型「{payload['label']}」",
    )
    return {
        "ok": True,
        "proposal_id": proposal.id,
        "label": payload["label"],
        "status": proposal.status,
        "issues": [{"code": i.code, "level": i.level, "message": i.message} for i in issues],
        "summary": f"提案 #{proposal.id}「{payload['label']}」已生成，等待人工确认（置信度 {confidence}）",
    }


async def _onto_propose_link(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    source = service.find_type_by_label(ctx.space_id, args["source"])
    target = service.find_type_by_label(ctx.space_id, args["target"])
    if not source or not target:
        return {"ok": False, "error": "源或目标对象类型必须已存在于本体中，请先为缺失概念生成提案"}
    payload = {
        "source": source.label,
        "target": target.label,
        "relation": args.get("relation", "关联"),
        "cardinality": args.get("cardinality", "n:m"),
        "description": args.get("description", ""),
        "category": args.get("category", "association"),
    }
    proposal = service.create_proposal(
        ctx.space_id,
        kind="link",
        payload=payload,
        rationale=args.get("rationale", "由 Agent 依据共现证据生成关联"),
        confidence=float(args.get("confidence") or 0.55),
        evidences=ctx.evidence_for(f"{source.label} {target.label}"),
        run_id=ctx.run_id,
        title=f"新增关联 {source.label} —{payload['relation']}→ {target.label}",
    )
    return {"ok": True, "proposal_id": proposal.id, "summary": f"关联提案 #{proposal.id} 已生成"}


async def _onto_propose_constraint(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    payload = {
        "subject": args["subject"],
        "name": args.get("name") or f"{args['subject']} 约束",
        "kind": args.get("kind", "regulatory"),
        "expression": args["expression"],
        "severity": args.get("severity", "warning"),
        "reg_ref": args.get("reg_ref", ""),
        "property": args.get("property", ""),
        "description": args.get("description", ""),
    }
    proposal = service.create_proposal(
        ctx.space_id,
        kind="constraint",
        payload=payload,
        rationale=args.get("rationale", "监管条款映射"),
        confidence=float(args.get("confidence") or 0.7),
        evidences=ctx.evidence_for(payload["expression"]),
        run_id=ctx.run_id,
        title=f"新增约束「{payload['name']}」",
    )
    return {"ok": True, "proposal_id": proposal.id}


async def _onto_propose_update(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    label = args.get("label") or ""
    target = service.find_type_by_label(ctx.space_id, label)
    if not target:
        return {"ok": False, "error": f"对象类型「{label}」不存在，无法生成更新提案"}
    patch = args.get("patch") or {}
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except json.JSONDecodeError:
            return {"ok": False, "error": "patch 需为 JSON 对象"}
    payload = {"label": target.label, "type_id": target.id, **patch}
    proposal = service.create_proposal(
        ctx.space_id,
        kind="property" if set(patch) == {"properties"} else "object_type",
        payload=payload,
        rationale=args.get("rationale", "定义精化"),
        confidence=float(args.get("confidence") or 0.75),
        evidences=ctx.evidence_for(str(patch)[:400]),
        run_id=ctx.run_id,
        title=f"更新对象类型「{target.label}」",
    )
    return {"ok": True, "proposal_id": proposal.id, "kind": proposal.kind}


async def _onto_validate(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    issues = service.validate_space(ctx.space_id)
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        grouped.setdefault(issue.code, []).append(issue.message)
    return {
        "ok": True,
        "summary": f"发现 {len(issues)} 个问题",
        "counts": {level: sum(1 for i in issues if i.level == level) for level in ("violation", "warning", "info")},
        "grouped": {k: v[:8] for k, v in grouped.items()},
        "stats": service.space_stats(ctx.space_id),
    }


async def _onto_export(ctx: ToolContext, args: dict) -> dict:
    fmt = (args.get("format") or "turtle").lower()
    if fmt == "turtle":
        return {"ok": True, "format": fmt, "chars": 0, "content": to_turtle(ctx.session, ctx.space_id)[:6000]}
    if fmt == "skos":
        return {"ok": True, "format": fmt, "content": to_skos(ctx.session, ctx.space_id)[:6000]}
    if fmt == "jsonschema":
        if not args.get("type_id"):
            return {"ok": False, "error": "jsonschema 导出需要 type_id"}
        return {"ok": True, "format": fmt, "content": json.dumps(to_json_schema(ctx.session, ctx.space_id, int(args["type_id"])), ensure_ascii=False, indent=2)[:6000]}
    return {"ok": False, "error": f"不支持的导出格式 {fmt}"}


# ---------------------------------------------------------------- 图谱 / Wiki
async def _graph_subgraph(ctx: ToolContext, args: dict) -> dict:
    from ..wiki.graph import GraphService

    service = GraphService(ctx.session)
    label = args.get("label") or ""
    type_id = int(args["type_id"]) if args.get("type_id") else None
    if not type_id:
        row = OntologyService(ctx.session).find_type_by_label(ctx.space_id, label)
        if not row:
            return {"ok": False, "error": f"对象类型「{label}」不存在"}
        type_id = row.id
    graph = service.subgraph(ctx.space_id, type_id, depth=int(args.get("depth") or 2))
    return {
        "ok": True,
        "center": label or str(type_id),
        "nodes": [{"label": n["label"], "type": n["type"], "degree": n.get("degree", 0)} for n in graph["nodes"]][:60],
        "edges": [{"source": e["source"], "relation": e.get("relation", ""), "target": e["target"]} for e in graph["edges"]][:80],
        "stats": graph["stats"],
    }


async def _graph_stats(ctx: ToolContext, args: dict) -> dict:
    from ..wiki.graph import GraphService

    return {"ok": True, **GraphService(ctx.session).insights(ctx.space_id)}


async def _wiki_draft_page(ctx: ToolContext, args: dict) -> dict:
    service = OntologyService(ctx.session)
    label = args["topic"]
    row = service.find_type_by_label(ctx.space_id, label)
    detail = service.detail(row.id) if row else {"label": label, "definition": args.get("definition", ""), "properties": [], "links": [], "annotations": {}}
    payload = {
        "label": detail["label"],
        "definition": detail.get("definition", ""),
        "properties": detail.get("properties", []),
        "links": detail.get("links", []),
        "annotations": detail.get("annotations", {}),
    }
    prompt = (
        "[[TASK:wiki_page]]\n请为下面的对象类型生成结构化 Wiki 页面（Markdown），只输出 Markdown。\n\n"
        f"## 页面数据\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n"
    )
    raw, _usage = await registry.llm().complete([ChatMessage(role="user", content=prompt)], task="wiki_page")
    proposal = service.create_proposal(
        ctx.space_id,
        kind="wiki_page",
        payload={"topic": label, "body_md": raw, "type_id": row.id if row else None},
        rationale="Agent 生成的页面草稿，需人工确认后再落为版本",
        confidence=0.6,
        run_id=ctx.run_id,
        title=f"Wiki 页面草稿「{label}」",
    )
    return {"ok": True, "proposal_id": proposal.id, "preview": raw[:1500], "chars": len(raw)}


async def _wiki_read_page(ctx: ToolContext, args: dict) -> dict:
    page = ctx.session.scalar(select(WikiPage).where(WikiPage.space_id == ctx.space_id, WikiPage.slug == args["slug"]))
    if page is None:
        pages = ctx.session.scalars(select(WikiPage).where(WikiPage.space_id == ctx.space_id).limit(40)).all()
        return {"ok": False, "error": f"页面 {args['slug']} 不存在", "available": [p.slug for p in pages]}
    from ..wiki.compiler import WikiCompiler

    return {"ok": True, "slug": page.slug, "title": page.title, "body": WikiCompiler(ctx.session).current_body(page)[:6000]}


# ---------------------------------------------------------------- 记忆 / 沙箱 / 搜索 / 数据源
async def _memory_remember(ctx: ToolContext, args: dict) -> dict:
    item = MemoryService(ctx.session).remember(
        args["content"],
        scope=f"space:{ctx.space_id}",
        kind=args.get("kind", "fact"),
        source_run_id=ctx.run_id,
    )
    return {"ok": bool(item), "memory_id": item.id if item else None, "summary": "已写入长期记忆" if item else "内容过短，未写入"}


async def _memory_recall(ctx: ToolContext, args: dict) -> dict:
    hits = MemoryService(ctx.session).recall(args["query"], scope=f"space:{ctx.space_id}", top_k=int(args.get("top_k") or 5))
    return {"ok": True, "count": len(hits), "memories": [h.as_dict() for h in hits]}


async def _sandbox_run(ctx: ToolContext, args: dict) -> dict:
    result = await asyncio.to_thread(sandbox.run_python, args["code"], float(args.get("timeout") or 8.0))
    return {
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr[:1500],
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "violations": result.violations,
    }


async def _search_web(ctx: ToolContext, args: dict) -> dict:
    provider = registry.web_search()
    if isinstance(provider, NoopWebSearch):
        return {"ok": False, "error": "网络搜索组件未启用（设置 FINONTO_WEB_SEARCH_PROVIDER=duckduckgo 或配置 API Key）"}
    results = await asyncio.to_thread(provider.search, args["query"], int(args.get("top_k") or 5))
    return {"ok": True, "count": len(results), "results": [r.__dict__ for r in results]}


async def _search_fetch(ctx: ToolContext, args: dict) -> dict:
    provider = registry.web_search()
    if isinstance(provider, NoopWebSearch):
        return {"ok": False, "error": "网络搜索组件未启用，无法抓取网页"}
    text = await asyncio.to_thread(provider.fetch, args["url"], int(args.get("max_chars") or 6000))
    return {"ok": True, "url": args["url"], "chars": len(text), "text": text}


async def _datasource_describe(ctx: ToolContext, args: dict) -> dict:
    source = registry.data_source(args["uri"])
    tables = await asyncio.to_thread(source.describe, args["uri"])
    return {
        "ok": True,
        "provider": source.name,
        "tables": [{"name": t.name, "columns": t.columns, "rows": len(t.rows)} for t in tables],
        "hint": "可用 ontology.propose_type 把表映射为对象类型，外键映射为关联",
    }


async def _datasource_profile(ctx: ToolContext, args: dict) -> dict:
    from ...providers.datasource.connectors import profile_source

    source = registry.data_source(args["uri"])
    tables = await asyncio.to_thread(profile_source, source, args["uri"])
    return {"ok": True, "profile": tables}


async def _mcp_fallback(ctx: ToolContext, name: str, args: dict) -> dict:
    parts = name.split(".", 2)
    if len(parts) != 3:
        return {"ok": False, "error": f"MCP 工具名格式应为 mcp.<server>.<tool>，收到 {name}"}
    result = await asyncio.to_thread(mcp_manager.call, parts[1], parts[2], args)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


# ---------------------------------------------------------------- 注册
def _register_builtin() -> None:
    R = registry_tools
    R.register("rag.search", "在本地知识库做混合检索（BM25+向量+重排序），返回带定位的片段", {"query": _param(**P), "top_k": _param(type="integer")}, _rag_search, category="rag")
    R.register("rag.read_chunk", "按 chunk_id 读取原文片段全文", {"chunk_id": _param(type="integer", **P)}, _rag_read_chunk, category="rag")
    R.register("rag.ask", "对知识库提一个需要引用的问题，返回带引用的回答", {"question": _param(**P), "top_k": _param(type="integer")}, _rag_ask, category="rag")
    R.register("doc.list", "列出知识库中的文档", {"limit": _param(type="integer")}, _doc_list, category="document")
    R.register("doc.read", "读取指定文档的正文片段（作为证据来源）", {"doc_id": _param(type="integer", **P), "max_chars": _param(type="integer")}, _doc_read, category="document")

    R.register("ontology.list_types", "列出本体空间中的对象类型与统计", {"q": _param(), "status": _param(), "domain": _param(), "limit": _param(type="integer")}, _onto_list, category="ontology")
    R.register("ontology.get_type", "读取对象类型完整详情", {"type_id": _param(type="integer", **P)}, _onto_get, category="ontology")
    R.register("ontology.find_similar", "查找与给定标签重复或相近的已有对象类型", {"label": _param(**P)}, _onto_find_similar, category="ontology")
    R.register(
        "ontology.propose_type",
        "提交新增对象类型提案（不直接写入本体，等待人工确认）",
        {
            "label": _param(**P),
            "definition": _param(**P),
            "label_en": _param(),
            "kind": _param(enum=["class", "entity", "event", "product", "party", "agreement", "document", "metric", "process", "regulation", "attribute_domain"]),
            "business_domain": _param(),
            "parent_label": _param(),
            "aliases": _param(type="array"),
            "tags": _param(type="array"),
            "properties": _param(type="array", description="[{label,datatype,required,unit,description}]"),
            "relations": _param(type="array", description="[{target,relation,description,cardinality}]"),
            "constraints": _param(type="array", description="[{name,kind,expression,severity,reg_ref}]"),
            "annotations": _param(type="object", description="领域扩展注解，键随领域包，如 data_classification/reg_refs/risk_dimension/lifecycle"),
            "confidence": _param(type="number"),
            "rationale": _param(),
        },
        _onto_propose_type,
        category="ontology",
        risk="write",
    )
    R.register(
        "ontology.propose_link",
        "提交两个已有对象类型之间的关联提案",
        {"source": _param(**P), "target": _param(**P), "relation": _param(**P), "description": _param(), "cardinality": _param(), "category": _param(), "confidence": _param(type="number"), "rationale": _param()},
        _onto_propose_link,
        category="ontology",
        risk="write",
    )
    R.register(
        "ontology.propose_constraint",
        "提交约束/合规规则提案",
        {"subject": _param(**P), "name": _param(), "kind": _param(), "expression": _param(**P), "severity": _param(), "reg_ref": _param(), "property": _param(), "description": _param(), "confidence": _param(type="number")},
        _onto_propose_constraint,
        category="ontology",
        risk="write",
    )
    R.register(
        "ontology.propose_update",
        "提交对已有对象类型的更新提案（补定义/补属性/补注解）",
        {"label": _param(**P), "patch": _param(type="object", **P, description='例如 {"definition": "...", "properties": [...]}'), "rationale": _param(), "confidence": _param(type="number")},
        _onto_propose_update,
        category="ontology",
        risk="write",
    )
    R.register("ontology.validate", "对本体空间做一致性校验", {}, _onto_validate, category="ontology")
    R.register("ontology.export", "导出本体序列化结果", {"format": _param(enum=["turtle", "skos", "jsonschema"]), "type_id": _param(type="integer")}, _onto_export, category="ontology")

    R.register("graph.subgraph", "查询以某对象类型为中心的知识图谱子图", {"label": _param(), "type_id": _param(type="integer"), "depth": _param(type="integer")}, _graph_subgraph, category="graph")
    R.register("graph.stats", "查看图谱规模、枢纽节点与覆盖缺口", {}, _graph_stats, category="graph")

    R.register("wiki.draft_page", "为某对象类型生成 Wiki 页面草稿（进入待确认提案）", {"topic": _param(**P), "definition": _param()}, _wiki_draft_page, category="wiki", risk="write")
    R.register("wiki.read_page", "读取已编译的 Wiki 页面", {"slug": _param(**P)}, _wiki_read_page, category="wiki")

    R.register("memory.remember", "把一条结论写入长期记忆", {"content": _param(**P), "kind": _param(enum=["fact", "pattern", "preference", "decision", "glossary"])}, _memory_remember, category="memory", risk="write")
    R.register("memory.recall", "从长期记忆召回相关经验", {"query": _param(**P), "top_k": _param(type="integer")}, _memory_recall, category="memory")

    R.register("sandbox.run_python", "在受限沙箱中执行 Python 计算（白名单模块、无网络无文件、超时终止）", {"code": _param(**P), "timeout": _param(type="integer")}, _sandbox_run, category="exec", risk="exec")
    R.register("search.web", "联网检索公开资料", {"query": _param(**P), "top_k": _param(type="integer")}, _search_web, category="network", risk="network")
    R.register("search.fetch", "抓取网页正文", {"url": _param(**P), "max_chars": _param(type="integer")}, _search_fetch, category="network", risk="network")
    R.register("datasource.describe", "列出数据源中的表与列", {"uri": _param(**P)}, _datasource_describe, category="data")
    R.register("datasource.profile", "剖析数据源表结构（空值率、基数、数值占比、样例值）", {"uri": _param(**P)}, _datasource_profile, category="data")


def register_mcp_tools() -> int:
    """把 MCP 服务器的工具动态注册进工具表。"""
    count = 0
    for tool in mcp_manager.tools():
        qualified = tool.qualified
        if registry_tools.has(qualified):
            continue
        parameters = {
            key: _param(type=value.get("type", "string"), required=bool(value.get("required")))
            for key, value in (tool.parameters or {}).items()
        }

        async def handler(ctx: ToolContext, args: dict[str, Any], _q: str = qualified) -> dict:
            return await _mcp_fallback(ctx, _q, args)

        registry_tools.register(qualified, f"[MCP:{tool.server}] {tool.description}", parameters, handler, category="mcp", risk="read")
        count += 1
    return count


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    return []


_register_builtin()
