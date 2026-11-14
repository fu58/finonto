"""Jinja2 环境与页面上下文。

* 模板里只做展示，所有数据由接口/页面处理器备好；
* ``md`` 过滤器把 Markdown 渲染成 HTML（先转义再放行白名单标签）；
* 全局注入中文枚举标签，模板里写 ``{{ kind_label(item.kind) }}`` 即可。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.responses import HTMLResponse

from ..core.ontology import kinds, packs
from ..core.text import truncate
from .markdown import render_markdown

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
    auto_reload=True,
)


def _md(text: str, title_of=None) -> Markup:
    return Markup(render_markdown(text or "", title_of))


def _nl2br(text: str) -> Markup:
    import html as html_lib

    return Markup("<br/>".join(html_lib.escape(line) for line in (text or "").splitlines()))


def _shorten(value: Any, length: int = 80) -> str:
    return truncate(str(value or ""), length)


env.filters["md"] = _md
env.filters["nl2br"] = _nl2br
env.filters["shorten"] = _shorten
env.globals.update(
    kind_label=kinds.kind_label,
    relation_label=kinds.relation_label,
    property_type_label=lambda key: kinds.PROPERTY_TYPE_LABELS.get(key, key),
    severity_label=kinds.severity_label,
    action_label=kinds.action_label,
    status_label=kinds.status_label,
    page_type_label=kinds.page_type_label,
    pack_label=lambda key: packs.get_pack(key).label,
    APP_NAME="FinOnto",
    VERSION="0.1.0",
)

NAV = [
    {"key": "chat", "label": "问答 · RAG", "href": "/", "role": ""},
    {"key": "agent", "label": "Agent 推理", "href": "/agent", "role": ""},
    {"key": "ontology", "label": "本体工作室", "href": "/ontology", "role": ""},
    {"key": "proposals", "label": "提案审核", "href": "/proposals", "role": ""},
    {"key": "wiki", "label": "Wiki 与版本", "href": "/wiki", "role": ""},
    {"key": "graph", "label": "知识图谱", "href": "/graph", "role": ""},
    {"key": "documents", "label": "文档入库", "href": "/documents", "role": "editor"},
    {"key": "packs", "label": "领域包", "href": "/packs", "role": ""},
    {"key": "components", "label": "组件中心", "href": "/components", "role": "admin"},
    {"key": "ops", "label": "运维 · 备份", "href": "/ops", "role": "admin"},
    {"key": "users", "label": "用户与角色", "href": "/users", "role": "admin"},
    {"key": "audit", "label": "审计日志", "href": "/audit", "role": "approver"},
]


def visible_nav(role: str) -> list[dict]:
    from ..security import has_role

    return [item for item in NAV if not item["role"] or has_role(role, item["role"])]


def page_context(
    session,
    *,
    space_id: int | None = None,
    active: str = "chat",
    request=None,
    **extra: Any,
) -> dict[str, Any]:
    """统一页面上下文：空间、导航（按角色过滤）、当前用户、领域包信息。"""
    from ..core.ontology import service
    from ..security import has_role, role_label

    spaces = service.list_spaces(session)
    current = None
    if spaces:
        current = next((item for item in spaces if item["id"] == space_id), spaces[0])
    summary = None
    vocab = {"label": "", "business_domains": [], "kinds": [], "relation_categories": [],
             "type_count": 0, "standards": []}
    questions: list[str] = []
    if current is not None:
        summary = service.space_summary(session, current["id"])
        vocab = packs.vocab_for(current["domain"])
        questions = packs.sample_questions_for(current["domain"])

    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    role = getattr(user, "role", "viewer") if user is not None else "viewer"
    auth_enabled = bool(getattr(getattr(request, "state", None), "auth_enabled", True))

    context = {
        "nav": visible_nav(role),
        "active": active,
        "spaces": spaces,
        "space": current,
        "space_id": current["id"] if current else 0,
        "summary": summary,
        "vocab": vocab,
        "sample_questions": questions,
        "user": user,
        "role": role,
        "role_label": role_label(role),
        "auth_enabled": auth_enabled,
        "can_edit": has_role(role, "editor"),
        "can_approve": has_role(role, "approver"),
        "can_admin": has_role(role, "admin"),
        "request_id": getattr(getattr(request, "state", None), "request_id", ""),
        "packs": [
            {"key": item.key, "label": item.label, "summary": item.summary,
             "type_count": len(item.object_types), "question": (item.sample_questions or [""])[0]}
            for item in packs.all_packs()
        ],
    }
    context.update(extra)
    return context


def render(template_name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(env.get_template(template_name).render(**context), status_code=status_code)


def render_fragment(template_name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(env.get_template(template_name).render(**context), status_code=status_code)


def render_error(request, status: int, title: str, detail: str = "") -> HTMLResponse:
    """统一的 HTML 错误页（401/403/404/429/500）。

    不套 base.html 的导航 —— 未登录时导航没有意义，且可能触发额外查询。
    """
    from ..security import client_ip

    context = {
        "status": status,
        "title": title,
        "detail": detail,
        "request_id": getattr(getattr(request, "state", None), "request_id", ""),
        "ip": client_ip(request) if request is not None else "",
        "user": getattr(getattr(request, "state", None), "user", None),
    }
    return HTMLResponse(env.get_template("error.html").render(**context), status_code=status)
