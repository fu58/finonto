"""HTTP 层冒烟：用 TestClient 打真实接口，覆盖前端会调用的每条链路。

    cd server
    .venv/Scripts/python.exe scripts/http_smoke.py

与 smoke.py 的分工：smoke.py 直接调服务层（覆盖业务逻辑分支），
本脚本走 ASGI（覆盖路由、参数校验、状态码、SSE 帧、安全项）。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
DATA_DIR = SERVER_DIR / "var-http-smoke"
os.environ["FINONTO_DATA_DIR"] = str(DATA_DIR)
os.environ["FINONTO_AUTO_BOOTSTRAP"] = "true"
HTTP_SMOKE_PASSWORD = "HttpSmoke-123"

shutil.rmtree(DATA_DIR, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASSED = 0
FAILED = 0
NOTES: list[str] = []


def check(condition: bool, message: str, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {message}")
    else:
        FAILED += 1
        NOTES.append(message)
        print(f"  ✗ {message}" + (f"  ← {detail}" if detail else ""))


def main() -> int:
    with TestClient(app) as client:
        print("\n── 认证（默认开启）──────────────────────────────────")
        check(client.get("/api/spaces").status_code == 401, "未登录时业务 API 401")
        # 把管理员密码重置为一个已知值并清除「强制改密」，后续用例都用这个身份
        from app.core import auth_service
        from app.db import session_scope

        with session_scope() as session:
            admin = auth_service.user_by_username(session, "admin")
            check(admin is not None, "冷启动创建了管理员 admin")
            if admin is not None:
                auth_service.reset_password(session, admin.id, HTTP_SMOKE_PASSWORD, must_change=False)
        login_response = client.post(
            "/login",
            data={"username": "admin", "password": HTTP_SMOKE_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        check(login_response.status_code == 303, "管理员登录成功", str(login_response.status_code))
        check(
            login_response.headers.get("set-cookie", "").find("HttpOnly") >= 0,
            "会话 cookie 带 HttpOnly",
        )

        print("\n── 冷启动与基础接口 ──────────────────────────────────")
        health = client.get("/api/health")
        payload = health.json()
        check(health.status_code == 200, "/api/health 200")
        check(payload["status"] == "ok", "健康状态 ok")
        check(payload["spaces"] == 5, "冷启动装入 5 个空间", str(payload["spaces"]))

        spaces = client.get("/api/spaces").json()["spaces"]
        check(len(spaces) == 5, "空间列表 5 条")
        space_id = spaces[0]["id"]
        check(
            client.get(f"/api/spaces/{space_id}").status_code == 200, "空间详情 200"
        )
        check(client.get("/api/spaces/9999").status_code == 404, "未知空间 404")

        print("\n── 页面渲染（9 个页签）──────────────────────────────")
        pages = ["/", "/agent", "/ontology", "/proposals", "/wiki", "/graph", "/documents", "/packs", "/components"]
        for path in pages:
            response = client.get(f"{path}?space={space_id}")
            check(response.status_code == 200, f"GET {path} 200", str(response.status_code))
            check(len(response.text) > 1500, f"GET {path} 有内容", str(len(response.text)))
        home = client.get(f"/?space={space_id}").text
        check("data-ask=" in home, "问答页渲染起手问题")
        check("data-stream=" in home, "问答页挂了 SSE 端点")

        print("\n── 问答（同步 + SSE）────────────────────────────────")
        ask = client.post(f"/api/rag/ask?space={space_id}&q=信贷合同年利率上限")
        data = ask.json()
        check(ask.status_code == 200, "POST /api/rag/ask 200")
        check(len(data["citations"]) >= 1, "同步问答产生引用", str(len(data["citations"])))
        check(data["plan"]["intent"] != "", "查询理解给出意图")
        check(data["trace_id"] > 0, "轨迹落库")

        frames = 0
        token_frames = 0
        with client.stream("GET", f"/api/rag/stream?space={space_id}&q=贷后检查频率") as stream:
            check(stream.status_code == 200, "SSE 200")
            check("text/event-stream" in stream.headers.get("content-type", ""), "SSE content-type")
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    frames += 1
                    if '"type": "token"' in line:
                        token_frames += 1
        check(frames >= 5, "SSE 至少 5 帧", str(frames))
        check(token_frames >= 1, "SSE 含 token 帧", str(token_frames))

        print("\n── 提案（草案 → 确认 → 冲突）────────────────────────")
        draft = client.post(f"/api/ontology/{space_id}/draft?question=补充贷后检查计划相关类型")
        check(draft.status_code == 200, "草案接口 200")
        proposals = client.get(f"/api/ontology/{space_id}/proposals?status=pending").json()["proposals"]
        check(len(proposals) >= 1, "草案产出待确认提案", str(len(proposals)))
        pid = proposals[-1]["id"]
        check(client.get(f"/api/ontology/proposals/{pid}").status_code == 200, "提案详情 200")
        accepted = client.post(f"/api/ontology/proposals/{pid}/accept")
        check(accepted.status_code == 200, "确认提案 200")
        check(accepted.json()["status"] == "accepted", "确认结果 accepted")
        again = client.post(f"/api/ontology/proposals/{pid}/accept")
        check(again.status_code == 200 and again.json()["status"] == "already_decided", "重复确认被拒")
        check(
            client.post("/api/ontology/proposals/99999/accept").status_code == 404,
            "未知提案 404",
        )

        print("\n── Agent（流式 + 沙箱 + MCP）───────────────────────")
        agent_frames = 0
        with client.stream(
            "GET", f"/api/agent/stream?space={space_id}&goal=补充来源登记相关类型&skill=doc-extraction"
        ) as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    agent_frames += 1
        check(agent_frames >= 6, "Agent SSE 至少 6 帧", str(agent_frames))

        sandbox = client.post("/api/agent/sandbox", json={"code": "import os\nprint(os.listdir('.'))"})
        check(sandbox.json()["ok"] is False, "沙箱拒绝 os 导入")
        check(bool(sandbox.json().get("blocked")), "沙箱给出拦截原因")
        safe = client.post("/api/agent/sandbox", json={"code": "print(sum([1,2,3]))"})
        check(safe.json()["ok"] is True, "沙箱允许白名单代码")

        validate = client.post("/api/agent/sandbox/validate", json={"code": "open('x','w')"})
        check(validate.json()["ok"] is False, "沙箱预检拦截 open")

        mcp = client.get("/api/agent/mcp").json()
        check(len(mcp["servers"]) >= 3, "内置 MCP 服务 3 个", str(len(mcp["servers"])))
        check(len(mcp["tools"]) >= 8, "MCP 工具不少于 8 个", str(len(mcp["tools"])))
        call = client.post(
            f"/api/agent/mcp/finonto-ontology/call?space={space_id}",
            json={"tool": "list_types", "arguments": {}},
        )
        check(call.json()["ok"] is True, "MCP 直连调用成功")
        check(client.get("/api/agent/skills").json()["skills"], "技能列表非空")

        print("\n── Wiki（编译 / 编辑 / 回滚 / diff）─────────────────")
        pages_json = client.get(f"/api/wiki/pages?space={space_id}").json()["pages"]
        check(len(pages_json) >= 15, "Wiki 页面数 ≥ 15", str(len(pages_json)))
        index_page = next((p for p in pages_json if p["slug"] == "index"), pages_json[0])
        page_id = index_page["id"]
        before = client.get(f"/api/wiki/pages/{page_id}").json()
        edit = client.post(
            f"/api/wiki/pages/{page_id}/edit",
            json={"body": before["body"] + "\n\n> HTTP 冒烟追加。", "message": "http smoke"},
        )
        check(edit.json()["status"] == "saved", "编辑页面成功")
        revs = client.get(f"/api/wiki/pages/{page_id}/revisions").json()["revisions"]
        check(len(revs) >= 2, "版本链 ≥ 2", str(len(revs)))
        old_version = revs[-1]["version"]
        diff = client.get(f"/api/wiki/pages/{page_id}/diff?left={old_version}&right={revs[0]['version']}")
        check(diff.json()["status"] == "ok", "diff 接口可用")
        rollback = client.post(f"/api/wiki/pages/{page_id}/rollback", json={"version": old_version})
        check(rollback.json()["status"] == "rolled_back", "回滚成功")
        lock = client.post(f"/api/wiki/pages/{page_id}/lock", json={"locked": True})
        check(lock.json()["status"] == "locked", "锁定成功")

        print("\n── 图谱与导出 ─────────────────────────────────────")
        graph = client.get(f"/api/wiki/graph?space={space_id}").json()
        check(len(graph["nodes"]) > 0, "全图非空")
        check(all("x" in node for node in graph["nodes"]), "全图带坐标")
        node_id = next(node["id"] for node in graph["nodes"] if node["kind"] == "type")
        sub = client.get(f"/api/wiki/subgraph?space={space_id}&node={node_id}&depth=1").json()
        check(len(sub["nodes"]) >= 1, "子图非空")
        insight = client.get(f"/api/wiki/insights?space={space_id}").json()
        check(insight["type_count"] > 0, "洞察有类型数")
        svg = client.get(f"/api/wiki/graph.svg?space={space_id}")
        check(svg.status_code == 200 and "<svg" in svg.text, "SVG 导出 200")

        ttl = client.get(f"/api/ontology/{space_id}/export?format=turtle")
        check(ttl.status_code == 200 and "owl:Class" in ttl.text, "Turtle 导出")
        skos = client.get(f"/api/ontology/{space_id}/export?format=skos")
        check(skos.status_code == 200 and "skos:Concept" in skos.text, "SKOS 导出")
        schema = client.get(f"/api/ontology/{space_id}/export?format=json")
        check(len(schema.json()["schemas"]) > 0, "JSON Schema 导出")
        check(
            client.get(f"/api/ontology/{space_id}/export?format=bogus").status_code == 422,
            "非法导出格式 422",
        )

        print("\n── 文档入库 ───────────────────────────────────────")
        upload = client.post(
            f"/api/documents/upload?space={space_id}&filename=smoke.md&title=HTTP冒烟文档",
            content="# 测试制度\n\n第一条 本文件由冒烟脚本写入。\n".encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
        )
        check(upload.status_code == 200, "上传入库 200")
        check(upload.json()["status"] == "indexed", "状态 indexed")
        doc_id = upload.json()["document_id"]
        duplicate = client.post(
            f"/api/documents/upload?space={space_id}&filename=smoke.md&title=HTTP冒烟文档",
            content="# 测试制度\n\n第一条 本文件由冒烟脚本写入。\n".encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
        )
        check(duplicate.json()["status"] == "duplicate", "相同内容不重复入库")
        check(
            client.post(f"/api/documents/upload?space={space_id}&filename=empty.md", content=b"").status_code
            == 400,
            "空文件 400",
        )
        check(client.post(f"/api/documents/{doc_id}/reindex").json()["status"] == "reindexed", "重建索引")
        chunks = client.get(f"/api/documents/{doc_id}/chunks").json()["chunks"]
        check(len(chunks) >= 1, "片段可查", str(len(chunks)))
        check(client.delete(f"/api/documents/{doc_id}").json()["status"] == "deleted", "删除文档")

        print("\n── 领域包与组件中心 ────────────────────────────────")
        gallery = client.get("/api/ontology/packs").json()
        check(gallery["count"] == 5, "领域包画廊 5 条")
        check(all(item["installed"] for item in gallery["packs"]), "5 个领域包均已安装")
        check(
            client.post("/api/ontology/packs/finance/install").json()["status"] == "installed",
            "领域包安装幂等",
        )
        check(
            client.post("/api/ontology/packs/unknown/install").status_code == 404,
            "未知领域包 404",
        )

        comps = client.get("/api/components").json()
        check(len(comps["categories"]) == 10, "10 类组件")
        secrets = client.get("/api/components/secrets").json()
        check(all(value is False for value in secrets["configured"].values()), "零密钥下全部未配置")
        check(
            all(isinstance(value, bool) for value in secrets["configured"].values()),
            "密钥接口只返回布尔状态（不回显原文）",
        )
        check("sk-" not in json_dumps(secrets), "响应中不含任何密钥样本")
        probe = client.get("/api/components/probe?category=llm_provider&key=mock").json()
        check(probe["ok"] is True, "LLM 探针可用")
        check(
            client.post("/api/components/activate", json={"category": "llm", "key": "nope"}).status_code == 400,
            "非法组件实现 400",
        )
        deploy = client.get("/api/components/deploy").json()
        check(deploy["single_port"] is True, "部署清单声明单端口")

    print("\n" + "=" * 62)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if NOTES:
        print("失败项：" + "；".join(NOTES))
    print("=" * 62)
    return 0 if FAILED == 0 else 1


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    started = time.perf_counter()
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 2
    print(f"耗时 {time.perf_counter() - started:.1f}s")
    sys.exit(code)
