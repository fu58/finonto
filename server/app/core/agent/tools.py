"""Agent 工具注册表。

设计约束
--------
1. **工具只返回结构化 observation**，不做副作用渲染；
2. **写本体只能通过提案**：``propose_type`` 产出 ``ChangeProposal``，绝不直接写库；
3. 每个工具声明风险等级 ``read / write / exec / network``，技能包按白名单授权；
4. 参数用 JSON Schema 描述，前端与真实模型共用同一份签名（可直接转 function calling）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import func, select

from ...models import Document, ObjectType
from ..ontology import packs, service
from ..rag.pipeline import ask
from ..rag.query_understanding import understand
from ..rag.retriever import retrieve
from ..text import truncate


@dataclass(slots=True)
class ToolContext:
    session: Any
    space_id: int
    components: Any
    run_id: int | None = None

    @property
    def domain(self) -> str:
        from ...models import OntologySpace

        space = self.session.get(OntologySpace, self.space_id)
        return space.domain if space else "general"


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict]
    category: str = "general"
    risk: str = "read"


@dataclass(slots=True)
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., dict],
        *,
        category: str = "general",
        risk: str = "read",
    ) -> None:
        self.tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            category=category,
            risk=risk,
        )

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def catalog(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        names = [n for n in self.names() if not allowed or n in allowed]
        return [
            {
                "name": self.tools[n].name,
                "description": self.tools[n].description,
                "parameters": self.tools[n].parameters,
                "category": self.tools[n].category,
                "risk": self.tools[n].risk,
            }
            for n in names
        ]

    def invoke(self, name: str, context: ToolContext, arguments: dict | None = None) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知工具：{name}"}
        started = time.perf_counter()
        try:
            result = tool.handler(context, **(arguments or {}))
            if not isinstance(result, dict):
                result = {"result": result}
            result.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001 - 工具异常必须变成 observation
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result["tool"] = name
        return result


R = ToolRegistry()


# --------------------------------------------------------------------------- #
# 读类工具
# --------------------------------------------------------------------------- #
def _rag_search(ctx: ToolContext, query: str = "", top_k: int = 5) -> dict:
    if not query:
        return {"ok": False, "error": "query 不能为空"}
    result = ask(ctx.session, ctx.space_id, query, components=ctx.components, top_k=int(top_k))
    return {
        "ok": True,
        "answer": truncate(result["answer"], 400),
        "citations": result["citations"][:5],
        "retrieved": [
            {
                "chunk_id": item["chunk_id"],
                "document_id": item["document_id"],
                "doc_title": item["doc_title"],
                "locator": item["locator"],
                "text": truncate(item["text"], 220),
            }
            for item in result["retrieved"][:5]
        ],
    }


def _doc_list(ctx: ToolContext, limit: int = 20) -> dict:
    rows = ctx.session.execute(
        select(Document).where(Document.space_id == ctx.space_id).order_by(Document.id.desc()).limit(int(limit))
    ).scalars().all()
    return {
        "ok": True,
        "count": len(rows),
        "documents": [
            {
                "id": row.id,
                "title": row.title,
                "doc_type": row.doc_type,
                "authority": row.authority,
                "jurisdiction": row.jurisdiction,
                "page_count": row.page_count,
            }
            for row in rows
        ],
    }


def _doc_chunks(ctx: ToolContext, document_id: int = 0, limit: int = 8) -> dict:
    from ...models import DocumentChunk

    query = select(DocumentChunk)
    if document_id:
        query = query.where(DocumentChunk.document_id == int(document_id))
    rows = ctx.session.execute(
        query.order_by(DocumentChunk.ordinal).limit(int(limit))
    ).scalars().all()
    return {
        "ok": True,
        "chunks": [
            {"chunk_id": row.id, "document_id": row.document_id, "locator": row.locator,
             "text": truncate(row.text, 200)}
            for row in rows
        ],
    }


def _ontology_list_types(ctx: ToolContext, keyword: str = "", limit: int = 30) -> dict:
    types = service.list_types(ctx.session, ctx.space_id, keyword=keyword)
    return {
        "ok": True,
        "count": len(types),
        "types": [
            {"id": item["id"], "label": item["label"], "kind": item["kind_label"],
             "business_domain": item["business_domain"], "properties": item["property_count"]}
            for item in types[: int(limit)]
        ],
    }


def _ontology_type_detail(ctx: ToolContext, type_id: int = 0) -> dict:
    detail = service.type_detail(ctx.session, int(type_id))
    if detail is None:
        return {"ok": False, "error": f"类型不存在：{type_id}"}
    return {"ok": True, "type": {
        "id": detail["id"], "label": detail["label"], "kind": detail["kind_label"],
        "definition": truncate(detail["definition"], 300),
        "properties": [p["label"] for p in detail["properties"]],
        "links": [f"{l['relation']}→{l['target_label']}" for l in detail["links"]],
        "constraints": [c["message"] for c in detail["constraints"]],
        "evidence": detail["evidence"][:3],
    }}


def _find_similar(ctx: ToolContext, label: str = "", aliases: list[str] | None = None) -> dict:
    if not label:
        return {"ok": False, "error": "label 必填"}
    similar = service.find_similar(ctx.session, ctx.space_id, label, aliases or [])
    return {
        "ok": True,
        "label": label,
        "similar": similar,
        "verdict": "疑似重复，建议改造成修改提案" if similar and similar[0]["score"] >= 0.8 else "可新建",
    }


def _graph_subgraph(ctx: ToolContext, node_id: str = "", depth: int = 1) -> dict:
    from ..wiki import graph as wiki_graph

    graph = wiki_graph.build_graph(ctx.session, ctx.space_id)
    if not node_id:
        return {"ok": True, "insights": wiki_graph.insights(ctx.session, ctx.space_id, graph)}
    sub = wiki_graph.subgraph(graph, node_id, int(depth))
    return {
        "ok": True,
        "focus": node_id,
        "nodes": [{"id": n["id"], "label": n["label"], "kind": n["kind"]} for n in sub["nodes"]],
        "edges": [{"source": e["source"], "target": e["target"], "kind": e["kind"], "label": e["label"]}
                  for e in sub["edges"]],
    }


def _wiki_list_pages(ctx: ToolContext, page_type: str = "", limit: int = 20) -> dict:
    from ...models import WikiPage

    query = select(WikiPage).where(WikiPage.space_id == ctx.space_id)
    if page_type:
        query = query.where(WikiPage.page_type == page_type)
    rows = ctx.session.execute(query.limit(int(limit))).scalars().all()
    return {
        "ok": True,
        "pages": [
            {"slug": row.slug, "title": row.title, "version": row.version,
             "dirty": row.is_dirty, "locked": row.is_locked}
            for row in rows
        ],
    }


def _wiki_read_page(ctx: ToolContext, slug: str = "") -> dict:
    from ...models import WikiPage

    page = ctx.session.execute(
        select(WikiPage).where(WikiPage.space_id == ctx.space_id, WikiPage.slug == slug)
    ).scalars().first()
    if page is None:
        return {"ok": False, "error": f"页面不存在：{slug}"}
    return {"ok": True, "slug": page.slug, "title": page.title, "version": page.version,
            "body": truncate(page.body, 800)}


def _memory_search(ctx: ToolContext, query: str = "", limit: int = 5) -> dict:
    from ..agent import memory as memory_service

    items = memory_service.search(ctx.session, ctx.space_id, query, limit=int(limit))
    return {"ok": True, "memories": items}


# --------------------------------------------------------------------------- #
# 写类工具（产出提案，不直接改本体）
# --------------------------------------------------------------------------- #
def _propose_type(ctx: ToolContext, draft: dict | None = None, rationale: str = "") -> dict:
    if not isinstance(draft, dict) or not (draft.get("label") or "").strip():
        return {"ok": False, "error": "draft.label 必填"}
    label = draft["label"].strip()
    similar = service.find_similar(ctx.session, ctx.space_id, label, draft.get("aliases") or [])
    existing = ctx.session.execute(
        select(ObjectType).where(ObjectType.space_id == ctx.space_id, ObjectType.label == label)
    ).scalars().first()
    if existing is not None:
        return {
            "ok": False,
            "error": f"类型「{label}」已存在（#{existing.id}），请改用 update_type 提案",
            "existing_type_id": existing.id,
        }
    evidence = []
    for item in draft.get("evidence") or []:
        if isinstance(item, dict):
            evidence.append(
                {
                    "quote": item.get("quote", ""),
                    "locator": item.get("locator", ""),
                    "note": label,
                }
            )
    proposal = service.create_proposal(
        ctx.session,
        ctx.space_id,
        "create_type",
        draft,
        rationale=rationale or f"Agent 提议新建类型「{label}」",
        author="agent",
        evidence=evidence,
        agent_run_id=ctx.run_id,
    )
    ctx.session.commit()
    return {
        "ok": True,
        "proposal_id": proposal.id,
        "label": label,
        "similar": similar,
        "requires_human_confirm": True,
        "note": "提案已创建，需在「提案审核」页签人工确认后才会写入本体",
    }


def _memory_save(ctx: ToolContext, content: str = "", kind: str = "fact", keywords: str = "") -> dict:
    from ..agent import memory as memory_service

    if not content.strip():
        return {"ok": False, "error": "content 不能为空"}
    item = memory_service.save(ctx.session, ctx.space_id, content, kind=kind, keywords=keywords)
    ctx.session.commit()
    return {"ok": True, "memory_id": item.id, "kind": item.kind}


# --------------------------------------------------------------------------- #
# 执行 / 网络类工具
# --------------------------------------------------------------------------- #
def _sandbox_run(ctx: ToolContext, code: str = "") -> dict:
    from ..agent import sandbox as sandbox_service

    result = sandbox_service.run(code)
    return result


def _search_web(ctx: ToolContext, query: str = "", top_k: int = 5) -> dict:
    searcher = ctx.components.web_search
    if getattr(searcher, "name", "none") == "none":
        return {"ok": False, "error": "网络搜索已关闭（组件中心可切换为 duckduckgo 或配置密钥）"}
    results = searcher.search(query, top_k=int(top_k))
    return {
        "ok": True,
        "count": len(results),
        "results": [{"title": r.title, "url": r.url, "snippet": truncate(r.snippet, 160)} for r in results],
    }


def _datasource_probe(ctx: ToolContext, uri: str = "") -> dict:
    from ...providers.datasource import route

    if not uri:
        return {"ok": False, "error": "uri 必填"}
    source = route(uri)
    return {"ok": True, "probe": source.probe(uri)}


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def _register_builtin() -> None:
    R.register(
        "rag.search",
        "在知识库中检索并回答问题，返回答案、引用与命中片段",
        {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 5}},
         "required": ["query"]},
        _rag_search,
        category="rag",
        risk="read",
    )
    R.register("doc.list", "列出当前空间已入库的文档",
               {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
               _doc_list, category="doc", risk="read")
    R.register("doc.chunks", "查看文档片段（用于取证）",
               {"type": "object", "properties": {"document_id": {"type": "integer"}, "limit": {"type": "integer", "default": 8}}},
               _doc_chunks, category="doc", risk="read")
    R.register("ontology.list_types", "列出当前空间的对象类型",
               {"type": "object", "properties": {"keyword": {"type": "string"}, "limit": {"type": "integer", "default": 30}}},
               _ontology_list_types, category="ontology", risk="read")
    R.register("ontology.type_detail", "查看某个对象类型的完整定义",
               {"type": "object", "properties": {"type_id": {"type": "integer"}}, "required": ["type_id"]},
               _ontology_type_detail, category="ontology", risk="read")
    R.register("find_similar", "检查某个标签是否与已有类型重复",
               {"type": "object", "properties": {"label": {"type": "string"}, "aliases": {"type": "array", "items": {"type": "string"}}},
                "required": ["label"]},
               _find_similar, category="ontology", risk="read")
    R.register("propose_type", "产出「新建对象类型」提案（需人工确认，不直接写本体）",
               {"type": "object", "properties": {"draft": {"type": "object"}, "rationale": {"type": "string"}},
                "required": ["draft"]},
               _propose_type, category="ontology", risk="write")
    R.register("graph.subgraph", "查询图谱邻域或整体洞察",
               {"type": "object", "properties": {"node_id": {"type": "string"}, "depth": {"type": "integer", "default": 1}}},
               _graph_subgraph, category="graph", risk="read")
    R.register("wiki.list_pages", "列出 Wiki 页面",
               {"type": "object", "properties": {"page_type": {"type": "string"}, "limit": {"type": "integer", "default": 20}}},
               _wiki_list_pages, category="wiki", risk="read")
    R.register("wiki.read_page", "读取某个 Wiki 页面正文",
               {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
               _wiki_read_page, category="wiki", risk="read")
    R.register("memory.search", "检索长期记忆",
               {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}},
               _memory_search, category="memory", risk="read")
    R.register("memory.save", "写入一条长期记忆",
               {"type": "object", "properties": {"content": {"type": "string"}, "kind": {"type": "string", "default": "fact"},
                                                 "keywords": {"type": "string"}},
                "required": ["content"]},
               _memory_save, category="memory", risk="write")
    R.register("sandbox.run_python", "在受限沙箱中运行 Python（仅允许白名单标准库）",
               {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
               _sandbox_run, category="sandbox", risk="exec")
    R.register("search.web", "联网搜索（默认关闭，需在组件中心启用）",
               {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 5}},
                "required": ["query"]},
               _search_web, category="search", risk="network")
    R.register("datasource.probe", "探查数据源结构（URI 前缀自动路由）",
               {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
               _datasource_probe, category="datasource", risk="network")


_register_builtin()


def registry() -> ToolRegistry:
    return R


def parse_arguments(raw: str) -> dict:
    """LLM 给出的参数可能是 JSON、也可能带注释；解析失败返回 {}。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
