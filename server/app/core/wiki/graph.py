"""知识图谱投影：把本体 + 证据 + 文档编译成可浏览、可查询的图。"""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    ChangeProposal,
    DataPropertyDef,
    DocumentRecord,
    Evidence,
    ObjectType,
    RelationType,
    Space,
    TypeDataProperty,
    TypeLink,
)

NODE_TYPE = "object_type"
NODE_PROPERTY = "data_property"
NODE_DOC = "document"
NODE_RELATION = "relation_type"


class GraphService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def build(
        self,
        space_id: int,
        *,
        include_properties: bool = False,
        include_docs: bool = False,
        status: list[str] | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        stmt = select(ObjectType).where(ObjectType.space_id == space_id)
        if status:
            stmt = stmt.where(ObjectType.status.in_(status))
        if domain:
            stmt = stmt.where(ObjectType.business_domain == domain)
        types = list(self.session.scalars(stmt).all())
        type_ids = {t.id for t in types}
        nodes: list[dict[str, Any]] = [
            {
                "id": f"t:{t.id}",
                "type": NODE_TYPE,
                "ref_id": t.id,
                "label": t.label,
                "sublabel": t.label_en,
                "kind": t.kind,
                "status": t.status,
                "domain": t.business_domain,
                "confidence": round(t.confidence, 3),
                "definition": (t.definition or "")[:160],
                "weight": 1,
            }
            for t in types
        ]
        edges: list[dict[str, Any]] = []

        for t in types:
            if t.parent_id and t.parent_id in type_ids:
                edges.append(
                    {
                        "source": f"t:{t.parent_id}",
                        "target": f"t:{t.id}",
                        "relation": "是一种",
                        "category": "taxonomy",
                        "weight": 1.4,
                    }
                )

        links = self.session.scalars(select(TypeLink).where(TypeLink.space_id == space_id)).all()
        rel_cache: dict[int, RelationType] = {}
        for link in links:
            if link.source_id not in type_ids or link.target_id not in type_ids:
                continue
            rel = rel_cache.get(link.relation_id) or self.session.get(RelationType, link.relation_id)
            if rel:
                rel_cache[rel.id] = rel
            edges.append(
                {
                    "id": f"l:{link.id}",
                    "source": f"t:{link.source_id}",
                    "target": f"t:{link.target_id}",
                    "relation": rel.label if rel else "关联",
                    "relation_iri": rel.iri if rel else "",
                    "category": rel.category if rel else "association",
                    "cardinality": link.cardinality,
                    "confidence": round(link.confidence, 3),
                    "origin": link.origin,
                    "description": (link.description or "")[:140],
                    "weight": 0.7 + link.confidence,
                }
            )

        if include_properties:
            slots = self.session.scalars(
                select(TypeDataProperty).join(ObjectType, ObjectType.id == TypeDataProperty.object_type_id).where(
                    ObjectType.space_id == space_id
                )
            ).all()
            for slot in slots:
                definition = self.session.get(DataPropertyDef, slot.property_id)
                if definition is None:
                    continue
                nodes.append(
                    {
                        "id": f"p:{definition.id}",
                        "type": NODE_PROPERTY,
                        "ref_id": definition.id,
                        "label": definition.label,
                        "sublabel": definition.datatype,
                        "kind": "property",
                        "status": "active",
                        "domain": "",
                        "required": slot.required,
                        "weight": 0.5,
                    }
                )
                edges.append(
                    {
                        "source": f"t:{slot.object_type_id}",
                        "target": f"p:{definition.id}",
                        "relation": "具有属性",
                        "category": "attribute",
                        "weight": 0.4,
                    }
                )

        if include_docs:
            doc_edges, doc_nodes = self._evidence_layer(type_ids)
            nodes.extend(doc_nodes)
            edges.extend(doc_edges)

        degrees: dict[str, int] = {}
        for edge in edges:
            degrees[edge["source"]] = degrees.get(edge["source"], 0) + 1
            degrees[edge["target"]] = degrees.get(edge["target"], 0) + 1
        for node in nodes:
            node["degree"] = degrees.get(node["id"], 0)
            node["weight"] = round(node["weight"] * (1 + 0.12 * node["degree"]), 3)

        return {
            "space_id": space_id,
            "nodes": nodes,
            "edges": edges,
            "stats": self._stats(nodes, edges),
        }

    def _evidence_layer(self, type_ids: set[int]) -> tuple[list[dict], list[dict]]:
        """把有证据链的文档挂到对应对象类型上。"""
        proposals = self.session.scalars(
            select(ChangeProposal).where(ChangeProposal.payload.isnot(None))
        ).all()
        label_to_id: dict[str, int] = {}
        for row in self.session.scalars(select(ObjectType).where(ObjectType.id.in_(type_ids))).all():
            label_to_id[row.label] = row.id
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        for proposal in proposals:
            payload = proposal.payload or {}
            label = payload.get("label") or payload.get("subject")
            if not label or label not in label_to_id:
                continue
            for ev in proposal.evidences:
                if not ev.doc_id:
                    continue
                doc = self.session.get(DocumentRecord, ev.doc_id)
                if doc is None:
                    continue
                node_id = f"d:{doc.id}"
                nodes.setdefault(
                    node_id,
                    {
                        "id": node_id,
                        "type": NODE_DOC,
                        "ref_id": doc.id,
                        "label": doc.name,
                        "sublabel": doc.doc_format,
                        "kind": doc.doc_type or "文档",
                        "status": doc.status,
                        "domain": "",
                        "authority": doc.authority,
                        "weight": 0.8,
                    },
                )
                edges.append(
                    {
                        "source": f"t:{label_to_id[label]}",
                        "target": node_id,
                        "relation": "证据来源",
                        "category": "evidence",
                        "locator": ev.locator,
                        "quote": (ev.quote or "")[:120],
                        "weight": 0.3,
                    }
                )
        return edges, list(nodes.values())

    def _stats(self, nodes: list[dict], edges: list[dict]) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for node in nodes:
            by_type[node["type"]] = by_type.get(node["type"], 0) + 1
        by_category: dict[str, int] = {}
        for edge in edges:
            by_category[edge.get("category", "association")] = by_category.get(edge.get("category", "association"), 0) + 1
        type_nodes = [n for n in nodes if n["type"] == NODE_TYPE]
        n = max(len(type_nodes), 1)
        orphans = [node["label"] for node in type_nodes if node.get("degree", 0) == 0]
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "type_count": len(type_nodes),
            "nodes_by_type": by_type,
            "edges_by_category": by_category,
            "density": round(len(edges) / (n * (n - 1)) if n > 1 else 0.0, 4),
            "avg_degree": round(2 * len(edges) / n, 2),
            "orphans": orphans[:20],
            "orphan_count": len(orphans),
            "components": self._components(nodes, edges),
        }

    def _components(self, nodes: list[dict], edges: list[dict]) -> int:
        adjacency: dict[str, set[str]] = {node["id"]: set() for node in nodes}
        for edge in edges:
            adjacency.setdefault(edge["source"], set()).add(edge["target"])
            adjacency.setdefault(edge["target"], set()).add(edge["source"])
        visited: set[str] = set()
        count = 0
        for node in nodes:
            if node["id"] in visited:
                continue
            count += 1
            queue = deque([node["id"]])
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                queue.extend(adjacency.get(current, set()) - visited)
        return count

    # ------------------------------------------------------------ 子图/路径
    def subgraph(self, space_id: int, center_type_id: int, *, depth: int = 2, include_properties: bool = False) -> dict[str, Any]:
        full = self.build(space_id, include_properties=include_properties)
        center = f"t:{center_type_id}"
        adjacency: dict[str, list[tuple[str, dict]]] = {}
        for edge in full["edges"]:
            adjacency.setdefault(edge["source"], []).append((edge["target"], edge))
            adjacency.setdefault(edge["target"], []).append((edge["source"], edge))
        seen: set[str] = {center}
        selected_edges: list[dict] = []
        frontier = [center]
        for level in range(depth):
            nxt: list[str] = []
            for node_id in frontier:
                for neighbour, edge in adjacency.get(node_id, []):
                    selected_edges.append({**edge, "hop": level + 1})
                    if neighbour not in seen:
                        seen.add(neighbour)
                        nxt.append(neighbour)
            frontier = nxt
        nodes = [node for node in full["nodes"] if node["id"] in seen]
        return {"center": center, "depth": depth, "nodes": nodes, "edges": dedupe_edges(selected_edges), "stats": self._stats(nodes, dedupe_edges(selected_edges))}

    def path(self, space_id: int, start_type_id: int, end_type_id: int, *, max_hops: int = 6) -> dict[str, Any]:
        full = self.build(space_id)
        adjacency: dict[str, list[tuple[str, dict]]] = {}
        for edge in full["edges"]:
            adjacency.setdefault(edge["source"], []).append((edge["target"], edge))
            adjacency.setdefault(edge["target"], []).append((edge["source"], edge))
        start, goal = f"t:{start_type_id}", f"t:{end_type_id}"
        queue: deque[list[Any]] = deque([[start, [], 0]])
        visited = {start}
        while queue:
            node_id, trail, hops = queue.popleft()
            if node_id == goal:
                return {"found": True, "hops": hops, "path": trail, "labels": self._node_labels(full, [step["from"] for step in trail] + [goal])}
            if hops >= max_hops:
                continue
            for neighbour, edge in adjacency.get(node_id, []):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                queue.append([neighbour, trail + [{"from": node_id, "to": neighbour, "relation": edge.get("relation", ""), "direction": edge.get("category", "")}], hops + 1])
        return {"found": False, "hops": -1, "path": [], "labels": []}

    def _node_labels(self, graph: dict[str, Any], ids: list[str]) -> list[str]:
        by_id = {node["id"]: node["label"] for node in graph["nodes"]}
        return [by_id.get(i, i) for i in dict.fromkeys(ids)]

    # ------------------------------------------------------------ 分析
    def insights(self, space_id: int) -> dict[str, Any]:
        graph = self.build(space_id, include_properties=False, include_docs=True)
        stats = graph["stats"]
        hub_nodes = sorted(graph["nodes"], key=lambda n: -n.get("degree", 0))[:8]
        proposals = list(
            self.session.scalars(
                select(ChangeProposal).where(ChangeProposal.space_id == space_id).order_by(ChangeProposal.id.desc()).limit(10)
            ).all()
        )
        types_without_evidence = self.session.execute(
            select(ObjectType.label)
            .where(ObjectType.space_id == space_id)
            .outerjoin(ChangeProposal, ChangeProposal.space_id == ObjectType.space_id)
            .outerjoin(Evidence, Evidence.proposal_id == ChangeProposal.id)
            .where(Evidence.id.is_(None))
            .group_by(ObjectType.label)
        ).all()
        return {
            "stats": stats,
            "hubs": [{"label": node["label"], "degree": node.get("degree", 0), "id": node["ref_id"]} for node in hub_nodes],
            "isolated": stats["orphans"],
            "coverage": {
                "types_without_evidence": [row[0] for row in types_without_evidence][:30],
                "count": len(types_without_evidence),
            },
            "recent_proposals": [
                {"id": p.id, "title": p.title, "status": p.status, "confidence": round(p.confidence, 2)} for p in proposals
            ],
            "domain_distribution": dict(
                self.session.execute(
                    select(ObjectType.business_domain, func.count()).where(ObjectType.space_id == space_id).group_by(ObjectType.business_domain)
                ).all()
            ),
        }

    def space_name(self, space_id: int) -> str:
        space = self.session.get(Space, space_id)
        return space.name if space else ""


def dedupe_edges(edges: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for edge in edges:
        key = (edge["source"], edge["target"], edge.get("relation", ""), edge.get("category", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(edge)
    return out
