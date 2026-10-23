import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import type { GraphEdge, GraphInsights, GraphNode, GraphPath, GraphPayload } from "../types";

type Pos = { x: number; y: number; vx: number; vy: number };

const VIEW_W = 960;
const VIEW_H = 560;

const CATEGORY_COLOR: Record<string, string> = {
  taxonomy: "#4ea1ff",
  association: "#7fd1b9",
  attribute: "#a48ad6",
  evidence: "#ffb454",
  hierarchy: "#4ea1ff",
};

const TYPE_COLOR: Record<string, string> = {
  object_type: "#dbe4ee",
  data_property: "#8fd6ff",
  document: "#ffcf87",
  relation_type: "#b6f0c2",
};

function seed(nodes: GraphNode[]): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  nodes.forEach((node, index) => {
    const angle = (index / Math.max(nodes.length, 1)) * Math.PI * 2;
    const radius = 150 + (index % 5) * 24;
    out[node.id] = {
      x: VIEW_W / 2 + Math.cos(angle) * radius,
      y: VIEW_H / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
    };
  });
  return out;
}

function step(
  prev: Record<string, Pos>,
  nodes: GraphNode[],
  edges: GraphEdge[],
  alpha: number,
  pinned: string | null,
): Record<string, Pos> {
  const next: Record<string, Pos> = {};
  nodes.forEach((node) => {
    const point = prev[node.id] ?? { x: VIEW_W / 2, y: VIEW_H / 2, vx: 0, vy: 0 };
    next[node.id] = { ...point };
  });
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = next[nodes[i].id];
      const b = next[nodes[j].id];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.max(Math.hypot(dx, dy), 1);
      const force = (2600 / (distance * distance)) * alpha;
      const ux = (dx / distance) * force;
      const uy = (dy / distance) * force;
      a.vx -= ux;
      a.vy -= uy;
      b.vx += ux;
      b.vy += uy;
    }
  }
  edges.forEach((edge) => {
    const a = next[edge.source];
    const b = next[edge.target];
    if (!a || !b) return;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.max(Math.hypot(dx, dy), 1);
    const rest = edge.category === "attribute" ? 62 : edge.category === "evidence" ? 96 : 118;
    const force = (distance - rest) * 0.018 * alpha;
    const ux = (dx / distance) * force;
    const uy = (dy / distance) * force;
    a.vx += ux;
    a.vy += uy;
    b.vx -= ux;
    b.vy -= uy;
  });
  nodes.forEach((node) => {
    const point = next[node.id];
    point.vx += (VIEW_W / 2 - point.x) * 0.006 * alpha;
    point.vy += (VIEW_H / 2 - point.y) * 0.006 * alpha;
    if (node.id === pinned) {
      point.vx = 0;
      point.vy = 0;
      return;
    }
    point.vx *= 0.82;
    point.vy *= 0.82;
    point.x += Math.max(-26, Math.min(26, point.vx));
    point.y += Math.max(-26, Math.min(26, point.vy));
    point.x = Math.max(28, Math.min(VIEW_W - 28, point.x));
    point.y = Math.max(26, Math.min(VIEW_H - 26, point.y));
  });
  return next;
}

export default function GraphView() {
  const { spaceId } = useApp();
  const [mode, setMode] = useState<"full" | "sub" | "path">("full");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [insights, setInsights] = useState<GraphInsights | null>(null);
  const [path, setPath] = useState<GraphPath | null>(null);
  const [includeProperties, setIncludeProperties] = useState(false);
  const [includeDocs, setIncludeDocs] = useState(false);
  const [status, setStatus] = useState("");
  const [center, setCenter] = useState<number | null>(null);
  const [depth, setDepth] = useState(2);
  const [startId, setStartId] = useState<number | null>(null);
  const [endId, setEndId] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [positions, setPositions] = useState<Record<string, Pos>>({});
  const alphaRef = useRef(1);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<string | null>(null);

  // graph 不变时保持同一个数组引用，否则下面的 seed/rAF 效果会自我触发
  const nodes = useMemo(() => graph?.nodes ?? [], [graph]);
  const edges = useMemo(() => {
    if (!graph) return [];
    const present = new Set(graph.nodes.map((node) => node.id));
    return graph.edges.filter((edge) => present.has(edge.source) && present.has(edge.target));
  }, [graph]);

  const nodeById = useMemo(() => {
    const map: Record<string, GraphNode> = {};
    nodes.forEach((node) => {
      map[node.id] = node;
    });
    return map;
  }, [nodes]);

  const typeNodes = useMemo(() => nodes.filter((node) => node.type === "object_type"), [nodes]);

  useEffect(() => {
    alphaRef.current = 1;
    setPositions(seed(nodes));
    setSelected(null);
  }, [nodes]);

  useEffect(() => {
    let frame = 0;
    let raf = 0;
    const tick = () => {
      if (alphaRef.current > 0.02 && nodes.length) {
        alphaRef.current *= 0.97;
        setPositions((prev) => step(prev, nodes, edges, alphaRef.current, dragRef.current));
      }
      frame += 1;
      if (frame < 1200) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [nodes, edges]);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      if (mode === "sub" && center) {
        const data = await api.get<GraphPayload>(
          `/wiki/graph/subgraph${qs({
            center_type_id: center,
            space_id: spaceId,
            depth,
            include_properties: includeProperties || undefined,
          })}`,
        );
        setGraph(data);
        const centerLabel = data.nodes.find((node) => node.id === `t:${center}`)?.label ?? `类型 #${center}`;
        setNotice(`以「${centerLabel}」为中心，${depth} 跳内 ${data.nodes.length} 节点 / ${data.edges.length} 关系`);
      } else {
        const data = await api.get<GraphPayload>(
          `/wiki/graph${qs({
            space_id: spaceId,
            include_properties: includeProperties || undefined,
            include_docs: includeDocs || undefined,
            status: status || undefined,
          })}`,
        );
        setGraph(data);
        setNotice(`全图 ${data.stats.node_count} 节点 / ${data.stats.edge_count} 关系`);
      }
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  }, [center, depth, includeDocs, includeProperties, mode, spaceId, status]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api
      .get<GraphInsights>(`/wiki/graph/insights${qs({ space_id: spaceId })}`)
      .then(setInsights)
      .catch(() => setInsights(null));
  }, [spaceId, graph]);

  const findPath = async () => {
    if (!startId || !endId) {
      setError("请先选择路径的起点与终点类型。");
      return;
    }
    setBusy(true);
    try {
      const data = await api.get<GraphPayload>(`/wiki/graph${qs({ space_id: spaceId })}`);
      setGraph(data);
      const result = await api.get<GraphPath>(`/wiki/graph/path${qs({ space_id: spaceId, start_type_id: startId, end_type_id: endId })}`);
      setPath(result);
      setError("");
      setNotice(
        result.found
          ? `最短链路 ${result.hops} 跳：${result.labels.join(" → ")}`
          : "两个类型在当前图中不可达，建议补充关联或放宽跳数上限。",
      );
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const toViewPoint = (event: React.PointerEvent) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: ((event.clientX - rect.left) / rect.width) * VIEW_W,
      y: ((event.clientY - rect.top) / rect.height) * VIEW_H,
    };
  };

  const onPointerMove = (event: React.PointerEvent) => {
    if (!dragRef.current) return;
    const point = toViewPoint(event);
    const id = dragRef.current;
    alphaRef.current = Math.max(alphaRef.current, 0.35);
    setPositions((prev) => ({ ...prev, [id]: { x: point.x, y: point.y, vx: 0, vy: 0 } }));
  };

  const pathNodeIds = useMemo(() => {
    if (!path?.found) return new Set<string>();
    const ids = new Set<string>();
    path.path.forEach((hop) => {
      ids.add(hop.from);
      ids.add(hop.to);
    });
    return ids;
  }, [path]);

  const neighbourIds = useMemo(() => {
    if (!selected) return new Set<string>();
    const ids = new Set<string>([selected]);
    edges.forEach((edge) => {
      if (edge.source === selected) ids.add(edge.target);
      if (edge.target === selected) ids.add(edge.source);
    });
    return ids;
  }, [edges, selected]);

  const highlight = pathNodeIds.size > 0 ? pathNodeIds : neighbourIds;
  const selectedNode = selected ? nodeById[selected] : null;
  const stats = graph?.stats;

  return (
    <div className="split">
      <div style={{ minWidth: 0 }}>
        <div className="card">
          <h3>图谱视图（本体 + 属性 + 证据文档的投影）</h3>
          <div className="row">
            {(["full", "sub", "path"] as const).map((value) => (
              <button key={value} className={mode === value ? "primary" : "ghost"} onClick={() => setMode(value)}>
                {value === "full" ? "全图" : value === "sub" ? "邻域子图" : "路径查询"}
              </button>
            ))}
            <span className="grow" />
            <label className="muted">
              <input type="checkbox" checked={includeProperties} onChange={(event) => setIncludeProperties(event.target.checked)} /> 含数据属性
            </label>
            <label className="muted">
              <input type="checkbox" checked={includeDocs} onChange={(event) => setIncludeDocs(event.target.checked)} disabled={mode === "sub"} /> 含证据文档
            </label>
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">全部状态</option>
              <option value="confirmed">仅已确认</option>
              <option value="draft">仅草稿</option>
              <option value="confirmed,draft">草稿+已确认</option>
              <option value="deprecated">已废弃</option>
            </select>
          </div>

          {mode === "sub" && (
            <div className="row" style={{ marginTop: 8 }}>
              <label className="muted">中心类型</label>
              <select value={center ?? ""} onChange={(event) => setCenter(Number(event.target.value))}>
                {typeNodes.map((node) => (
                  <option key={node.ref_id} value={node.ref_id}>
                    {node.label}
                  </option>
                ))}
              </select>
              <label className="muted">跳数</label>
              <select value={depth} onChange={(event) => setDepth(Number(event.target.value))}>
                {[1, 2, 3, 4].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          )}

          {mode === "path" && (
            <div className="row" style={{ marginTop: 8 }}>
              <label className="muted">起点</label>
              <select value={startId ?? ""} onChange={(event) => setStartId(Number(event.target.value))}>
                <option value="">选择…</option>
                {typeNodes.map((node) => (
                  <option key={node.ref_id} value={node.ref_id}>
                    {node.label}
                  </option>
                ))}
              </select>
              <label className="muted">终点</label>
              <select value={endId ?? ""} onChange={(event) => setEndId(Number(event.target.value))}>
                <option value="">选择…</option>
                {typeNodes.map((node) => (
                  <option key={node.ref_id} value={node.ref_id}>
                    {node.label}
                  </option>
                ))}
              </select>
              <button onClick={findPath} disabled={busy}>
                查询最短链路
              </button>
            </div>
          )}

          <div className="muted" style={{ marginTop: 8 }}>
            {notice}
            {error && <div className="error">{error}</div>}
          </div>
        </div>

        <div className="card" style={{ padding: 8 }}>
          <svg
            ref={svgRef}
            className="graph"
            viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
            preserveAspectRatio="xMidYMid meet"
            onPointerMove={onPointerMove}
            onPointerUp={() => {
              dragRef.current = null;
            }}
            onPointerLeave={() => {
              dragRef.current = null;
            }}
          >
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#3f5f80" />
              </marker>
            </defs>
            {edges.map((edge, index) => {
              const a = positions[edge.source];
              const b = positions[edge.target];
              if (!a || !b) return null;
              const hot = highlight.size > 0 && (highlight.has(edge.source) && highlight.has(edge.target));
              const dim = highlight.size > 0 && !hot;
              const color = CATEGORY_COLOR[edge.category] ?? "#3f5f80";
              return (
                <g key={`${edge.source}-${edge.target}-${index}`}>
                  <line
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={hot ? color : undefined}
                    className={hot ? "hot" : undefined}
                    opacity={dim ? 0.18 : 0.8}
                    markerEnd={edge.category === "attribute" ? undefined : "url(#arrow)"}
                  />
                  {hot && (
                    <text className="edge" x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 2} textAnchor="middle">
                      {edge.relation}
                    </text>
                  )}
                </g>
              );
            })}
            {nodes.map((node) => {
              const point = positions[node.id];
              if (!point) return null;
              const dim = highlight.size > 0 && !highlight.has(node.id);
              const radius = Math.min(6 + node.degree * 1.6 + (node.weight ?? 1) * 2, 20);
              return (
                <g
                  className="node"
                  key={node.id}
                  opacity={dim ? 0.25 : 1}
                  onPointerDown={(event) => {
                    event.stopPropagation();
                    dragRef.current = node.id;
                  }}
                  onClick={() => setSelected(node.id === selected ? null : node.id)}
                >
                  <circle
                    cx={point.x}
                    cy={point.y}
                    r={radius}
                    fill={node.type === "object_type" ? (node.status === "confirmed" ? "#1d4f80" : "#3a3f2a") : "#22303f"}
                    stroke={selected === node.id ? "#ffb454" : TYPE_COLOR[node.type] ?? "#dbe4ee"}
                    strokeWidth={selected === node.id ? 2.4 : 1.3}
                  />
                  <text x={point.x} y={point.y + radius + 11} textAnchor="middle">
                    {node.label.length > 14 ? `${node.label.slice(0, 13)}…` : node.label}
                  </text>
                </g>
              );
            })}
          </svg>
          <div className="row" style={{ marginTop: 6 }}>
            <span className="muted">节点数 {stats?.node_count ?? 0} · 关系数 {stats?.edge_count ?? 0} · 密度 {stats?.density ?? 0} · 平均度 {stats?.avg_degree ?? 0} · 连通分量 {stats?.components ?? 0}</span>
            <span className="grow" />
            <span className="chip">蓝=已确认类型</span>
            <span className="chip">橄榄=草稿</span>
            <span className="chip">橙=文档</span>
            <span className="muted">拖动可改变布局，点击节点看详情并聚焦</span>
          </div>
        </div>
      </div>

      <div>
        <div className="card">
          <h3>节点详情</h3>
          {!selectedNode && <div className="empty">点击图中节点。</div>}
          {selectedNode && (
            <>
              <div className="row">
                <h2 className="grow">{selectedNode.label}</h2>
                <span className="chip">{selectedNode.type === "object_type" ? "对象类型" : selectedNode.type === "data_property" ? "数据属性" : "文档"}</span>
                {selectedNode.status && <span className={`chip ${selectedNode.status}`}>{selectedNode.status}</span>}
              </div>
              {selectedNode.sublabel && <div className="muted mono">{selectedNode.sublabel}</div>}
              {selectedNode.definition && <div style={{ marginTop: 8 }}>{selectedNode.definition}…</div>}
              <div className="muted" style={{ marginTop: 8 }}>
                度（连接数）：{selectedNode.degree}
                {typeof selectedNode.confidence === "number" && ` · 置信 ${selectedNode.confidence}`}
                {typeof selectedNode.authority === "number" && ` · 权威度 ${selectedNode.authority}`}
              </div>
              <div className="row" style={{ marginTop: 10 }}>
                {selectedNode.type === "object_type" && (
                  <>
                    <button
                      className="mini"
                      onClick={() => {
                        setCenter(selectedNode.ref_id);
                        setMode("sub");
                      }}
                    >
                      聚焦邻域子图
                    </button>
                    <button className="mini ghost" onClick={() => setStartId(selectedNode.ref_id)}>
                      设为路径起点
                    </button>
                    <button className="mini ghost" onClick={() => setEndId(selectedNode.ref_id)}>
                      设为路径终点
                    </button>
                  </>
                )}
              </div>
              <div className="title-sm">相邻关系</div>
              {edges
                .filter((edge) => edge.source === selectedNode.id || edge.target === selectedNode.id)
                .slice(0, 24)
                .map((edge, index) => {
                  const other = edge.source === selectedNode.id ? edge.target : edge.source;
                  return (
                    <div key={`${other}-${index}`} className="muted">
                      <span className="mono">{nodeById[edge.source]?.label}</span>
                      <span style={{ color: CATEGORY_COLOR[edge.category] ?? "#8b9aac" }}> —{edge.relation}→ </span>
                      <span className="mono">{nodeById[edge.target]?.label}</span>
                      {edge.cardinality && <span className="chip">{edge.cardinality}</span>}
                    </div>
                  );
                })}
            </>
          )}
        </div>

        {insights && (
          <div className="card">
            <h3>图谱洞察</h3>
            <div className="row">
              <span className="stat">类型<b>{insights.stats.type_count}</b></span>
              <span className="stat">孤立<b>{insights.stats.orphan_count}</b></span>
              <span className="stat">无证据类型<b>{insights.coverage.count}</b></span>
            </div>
            <div className="title-sm">枢纽节点（连接最多）</div>
            {insights.hubs.map((hub) => (
              <span
                key={hub.label}
                className="chip read"
                style={{ cursor: "pointer" }}
                onClick={() => setSelected(nodes.find((node) => node.label === hub.label)?.id ?? null)}
              >
                {hub.label} · {hub.degree}
              </span>
            ))}
            <div className="title-sm">业务域分布</div>
            <table className="grid">
              <tbody>
                {Object.entries(insights.domain_distribution).map(([domain, count]) => (
                  <tr key={domain || "未分类"}>
                    <td>{domain || "未分类"}</td>
                    <td className="mono">{count}</td>
                    <td>
                      <div style={{ height: 8, width: `${Math.min(count * 8, 100)}%`, background: "#2c6ba8", borderRadius: 4 }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {insights.coverage.types_without_evidence.length > 0 && (
              <>
                <div className="title-sm">缺少原文证据的类型（建议补充提案或驳回）</div>
                <div>
                  {insights.coverage.types_without_evidence.map((label) => (
                    <span key={label} className="chip pending">{label}</span>
                  ))}
                </div>
              </>
            )}
            {insights.isolated.length > 0 && (
              <>
                <div className="title-sm">孤立节点</div>
                <div>
                  {insights.isolated.map((label) => (
                    <span key={label} className="chip">{label}</span>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {path?.found && (
          <div className="card">
            <h3>链路结果（{path.hops} 跳）</h3>
            {path.path.map((hop, index) => (
              <div key={index} className="muted">
                <span className="mono">{nodeById[hop.from]?.label ?? hop.from}</span>
                <span style={{ color: "#ffb454" }}> —{hop.relation}→ </span>
                <span className="mono">{nodeById[hop.to]?.label ?? hop.to}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
