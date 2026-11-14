"""数据源组件：按 URI 前缀自动路由——local://, file://, sqlite://, http(s)://, mysql+pymysql://。

「加一个数据源」= 写一个类 + 在 ``DATA_SOURCE_IMPLEMENTATIONS`` 里注册 + 在前缀表加一行，
入库接口与前端都不用改。
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .base import FetchedSource

#: 前缀 → 实现名（顺序即匹配优先级）
SCHEME_ROUTES: tuple[tuple[str, str], ...] = (
    ("local://", "file"),
    ("file://", "file"),
    ("sqlite://", "sqlite"),
    ("http://", "http_json"),
    ("https://", "http_json"),
    ("mysql+pymysql://", "jdbc"),
    ("mysql://", "jdbc"),
    ("postgresql://", "jdbc"),
)


class FileDataSource:
    name = "file"
    schemes = ("local://", "file://")

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "schemes": list(self.schemes), "needs_key": False}

    def handles(self, uri: str) -> bool:
        return uri.startswith(("local://", "file://"))

    def _path(self, uri: str) -> Path:
        raw = uri.split("://", 1)[1] if "://" in uri else uri
        if uri.startswith("file://"):
            parsed = urlparse(uri)
            raw = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
            raw = raw.lstrip("/") if raw.startswith("/") and len(raw) > 2 and raw[2] == ":" else raw
        return Path(unquote(raw))

    def probe(self, uri: str) -> dict[str, Any]:
        path = self._path(uri)
        return {
            "uri": uri,
            "scheme": "file",
            "path": str(path),
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
            "hint": "" if path.is_file() else "路径不存在或不是文件",
        }

    def fetch(self, uri: str) -> FetchedSource:
        path = self._path(uri)
        if not path.is_file():
            raise FileNotFoundError(f"本地文件不存在：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FetchedSource(uri=uri, filename=path.name, mime=mime, data=path.read_bytes())


class SqliteDataSource:
    """``sqlite:///abs/path.db?table=foo&limit=500``：把表或查询结果转成 CSV 入库。"""

    name = "sqlite"
    schemes = ("sqlite://",)

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "schemes": list(self.schemes), "needs_key": False}

    def handles(self, uri: str) -> bool:
        return uri.startswith("sqlite://")

    def _parts(self, uri: str) -> tuple[Path, dict[str, str]]:
        parsed = urlparse(uri)
        db_path = Path(unquote((parsed.netloc + parsed.path).lstrip("/")))
        params = {k: v for k, v in (p.split("=", 1) for p in parsed.query.split("&") if "=" in p)}
        return db_path, params

    def probe(self, uri: str) -> dict[str, Any]:
        path, params = self._parts(uri)
        if not path.is_file():
            return {"uri": uri, "scheme": "sqlite", "exists": False, "hint": f"数据库不存在：{path}"}
        with sqlite3.connect(path) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            "uri": uri,
            "scheme": "sqlite",
            "exists": True,
            "tables": tables[:50],
            "selected": params.get("table", ""),
            "hint": "用 ?table=表名&limit=N 指定导出的表",
        }

    def fetch(self, uri: str) -> FetchedSource:
        path, params = self._parts(uri)
        table = params.get("table", "")
        limit = int(params.get("limit", "500"))
        if not table:
            raise ValueError("sqlite 数据源需要 ?table=表名 参数")
        if not table.replace("_", "").isalnum():
            raise ValueError("表名只允许字母数字下划线（防注入）")
        with sqlite3.connect(path) as conn:
            cursor = conn.execute(f"SELECT * FROM {table} LIMIT {limit}")  # noqa: S608 - 表名已白名单校验
            headers = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
        lines = ["\t".join(headers)] + ["\t".join("" if v is None else str(v) for v in row) for row in rows]
        data = ("\n".join(lines)).encode("utf-8")
        return FetchedSource(
            uri=uri,
            filename=f"{table}.tsv",
            mime="text/tab-separated-values",
            data=data,
            meta={"rows": len(rows), "table": table},
        )


class HttpJsonDataSource:
    name = "http_json"
    schemes = ("http://", "https://")

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "schemes": list(self.schemes), "needs_key": False}

    def handles(self, uri: str) -> bool:
        return uri.startswith(("http://", "https://"))

    def probe(self, uri: str) -> dict[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                response = client.head(uri)
            return {
                "uri": uri,
                "scheme": "http",
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "exists": response.status_code < 400,
            }
        except Exception as exc:  # noqa: BLE001
            return {"uri": uri, "scheme": "http", "exists": False, "hint": str(exc)}

    def fetch(self, uri: str) -> FetchedSource:
        import httpx

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(uri)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        name = Path(urlparse(uri).path).name or "remote"
        if "json" in content_type:
            text = json.dumps(response.json(), ensure_ascii=False, indent=2)
            return FetchedSource(
                uri=uri,
                filename=f"{Path(name).stem}.json",
                mime="application/json",
                data=text.encode("utf-8"),
            )
        return FetchedSource(uri=uri, filename=name, mime=content_type, data=response.content)


class JdbcDataSource:
    """ODBC/JDBC 场景的替代：走 SQLAlchemy 连接串，把查询结果导成 CSV。"""

    name = "jdbc"
    schemes = ("mysql+pymysql://", "mysql://", "postgresql://")

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schemes": list(self.schemes),
            "needs_key": True,
            "note": "用 ?sql=... 指定查询；需要对应驱动（pymysql / psycopg）",
        }

    def handles(self, uri: str) -> bool:
        return uri.startswith(self.schemes)

    def probe(self, uri: str) -> dict[str, Any]:
        try:
            import sqlalchemy  # noqa: F401
        except ImportError:
            return {"uri": uri, "scheme": "jdbc", "exists": False, "hint": "缺少 SQLAlchemy"}
        return {"uri": uri, "scheme": "jdbc", "exists": True, "hint": "请用 ?sql=SELECT ... 触发一次探查"}

    def fetch(self, uri: str) -> FetchedSource:
        from sqlalchemy import create_engine, text

        parsed = urlparse(uri)
        params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        sql = params.get("sql", "")
        if not sql.strip().lower().startswith(("select", "with")):
            raise ValueError("只允许 SELECT / WITH 查询")
        dsn = uri.split("?", 1)[0]
        engine = create_engine(dsn)
        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                headers = list(result.keys())
                rows = result.fetchall()
        finally:
            engine.dispose()
        lines = ["\t".join(headers)] + ["\t".join("" if v is None else str(v) for v in row) for row in rows]
        return FetchedSource(
            uri=uri,
            filename="query.tsv",
            mime="text/tab-separated-values",
            data="\n".join(lines).encode("utf-8"),
            meta={"rows": len(rows)},
        )


DATA_SOURCE_IMPLEMENTATIONS = {
    "file": FileDataSource,
    "sqlite": SqliteDataSource,
    "http_json": HttpJsonDataSource,
    "jdbc": JdbcDataSource,
}


def route(uri: str):
    for prefix, key in SCHEME_ROUTES:
        if uri.startswith(prefix):
            return DATA_SOURCE_IMPLEMENTATIONS[key]()
    # 裸路径当作本地文件
    return FileDataSource()
