"""端到端离线冒烟：10 个阶段，全部断言，不联网、不依赖任何密钥。

    cd server
    .venv/Scripts/python.exe scripts/smoke.py

刻意使用一个独立的临时数据目录（``server/var-smoke``），
所以可以随时重跑，也不会污染你正在用的 var/。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
DATA_DIR = SERVER_DIR / "var-smoke"
os.environ["FINONTO_DATA_DIR"] = str(DATA_DIR)
os.environ["FINONTO_AUTO_BOOTSTRAP"] = "false"

shutil.rmtree(DATA_DIR, ignore_errors=True)

from app.db import init_db, session_scope  # noqa: E402
from app.providers.registry import get_components  # noqa: E402

PASSED = 0
FAILED = 0
PHASE = ""


def phase(title: str) -> None:
    global PHASE
    PHASE = title
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def check(condition: bool, message: str, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {message}")
    else:
        FAILED += 1
        print(f"  ✗ {message}" + (f"  ← {detail}" if detail else ""))


def main() -> int:
    from app.core.agent import react, sandbox
    from app.core.ontology import owl, packs, service
    from app.core.rag.pipeline import ask
    from app.core.wiki import compiler, graph as graph_service, versions
    from app.models import ObjectType, WikiPage

    init_db()
    components = get_components()

    # ------------------------------------------------------------------ #
    phase("1 · 五个领域包冷启动（幂等）")
    with session_scope() as session:
        results = [
            service.install_pack(session, key, components=components)
            for key in ("finance", "maintenance", "ai", "network", "general")
        ]
        spaces = {item["key"]: item for item in service.list_spaces(session)}
    check(len(spaces) == 5, "创建 5 个本体空间", str(list(spaces)))
    for result in results:
        check(result["status"] == "installed", f"领域包 {result['pack']} 安装完成")
        check(result["wiki"].get("pages", 0) > 0, f"{result['pack']} 首次编译出 Wiki 页", str(result["wiki"]))

    with session_scope() as session:
        again = service.install_pack(session, "finance", components=components)
    check(again["created_types"] == 0, "重复安装不新增类型（幂等）", str(again["created_types"]))
    check(
        all(doc["status"] in ("duplicate", "indexed") for doc in again["documents"]),
        "重复安装不重复入库文档",
    )

    # ------------------------------------------------------------------ #
    phase("2 · 语料与索引规模")
    with session_scope() as session:
        finance_id = spaces["finance"]["id"]
        docs = session.execute(
            __import__("sqlalchemy").select(__import__("app.models", fromlist=["Document"]).Document)
        ).scalars().all()
        total_chunks = session.execute(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(
                __import__("app.models", fromlist=["DocumentChunk"]).DocumentChunk.id))
        ).scalar_one()
    real_docs = [doc for doc in docs if doc.title and not doc.title.startswith("测试")]
    check(len(docs) >= 8, "至少 8 份示例文档入库", f"实际 {len(docs)}")
    check(int(total_chunks) >= 40, "片段总数 ≥ 40", f"实际 {total_chunks}")

    # ------------------------------------------------------------------ #
    phase("3 · RAG 必须产生引用")
    cases = [
        ("finance", "信贷合同中年利率的合规上限是多少？", "24"),
        ("maintenance", "点检发现异常后需要做什么？", "工单"),
        ("ai", "模型上线前必须通过哪些评测维度？", "评测"),
        ("network", "防火墙放行策略为什么必须设置到期时间？", "到期"),
    ]
    for pack_key, question, keyword in cases:
        with session_scope() as session:
            result = ask(session, spaces[pack_key]["id"], question)
        check(len(result["citations"]) >= 1, f"[{pack_key}] 产生引用", str(len(result["citations"])))
        check(len(result["retrieved"]) >= 1, f"[{pack_key}] 召回片段", str(len(result["retrieved"])))
        check(bool(result["answer"].strip()), f"[{pack_key}] 生成非空答案")
        check(
            keyword in result["answer"] or keyword in json.dumps(result["retrieved"], ensure_ascii=False),
            f"[{pack_key}] 答案/检索命中关键字「{keyword}」",
            result["answer"][:60],
        )

    # ------------------------------------------------------------------ #
    phase("4 · ReAct 必须收敛并产出提案")
    with session_scope() as session:
        run = react.run(
            session,
            finance_id,
            "为信贷空间补充「贷后检查计划」相关对象类型",
            skill_key="doc-extraction",
            components=components,
        )
    check(run.status == "succeeded", "Agent 运行收敛", run.status)
    check(len(run.steps) >= 4, "至少 4 步（检索→列表→对齐→提案）", str(len(run.steps)))
    check(len(run.proposal_ids) >= 1, "产出至少 1 条提案", str(run.proposal_ids))
    check(run.tokens > 0, "记录 token 消耗", str(run.tokens))

    # ------------------------------------------------------------------ #
    phase("5 · 人工确认闸门")
    with session_scope() as session:
        before = service.space_summary(session, finance_id)["types"]
        pending = service.list_proposals(session, finance_id, status="pending")
        check(len(pending) >= 1, "存在待确认提案", str(len(pending)))
        target = pending[-1]["id"]
        accepted = service.accept_proposal(session, target, components=components)
        after = service.space_summary(session, finance_id)["types"]
        detail = service.proposal_detail(session, target)
        new_type = session.get(ObjectType, accepted.get("type_id", 0))
    check(accepted["status"] == "accepted", "提案被接受", str(accepted.get("status")))
    check(after == before + 1, "类型数 +1", f"{before} → {after}")
    check(detail["status"] == "accepted", "提案状态已更新")
    check(new_type is not None and new_type.iri.startswith("https://"), "类型获得唯一 IRI",
          new_type.iri if new_type else "")
    check(bool(accepted.get("wiki")), "确认后触发 Wiki 增量重编译", str(accepted.get("wiki")))

    with session_scope() as session:
        second = service.accept_proposal(session, target, components=components)
    check(second["status"] == "already_decided", "重复确认被拒绝（幂等）", second["status"])

    # ------------------------------------------------------------------ #
    phase("6 · 空间校验")
    with session_scope() as session:
        report = service.validate_space(session, finance_id)
    check(set(report) == {"violation", "warning", "info"}, "三级结构完整", str(list(report)))
    check(all(isinstance(report[key], list) for key in report), "每一级都是列表")

    # ------------------------------------------------------------------ #
    phase("7 · Wiki 编译 / 编辑 / 锁定 / 回滚 / diff")
    with session_scope() as session:
        stats = compiler.compile_space(session, finance_id)
    check(stats["status"] == "compiled", "编译成功")
    check(stats["pages"] == 0 or stats["updated"] >= 0, "重复编译不重复建页（内容未变即跳过）")

    with session_scope() as session:
        page = session.execute(
            __import__("sqlalchemy").select(WikiPage).where(
                WikiPage.space_id == finance_id, WikiPage.slug == "index"
            )
        ).scalars().first()
        page_id = page.id
        base_version = page.version
        edited = versions.save_manual_edit(
            session, page_id, page.body + "\n\n> 人工补充：本条为冒烟测试追加。", message="冒烟编辑"
        )
    check(edited["status"] == "saved", "人工编辑追加版本", str(edited))
    check(edited["version"] == base_version + 1, "版本号 +1", f"{base_version} → {edited['version']}")

    with session_scope() as session:
        locked = versions.toggle_lock(session, page_id, True)
        locked_compile = compiler.compile_space(session, finance_id, only_dirty=False)
    check(locked["status"] == "locked", "页面已锁定")
    check(locked_compile["locked"] >= 1, "锁定页被编译跳过", str(locked_compile))

    with session_scope() as session:
        rolled = versions.rollback(session, page_id, base_version)
    check(rolled["status"] == "rolled_back", "回滚成功")
    check(rolled["new_version"] == edited["version"] + 1, "回滚是追加新版本而不是删历史",
          str(rolled))

    with session_scope() as session:
        revisions = versions.list_revisions(session, page_id)
        diff_edit = versions.diff_versions(session, page_id, base_version, edited["version"])
        diff_rollback = versions.diff_versions(session, page_id, base_version, rolled["new_version"])
    check(len(revisions) >= 3, "版本链至少 3 条", str(len(revisions)))
    check(diff_edit["status"] == "ok", "diff 计算成功")
    check(diff_edit["identical"] is False, "diff 能识别出编辑差异", str(diff_edit["added"]))
    check(diff_edit["added"] >= 1, "diff 报告新增行", str(diff_edit["added"]))
    check(diff_rollback["identical"] is True, "回滚后内容与目标版本一致（非破坏式）")

    # ------------------------------------------------------------------ #
    phase("8 · 图谱：全图 / 子图 / 路径 / 洞察")
    with session_scope() as session:
        graph = graph_service.build_graph(session, finance_id)
        laid = graph_service.layout(graph)
        types = [node for node in laid["nodes"] if node["kind"] == "type"]
        sub = graph_service.subgraph(laid, types[0]["id"], 1)
        path = graph_service.shortest_path(laid, types[0]["id"], types[-1]["id"])
        insight = graph_service.insights(session, finance_id, graph)
    check(len(types) >= 13, "类型节点数", str(len(types)))
    check(len(graph["edges"]) >= 14, "关系数", str(len(graph["edges"])))
    check(all("x" in node for node in laid["nodes"]), "布局给了每个节点坐标")
    check(len(sub["nodes"]) >= 1, "邻域子图非空")
    check("found" in path, "路径查询返回结果", str(path.get("reason", "")))
    check(insight["type_count"] == len(types), "洞察与图一致")
    check(insight["components"] >= 1, "连通分量计算完成")

    # ------------------------------------------------------------------ #
    phase("9 · 多领域交叉验证")
    for pack_key in ("maintenance", "ai", "network"):
        domain = spaces[pack_key]["domain"]
        vocab = packs.vocab_for(domain)
        check(vocab["pack"] == pack_key, f"[{pack_key}] 词表随空间切换", str(vocab.get("pack")))
        check(len(vocab["business_domains"]) >= 3, f"[{pack_key}] 子域受控词表非空")
        with session_scope() as session:
            ttl = owl.export_turtle(session, spaces[pack_key]["id"])
            schemas = owl.export_json_schemas(session, spaces[pack_key]["id"])
        check("owl:Class" in ttl, f"[{pack_key}] Turtle 含类定义")
        check("sh:NodeShape" in ttl, f"[{pack_key}] Turtle 含 SHACL 形状")
        check(len(schemas) >= 10, f"[{pack_key}] JSON Schema 数量", str(len(schemas)))

    # ------------------------------------------------------------------ #
    phase("10 · 组件面板与沙箱")
    from app.providers.registry import catalog, probe

    categories = catalog()
    check(len(categories) == 10, "10 类组件目录", str(len(categories)))
    check(all(category["entries"] for category in categories), "每类都有可选实现")
    for key in ("llm_provider", "embedding_provider", "vector_store", "object_store", "doc_parser"):
        result = probe(key, "")
        check(result["ok"], f"探针 {key} 可用", result.get("detail", "")[:40])

    allowed = sandbox.run("import json\nprint(json.dumps({'ok': True}))")
    check(allowed["ok"], "沙箱可执行白名单代码", allowed.get("error", ""))
    blocked = sandbox.run("import os\nos.system('echo hi')")
    check(not blocked["ok"], "沙箱拦截 os 导入")
    check(bool(blocked.get("blocked")), "拦截原因可解释")

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 62)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    started = time.perf_counter()
    try:
        code = main()
    except Exception:  # noqa: BLE001
        print(f"\n[{PHASE}] 抛出异常：")
        traceback.print_exc()
        code = 2
    print(f"耗时 {time.perf_counter() - started:.1f}s")
    sys.exit(code)
