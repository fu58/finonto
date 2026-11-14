"""文档格式组件：内置多格式解析器（零三方依赖）。

支持：txt / md / csv / tsv / json / jsonl / html / xml / log / docx / xlsx / pdf。
实现只用标准库（``zipfile`` + ``xml.etree`` + ``zlib`` + ``html.parser``），
所以「不装任何依赖也能入库」成立。缺 pypdf 时 PDF 走内置文本层抽取（扫描件仍需 OCR）。
"""

from __future__ import annotations

import io
import json
import mimetypes
import re
import zipfile
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..core.text import normalize
from .base import ParsedDocument

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".html", ".htm", ".xml", ".log", ".yaml", ".yml", ".ini", ".conf",
}
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #
def decode_bytes(data: bytes) -> tuple[str, str]:
    """按常见中文编码依次尝试，返回 (文本, 实际编码)。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


class _HTMLTextExtractor:
    """极简 HTML → 段落文本：只保留可读内容，丢弃 script/style。"""

    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        from html.parser import HTMLParser

        self._parser_cls = HTMLParser
        self.chunks: list[str] = []

    def feed(self, html: str) -> str:
        chunks = self.chunks
        skip = {"depth": 0}

        class _Parser(self._parser_cls):  # type: ignore[misc]
            def handle_starttag(self, tag, attrs):
                if tag in _HTMLTextExtractor.SKIP_TAGS:
                    skip["depth"] += 1
                if tag in _HTMLTextExtractor.BLOCK_TAGS:
                    chunks.append("\n")

            def handle_endtag(self, tag):
                if tag in _HTMLTextExtractor.SKIP_TAGS and skip["depth"] > 0:
                    skip["depth"] -= 1
                if tag in _HTMLTextExtractor.BLOCK_TAGS:
                    chunks.append("\n")

            def handle_data(self, data):
                if skip["depth"] == 0 and data.strip():
                    chunks.append(data)

        parser = _Parser(convert_charrefs=True)
        parser.feed(html)
        return normalize("".join(chunks))


# --------------------------------------------------------------------------- #
# DOCX / XLSX（Office 就是 zip + xml）
# --------------------------------------------------------------------------- #
def parse_docx(data: bytes) -> ParsedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        return ParsedDocument(text="", warnings=[f"DOCX 解析失败：{exc}"])

    root = ElementTree.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{W_NS}p"):
        pieces = [node.text or "" for node in paragraph.iter(f"{W_NS}t")]
        line = "".join(pieces).strip()
        if line:
            paragraphs.append(line)
    text = normalize("\n\n".join(paragraphs))
    return ParsedDocument(
        text=text,
        pages=[text] if text else [],
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        meta={"paragraphs": len(paragraphs)},
        warnings=warnings,
    )


def parse_xlsx(data: bytes) -> ParsedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                sroot = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in sroot.iter(f"{S_NS}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{S_NS}t")))
            sheets = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
            pages: list[str] = []
            for sheet in sheets:
                root = ElementTree.fromstring(archive.read(sheet))
                rows: list[str] = []
                for row in root.iter(f"{S_NS}row"):
                    cells: list[str] = []
                    for cell in row.iter(f"{S_NS}c"):
                        cell_type = cell.get("t", "")
                        value_node = cell.find(f"{S_NS}v")
                        inline_node = cell.find(f"{S_NS}is")
                        if cell_type == "s" and value_node is not None:
                            try:
                                cells.append(shared[int(value_node.text or 0)])
                            except (ValueError, IndexError):
                                cells.append("")
                        elif inline_node is not None:
                            cells.append("".join(n.text or "" for n in inline_node.iter(f"{S_NS}t")))
                        elif value_node is not None:
                            cells.append(value_node.text or "")
                        else:
                            cells.append("")
                    line = "\t".join(c for c in cells).strip()
                    if line:
                        rows.append(line)
                if rows:
                    pages.append(normalize(f"# {sheet}\n" + "\n".join(rows)))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        return ParsedDocument(text="", warnings=[f"XLSX 解析失败：{exc}"])

    text = "\n\n".join(pages)
    return ParsedDocument(
        text=text,
        pages=pages,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        meta={"sheets": len(pages)},
    )


# --------------------------------------------------------------------------- #
# PDF：文本层抽取（简化版）
# --------------------------------------------------------------------------- #
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)")
_PDF_HEX = re.compile(rb"<([0-9A-Fa-f\s]{4,})>")


def _pdf_unescape(raw: bytes) -> str:
    raw = raw[1:-1]
    replacements = {b"\\n": b"\n", b"\\r": b"\r", b"\\t": b"\t",
                    b"\\(": b"(", b"\\)": b")", b"\\\\": b"\\"}
    for source, target in replacements.items():
        raw = raw.replace(source, target)
    raw = re.sub(rb"\\[0-7]{1,3}", lambda m: bytes([int(m.group(0)[1:], 8) & 0xFF]), raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_pdf(data: bytes) -> ParsedDocument:
    # 优先使用 pypdf（若环境恰好装了）
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(data))
        pages = [normalize(page.extract_text() or "") for page in reader.pages]
        pages = [p for p in pages if p]
        return ParsedDocument(
            text="\n\n".join(pages),
            pages=pages,
            mime="application/pdf",
            meta={"engine": "pypdf", "pages": len(pages)},
        )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - 损坏 PDF 交给内置实现兜底
        pass

    warnings = ["PDF 走内置文本层抽取（不还原版面），扫描件请接外部 OCR"]
    pages: list[str] = []
    for match in re.finditer(rb"stream\r?\n?(.*?)\r?\n?endstream", data, re.S):
        chunk = match.group(1)
        payload = chunk
        if chunk[:2] == b"\x78\x9c" or chunk[:1] == b"\x78":
            try:
                payload = zlib.decompress(chunk)
            except zlib.error:
                continue
        if b"Tj" not in payload and b"TJ" not in payload:
            continue
        lines: list[str] = []
        for text_op in re.finditer(rb"(\[(?:[^\]]*)\]\s*TJ)|(\((?:\\.|[^\\()])*\)\s*(?:Tj|'))", payload):
            fragment = text_op.group(0)
            strings = _PDF_STRING.findall(fragment)
            if not strings:
                continue
            line = "".join(_pdf_unescape(s) for s in strings)
            if line.strip():
                lines.append(line)
        for hex_match in _PDF_HEX.finditer(payload):
            try:
                raw_hex = re.sub(rb"\s", b"", hex_match.group(1))
                if len(raw_hex) % 4 == 0:
                    decoded = bytes.fromhex(raw_hex.decode("ascii")).decode("utf-16-be", errors="ignore")
                    if decoded.strip():
                        lines.append(decoded)
            except (ValueError, UnicodeDecodeError):
                continue
        page_text = normalize("\n".join(lines))
        if page_text:
            pages.append(page_text)

    return ParsedDocument(
        text="\n\n".join(pages),
        pages=pages,
        mime="application/pdf",
        meta={"engine": "builtin", "pages": len(pages)},
        warnings=warnings if not pages else warnings,
    )


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
class DefaultMultiFormatParser:
    name = "default_multi_format"
    kind = "doc_parser"
    supported_extensions = tuple(sorted(TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx"}))

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "builtin",
            "needs_key": False,
            "extensions": list(self.supported_extensions),
            "note": "PDF 仅文本层；扫描件需 OCR",
        }

    def supports(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in set(self.supported_extensions)

    def parse(self, filename: str, data: bytes) -> ParsedDocument:
        suffix = Path(filename).suffix.lower()
        mime = mimetypes.guess_type(filename)[0] or "text/plain"

        if suffix == ".pdf":
            return parse_pdf(data)
        if suffix == ".docx":
            return parse_docx(data)
        if suffix == ".xlsx":
            return parse_xlsx(data)

        text, encoding = decode_bytes(data)

        if suffix in (".html", ".htm"):
            extracted = _HTMLTextExtractor().feed(text)
            return ParsedDocument(
                text=extracted,
                pages=[extracted] if extracted else [],
                mime="text/html",
                meta={"encoding": encoding},
            )

        if suffix == ".json":
            try:
                parsed = json.loads(text)
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        elif suffix == ".jsonl":
            lines = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.dumps(json.loads(line), ensure_ascii=False))
                except json.JSONDecodeError:
                    lines.append(line)
            text = "\n".join(lines)

        text = normalize(text)
        return ParsedDocument(
            text=text,
            pages=[text] if text else [],
            mime=mime,
            meta={"encoding": encoding, "chars": len(text)},
        )
