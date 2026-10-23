"""默认向量库：SQLite 存储 + numpy 内存暴力检索。

自带独立命名空间表，与 chunk 业务表解耦 —— 换成 pgvector / DashVector 时
只需要替换本实现，检索接口不变。小规模（万级 chunk）下全内存 cosine 足够快。
"""

from __future__ import annotations

import json
import threading

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from ..base import VectorHit


class SqliteNumpyVectorStore:
    name = "sqlite_numpy"

    def __init__(self, dsn: str) -> None:
        kwargs: dict = {"future": True}
        if dsn.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        self._engine = create_engine(dsn, **kwargs)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[np.ndarray, list[int], list[dict]]] = {}
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS vs_items (
                        namespace TEXT NOT NULL,
                        ref_id INTEGER NOT NULL,
                        dim INTEGER NOT NULL,
                        vector BLOB NOT NULL,
                        meta TEXT NOT NULL DEFAULT '{}',
                        PRIMARY KEY (namespace, ref_id)
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS vs_ns ON vs_items(namespace)"))

    # ------------------------------------------------------------------
    def upsert(self, namespace: str, vectors: dict[int, np.ndarray], meta: dict[int, dict] | None = None) -> int:
        if not vectors:
            return 0
        meta = meta or {}
        rows = [
            {
                "namespace": namespace,
                "ref_id": ref_id,
                "dim": int(vec.shape[0]),
                "vector": np.asarray(vec, dtype=np.float32).tobytes(),
                "meta": json.dumps(meta.get(ref_id, {}), ensure_ascii=False),
            }
            for ref_id, vec in vectors.items()
        ]
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO vs_items(namespace, ref_id, dim, vector, meta) "
                    "VALUES (:namespace, :ref_id, :dim, :vector, :meta) "
                    "ON CONFLICT(namespace, ref_id) DO UPDATE SET "
                    "dim=excluded.dim, vector=excluded.vector, meta=excluded.meta"
                ),
                rows,
            )
            self._cache.pop(namespace, None)
        return len(rows)

    def delete(self, namespace: str, ref_ids: list[int]) -> int:
        if not ref_ids:
            return 0
        with self._lock, self._engine.begin() as conn:
            placeholders = ",".join(str(int(r)) for r in ref_ids)
            result = conn.execute(
                text(f"DELETE FROM vs_items WHERE namespace=:ns AND ref_id IN ({placeholders})"),
                {"ns": namespace},
            )
        with self._lock:
            self._cache.pop(namespace, None)
        return int(result.rowcount or 0)

    def delete_namespace(self, namespace: str) -> int:
        with self._lock, self._engine.begin() as conn:
            result = conn.execute(text("DELETE FROM vs_items WHERE namespace=:ns"), {"ns": namespace})
        self._cache.pop(namespace, None)
        return int(result.rowcount or 0)

    def count(self, namespace: str) -> int:
        return self._matrix(namespace)[0].shape[0]

    # ------------------------------------------------------------------
    def _matrix(self, namespace: str) -> tuple[np.ndarray, list[int], list[dict]]:
        with self._lock:
            if namespace in self._cache:
                return self._cache[namespace]
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT ref_id, dim, vector, meta FROM vs_items WHERE namespace=:ns"),
                    {"ns": namespace},
                ).fetchall()
            if not rows:
                empty = (np.zeros((0, 1), dtype=np.float32), [], [])
                self._cache[namespace] = empty
                return empty
            dim = int(rows[0][1])
            matrix = np.zeros((len(rows), dim), dtype=np.float32)
            ids: list[int] = []
            metas: list[dict] = []
            for i, (ref_id, row_dim, blob, meta_json) in enumerate(rows):
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] == dim:
                    matrix[i] = vec
                ids.append(int(ref_id))
                metas.append(json.loads(meta_json or "{}"))
            result = (matrix, ids, metas)
            self._cache[namespace] = result
            return result

    def search(
        self, namespace: str, vector: np.ndarray, top_k: int, filters: dict | None = None
    ) -> list[VectorHit]:
        matrix, ids, metas = self._matrix(namespace)
        if matrix.size == 0 or top_k <= 0:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if query.shape[0] != matrix.shape[1]:
            return []
        norm = float(np.linalg.norm(query)) or 1.0
        scores = matrix @ (query / norm)
        order = np.argsort(-scores)[: max(top_k * 4, top_k)]
        hits: list[VectorHit] = []
        for idx in order:
            if idx < 0 or idx >= len(ids):
                continue
            meta = metas[idx]
            if filters and not all(
                (meta.get(k) == v or (isinstance(v, (list, tuple, set)) and meta.get(k) in v))
                for k, v in filters.items()
            ):
                continue
            hits.append(VectorHit(ref_id=ids[idx], score=float(scores[idx]), meta=meta))
            if len(hits) >= top_k:
                break
        return hits

    def get_vector(self, namespace: str, ref_id: int) -> np.ndarray | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT vector FROM vs_items WHERE namespace=:ns AND ref_id=:r"),
                {"ns": namespace, "r": ref_id},
            ).fetchone()
        return np.frombuffer(row[0], dtype=np.float32) if row else None

    def drop_all(self) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(text("DELETE FROM vs_items"))
        self._cache.clear()
