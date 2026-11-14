"""认证 / 授权 / 审计 / 备份 / 限流 的 HTTP 层冒烟。

    cd server
    .venv/Scripts/python.exe scripts/auth_smoke.py

与其他脚本的分工
----------------
* ``smoke.py``       —— 服务层业务链路（不关心 HTTP）
* ``http_smoke.py``  —— 业务 API 与页面（以默认管理员身份）
* ``auth_smoke.py``  —— **安全边界**：未登录、登录失败、强制改密、四级角色、审计留痕、限流、备份
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
DATA_DIR = SERVER_DIR / "var-auth-smoke"
os.environ["FINONTO_DATA_DIR"] = str(DATA_DIR)
os.environ["FINONTO_AUTO_BOOTSTRAP"] = "true"
os.environ.setdefault("FINONTO_RATE_LIMIT_PER_MIN", "60")

shutil.rmtree(DATA_DIR, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(condition: bool, message: str, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {message}")
    else:
        FAILED.append(message)
        print(f"  ✗ {message}" + (f"  ← {detail}" if detail else ""))


def form_post(client, path: str, data: dict, **kwargs):
    return client.post(
        path,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
        **kwargs,
    )


def login(client, username: str, password: str):
    return form_post(client, "/login", {"username": username, "password": password})


def change_password(client, old: str, new: str):
    return form_post(client, "/profile/password", {"old_password": old, "new_password": new})


def main() -> int:
    with TestClient(app, follow_redirects=False) as client:
        print("\n── 未登录访问 ──────────────────────────────────")
        response = client.get("/")
        check(response.status_code == 303 and "/login" in response.headers.get("location", ""),
              "页面未登录跳登录", str(response.status_code))
        check(client.get("/api/spaces").status_code == 401, "业务 API 未登录 401")
        check(client.get("/users").status_code == 303, "管理页未登录跳登录")
        for public in ("/api/health", "/api/ready", "/api/metrics"):
            check(client.get(public).status_code == 200, f"{public} 公开可访问")
        check("finonto_requests_total" in client.get("/api/metrics").text, "指标含请求计数")
        ready = client.get("/api/ready").json()
        check(ready["ready"] is True, "就绪探针 ready")

        print("\n── 登录与强制改密 ──────────────────────────────")
        check(login(client, "admin", "wrong-password").status_code == 401, "错误密码 401")
        response = login(client, "admin", "admin123")
        check(response.status_code == 303, "初始密码可登录")
        check(response.headers.get("location") == "/profile", "首次登录强制改密")
        check(bool(response.cookies.get("finonto_session")), "下发 HttpOnly 会话 cookie")
        cookie_header = response.headers.get("set-cookie", "")
        check("HttpOnly" in cookie_header, "cookie 带 HttpOnly", cookie_header[:80])
        check(client.get("/").status_code == 303, "未改密时业务页被拦")
        check(client.get("/api/spaces").status_code == 403, "未改密时业务 API 被拦")

        weak = change_password(client, "admin123", "12345678")
        check(weak.status_code == 400, "弱密码被拒绝（纯数字）", str(weak.status_code))
        changed = change_password(client, "admin123", "Adm1n-Passw0rd!")
        check(changed.status_code == 303, "改密成功")
        check(login(client, "admin", "Adm1n-Passw0rd!").status_code == 303, "新密码可登录")
        for path in ("/", "/packs", "/ontology", "/wiki", "/graph", "/documents",
                     "/components", "/ops", "/users", "/audit", "/profile"):
            check(client.get(path).status_code == 200, f"管理员可访问 {path}")

        print("\n── 用户与四级角色 ──────────────────────────────")
        check(form_post(client, "/users/create",
                        {"username": "zhang", "password": "viewer-pass1", "role": "viewer"}).status_code == 303,
              "创建 viewer")
        check(form_post(client, "/users/create",
                        {"username": "li", "password": "approver-pass1", "role": "approver"}).status_code == 303,
              "创建 approver")
        check(form_post(client, "/users/create",
                        {"username": "wang", "password": "editor-pass1", "role": "editor"}).status_code == 303,
              "创建 editor")
        duplicate = form_post(client, "/users/create",
                              {"username": "zhang", "password": "viewer-pass1", "role": "viewer"})
        check(duplicate.status_code == 303, "重名用户被拒（重定向带冲突状态）")

        for user, initial, final in (
            ("zhang", "viewer-pass1", "viewer-pass2"),
            ("wang", "editor-pass1", "editor-pass2"),
            ("li", "approver-pass1", "approver-pass2"),
        ):
            client.get("/logout")
            checked = login(client, user, initial)
            check(checked.status_code == 303 and checked.headers.get("location") == "/profile",
                  f"{user} 首次登录强制改密")
            change_password(client, initial, final)
            login(client, user, final)

            if user == "zhang":
                check(client.get("/").status_code == 200, "viewer 可看问答")
                check(client.get("/documents").status_code == 403, "viewer 不能进文档页")
                check(client.get("/wiki").status_code == 200, "viewer 可看 Wiki")
                check(client.post("/api/ontology/1/types", json={"label": "x"}).status_code == 403,
                      "viewer 不能建类型")
                check(client.post("/api/ontology/proposals/1/accept").status_code == 403,
                      "viewer 不能确认提案")
                check(client.get("/audit").status_code == 403, "viewer 不能看审计")
                check(client.get("/users").status_code == 403, "viewer 不能管理用户")
            if user == "wang":
                check(client.get("/documents").status_code == 200, "editor 可进文档页")
                check(client.post("/api/ontology/proposals/1/accept").status_code == 403,
                      "editor 不能确认提案")
            if user == "li":
                check(client.get("/audit").status_code == 200, "approver 可看审计")
                check(client.get("/users").status_code == 403, "approver 不能管理用户")
                draft = client.post("/api/ontology/1/draft?question=补充贷后检查相关类型")
                check(draft.status_code == 200, "approver 可生成草案")
                proposals = client.get("/api/ontology/1/proposals?status=pending").json()["proposals"]
                check(len(proposals) >= 1, "有可确认的提案")
                if proposals:
                    accepted = client.post(f"/api/ontology/proposals/{proposals[-1]['id']}/accept")
                    check(accepted.status_code == 200 and accepted.json()["status"] == "accepted",
                          "approver 确认提案成功")

        print("\n── 审计留痕 ───────────────────────────────────")
        client.get("/logout")
        login(client, "admin", "Adm1n-Passw0rd!")
        logs = client.get("/api/audit").json()
        actions = {item["action"] for item in logs["logs"]}
        for action, label in (
            ("login", "登录"), ("login_failed", "登录失败"), ("change_password", "改密"),
            ("create_user", "创建用户"), ("accept_proposal", "确认提案"),
            ("role_denied", "越权被拒"),
        ):
            check(action in actions, f"审计记录 {label}")
        check(any(item["status"] == "denied" for item in logs["logs"]), "审计区分 denied 状态")
        csv_text = client.get("/api/audit/export.csv").text
        check(csv_text.startswith("时间,操作人"), "审计 CSV 可导出")
        check("登录" in csv_text, "CSV 使用中文动作名")

        print("\n── 备份与诊断 ──────────────────────────────────")
        backup = form_post(client, "/ops/backup", {"note": "auth-smoke"})
        check(backup.status_code == 303, "备份触发成功")
        backups = client.get("/api/system/backups").json()["backups"]
        check(len(backups) >= 1, "备份列表非空")
        check(backups[0]["name"].endswith(".zip"), "备份为 zip 包")
        diag = client.get("/api/system/diagnostics").json()
        check(diag["counts"]["fail"] == 0, "诊断无 fail 项", str(diag["counts"]))
        check(diag["runtime"]["auth_enabled"] is True, "诊断显示认证已开启")
        check(diag["runtime"]["sessions"]["active"] >= 1, "诊断统计活跃会话")

        print("\n── 限流 ───────────────────────────────────────")
        codes = [client.get("/api/rag/ask?space=1&q=x").status_code for _ in range(70)]
        check(429 in codes, "超过配额返回 429", f"max={max(codes)}")
        check(client.get("/api/health").status_code == 200, "限流不影响健康检查")

        print("\n── 退出登录 ───────────────────────────────────")
        client.get("/logout")
        check(client.get("/api/spaces").status_code == 401, "退出后 API 401")
        check(client.get("/").status_code == 303, "退出后页面跳登录")

    print("\n" + "=" * 62)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    print("=" * 62)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 2
    sys.exit(code)
