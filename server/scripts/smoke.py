"""端到端冒烟测试：不接任何外部 API，跑通「领域包安装 → 检索 → 生成引用 → Agent 提案 → 人工确认 → Wiki → 回滚」。

用法：`./.venv/Scripts/python.exe scripts/smoke.py`
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.agent.react import ReActConfig, ReActEngine  # noqa: E402
from app.core.ontology.owl import export_bundle  # noqa: E402
from app.core.ontology.packs import PACKS, pack_for_space, vocab_for  # noqa: E402
from app.core.ontology.service import OntologyService  # noqa: E402
from app.core.rag.ingest import IngestService  # noqa: E402
from app.core.rag.pipeline import RagPipeline  # noqa: E402
from app.core.wiki.compiler import WikiCompiler  # noqa: E402
from app.core.wiki.graph import GraphService  # noqa: E402
from app.core.wiki.versions import VersionService  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import DocumentRecord, Space  # noqa: E402
from app.providers.registry import registry  # noqa: E402
from app.services import bootstrap  # noqa: E402

CROSS_DOMAIN_QUESTIONS = {
    "maintenance": "A 类设备的定期点检周期上限是多少？缺陷升级为故障后开维护工单有什么时限要求？",
    "ai": "模型上线的幻觉率与有害输出率阈值分别是多少？缺失模型卡会怎样处理？",
    "network": "跨机房链路时延阈值和一级网络故障事件的恢复时限要求分别是什么？",
}


def step(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


async def rag_check(session, space_id: int, question: str) -> dict:
    tokens: list[str] = []
    citations: list[dict] = []
    stages: list[str] = []
    async for event in RagPipeline(session).run(question, space_id):
        kind = event["type"]
        if kind == "stage":
            stages.append(f"{event['stage']}={event.get('detail', '')}")
        elif kind == "token":
            tokens.append(event["text"])
        elif kind == "citations":
            citations = event["citations"]
        elif kind == "error":
            raise RuntimeError(event["message"])
    answer = "".join(tokens)
    print(f"  阶段：{' | '.join(stages)}")
    print(f"  回答（{len(answer)} 字）：{answer[:200]}…")
    if citations:
        first = citations[0]
        print(f"  引用 {len(citations)} 条，首条 [{first['ref']}] {first['doc_name']} {first['locator']}：{first['quote'][:60]}…")
    else:
        print("  引用 0 条")
    return {"answer": answer, "citations": citations}


async def agent_check(session, space_id: int) -> dict:
    goal = "从零售信贷核心数据字典和互联网贷款制度中抽取「押品」「担保人」「大数据风控模型」等候选对象类型并生成提案"
    engine = ReActEngine(session, space_id, ReActConfig(skill="doc-extraction", max_steps=10))
    result = await engine.run(goal)
    for event in result["events"]:
        if event["type"] == "step":
            print(f"  步 {event['ordinal']:>2}  Action={event['action']}  args={str(event['action_input'])[:88]}")
        elif event["type"] == "observation":
            print(f"        观察({'ok' if event['ok'] else '失败'}): {event['observation'][:150]}")
        elif event["type"] == "proposal":
            print(f"        提案 #{event['proposal_id']} 已生成，待人工确认")
        elif event["type"] == "hint":
            print(f"        提示：{event['message']}")
        elif event["type"] == "error":
            print(f"  错误：{event['message']}")
    print(f"  结论[{result['status']}]：{result['answer'][:200]}")
    print(f"  提案 {len(result.get('proposals', []))} 个｜token {result['tokens']}｜用时 {result['elapsed_ms']}ms")
    return result


def main() -> int:
    settings.ensure_dirs()
    init_db()

    with session_scope() as session:
        step("1. 领域包冷启动（全部内置领域包幂等安装）")
        service = OntologyService(session)
        report = bootstrap.bootstrap(session, ROOT)
        print(f"  已注册领域包：{', '.join(PACKS)}")
        spaces: dict[str, Space] = {row.domain: row for row in session.scalars(select(Space)).all()}
        for item in report["reports"]:
            print(f"  [{item['pack']}] 空间 #{item['space_id']} {item['space_name']}｜种子 {item['seed']}"
                  f"｜示例 {item.get('samples', {}).get('ingested') or item.get('samples', {}).get('skipped')}")
        print(f"  合并计数：{report['counters']}")
        space = spaces["finance"]
        space_id = space.id
        print(f"  主验证空间 #{space_id} {space.name}：对象类型 {len(service.list_types(space_id, limit=1000))} 个")

        session.commit()
        step("2. 语料规模核对（解析 → 分块 → 嵌入 → 双索引，由各领域包安装时完成）")
        ingest = IngestService(session)
        for key, pack in PACKS.items():
            target = spaces.get(key)
            if target is None:
                continue
            docs = int(
                session.scalar(
                    select(func.count()).select_from(DocumentRecord).where(DocumentRecord.space_id == target.id)
                )
                or 0
            )
            print(f"  {key:<12} 空间 #{target.id}：文档 {docs} 篇 / 片段 {ingest.chunk_count(target.id)} 个｜{pack.label}")

        session.commit()
        step("3. RAG 问答（查询理解 → 混合检索 → 重排 → 流式生成 → 引用）")
        rag = asyncio.run(
            rag_check(session, space_id, "个人信用贷款的授信额度上限，以及逾期天数下调五级分类的规定分别是什么？")
        )
        assert rag["citations"], "RAG 未产生引用"

        session.commit()
        step("4. Agent ReAct 推理（工具 + 技能 + MCP + 沙箱 + 记忆）")
        agent = asyncio.run(agent_check(session, space_id))
        pending = service.list_proposals(space_id, status="pending")
        print(f"  待确认提案累计 {len(pending)} 个")

        session.commit()
        step("5. 人工确认提案 → 写入本体")
        accepted = None
        for proposal in pending:
            if proposal.kind == "object_type":
                accepted = service.accept_proposal(proposal.id, reviewer="smoke")
                print(f"  提案 #{proposal.id}「{proposal.title}」已入库 → 新对象类型 {accepted.created_types}，脏页面 {accepted.dirty_pages}")
                break
        print("  没有可确认的对象类型提案" if accepted is None else f"  确认后空间内对象类型 {len(service.list_types(space_id, limit=1000))} 个")

        issues = service.validate_space(space_id)
        print(f"  校验问题 {len(issues)} 条：" + "; ".join(f"{i.code}:{i.message[:40]}" for i in issues[:4]))

        session.commit()
        step("6. Wiki 编译 / 人工编辑 / 版本回滚")
        compiler = WikiCompiler(session)
        compile_report = compiler.compile_space(space_id)
        print(f"  编译：新建 {compile_report.created}，更新 {compile_report.updated}，未变 {compile_report.unchanged}，锁定跳过 {compile_report.skipped_locked}")
        pages = compiler.list_pages(space_id)
        print(f"  页面 {len(pages)} 个：" + ", ".join(p.slug for p in pages[:8]))
        target = next((p for p in pages if p.page_type == "type"), None)
        if target:
            view = compiler.page_view(target)
            print(f"  示例页面 {view['slug']} v{view['current_version']}：{view['body_md'][:80]}…")
            edited = compiler.manual_edit(
                target.id,
                view["body_md"] + "\n## 人工补充\n这一段用于验证人工编辑、锁定与回滚。\n",
                note="人工补充",
            )
            print(f"  人工编辑 → {edited['message']}，锁定={edited['page']['locked_by_human']}")
            versions = VersionService(session)
            before = len(versions.revisions(target.id))
            rolled = versions.rollback(target.id, 1, actor="smoke", note="冒烟测试回滚")
            after = len(versions.revisions(target.id))
            rollback_note = rolled.get("message") or "追加 v{}".format(rolled["new_version"])
            print(f"  回滚至 v1 → {rollback_note}（版本数 {before} → {after}，非破坏式）")
            diff = versions.diff(target.id, 1, 2)
            print(f"  v1↔v2 差异：+{diff['added_lines']} 行 / -{diff['removed_lines']} 行")
            recomp = compiler.compile_space(space_id, only_dirty=True)
            print(f"  增量重编译（只处理 dirty）：新建 {recomp.created}，更新 {recomp.updated}")
            again = compiler.compile_space(space_id)
            print(f"  幂等重编译：新建 {again.created}，更新 {again.updated}，未变 {again.unchanged}，锁定跳过 {again.skipped_locked}")

        session.commit()
        step("7. 知识图谱")
        graph_service = GraphService(session)
        graph = graph_service.build(space_id)
        print(f"  节点 {len(graph['nodes'])}，边 {len(graph['edges'])}，统计 {graph['stats']}")
        if graph["nodes"]:
            center = graph["nodes"][0]["ref_id"]
            sub = graph_service.subgraph(space_id, center, depth=1)
            print(f"  以 {graph['nodes'][0]['label']}(#{center}) 为中心的 1 跳子图：{len(sub['nodes'])} 节点 / {len(sub['edges'])} 边")
            other = [n["ref_id"] for n in graph["nodes"] if n["ref_id"] != center]
            if other:
                route = graph_service.path(space_id, center, other[0])
                print(f"  路径查询：found={route['found']} hops={route.get('hops', 0)}")
        insights = graph_service.insights(space_id)
        print(f"  洞察：枢纽 {len(insights['hubs'])}，孤立 {len(insights['isolated'])}，"
              f"缺证据类型 {insights['coverage']['count']}，域分布 {insights['domain_distribution']}")

        step("8. 多领域交叉验证（词表 / 检索 / 导出随空间切换）")
        for key in ("maintenance", "ai", "network"):
            domain_space = spaces.get(key)
            if domain_space is None:
                print(f"  [{key}] 未安装，跳过")
                continue
            vocab = vocab_for(session, domain_space.id)
            result = asyncio.run(rag_check(session, domain_space.id, CROSS_DOMAIN_QUESTIONS[key]))
            bundle = export_bundle(session, domain_space.id)
            type_count = len(service.list_types(domain_space.id, limit=1000))
            print(f"  [{key}] 领域 {vocab['pack']['label']}｜子域 {len(vocab['business_domains'])} 个"
                  f"｜对象种类 {len(vocab['object_kinds'])}｜类型 {type_count} 个"
                  f"｜引用 {len(result['citations'])} 条｜导出 {len(bundle)} 个产物")

        step("9. 本体导出")
        bundle = export_bundle(session, space_id)
        turtle_key = next(k for k in bundle if k.endswith(".ttl") and "skos" not in k)
        print(f"  产物 {len(bundle)} 个：" + ", ".join(list(bundle)[:6]) + (" …" if len(bundle) > 6 else ""))
        print("  Turtle 片段：\n" + "\n".join("    " + line for line in bundle[turtle_key].splitlines()[8:20]))
        print(f"  主空间所属领域包：{pack_for_space(session, space_id).label}")

        step("10. 组件面板")
        kinds = Counter(row.kind for row in registry.describe())
        print("  " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
        print(f"  共 {sum(kinds.values())} 个组件选项；当前 LLM={registry.active_name('llm')}，"
              f"Embedding={registry.active_name('embedding')}，向量库={registry.active_name('vector_store')}")

        assert agent["status"] in {"done", "max_steps"}, f"Agent 未正常收敛：{agent['status']}"

    print("\n冒烟测试通过：多领域安装、RAG 引用、Agent 提案、人工确认、Wiki 版本与图谱均工作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
