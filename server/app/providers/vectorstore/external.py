"""外部向量库适配（需自行安装客户端并配置连接串）。

保留同样的 VectorStore 协议，配置后可直接替换默认的 sqlite_numpy 实现。
"""

from __future__ import annotations

import json

import numpy as np

from ..base import VectorHit, ProviderError


class PgVectorStore:
    """PostgreSQL + pgvector。dsn 例：postgresql+psycopg://user:pw@host:5432/db"""

    name = "pgvector"

    def __init__(self, dsn: str, *, table: str = "finonto_vectors") -> None:
        self.dsn = dsn
        self.table = table
        try:
            from sqlalchemy import create_engine, text  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(f"缺少依赖: {exc}") from exc
        self._engine = None

    def _connect(self):  # noqa: ANN201
        if self._engine is None:
            from sqlalchemy import create_engine, text

            self._engine = create_engine(self.dsn, future=True, pool_pre_ping=True)
            with self._engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {self.table} ("
                        " namespace TEXT NOT NULL, ref_id BIGINT NOT NULL,"
                        " embedding vector, meta JSONB, PRIMARY KEY(namespace, ref_id))"
                    )
                )
        return self._engine

    def upsert(self, namespace: str, vectors: dict[int, np.ndarray], meta: dict[int, dict] | None = None) -> int:
        from sqlalchemy import text

        meta = meta or {}
        engine = self._connect()
        count = 0
        with engine.begin() as conn:
            for ref_id, vec in vectors.items():
                conn.execute(
                    text(
                        f"INSERT INTO {self.table}(namespace, ref_id, embedding, meta) "
                        "VALUES (:ns, :rid, CAST(:emb AS vector), CAST(:meta AS jsonb)) "
                        "ON CONFLICT(namespace, ref_id) DO UPDATE "
                        "SET embedding=EXCLUDED.embedding, meta=EXCLUDED.meta"
                    ),
                    {
                        "ns": namespace,
                        "rid": ref_id,
                        "emb": "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]",
                        "meta": json.dumps(meta.get(ref_id, {})),
                    },
                )
                count += 1
        return count

    def delete(self, namespace: str, ref_ids: list[int]) -> int:
        from sqlalchemy import text

        with self._connect().begin() as conn:
            result = conn.execute(
                text(f"DELETE FROM {self.table} WHERE namespace=:ns AND ref_id = ANY(:ids)"),
                {"ns": namespace, "ids": ref_ids},
            )
        return int(result.rowcount or 0)

    def search(self, namespace: str, vector: np.ndarray, top_k: int, filters: dict | None = None) -> list[VectorHit]:
        from sqlalchemy import text

        emb = "[" + ",".join(f"{float(x):.7f}" for x in vector) + "]"
        with self._connect().connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT ref_id, 1 - (embedding <=> CAST(:emb AS vector)) AS score, meta "
                    f"FROM {self.table} WHERE namespace=:ns ORDER BY embedding <=> CAST(:emb AS vector) "
                    "LIMIT :k"
                ),
                {"ns": namespace, "emb": emb, "k": top_k},
            ).fetchall()
        return [VectorHit(ref_id=int(r[0]), score=float(r[1]), meta=json.loads(r[2] or "{}")) for r in rows]

    def count(self, namespace: str) -> int:
        from sqlalchemy import text

        with self._connect().connect() as conn:
            return int(
                conn.execute(
                    text(f"SELECT count(*) FROM {self.table} WHERE namespace=:ns"), {"ns": namespace}
                ).scalar_one()
            )


class DashVectorStore:
    """阿里云 DashVector Collection（需 dashvector 客户端与 API Key）。"""

    name = "dashvector"

    def __init__(self, *, api_key: str, endpoint: str, collection: str, dim: int) -> None:
        try:
            import dashvector  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "未安装 dashvector 客户端，请 pip install dashvector 并配置 FINONTO_VECTOR_STORE=dashvector"
            ) from exc
        self._client = dashvector.Client(api_key=api_key, endpoint=endpoint)
        self._collection = collection
        self.dim = dim

    def upsert(self, namespace: str, vectors: dict[int, np.ndarray], meta: dict[int, dict] | None = None) -> int:
        meta = meta or {}
        docs = [
            {"id": f"{namespace}:{ref_id}", "vector": list(map(float, vec)), "fields": meta.get(ref_id, {})}
            for ref_id, vec in vectors.items()
        ]
        self._client.collection(self._collection).upsert(docs)
        return len(docs)

    def delete(self, namespace: str, ref_ids: list[int]) -> int:
        ids = [f"{namespace}:{r}" for r in ref_ids]
        self._client.collection(self._collection).delete(ids)
        return len(ids)

    def search(self, namespace: str, vector: np.ndarray, top_k: int, filters: dict | None = None) -> list[VectorHit]:
        result = self._client.collection(self._collection).query(
            vector=list(map(float, vector)), topk=top_k, filter=f"namespace='{namespace}'"
        )
        hits = []
        for doc in result.output or []:
            ref = str(doc.id).split(":")[-1]
            hits.append(VectorHit(ref_id=int(ref), score=float(doc.score), meta=doc.fields or {}))
        return hits

    def count(self, namespace: str) -> int:  # pragma: no cover
        return self._client.collection(self._collection).stats().as_dict().get("total_doc_count", 0)
