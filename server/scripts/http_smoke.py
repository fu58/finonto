"""HTTP 层端到端冒烟：用 TestClient 打一遍真实接口，验证前端要调的每条链路都能通。

用法：`./.venv/Scripts/python.exe scripts/http_smoke.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = [], []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}{('  → ' + detail) if detail else ''}")


def main() -> int:
    with TestClient(app) as client:
        print("\n[1] 系统与健康")
        r = client.get("/api/health")
        check("GET /api/health", r.status_code == 200, r.json().get("status", ""))
        r = client.get("/api/system/status")
        counts = r.json().get("counts", {})
        check("GET /api/system/status 有计数", r.status_code == 200 and counts.get("object_types", 0) > 0,
              json.dumps(counts, ensure_ascii=False))
        r = client.get("/api/system/samples")
        check("GET /api/system/samples 示例清单", r.status_code == 200 and len(r.json()) >= 4, f"{len(r.json())} 个文件")

        print("\n[2] 本体：空间 / 类型 / 词表 / 校验")
        space_id = client.get("/api/ontology/spaces").json()["spaces"][0]["id"]
        r = client.get(f"/api/ontology/{space_id}/summary")
        check(f"GET /api/ontology/{space_id}/summary", r.status_code == 200, json.dumps(r.json().get("stats", {}), ensure_ascii=False)[:120])
        r = client.get(f"/api/ontology/{space_id}/types", params={"limit": 200})
        types = r.json()["types"]
        check("GET types 列表", r.status_code == 200 and len(types) > 10, f"{len(types)} 个")
        first = types[0]
        r = client.get(f"/api/ontology/types/{first['id']}")
        check("GET type 详情", r.status_code == 200, first["label"])
        r = client.get(f"/api/ontology/{space_id}/validate")
        check("GET validate", r.status_code == 200, f"问题 {len(r.json().get('issues', []))} 条 / 阻断 {r.json().get('blocking')}")
        r = client.get(f"/api/ontology/{space_id}/similar", params={"label": "贷款合同"})
        check("GET similar 对齐已有类型", r.status_code == 200, f"疑似重复 {len(r.json().get('duplicates', []))}")

        print("\n[3] RAG：检索 / 问答 / 流式 / 轨迹")
        q = "互联网贷款业务中，单户在银行的信用贷款总额度上限是多少？"
        r = client.post("/api/rag/search", json={"question": q, "space_id": space_id})
        body = r.json()
        check("POST /api/rag/search 混合检索", r.status_code == 200 and body.get("candidates"),
              f"意图 {body.get('understood', {}).get('intent')}，候选 {len(body.get('candidates', []))}")
        r = client.post("/api/rag/ask", json={"question": q, "space_id": space_id})
        answer = r.json()
        check("POST /api/rag/ask 生成+引用", r.status_code == 200 and answer.get("citations"),
              f"回答 {len(answer.get('answer', ''))} 字，引用 {len(answer.get('citations', []))} 条")
        with client.stream("GET", "/api/rag/stream", params={"question": q, "space_id": space_id}) as stream:
            events = [line for line in stream.iter_lines() if line.startswith("event:")]
        check("GET /api/rag/stream SSE", stream.status_code == 200 and len(events) > 3, f"{len(events)} 个事件帧")
        r = client.get("/api/rag/traces", params={"space_id": space_id})
        check("GET /api/rag/traces", r.status_code == 200, f"{len(r.json().get('traces', []))} 条轨迹")

        print("\n[4] 问答驱动的本体草案（草稿 → 提案）")
        r = client.post(f"/api/ontology/{space_id}/draft", json={"question": "担保人与抵质押物在信贷流程中如何建模？"})
        draft = r.json()
        candidates = draft.get("draft", {}).get("object_types", [])
        check("POST draft 生成草案", r.status_code == 200 and candidates, f"{len(candidates)} 个候选概念，证据 {len(draft.get('evidence', []))} 段")
        r = client.post(f"/api/ontology/{space_id}/draft/propose", json={"question": "反洗钱场景中需要哪些客户风险对象类型？"})
        proposed = r.json()
        check("POST draft/propose 落提案", r.status_code == 200 and proposed.get("proposals"),
              f"新增提案 {len(proposed.get('proposals', []))} 个")

        print("\n[5] Agent：技能 / 工具 / ReAct 推理 / MCP / 沙箱 / 记忆")
        skills = client.get("/api/agent/skills").json()
        check("GET /api/agent/skills", len(skills) >= 3, "、".join(s["display"] for s in skills[:3]))
        tools = client.get("/api/agent/tools", params={"skill": skills[0]["name"]}).json()
        check("GET /api/agent/tools", len(tools["builtin"]) + len(tools["mcp"]) > 10,
              f"内置 {len(tools['builtin'])} + MCP {len(tools['mcp'])}，风险分布 {tools['risk_summary']}")
        r = client.post(
            "/api/agent/run",
            json={
                "goal": "从资料中提炼「借款人风险分级」相关的对象类型并给出定义",
                "space_id": space_id,
                "skill": "doc-extraction",
                "max_steps": 8,
            },
        )
        run = r.json()
        check("POST /api/agent/run ReAct", r.status_code == 200 and run.get("status") in ("done", "max_steps"),
              f"{run.get('steps')} 步 / {run.get('tokens')} tokens / 提案 {len(run.get('proposals', []))} 个 / {run.get('elapsed_ms')}ms")
        with client.stream("GET", "/api/agent/stream", params={"goal": "梳理提前收回贷款的触发条件", "space_id": space_id}) as stream:
            agent_events = [line.split(":", 1)[1].strip() for line in stream.iter_lines() if line.startswith("event:")]
        check("GET /api/agent/stream SSE", stream.status_code == 200 and "final" in agent_events, f"事件 {len(agent_events)} 个")
        runs = client.get("/api/agent/runs", params={"space_id": space_id}).json()["runs"]
        check("GET /api/agent/runs", len(runs) >= 1, f"历史 {len(runs)} 次运行")
        detail = client.get(f"/api/agent/runs/{runs[0]['id']}").json()
        check("GET /api/agent/runs/{id} 步骤可回放", len(detail["steps"]) > 0, f"{len(detail['steps'])} 步")
        mcp = client.get("/api/agent/mcp/servers").json()
        check("GET /api/agent/mcp/servers", len(mcp) >= 3, f"{len(mcp)} 个服务，工具 {sum(len(s['tools']) for s in mcp)} 个")
        r = client.post("/api/agent/sandbox", json={"code": "print(sum(range(101)))"})
        check("POST /api/agent/sandbox 执行", r.status_code == 200 and r.json()["ok"] and "5050" in r.json()["stdout"])
        r = client.post("/api/agent/sandbox", json={"code": "import os; print(os.listdir('.'))"})
        check("沙箱拒绝越权导入", r.json()["ok"] is False and r.json()["violations"], str(r.json()["violations"][:1]))
        r = client.post("/api/agent/memory", json={"content": "监管制度类文档权威度应设为 0.85 以上。", "kind": "preference"})
        memory_id = r.json().get("id")
        check("POST /api/agent/memory 写入长期记忆", bool(memory_id), f"#{memory_id}")
        r = client.post("/api/agent/memory/recall", json={"query": "文档权威度怎么设", "top_k": 3})
        check("POST memory/recall 召回", len(r.json()["hits"]) >= 1, f"{len(r.json()['hits'])} 条命中")

        print("\n[6] 人工确认提案 → 写入本体 → Wiki 增量重编译")
        pending = client.get(f"/api/ontology/{space_id}/proposals", params={"status": "pending"}).json()["proposals"]
        check("GET proposals?status=pending", len(pending) > 0, f"{len(pending)} 个待确认")
        target = next((p for p in pending if p["kind"] == "object_type"), pending[0])
        r = client.post(f"/api/ontology/proposals/{target['id']}/accept", json={"note": "HTTP 冒烟确认", "notify": True})
        accepted = r.json()
        check("POST proposals/{id}/accept", r.status_code == 200 and "result" in accepted,
              json.dumps(accepted.get("result", {}), ensure_ascii=False)[:140])
        check("确认后立即通知 IM 渠道", bool(accepted.get("side_effects", {}).get("notify")))
        r = client.get("/api/wiki/pages", params={"space_id": space_id})
        pages = r.json()["pages"]
        check("GET /api/wiki/pages 编译产物", len(pages) > 5, f"{len(pages)} 页")
        type_page = next((p for p in pages if p["page_type"] == "type"), pages[0])
        page = client.get(f"/api/wiki/pages/{type_page['id']}").json()
        check("GET wiki page 正文", len(page["body_md"]) > 50, f"{page['title']} v{page['current_version']}")
        r = client.put(
            f"/api/wiki/pages/{type_page['id']}",
            json={"body_md": page["body_md"] + "\n\n## 人工补充\n这一节由人工编辑追加，用于验证版本与回滚。\n", "note": "补充口径"},
        )
        check("PUT wiki page 人工编辑", r.status_code == 200 and r.json()["changed"], f"→ v{r.json()['page']['current_version']}")
        new_version = r.json()["page"]["current_version"]
        r = client.get(f"/api/wiki/pages/{type_page['id']}/diff", params={"left": new_version - 1, "right": new_version})
        d = r.json()
        check("GET wiki diff", d["added_lines"] >= 1, f"+{d['added_lines']} / -{d['removed_lines']} 行")
        r = client.post(f"/api/wiki/pages/{type_page['id']}/rollback", json={"to_version": new_version - 1})
        rolled = r.json()
        check("POST wiki rollback 非破坏回滚", rolled["rolled_back"] and rolled["new_version"] > new_version,
              f"v{rolled['from_version']} → 追加 v{rolled['new_version']}")
        revisions = client.get(f"/api/wiki/pages/{type_page['id']}/revisions").json()["revisions"]
        check("版本链保留", len(revisions) >= 3, f"{len(revisions)} 个版本")

        print("\n[7] 知识图谱")
        g = client.get("/api/wiki/graph", params={"space_id": space_id}).json()
        check("GET graph", len(g["nodes"]) > 5 and len(g["edges"]) > 5, f"{len(g['nodes'])} 节点 / {len(g['edges'])} 边")
        center = g["nodes"][0]
        sub = client.get(
            "/api/wiki/graph/subgraph", params={"space_id": space_id, "center_type_id": center["ref_id"], "depth": 1}
        ).json()
        check("GET graph/subgraph", len(sub["nodes"]) >= 1, f"{len(sub['nodes'])} 节点 / {len(sub['edges'])} 边")
        ins = client.get("/api/wiki/graph/insights", params={"space_id": space_id}).json()
        check("GET graph/insights", "coverage" in ins, f"缺证据类型 {len(ins['coverage']['types_without_evidence'])} 个")

        print("\n[8] 组件中心")
        catalog = client.get("/api/components").json()
        check("GET /api/components", len(catalog["groups"]) >= 8,
              " / ".join(f"{g['label']}({g['count']})" for g in catalog["groups"]))
        check("密钥只回显是否配置", catalog["settings"]["llm_key_present"] in (True, False)
              and "llm_api_key" not in catalog["settings"])
        for kind in ("llm", "embedding", "vector_store", "object_store", "rerank", "doc_parser"):
            r = client.post("/api/components/probe", json={"kind": kind})
            check(f"POST probe {kind}", r.status_code == 200 and r.json()["ok"], str(r.json())[:100])
        r = client.post("/api/components/active", json={"kind": "rerank", "name": "heuristic"})
        check("POST /api/components/active 切换实现", r.status_code == 200, r.json()["active"])

        print("\n[9] 文档与导出")
        docs = client.get("/api/documents", params={"space_id": space_id}).json()
        check("GET /api/documents", len(docs["documents"]) >= 4, f"{len(docs['documents'])} 篇 / {docs['total_chunks']} 片段")
        r = client.post("/api/documents/text", json={
            "space_id": space_id, "name": "HTTP 冒烟补充材料.md", "doc_type": "参考资料",
            "text": "# 合作机构准入\n开展联合贷前，银行应对合作机构的授信审批与风控能力进行评估，并明确风险分担安排。",
        })
        report = r.json().get("report", {})
        check("POST /api/documents/text 纯文本入库", r.status_code == 200 and report.get("chunks", 0) > 0,
              f"{report.get('chunks')} 片段 / {report.get('elapsed_ms')}ms")
        new_doc_id = report.get("doc_id")
        chunks = client.get(f"/api/documents/{new_doc_id}/chunks").json()
        check("GET /api/documents/{id}/chunks", len(chunks.get("chunks", [])) > 0, f"{len(chunks.get('chunks', []))} 片段")
        r = client.get(f"/api/ontology/{space_id}/export")
        bundle = r.json()
        check("GET export Turtle/SKOS/JSON Schema", bundle.get("count", 0) >= 1, f"{bundle['count']} 个产物")
        r = client.delete(f"/api/documents/{new_doc_id}")
        check("DELETE /api/documents/{id}", r.status_code == 200, str(r.json())[:80])

        print("\n[10] 多领域：领域包目录 / 幂等安装 / 空间感知词表 / 跨空间问答")
        packs = client.get("/api/ontology/packs").json()["packs"]
        check("GET /api/ontology/packs 领域包画廊", len(packs) >= 5,
              "、".join(f"{p['label']}({'已装' if p['installed'] else '未装'})" for p in packs))
        keys = {p["key"] for p in client.get("/api/system/packs").json()}
        check("GET /api/system/packs 轻量目录", {"finance", "maintenance", "ai", "network", "general"} <= keys, "、".join(sorted(keys)))
        net = next(p for p in packs if p["key"] == "network")
        net_space = net["space"]["id"]
        r = client.post("/api/ontology/packs/network/install", params={"load_samples": False, "compile_wiki": False})
        check("POST packs/{key}/install 幂等重装", r.status_code == 200 and r.json()["space_id"] == net_space,
              json.dumps(r.json().get("seed", {}), ensure_ascii=False)[:60])
        check("POST /api/system/bootstrap?pack_keys 只装指定包", client.post("/api/system/bootstrap", params={"pack_keys": "ai"}).status_code == 200)
        check("未知领域包返回 400/404", client.post("/api/system/bootstrap", params={"pack_keys": "not-a-domain"}).status_code == 400
              and client.post("/api/ontology/packs/not-a-domain/install").status_code == 404)
        general_space = next(p["space"]["id"] for p in packs if p["key"] == "general")
        for sid, want in ((net_space, "计算机网络"), (general_space, "通用")):
            v = client.get("/api/ontology/vocab", params={"space_id": sid}).json()
            check(f"GET vocab?space_id={sid} 词表跟随空间领域", want in v["pack"]["label"],
                  f"{v['pack']['label']}｜子域 {len(v['business_domains'])} 个｜标准 {len(v['standards'])} 条")
        starters = {k: client.get("/api/ontology/vocab", params={"space_id": p["space"]["id"]}).json()["sample_questions"]
                    for k, p in ((p["key"], p) for p in packs if p["space"])}
        check("每个领域包自带起手问题", all(starters[k] for k in starters),
              "；".join(f"{k}:{starters[k][0][:14]}…" for k in ("finance", "maintenance", "ai", "network", "general")))
        net_vocab = client.get("/api/ontology/vocab", params={"space_id": net_space}).json()
        r = client.post(f"/api/ontology/{net_space}/draft", json={"question": "网络故障处置流程里需要哪些对象类型？"})
        domains = {t.get("business_domain") for t in r.json().get("draft", {}).get("object_types", [])}
        check("非金融空间的草案子域受领域包约束", bool(r.json().get("draft", {}).get("object_types"))
              and domains <= set(net_vocab["business_domains"]), f"子域 {sorted(domains)}")
        q_net = "跨机房链路时延的阈值是多少？"
        r = client.post("/api/rag/ask", json={"question": q_net, "space_id": net_space})
        check("POST /api/rag/ask 在非金融空间作答", bool(r.json().get("citations")),
              f"{len(r.json().get('answer', ''))} 字 / 引用 {len(r.json().get('citations', []))} 条")
        r = client.get(f"/api/ontology/{net_space}/export")
        check("GET 非金融空间导出", r.json().get("count", 0) >= 1, f"{r.json()['count']} 个产物")

    print(f"\n{'=' * 72}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败清单：" + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
