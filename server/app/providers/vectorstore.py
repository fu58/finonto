"""向量库组件：SQLite+numpy（默认）、纯内存、pgvector。

默认实现把向量存成 float32 BLOB 放进主库，进程内用 numpy 做矩阵乘；
十万级片段下单次检索约 5-15ms。更大规模请换 pgvector。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

import numpy as np
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import ChunkVector
from .base import VectorHit


class SqliteNumpyVectorStore:
    name = "sqlite_numpy"
    kind = "vector_store"

    def __init__(self, dim: int = 384) -> None:
        self.dim = int(dim)
        self._lock = threading.Lock()
        self._ids: np.ndarray | None = None
        self._matrix: np.ndarray | None = None
        self._dirty = True

    # ------------------------------------------------------------------ #
    def info(self) -> dict[str, Any]:
        stats = self.stats()
        return {
            "name": self.name,
            "provider": "sqlite_numpy",
            "needs_key": False,
            "dim": self.dim,
            "vectors": stats["vectors"],
            "note": "numpy 暴力算分；十万级以上建议切 pgvector",
        }

    def _invalidate(self) -> None:
        self._dirty = True

    def _load(self) -> None:
        if not self._dirty and self._matrix is not None:
            return
        with self._lock:
            if not self._dirty and self._matrix is not None:
                return
            with session_scope() as session:
                rows = session.execute(
                    select(ChunkVector.chunk_id, ChunkVector.dim, ChunkVector.vector)
                ).all()
            ids: list[int] = []
            vectors: list[np.ndarray] = []
            for chunk_id, dim, blob in rows:
                if dim != self.dim:
                    continue  # 换 embedding 后旧维度的向量不参与检索（需重建索引）
                ids.append(int(chunk_id))
                vectors.append(np.frombuffer(blob, dtype=np.float32))
            if vectors:
                self._matrix = np.vstack(vectors).astype(np.float32)
                self._ids = np.asarray(ids, dtype=np.int64)
            else:
                self._matrix = None
                self._ids = None
            self._dirty = False

    # ------------------------------------------------------------------ #
    def upsert(self, items: Iterable[tuple[int, list[float]]]) -> int:
        count = 0
        rows = list(items)
        if not rows:
            return 0
        with session_scope() as session:
            existing = {
                int(cid)
                for (cid,) in session.execute(
                    select(ChunkVector.chunk_id).where(
                        ChunkVector.chunk_id.in_([cid for cid, _ in rows])
                    )
                ).all()
            }
            for chunk_id, vector in rows:
                arr = np.asarray(vector, dtype=np.float32)
                blob = arr.tobytes()
                if chunk_id in existing:
                    record = session.get(ChunkVector, chunk_id)
                    if record is None:
                        continue
                    record.vector = blob
                    record.dim = int(arr.shape[0])
                    record.model = getattr(self, "model", "local_hash")
                else:
                    session.add(
                        ChunkVector(
                            chunk_id=chunk_id,
                            dim=int(arr.shape[0]),
                            model=getattr(self, "model", "local_hash"),
                            vector=blob,
                        )
                    )
                count += 1
        self._invalidate()
        return count

    def delete(self, chunk_ids: Iterable[int]) -> int:
        ids = [int(i) for i in chunk_ids]
        if not ids:
            return 0
        with session_scope() as session:
            result = session.execute(delete(ChunkVector).where(ChunkVector.chunk_id.in_(ids)))
        self._invalidate()
        return int(result.rowcount or 0)

    def search(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[int] | None = None,
    ) -> list[VectorHit]:
        self._load()
        if self._matrix is None or self._ids is None:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if query.shape[0] != self._matrix.shape[1]:
            return []
        scores = self._matrix @ query
        if allowed_ids is not None:
            mask = np.isin(self._ids, np.fromiter(allowed_ids, dtype=np.int64))
            scores = np.where(mask, scores, -np.inf)
        top_k = max(1, int(top_k))
        if scores.size <= top_k:
            order = np.argsort(-scores)
        else:
            partial = np.argpartition(-scores, top_k)[: top_k * 2]
            order = partial[np.argsort(-scores[partial])]
        hits: list[VectorHit] = []
        for index in order:
            score = float(scores[int(index)])
            if not np.isfinite(score):
                continue
            hits.append(VectorHit(chunk_id=int(self._ids[int(index)]), score=score))
            if len(hits) >= top_k:
                break
        return hits

    def clear(self) -> int:
        with session_scope() as session:
            result = session.execute(delete(ChunkVector))
        self._invalidate()
        return int(result.rowcount or 0)

    def stats(self) -> dict[str, Any]:
        with session_scope() as session:
            total = session.execute(select(ChunkVector.id)).all()
        return {"vectors": len(total), "dim": self.dim, "backend": self.name}


class MemoryVectorStore:
    """进程内向量库：测试与「不想落盘」的场景使用。"""

    name = "memory"
    kind = "vector_store"

    def __init__(self, dim: int = 384) -> None:
        self.dim = int(dim)
        self._data: dict[int, np.ndarray] = {}

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "memory", "needs_key": False, "dim": self.dim}

    def upsert(self, items: Iterable[tuple[int, list[float]]]) -> int:
        count = 0
        for chunk_id, vector in items:
            self._data[int(chunk_id)] = np.asarray(vector, dtype=np.float32)
            count += 1
        return count

    def delete(self, chunk_ids: Iterable[int]) -> int:
        return sum(1 for cid in chunk_ids if self._data.pop(int(cid), None) is not None)

    def search(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[int] | None = None,
    ) -> list[VectorHit]:
        if not self._data:
            return []
        query = np.asarray(vector, dtype=np.float32)
        scored: list[VectorHit] = []
        for chunk_id, vec in self._data.items():
            if allowed_ids is not None and chunk_id not in allowed_ids:
                continue
            if vec.shape[0] != query.shape[0]:
                continue
            scored.append(VectorHit(chunk_id=chunk_id, score=float(vec @ query)))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[: max(1, int(top_k))]

    def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count

    def stats(self) -> dict[str, Any]:
        return {"vectors": len(self._data), "dim": self.dim, "backend": self.name}


class PgVectorStore:
    """pgvector 适配：需要 ``CREATE EXTENSION vector`` 与 ``psycopg``。

    未安装时构造即抛错并给出可执行的修复提示，而不是静默降级——静默降级会让
    「检索质量下降」变成无法排查的幽灵问题。
    """

    name = "pgvector"
    kind = "vector_store"

    def __init__(self, url: str, dim: int = 384) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 取决于部署环境
            raise RuntimeError(
                "pgvector 需要 psycopg：pip install \"psycopg[binary]\"，"
                "并确保目标库已执行 CREATE EXTENSION vector;"
            ) from exc
        self.url = url
        self.dim = int(dim)

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "pgvector", "needs_key": False, "dim": self.dim}

    def _connect(self):
        import psycopg

        return psycopg.connect(self.url)

    def upsert(self, items: Iterable[tuple[int, list[float]]]) -> int:
        rows = list(items)
        if not rows:
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_vectors (
                    chunk_id BIGINT PRIMARY KEY,
                    dim INT NOT NULL,
                    embedding vector NOT NULL
                )
                """
            )
            for chunk_id, vector in rows:
                literal = "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"
                cur.execute(
                    """
                    INSERT INTO chunk_vectors (chunk_id, dim, embedding)
                    VALUES (%s, %s, %s::vector)
                    ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding
                    """,
                    (int(chunk_id), len(vector), literal),
                )
            conn.commit()
        return len(rows)

    def delete(self, chunk_ids: Iterable[int]) -> int:
        ids = [int(i) for i in chunk_ids]
        if not ids:
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunk_vectors WHERE chunk_id = ANY(%s)", (ids,))
            conn.commit()
        return len(ids)

    def search(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[int] | None = None,
    ) -> list[VectorHit]:
        literal = "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"
        sql = "SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score FROM chunk_vectors"
        params: list[Any] = [literal]
        if allowed_ids:
            sql += " WHERE chunk_id = ANY(%s)"
            params.append(list(allowed_ids))
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([literal, int(top_k)])
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [VectorHit(chunk_id=int(cid), score=float(score)) for cid, score in cur.fetchall()]

    def clear(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunk_vectors")
            conn.commit()
        return 0

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunk_vectors")
            value = cur.fetchone()
        return {"vectors": int(value[0]) if value else 0, "dim": self.dim, "backend": self.name}
