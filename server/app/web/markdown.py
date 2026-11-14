"""零依赖 Markdown 渲染器（服务端）。

支持：标题、段落、粗斜体、行内代码、代码块、无序/有序列表、引用、
表格、分隔线、链接、``[[slug]]`` 内部链接、``[cN]`` 引用角标。

为什么自己写：项目约定前端零三方库，而 Wiki 正文本来就是编译器生成的受控 Markdown，
不需要 CommonMark 全集。渲染前先做 HTML 转义，再放行白名单标签，避免 XSS。
"""

from __future__ import annotations

import html
import re
from typing import Callable

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
WIKILINK = re.compile(r"\[\[([a-zA-Z0-9\-_]+)\]\]")
CITATION = re.compile(r"\[(c\d+)\]")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
UL_ITEM = re.compile(r"^[-*+]\s+(.*)$")
OL_ITEM = re.compile(r"^\d+[.)]\s+(.*)$")
TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")


def _inline(text: str, title_of: Callable[[str], str] | None) -> str:
    # 先转义，后面再按白名单还原成标签
    text = html.escape(text, quote=False)
    text = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    def link_repl(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if url.startswith(("javascript:", "data:")):
            return label
        external = url.startswith(("http://", "https://"))
        attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
        return f'<a href="{url}"{attrs}>{label}</a>'

    text = LINK.sub(link_repl, text)

    def wiki_repl(match: re.Match) -> str:
        slug = match.group(1)
        label = title_of(slug) if title_of else slug
        if slug.startswith(("type-", "domain-")) or slug in ("index", "relations", "quality"):
            return f'<a class="wikilink" href="/wiki/{slug}">{html.escape(label)}</a>'
        return html.escape(label)

    text = WIKILINK.sub(wiki_repl, text)
    text = CITATION.sub(lambda m: f'<span class="cite" data-tag="{m.group(1)}">[{m.group(1)}]</span>', text)
    return text


def _table(rows: list[str], title_of) -> str:
    header = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    body_rows = rows[2:]
    parts = ['<div class="table-wrap"><table>', "<thead><tr>"]
    for cell in header:
        parts.append(f"<th>{_inline(cell, title_of)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body_rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        parts.append("<tr>")
        for cell in cells:
            parts.append(f"<td>{_inline(cell, title_of)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def render_markdown(text: str, title_of: Callable[[str], str] | None = None) -> str:
    if not text:
        return ""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    output: list[str] = []
    index = 0
    in_code = False
    code_buffer: list[str] = []
    list_buffer: list[str] = []
    list_ordered = False
    table_buffer: list[str] = []

    def flush_list() -> None:
        nonlocal list_buffer, list_ordered
        if not list_buffer:
            return
        tag = "ol" if list_ordered else "ul"
        output.append(f"<{tag}>")
        for item in list_buffer:
            output.append(f"<li>{_inline(item, title_of)}</li>")
        output.append(f"</{tag}>")
        list_buffer = []

    def flush_table() -> None:
        nonlocal table_buffer
        if table_buffer:
            output.append(_table(table_buffer, title_of))
            table_buffer = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                output.append("<pre><code>" + html.escape("\n".join(code_buffer)) + "</code></pre>")
                code_buffer = []
                in_code = False
            else:
                flush_list()
                flush_table()
                in_code = True
            index += 1
            continue

        if in_code:
            code_buffer.append(line)
            index += 1
            continue

        if not stripped:
            flush_list()
            flush_table()
            index += 1
            continue

        heading = HEADING.match(stripped)
        if heading:
            flush_list()
            flush_table()
            level = len(heading.group(1))
            anchor = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", heading.group(2)).strip("-").lower()
            output.append(
                f'<h{level} id="{anchor}">{_inline(heading.group(2), title_of)}</h{level}>'
            )
            index += 1
            continue

        if TABLE_SEP.match(stripped) and table_buffer:
            table_buffer.append(stripped)
            index += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_list()
            table_buffer.append(stripped)
            index += 1
            continue

        flush_table()

        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            flush_list()
            output.append("<hr/>")
            index += 1
            continue

        if stripped.startswith(">"):
            flush_list()
            output.append(f"<blockquote>{_inline(stripped.lstrip('> ').strip(), title_of)}</blockquote>")
            index += 1
            continue

        ul = UL_ITEM.match(stripped)
        if ul:
            if list_buffer and list_ordered:
                flush_list()
            list_ordered = False
            list_buffer.append(ul.group(1))
            index += 1
            continue

        ol = OL_ITEM.match(stripped)
        if ol:
            if list_buffer and not list_ordered:
                flush_list()
            list_ordered = True
            list_buffer.append(ol.group(1))
            index += 1
            continue

        flush_list()
        output.append(f"<p>{_inline(stripped, title_of)}</p>")
        index += 1

    if in_code and code_buffer:
        output.append("<pre><code>" + html.escape("\n".join(code_buffer)) + "</code></pre>")
    flush_list()
    flush_table()
    return "\n".join(output)
