"""RAG 主流程：查询理解 → 混合检索 → 重排 → 生成 → 引用绑定 → 轨迹落库。

两个入口：
* ``ask``        —— 一次性返回完整结果（同步接口、Agent 工具调用）；
* ``ask_stream`` —— 逐 token 推送（SSE），事件类型见 SSE_EVENTS。

注意 ``ask_stream`` 自己开 session：流式响应返回后请求级 session 已关闭，
如果在生成器里继续用请求 session 会拿到已关闭的连接。
"""

from __future__ import annotations

import time
from typing import Iterator

from ...db import session_scope
from ...models import OntologySpace, QueryTrace
from ...providers.base import RerankItem
from ...providers.registry import get_components
from ..ontology import packs
from ..text import truncate
from .citations import bind, normalize_tags
from .generator import generate, generate_stream
from .query_understanding import QueryPlan, understand
from .retriever import retrieve

SSE_EVENTS = ("plan", "retrieval", "token", "citations", "done", "error")


def _domain_of(session, space_id: int) -> str:
    space = session.get(OntologySpace, space_id)
    return space.domain if space else "general"


def _plan_for(session, space_id: int, question: str, components) -> QueryPlan:
    domain = _domain_of(session, space_id)
    lexicon = packs.lexicon_for(domain)
    return understand(
        session,
        space_id,
        question,
        synonyms=lexicon.get("synonyms") or {},
        llm=components.llm,
    )


def ask(
    session,
    space_id: int,
    question: str,
    *,
    components=None,
    top_k: int | None = None,
) -> dict:
    components = components or get_components()
    started = time.perf_counter()

    plan = _plan_for(session, space_id, question, components)
    chunks = retrieve(session, space_id, plan, components=components, top_k=top_k or 8)
    reranked = components.reranker.rerank(
        plan.question,
        [
            RerankItem(
                chunk_id=item.chunk_id,
                text=item.text,
                score=item.score,
                meta={**item.signals, "authority": item.signals.get("authority", 3)},
            )
            for item in chunks
        ],
        top_k=top_k or 8,
    )
    by_id = {item.chunk_id: item for item in chunks}
    ordered = []
    for hit in reranked:
        original = by_id.get(hit.chunk_id)
        if original is not None:
            original.score = hit.score
            original.signals["reranked"] = True
            original.signals["why"] = hit.meta.get("why", "")
            ordered.append(original)

    domain_context = packs.prompt_context_for(_domain_of(session, space_id))
    answer, tag_map = generate(components, plan, ordered, domain_context=domain_context)
    answer = normalize_tags(answer)
    citations = bind(answer, tag_map)

    latency_ms = int((time.perf_counter() - started) * 1000)
    trace = QueryTrace(
        space_id=space_id,
        question=question,
        intent=plan.intent,
        sub_questions=plan.sub_questions,
        filters=plan.filters,
        retrieved=[item.as_dict() for item in ordered],
        answer=answer,
        citations=[citation.as_dict() for citation in citations],
        provider=getattr(components.llm, "name", "unknown"),
        latency_ms=latency_ms,
    )
    session.add(trace)
    session.commit()

    return {
        "trace_id": trace.id,
        "question": question,
        "answer": answer,
        "citations": [citation.as_dict() for citation in citations],
        "plan": plan.as_dict(),
        "retrieved": [item.as_dict() for item in ordered],
        "provider": getattr(components.llm, "name", "unknown"),
        "latency_ms": latency_ms,
    }


def ask_stream(
    space_id: int,
    question: str,
    *,
    top_k: int | None = None,
) -> Iterator[dict]:
    """SSE 事件流：每个 dict 会被 api 层编码成一条 ``data:``。"""
    components = get_components()
    started = time.perf_counter()
    try:
        with session_scope() as session:
            plan = _plan_for(session, space_id, question, components)
            chunks = retrieve(session, space_id, plan, components=components, top_k=top_k or 8)
            domain_context = packs.prompt_context_for(_domain_of(session, space_id))

        yield {"type": "plan", "data": plan.as_dict()}
        yield {"type": "retrieval", "data": [item.as_dict() for item in chunks]}

        stream, tag_map = generate_stream(components, plan, chunks, domain_context=domain_context)
        collected: list[str] = []
        for piece in stream:
            if not piece:
                continue
            collected.append(piece)
            yield {"type": "token", "text": piece}

        answer = normalize_tags("".join(collected))
        citations = bind(answer, tag_map)
        latency_ms = int((time.perf_counter() - started) * 1000)

        with session_scope() as session:
            trace = QueryTrace(
                space_id=space_id,
                question=question,
                intent=plan.intent,
                sub_questions=plan.sub_questions,
                filters=plan.filters,
                retrieved=[item.as_dict() for item in chunks],
                answer=answer,
                citations=[citation.as_dict() for citation in citations],
                provider=getattr(components.llm, "name", "unknown"),
                latency_ms=latency_ms,
            )
            session.add(trace)
            session.flush()
            trace_id = trace.id

        yield {
            "type": "citations",
            "data": [citation.as_dict() for citation in citations],
        }
        yield {
            "type": "done",
            "data": {
                "trace_id": trace_id,
                "answer": answer,
                "latency_ms": latency_ms,
                "provider": getattr(components.llm, "name", "unknown"),
                "uncited": [truncate(item.text, 60) for item in chunks[len(citations) :]],
            },
        }
    except Exception as exc:  # noqa: BLE001 - 流式接口必须把错误变成事件
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
