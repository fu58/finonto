"""数据源适配：本地文件(CSV/Excel) / SQLite / HTTP JSON API / BI 库(配置后启用)。

Agent 可用 `datasource.tables` + `datasource.profile` 把结构化表反推为本体对象类型。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from ..base import DataSource, ProviderError, SourceTable


def _infer_columns(header: list[str], rows: list[list]) -> list[str]:
    return [str(h).strip() or f"col_{i}" for i, h in enumerate(header)]


def _profile_table(table: SourceTable) -> dict:
    out: dict = {"columns": [], "row_count": len(table.rows)}
    for idx, col in enumerate(table.columns):
        values = [r[idx] for r in table.rows if idx < len(r)]
        non_null = [v for v in values if v not in (None, "", "NULL")]
        distinct = {str(v) for v in non_null}
        numeric = 0
        for v in non_null[:200]:
            try:
                float(str(v).replace(",", ""))
                numeric += 1
            except ValueError:
                pass
        out["columns"].append(
            {
                "name": col,
                "non_null": len(non_null),
                "distinct": len(distinct),
                "numeric_ratio": round(numeric / max(len(non_null[:200]), 1), 3),
                "examples": [str(v) for v in list(distinct)[:4]],
            }
        )
    return out


class FileDataSource:
    """uri 形如 `file:///abs/path/x.csv` 或 `local://data/a.xlsx`。"""

    name = "file"

    def describe(self, uri: str) -> list[SourceTable]:
        path = Path(uri.replace("file:///", "/").replace("local://", ""))
        if not path.exists():
            raise ProviderError(f"数据源文件不存在: {uri}")
        suffix = path.suffix.lower()
        if suffix in (".csv", ".tsv"):
            return [self._read_csv(path, suffix)]
        if suffix in (".xlsx", ".xls"):
            return self._read_excel(path)
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = [data]
            cols = sorted({k for row in data if isinstance(row, dict) for k in row})
            rows = [[row.get(c) for c in cols] for row in data if isinstance(row, dict)]
            return [SourceTable(name=path.stem, columns=cols, rows=rows)]
        text = path.read_text(encoding="utf-8", errors="replace")
        return [SourceTable(name=path.stem, columns=["line"], rows=[[l] for l in text.splitlines()])]

    def sample(self, uri: str, table: str, limit: int = 20) -> SourceTable:
        for t in self.describe(uri):
            if t.name == table or not table:
                t.rows = t.rows[:limit]
                return t
        raise ProviderError(f"未找到表 {table}")

    def _read_csv(self, path: Path, suffix: str) -> SourceTable:
        import csv

        delim = "\t" if suffix == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8", errors="replace")), delimiter=delim))
        if not rows:
            return SourceTable(name=path.stem, columns=[], rows=[])
        header = _infer_columns(rows[0], rows[1:])
        return SourceTable(name=path.stem, columns=header, rows=rows[1:])

    def _read_excel(self, path: Path) -> list[SourceTable]:
        try:
            import openpyxl
        except ImportError as exc:
            raise ProviderError("读取 Excel 需要 openpyxl") from exc
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        tables: list[SourceTable] = []
        for sheet in wb.worksheets:
            rows = [list(r) for r in sheet.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
            if not rows:
                continue
            tables.append(SourceTable(name=sheet.title, columns=_infer_columns(rows[0], rows[1:]), rows=rows[1:]))
        return tables


class SQLiteDataSource:
    """uri 形如 `sqlite:///abs/path.db`，可直接把行内库表反推为本体。"""

    name = "sqlite"

    def describe(self, uri: str) -> list[SourceTable]:
        from sqlalchemy import create_engine, inspect

        engine = create_engine(uri, future=True)
        inspector = inspect(engine)
        tables: list[SourceTable] = []
        for name in inspector.get_table_names():
            with engine.connect() as conn:
                cols = [c["name"] for c in inspector.get_columns(name)]
                result = conn.execute(__import__("sqlalchemy").text(f"SELECT * FROM {name} LIMIT 200"))
                rows = [list(r) for r in result.fetchall()]
            tables.append(SourceTable(name=name, columns=cols, rows=rows))
        return tables

    def sample(self, uri: str, table: str, limit: int = 20) -> SourceTable:
        for t in self.describe(uri):
            if t.name == table:
                t.rows = t.rows[:limit]
                return t
        raise ProviderError(f"未找到表 {table}")


class HttpJsonDataSource:
    """uri 形如 `https://host/api?limit=100`，返回对象数组即可被表格化。"""

    name = "http_json"

    def describe(self, uri: str) -> list[SourceTable]:
        if not urlparse(uri).scheme.startswith("http"):
            raise ProviderError("需要 http/https URI")
        import httpx

        payload = httpx.get(uri, timeout=20.0, follow_redirects=True)
        payload.raise_for_status()
        data = payload.json()
        if isinstance(data, dict):
            for key in ("data", "items", "records", "rows", "result"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data]
        cols = sorted({k for row in data if isinstance(row, dict) for k in row})
        rows = [[row.get(c) for c in cols] for row in data if isinstance(row, dict)]
        return [SourceTable(name=re.sub(r"\W+", "_", uri)[-40:], columns=cols or ["value"], rows=rows or [[data]])]

    def sample(self, uri: str, table: str, limit: int = 20) -> SourceTable:
        tables = self.describe(uri)
        tables[0].rows = tables[0].rows[:limit]
        return tables[0]


class JdbcDataSource:
    """企业内 JDBC/Oracle/MySQL 风格数据源占位：给出 sqlalchemy URL 即启用。"""

    name = "jdbc"

    def describe(self, uri: str) -> list[SourceTable]:
        from sqlalchemy import create_engine

        if not re.match(r"^\w+\+\w+://", uri):
            raise ProviderError(
                "jdbc 数据源需要提供 SQLAlchemy URL，例如 "
                "mysql+pymysql://user:pw@host:3306/db 或 oracle+cx_Oracle://user:pw@host:1521/sid"
            )
        create_engine(uri, future=True)
        return SQLiteDataSource().describe(uri)

    def sample(self, uri: str, table: str, limit: int = 20) -> SourceTable:
        return self.describe(uri)[0]


def profile_source(source: DataSource, uri: str) -> list[dict]:
    return [_profile_table(t) for t in source.describe(uri)]
