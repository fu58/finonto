"""文档格式解析适配（可插拔的「文档格式」组件）。

统一产出 ParsedDocument.blocks（带 locator / page / heading），切块策略由入库服务负责，
新增格式只需在这里注册一个 parser 或在组件表里加一条实现。
"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser

from ..base import ParsedBlock, ParsedDocument


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("p", "br", "li", "h1", "h2", "h3", "tr", "div"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):  # noqa: ANN001
        if not self._skip and data.strip():
            self.chunks.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextHTMLParser()
    parser.feed(html or "")
    text = "".join(parser.chunks)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CLAUSE = re.compile(r"^\s*(第[一二三四五六七八九十百千0-9]+[章节条款篇])\s*[、，,.：:]?\s*(.*)$")


def _md_blocks(text: str) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    heading = ""
    buffer: list[str] = []
    ordinal = 0

    def flush(loc: str) -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        buffer = []
        if body:
            blocks.append(ParsedBlock(text=body, locator=loc, heading=heading, kind="paragraph"))

    for line in (text or "").splitlines():
        heading_m = _HEADING.match(line)
        clause_m = _CLAUSE.match(line)
        if heading_m or clause_m:
            flush(f"para-{ordinal}")
            ordinal += 1
            heading = (heading_m.group(2) if heading_m else line).strip()
            buffer.append(line)
            blocks.append(ParsedBlock(text=line.strip(), locator=f"para-{ordinal}", heading=heading, kind="heading"))
            continue
        if line.strip() == "" and buffer and buffer[-1].strip() == "":
            continue
        buffer.append(line)
        if len("".join(buffer)) > 1400:
            flush(f"para-{ordinal}")
            ordinal += 1
    flush(f"para-{ordinal}")
    return blocks


class DefaultDocParser:
    name = "default_multi_format"
    formats = ("txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "html", "htm", "xml", "pdf", "docx", "xlsx", "log")

    def can_parse(self, doc_format: str) -> bool:
        return (doc_format or "").lower().lstrip(".") in self.formats

    def parse(self, payload: bytes, doc_format: str, filename: str = "") -> ParsedDocument:
        fmt = (doc_format or "").lower().lstrip(".")
        if not fmt:
            fmt = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
        handler = {
            "txt": self._plain,
            "log": self._plain,
            "md": self._markdown,
            "markdown": self._markdown,
            "csv": self._csv,
            "tsv": self._csv,
            "json": self._json,
            "jsonl": self._json,
            "html": self._html,
            "htm": self._html,
            "xml": self._html,
            "pdf": self._pdf,
            "docx": self._docx,
            "xlsx": self._xlsx,
        }.get(fmt, self._plain)
        blocks, title, meta = handler(payload, filename)
        return ParsedDocument(blocks=blocks, doc_format=fmt, title=title, meta=meta)

    # ------------------------------------------------------------------
    def _decode(self, payload: bytes) -> str:
        for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
            try:
                return payload.decode(enc)
            except UnicodeDecodeError:
                continue
        return payload.decode("utf-8", errors="replace")

    def _plain(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        text = self._decode(payload)
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paras:
            paras = [text.strip()] if text.strip() else []
        blocks = [
            ParsedBlock(text=p, locator=f"para-{i + 1}", kind="paragraph") for i, p in enumerate(paras)
        ]
        title = (filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) or ""
        return blocks, title, {"encoding": "autodetect"}

    def _markdown(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        text = self._decode(payload)
        blocks = _md_blocks(text)
        title = blocks[0].heading if blocks and blocks[0].kind == "heading" else filename
        return blocks, title or "", {"source": "markdown"}

    def _csv(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        text = self._decode(payload)
        dialect = csv.excel_tab if "\t" in text[:400] and "," not in text[:400] else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect=dialect))
        if not rows:
            return [], filename, {}
        header = rows[0]
        blocks: list[ParsedBlock] = [
            ParsedBlock(
                text="表头: " + " | ".join(header), locator="row-0", kind="table_header", meta={"columns": header}
            )
        ]
        for i, row in enumerate(rows[1:], start=1):
            pairs = [f"{h}={v}" for h, v in zip(header, row) if str(v).strip()]
            if pairs:
                blocks.append(
                    ParsedBlock(
                        text="；".join(pairs),
                        locator=f"row-{i}",
                        kind="table_row",
                        meta={"columns": header, "values": row},
                    )
                )
        return blocks, filename, {"columns": header, "row_count": len(rows) - 1}

    def _json(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        text = self._decode(payload)
        blocks: list[ParsedBlock] = []
        try:
            if filename.endswith(".jsonl") or "\n{" in text[:200]:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                data = [json.loads(text)]
        except json.JSONDecodeError:
            return self._plain(payload, filename)

        def walk(node, path: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(node, list):
                for idx, v in enumerate(node[:200]):
                    walk(v, f"{path}[{idx}]")
            else:
                if node is None or str(node).strip() == "":
                    return
                blocks.append(ParsedBlock(text=f"{path} = {node}", locator=path or "root", kind="field"))

        for item in data:
            walk(item, "")
        return blocks, filename, {"records": len(data)}

    def _html(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        text = _html_to_text(self._decode(payload))
        blocks, _, _ = self._plain(text.encode("utf-8"), filename)
        m = re.search(r"<title[^>]*>(.*?)</title>", self._decode(payload), re.I | re.S)
        return blocks, (m.group(1).strip() if m else filename), {"source": "html"}

    def _pdf(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        try:
            from pypdf import PdfReader
        except ImportError:
            return self._plain(payload, filename)
        reader = PdfReader(io.BytesIO(payload))
        title = (reader.metadata or {}).get("/Title", "") if reader.metadata else ""
        blocks: list[ParsedBlock] = []
        for page_no, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            blocks.extend(
                ParsedBlock(text=p.strip(), locator=f"p{page_no}", page=page_no, kind="paragraph")
                for p in re.split(r"\n\s*\n", page_text)
                if p.strip()
            )
        return blocks, title or filename, {"pages": len(reader.pages), "source": "pdf"}

    def _docx(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        try:
            import docx
        except ImportError:
            return self._plain(payload, filename)
        document = docx.Document(io.BytesIO(payload))
        blocks: list[ParsedBlock] = []
        heading = ""
        for idx, para in enumerate(document.paragraphs, start=1):
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            kind = "heading" if "heading" in style or "标题" in style else "paragraph"
            if kind == "heading":
                heading = text
            blocks.append(ParsedBlock(text=text, locator=f"para-{idx}", heading=heading, kind=kind))
        for t_idx, table in enumerate(document.tables, start=1):
            for r_idx, row in enumerate(table.rows):
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    blocks.append(
                        ParsedBlock(
                            text=" | ".join(cells), locator=f"table{t_idx}-r{r_idx}", kind="table_row", heading=heading
                        )
                    )
        core = document.core_properties
        return blocks, (core.title or filename), {"source": "docx"}

    def _xlsx(self, payload: bytes, filename: str) -> tuple[list[ParsedBlock], str, dict]:
        try:
            import openpyxl
        except ImportError:
            return [], filename, {}
        workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        blocks: list[ParsedBlock] = []
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            header = [str(c) if c is not None else "" for c in rows[0]]
            blocks.append(
                ParsedBlock(text=f"[{sheet.title}] 表头: " + " | ".join(header), locator=f"{sheet.title}#0", kind="table_header")
            )
            for i, row in enumerate(rows[1:], start=1):
                pairs = [f"{h}={v}" for h, v in zip(header, row) if str(v).strip() not in ("", "None")]
                if pairs:
                    blocks.append(
                        ParsedBlock(
                            text=f"[{sheet.title}] " + "；".join(pairs), locator=f"{sheet.title}#r{i}", kind="table_row"
                        )
                    )
        return blocks, filename, {"sheets": [s.title for s in workbook.worksheets], "source": "xlsx"}
