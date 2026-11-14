"""知识图谱投影：节点/边构造、力导向布局（numpy）、邻域、路径与洞察。

为什么布局放在 Python
---------------------
1. **确定性**：固定迭代次数 + 固定随机种子，同一张图每次编译坐标一致，
   于是图谱快照可以进版本 diff（坐标变了 = 结构变了）；
2. **可导出**：服务端能直接产出 SVG（报告、邮件、归档），不需要浏览器；
3. **前端更薄**：浏览器只做 pan/zoom 与点击，约 120 行 JS。

节点前缀：``t:`` 对象类型、``p:`` 数据属性、``d:`` 文档。
边类型：``taxonomy``（父子）、``association``（类型间关联）、``attribute``（类型→属性）、
``evidence``（文档→类型，虚线，表示证据来源）。
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any

import numpy as np
from sqlalchemy import select

from ...models import Constraint, DataPropertyDef, Document, Evidence, ObjectType, RelationType, TypeDataProperty, TypeLink
from ..ontology import kinds


# --------------------------------------------------------------------------- #
# 图构造
# --------------------------------------------------------------------------- #
def build_graph(session, space_id: int) -> dict[str, Any]:
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id)
    ).scalars().all()
    type_by_id = {item.id: item for item in types}
    type_by_label = {item.label: item for item in types}

    links = session.execute(select(TypeLink).where(TypeLink.space_id == space_id)).scalars().all()
    relations = {
        item.id: item
        for item in session.execute(select(RelationType).where(RelationType.space_id == space_id)).scalars()
    }

    prop_rows = session.execute(
        select(DataPropertyDef.id, DataPropertyDef.label, DataPropertyDef.data_type, TypeDataProperty.type_id)
        .join(TypeDataProperty, TypeDataProperty.property_id == DataPropertyDef.id)
        .where(TypeDataProperty.type_id.in_([item.id for item in types] or [-1]))
    ).all()

    constraints = session.execute(
        select(Constraint.type_id, Constraint.severity).where(Constraint.space_id == space_id)
    ).all()
    constraint_count = Counter(row[0] for row in constraints)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for item in types:
        nodes.append(
            {
                "id": f"t:{item.id}",
                "kind": "type",
                "label": item.label,
                "type_kind": item.kind,
                "type_kind_label": kinds.kind_label(item.kind),
                "business_domain": item.business_domain or "未分域",
                "confidence": round(float(item.confidence or 0), 2),
                "constraints": constraint_count.get(item.id, 0),
                "is_abstract": bool(item.is_abstract),
            }
        )

    for item in types:
        if item.parent_id and item.parent_id in type_by_id:
            edges.append(
                {
                    "source": f"t:{item.parent_id}",
                    "target": f"t:{item.id}",
                    "kind": "taxonomy",
                    "label": "子类",
                    "weight": 1.2,
                }
            )

    for link in links:
        if link.source_type_id not in type_by_id or link.target_type_id not in type_by_id:
            continue
        relation = relations.get(link.relation_id)
        edges.append(
            {
                "source": f"t:{link.source_type_id}",
                "target": f"t:{link.target_type_id}",
                "kind": "association",
                "label": f"{relation.label} ({kinds.relation_label(relation.category)})" if relation else "关联",
                "weight": 1.0,
            }
        )

    seen_props: set[int] = set()
    for prop_id, label, data_type, type_id in prop_rows:
        node_id = f"p:{prop_id}"
        if prop_id not in seen_props:
            seen_props.add(prop_id)
            nodes.append(
                {
                    "id": node_id,
                    "kind": "property",
                    "label": label,
                    "data_type": data_type,
                    "business_domain": "属性",
                    "confidence": 1.0,
                }
            )
        edges.append(
            {
                "source": f"t:{type_id}",
                "target": node_id,
                "kind": "attribute",
                "label": data_type,
                "weight": 0.5,
            }
        )

    evidence_rows = session.execute(
        select(Evidence.document_id, Evidence.note, Document.title)
        .outerjoin(Document, Document.id == Evidence.document_id)
        .where(Evidence.space_id == space_id)
    ).all()
    doc_ids: set[int] = set()
    for document_id, note, title in evidence_rows:
        if document_id is None:
            continue
        target_label = ""
        if note:
            for label in type_by_label:
                if label in note:
                    target_label = label
                    break
        if not target_label:
            continue
        target = type_by_label[target_label]
        document_key = f"d:{document_id}"
        if document_id not in doc_ids:
            doc_ids.add(document_id)
            nodes.append(
                {
                    "id": document_key,
                    "kind": "document",
                    "label": title or f"文档 #{document_id}",
                    "business_domain": "证据",
                    "confidence": 1.0,
                }
            )
        edges.append(
            {
                "source": document_key,
                "target": f"t:{target.id}",
                "kind": "evidence",
                "label": "证据",
                "weight": 0.8,
            }
        )

    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# 布局（Fruchterman-Reingold，确定性）
# --------------------------------------------------------------------------- #
def layout(
    graph: dict[str, Any],
    *,
    width: float = 920.0,
    height: float = 620.0,
    iterations: int = 180,
    seed: int = 20250101,
) -> dict[str, Any]:
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not nodes:
        return {**graph, "width": width, "height": height}

    index = {node["id"]: i for i, node in enumerate(nodes)}
    count = len(nodes)
    rng = np.random.default_rng(seed)
    positions = rng.uniform(
        [width * 0.15, height * 0.15], [width * 0.85, height * 0.85], size=(count, 2)
    )

    if count > 1:
        source = np.array([index[e["source"]] for e in edges if e["source"] in index and e["target"] in index], dtype=int)
        target = np.array([index[e["target"]] for e in edges if e["source"] in index and e["target"] in index], dtype=int)
        weights = np.array([float(e.get("weight", 1.0)) for e in edges if e["source"] in index and e["target"] in index])
    else:
        source = target = np.array([], dtype=int)
        weights = np.array([], dtype=float)

    area = width * height
    k = float(np.sqrt(area / max(1, count)))
    temperature = min(width, height) / 8.0
    cooling = temperature / max(1, iterations)

    for _ in range(iterations):
        delta = positions[:, None, :] - positions[None, :, :]
        distance = np.linalg.norm(delta, axis=2)
        np.fill_diagonal(distance, np.inf)
        distance = np.maximum(distance, 0.01)
        repulsion = (k * k / distance)[:, :, None] * (delta / distance[:, :, None])
        displacement = repulsion.sum(axis=1)

        if source.size:
            diff = positions[source] - positions[target]
            dist = np.maximum(np.linalg.norm(diff, axis=1), 0.01)
            attraction = (dist * dist / k)[:, None] * (diff / dist[:, None])
            np.add.at(displacement, source, -attraction * weights[:, None])
            np.add.at(displacement, target, attraction * weights[:, None])

        norms = np.maximum(np.linalg.norm(displacement, axis=1), 0.01)
        step = displacement / norms[:, None] * np.minimum(norms, temperature)[:, None]
        positions = positions + step
        positions[:, 0] = np.clip(positions[:, 0], 40.0, width - 40.0)
        positions[:, 1] = np.clip(positions[:, 1], 40.0, height - 40.0)
        temperature = max(cooling, temperature - cooling)

    for i, node in enumerate(nodes):
        node["x"] = round(float(positions[i][0]), 2)
        node["y"] = round(float(positions[i][1]), 2)
        node["r"] = 9.0 if node["kind"] == "type" else 6.0

    return {**graph, "width": width, "height": height}


# --------------------------------------------------------------------------- #
# 查询模式
# --------------------------------------------------------------------------- #
def _adjacency(graph: dict[str, Any]) -> dict[str, list[tuple[str, dict]]]:
    adj: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for edge in graph["edges"]:
        adj[edge["source"]].append((edge["target"], edge))
        adj[edge["target"]].append((edge["source"], edge))
    return adj


def subgraph(graph: dict[str, Any], node_id: str, depth: int = 1) -> dict[str, Any]:
    index = {node["id"]: node for node in graph["nodes"]}
    if node_id not in index:
        return {"nodes": [], "edges": [], "focus": node_id, "depth": depth}
    adj = _adjacency(graph)
    keep = {node_id}
    frontier = deque([(node_id, 0)])
    while frontier:
        current, level = frontier.popleft()
        if level >= depth:
            continue
        for neighbour, _edge in adj.get(current, []):
            if neighbour not in keep:
                keep.add(neighbour)
                frontier.append((neighbour, level + 1))
    edges = [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep]
    return {
        "nodes": [index[nid] for nid in keep if nid in index],
        "edges": edges,
        "focus": node_id,
        "depth": depth,
    }


def shortest_path(graph: dict[str, Any], source: str, target: str) -> dict[str, Any]:
    index = {node["id"]: node for node in graph["nodes"]}
    if source not in index or target not in index:
        return {"found": False, "reason": "节点不存在", "path": []}
    adj = _adjacency(graph)
    queue = deque([source])
    previous: dict[str, str] = {source: ""}
    edge_used: dict[str, dict] = {}
    while queue:
        current = queue.popleft()
        if current == target:
            break
        for neighbour, edge in adj.get(current, []):
            if neighbour not in previous:
                previous[neighbour] = current
                edge_used[neighbour] = edge
                queue.append(neighbour)
    if target not in previous:
        return {"found": False, "reason": "两点不连通", "path": []}
    chain = [target]
    while chain[-1] != source:
        chain.append(previous[chain[-1]])
    chain.reverse()
    steps = []
    for i in range(1, len(chain)):
        edge = edge_used[chain[i]]
        steps.append(
            {
                "from": chain[i - 1],
                "to": chain[i],
                "edge": edge,
                "label": edge.get("label", ""),
            }
        )
    return {
        "found": True,
        "path": [index[node] for node in chain if node in index],
        "steps": steps,
        "length": len(chain) - 1,
    }


def insights(session, space_id: int, graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph["nodes"]
    edges = graph["edges"]
    degree: Counter[str] = Counter()
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    label_of = {node["id"]: node["label"] for node in nodes}
    hubs = [
        {"id": node_id, "label": label_of[node_id], "degree": value}
        for node_id, value in degree.most_common(8)
    ]

    domains = Counter(
        node.get("business_domain", "未分域") for node in nodes if node["kind"] == "type"
    )

    evidenced = {edge["target"] for edge in edges if edge["kind"] == "evidence"}
    type_nodes = [node for node in nodes if node["kind"] == "type"]
    no_evidence = [
        {"id": node["id"], "label": node["label"]}
        for node in type_nodes
        if node["id"] not in evidenced
    ]
    isolated = [
        {"id": node["id"], "label": node["label"]}
        for node in type_nodes
        if degree.get(node["id"], 0) == 0
    ]

    # 连通分量（只看类型节点，属性/文档节点不计入）
    type_ids = {node["id"] for node in type_nodes}
    adj: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge["source"] in type_ids and edge["target"] in type_ids:
            adj[edge["source"]].add(edge["target"])
            adj[edge["target"]].add(edge["source"])
    visited: set[str] = set()
    components = 0
    for node_id in type_ids:
        if node_id in visited:
            continue
        components += 1
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(adj[current] - visited)

    type_edges = [
        edge for edge in edges if edge["source"] in type_ids and edge["target"] in type_ids
    ]
    n = max(1, len(type_ids))
    density = 2 * len(type_edges) / (n * (n - 1)) if n > 1 else 0.0
    avg_degree = sum(degree[i] for i in type_ids) / n if type_ids else 0.0

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "type_count": len(type_ids),
        "hubs": hubs,
        "domains": [{"domain": domain, "count": count} for domain, count in domains.most_common()],
        "no_evidence": no_evidence,
        "isolated": isolated,
        "components": components,
        "density": round(density, 4),
        "avg_degree": round(avg_degree, 2),
        "edge_kinds": [
            {"kind": kind, "count": count}
            for kind, count in Counter(edge["kind"] for edge in edges).most_common()
        ],
    }


def to_svg(graph: dict[str, Any]) -> str:
    """服务端直接出 SVG：用于归档与报告（浏览器端用的是同一份坐标）。"""
    width, height = graph.get("width", 920), graph.get("height", 620)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" class="fonto-graph">',
        '<rect width="100%" height="100%" fill="#0f1420"/>',
    ]
    dash = {"taxonomy": "", "association": "", "attribute": "4 3", "evidence": "2 4"}
    color = {"taxonomy": "#7aa2f7", "association": "#9ece6a", "attribute": "#565f89", "evidence": "#e0af68"}
    positions = {node["id"]: (node["x"], node["y"]) for node in graph["nodes"] if "x" in node}
    for edge in graph["edges"]:
        if edge["source"] not in positions or edge["target"] not in positions:
            continue
        x1, y1 = positions[edge["source"]]
        x2, y2 = positions[edge["target"]]
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color.get(edge["kind"], "#888")}" '
            f'stroke-width="{1.6 if edge["kind"] != "attribute" else 0.8}" '
            f'stroke-dasharray="{dash.get(edge["kind"], "")}" opacity="0.75"/>'
        )
    for node in graph["nodes"]:
        if "x" not in node:
            continue
        radius = node.get("r", 8)
        fill = {"type": "#7aa2f7", "property": "#565f89", "document": "#e0af68"}.get(node["kind"], "#999")
        parts.append(
            f'<circle cx="{node["x"]}" cy="{node["y"]}" r="{radius}" fill="{fill}" opacity="0.9"/>'
        )
        parts.append(
            f'<text x="{node["x"]}" y="{node["y"] - radius - 4}" fill="#c0caf5" font-size="10" '
            f'text-anchor="middle">{_escape(node["label"][:18])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )
