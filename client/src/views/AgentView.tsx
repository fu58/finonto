import { useCallback, useEffect, useRef, useState } from "react";
import { api, qs, streamSSE } from "../api";
import { useApp } from "../App";
import type {
  AgentRunRow,
  AgentStepRow,
  McpServer,
  MemoryRow,
  SandboxConfig,
  SandboxRun,
  SkillRow,
  StreamEventPayload,
  ToolRow,
} from "../types";

type TraceItem = {
  ordinal: number;
  thought: string;
  action: string;
  action_input: Record<string, unknown>;
  observation: string;
  ok: boolean;
  repaired: string;
  proposal?: { proposal_id: number; summary: string };
  chips: string[];
};

const SUBTABS = [
  { key: "react", label: "ReAct 推理" },
  { key: "memory", label: "长期记忆" },
  { key: "mcp", label: "MCP 服务" },
  { key: "sandbox", label: "代码沙箱" },
];

export default function AgentView() {
  const [sub, setSub] = useState("react");
  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        {SUBTABS.map((item) => (
          <button key={item.key} className={sub === item.key ? "primary" : "ghost"} onClick={() => setSub(item.key)}>
            {item.label}
          </button>
        ))}
      </div>
      {sub === "react" && <ReactPanel />}
      {sub === "memory" && <MemoryPanel />}
      {sub === "mcp" && <McpPanel />}
      {sub === "sandbox" && <SandboxPanel />}
    </div>
  );
}

function ReactPanel() {
  const { spaceId, refresh } = useApp();
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [skill, setSkill] = useState("doc-extraction");
  const [goal, setGoal] = useState("从资料中提炼「借款人风险分级」相关概念，为每个概念给出定义并生成本体提案");
  const [maxSteps, setMaxSteps] = useState(8);
  const [trace, setTrace] = useState<TraceItem[]>([]);
  const [meta, setMeta] = useState<{ tools: string[]; memory_hits: string[]; run_id: number | null }>({ tools: [], memory_hits: [], run_id: null });
  const [final, setFinal] = useState<{ answer: string; status: string; steps: number; tokens: number; elapsed_ms: number; proposals: { id: number; title: string }[]; memory_item_id: number | null } | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState<AgentRunRow[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const toolsRef = useRef<Record<string, ToolRow>>({});

  useEffect(() => {
    api.get<SkillRow[]>("/agent/skills").then((rows) => {
      setSkills(rows);
      if (rows.length && !rows.some((row) => row.name === skill)) setSkill(rows[0].name);
    });
    api
      .get<{ builtin: ToolRow[]; mcp: ToolRow[] }>("/agent/tools")
      .then((result) => {
        const map: Record<string, ToolRow> = {};
        [...result.builtin, ...result.mcp].forEach((tool) => (map[tool.name] = tool));
        toolsRef.current = map;
      });
  }, [skill]);

  const loadHistory = useCallback(() => {
    api
      .get<{ runs: AgentRunRow[] }>(`/agent/runs${qs({ space_id: spaceId, limit: 15 })}`)
      .then((result) => setHistory(result.runs))
      .catch(() => setHistory([]));
  }, [spaceId]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [trace.length]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const run = async () => {
    if (running) return;
    setRunning(true);
    setTrace([]);
    setFinal(null);
    setError("");
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      for await (const event of streamSSE(
        `/agent/stream${qs({ goal, space_id: spaceId, skill, max_steps: maxSteps })}`,
        controller.signal,
      )) {
        handle(event as StreamEventPayload);
      }
    } catch (exc) {
      setError(String(exc));
    } finally {
      setRunning(false);
      abortRef.current = null;
      loadHistory();
      refresh();
    }
  };

  const handle = (event: StreamEventPayload) => {
    switch (event.type) {
      case "run_start":
        setMeta({ tools: event.tools ?? [], memory_hits: event.memory_hits ?? [], run_id: event.run_id ?? null });
        break;
      case "hint":
        setTrace((prev) => [...prev, { ordinal: -1, thought: String(event.message ?? ""), action: "hint", action_input: {}, observation: "", ok: true, repaired: "", chips: ["提示"] }]);
        break;
      case "step": {
        const risk = toolsRef.current[String(event.action)]?.risk ?? "read";
        setTrace((prev) => [
          ...prev,
          {
            ordinal: Number(event.ordinal),
            thought: String(event.thought ?? ""),
            action: String(event.action),
            action_input: (event.action_input as Record<string, unknown>) ?? {},
            observation: "",
            ok: true,
            repaired: String(event.repaired ?? ""),
            chips: [`剩余 ${event.remaining} 步`, risk],
          },
        ]);
        break;
      }
      case "observation":
        setTrace((prev) => {
          const index = prev.findIndex((item) => item.ordinal === event.ordinal);
          if (index < 0) return prev;
          const copy = [...prev];
          copy[index] = { ...copy[index], observation: String(event.observation ?? ""), ok: Boolean(event.ok) };
          return copy;
        });
        break;
      case "proposal":
        setTrace((prev) => {
          const index = prev.findIndex((item) => item.ordinal === event.ordinal);
          const patch = { proposal: { proposal_id: Number(event.proposal_id), summary: String(event.summary ?? "") } };
          if (index < 0) return prev;
          const copy = [...prev];
          copy[index] = { ...copy[index], ...patch };
          return copy;
        });
        break;
      case "final":
        setFinal({
          answer: String(event.answer ?? ""),
          status: String(event.status ?? ""),
          steps: Number(event.steps ?? 0),
          tokens: Number(event.tokens ?? 0),
          elapsed_ms: Number(event.elapsed_ms ?? 0),
          proposals: (event.proposals as { id: number; title: string }[]) ?? [],
          memory_item_id: (event.memory_item_id as number | null) ?? null,
        });
        break;
      case "error":
        setError(String(event.message ?? ""));
        break;
      default:
        break;
    }
  };

  const replay = async (runId: number) => {
    const detail = await api.get<{ goal: string; status: string; steps: AgentStepRow[]; final_answer: string }>(
      `/agent/runs/${runId}`,
    );
    setGoal(detail.goal);
    setFinal({ answer: detail.final_answer, status: detail.status, steps: detail.steps.length, tokens: 0, elapsed_ms: 0, proposals: [], memory_item_id: null });
    setTrace(
      detail.steps.map((step) => ({
        ordinal: step.ordinal,
        thought: step.thought,
        action: step.action,
        action_input: step.action_input,
        observation: step.observation,
        ok: step.status !== "tool_error" && step.status !== "invalid",
        repaired: "",
        chips: [step.status, `${step.latency_ms}ms`, toolsRef.current[step.action]?.risk ?? "read"],
      })),
    );
    setError("");
  };

  const activeSkill = skills.find((row) => row.name === skill);

  return (
    <div className="split">
      <div>
        <div className="card">
          <h3>目标驱动推理</h3>
          <div className="col">
            <textarea rows={2} value={goal} onChange={(event) => setGoal(event.target.value)} />
            <div className="row">
              <select value={skill} onChange={(event) => setSkill(event.target.value)} style={{ maxWidth: 220 }}>
                {skills.map((row) => (
                  <option key={row.name} value={row.name}>{row.display}</option>
                ))}
              </select>
              <label className="muted">
                步数上限
                <input type="number" min={1} max={20} value={maxSteps} style={{ width: 70 }} onChange={(event) => setMaxSteps(Number(event.target.value))} />
              </label>
              <button className="primary" onClick={run} disabled={running}>{running ? "推理中…" : "运行 ReAct"}</button>
              {running && <button className="mini danger" onClick={() => abortRef.current?.abort()}>中断</button>}
            </div>
            {activeSkill && (
              <>
                <div className="muted">{activeSkill.description}</div>
                <div>
                  {activeSkill.tools.map((tool) => (
                    <span key={tool} className={`chip ${toolsRef.current[tool]?.risk ?? "read"}`}>{tool}</span>
                  ))}
                </div>
                {activeSkill.example && (
                  <button className="mini ghost" onClick={() => setGoal(activeSkill.example)}>用示例目标</button>
                )}
              </>
            )}
          </div>
        </div>

        {meta.memory_hits.length > 0 && (
          <div className="card">
            <h3>本轮注入的长期记忆</h3>
            {meta.memory_hits.map((hit, index) => (
              <div key={index} className="muted">· {hit}</div>
            ))}
          </div>
        )}

        <div className="card">
          <h3>推理轨迹（{trace.length} 步）</h3>
          {trace.map((item, index) => (
            <div key={`${item.ordinal}-${index}`} className="step">
              <div className="head">
                <b>#{item.ordinal}</b>
                <span className="action">{item.action}</span>
                {item.chips.map((chip) => (
                  <span key={chip} className={`chip ${chip}`}>{chip}</span>
                ))}
                {item.repaired && <span className="chip write">已修复：{item.repaired}</span>}
              </div>
              {item.thought && <div className="thought">{item.thought}</div>}
              <pre className="trace" style={{ margin: "4px 0" }}>{JSON.stringify(item.action_input, null, 2)}</pre>
              {item.observation && <div className={`obs${item.ok ? "" : " error"}`}>{item.observation}</div>}
              {item.proposal && <div className="ok-text">{item.proposal.summary}</div>}
            </div>
          ))}
          {trace.length === 0 && !running && <div className="empty">点「运行 ReAct」开始。</div>}
          {error && <div className="error">{error}</div>}
          <div ref={bottomRef} />
        </div>

        {final && (
          <div className="card">
            <h3>结论（{final.status}）</h3>
            <div className="answer">{final.answer}</div>
            <div className="muted" style={{ marginTop: 8 }}>
              {final.steps} 步 · {final.tokens} tokens · {final.elapsed_ms}ms · 提案 {final.proposals.length} 个
              {final.memory_item_id ? ` · 已沉淀记忆 #${final.memory_item_id}` : ""}
            </div>
            {final.proposals.map((proposal) => (
              <div key={proposal.id} className="muted">#{proposal.id} {proposal.title}</div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="card">
          <h3>运行历史（可回放）</h3>
          <div className="scroll-y">
            {history.map((row) => (
              <div key={row.id} className="list-item" onClick={() => replay(row.id)}>
                <span className="name">#{row.id} {row.goal}</span>
                <span className="muted">
                  <span className={`chip ${row.status}`}>{row.status}</span> {row.steps_used} 步 · {row.created_at.slice(0, 19)}
                </span>
              </div>
            ))}
            {history.length === 0 && <div className="empty">暂无运行记录。</div>}
          </div>
        </div>
        <div className="card">
          <h3>本轮可见工具（{meta.tools.length}）</h3>
          <div>
            {meta.tools.map((tool) => (
              <span key={tool} className={`chip ${toolsRef.current[tool]?.risk ?? "read"}`}>{tool}</span>
            ))}
            {meta.tools.length === 0 && <div className="empty">运行后会显示实际工具面。</div>}
          </div>
          <div className="muted" style={{ marginTop: 8 }}>风险分级：read 只读 / write 产生提案 / exec 子进程 / network 外部请求</div>
        </div>
      </div>
    </div>
  );
}

function MemoryPanel() {
  const [rows, setRows] = useState<MemoryRow[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("fact");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<MemoryRow[]>([]);
  const [message, setMessage] = useState("");

  const load = useCallback(() => {
    api
      .get<{ items: MemoryRow[]; stats: Record<string, number> }>("/agent/memory")
      .then((result) => {
        setRows(result.items);
        setStats(result.stats);
      })
      .catch((exc) => setMessage(String(exc)));
  }, []);

  useEffect(load, [load]);

  const add = async () => {
    if (!content.trim()) return;
    await api.post("/agent/memory", { content, kind });
    setContent("");
    setMessage("已写入长期记忆（下次推理会自动召回）。");
    load();
  };

  const recall = async () => {
    const result = await api.post<{ hits: MemoryRow[] }>("/agent/memory/recall", { query, top_k: 6 });
    setHits(result.hits);
  };

  return (
    <div className="split">
      <div className="card">
        <h3>写入记忆</h3>
        <textarea rows={3} value={content} onChange={(event) => setContent(event.target.value)} placeholder="例如：监管制度类文档权威度应设为 0.85" />
        <div className="row" style={{ marginTop: 8 }}>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            {["fact", "pattern", "preference", "decision", "glossary"].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <button className="primary" onClick={add}>记住</button>
          <span className="grow" />
          {Object.entries(stats).map(([key, value]) => (
            <span key={key} className="chip">{key}: {value}</span>
          ))}
        </div>
        {message && <div className="ok-text">{message}</div>}
        <div className="hr" />
        <div className="row">
          <input className="grow" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="召回测试：文档权威度" />
          <button onClick={recall}>召回</button>
          <button
            onClick={async () => {
              await api.post(`/agent/memory/decay${qs({ half_life_days: 45 })}`);
              load();
            }}
          >
            执行衰减
          </button>
        </div>
        {hits.length > 0 && (
          <>
            <div className="title-sm">召回结果</div>
            {hits.map((hit) => (
              <div key={hit.id} className="step">
                <div className="head">
                  <b>#{hit.id}</b>
                  <span className="chip">{hit.kind}</span>
                  <span className="chip">显著度 {Number(hit.salience).toFixed(2)}</span>
                </div>
                <div>{hit.content}</div>
              </div>
            ))}
          </>
        )}
      </div>
      <div className="card">
        <h3>记忆库（{rows.length}）</h3>
        <div className="scroll-y">
          {rows.map((row) => (
            <div key={row.id} className="step">
              <div className="head">
                <b>#{row.id}</b>
                <span className="chip">{row.kind}</span>
                <span className="chip">{row.scope}</span>
                <span className="chip">显著 {row.salience.toFixed(2)}</span>
                <span className="chip">用 {row.use_count} 次</span>
                <button className="mini ghost" onClick={async () => { await api.del(`/agent/memory/${row.id}`); load(); }}>删除</button>
              </div>
              <div>{row.content}</div>
              {row.source_run_id && <div className="muted mono">来源运行 #{row.source_run_id}</div>}
            </div>
          ))}
          {rows.length === 0 && <div className="empty">记忆库为空。</div>}
        </div>
      </div>
    </div>
  );
}

function McpPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [call, setCall] = useState({ server: "", tool: "", arguments: "{}" });
  const [output, setOutput] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api.get<McpServer[]>("/agent/mcp/servers").then(setServers).catch((exc) => setError(String(exc)));
  }, []);
  useEffect(load, [load]);

  const invoke = async () => {
    setError("");
    try {
      const result = await api.post("/agent/mcp/call", {
        server: call.server,
        tool: call.tool,
        arguments: JSON.parse(call.arguments || "{}"),
      });
      setOutput(JSON.stringify(result, null, 2));
    } catch (exc) {
      setError(String(exc));
    }
  };

  const addServer = async () => {
    const name = window.prompt("新 MCP 服务名称", "finance-calc");
    if (!name) return;
    const transport = window.prompt("传输方式（inproc / stdio / http）", "stdio");
    const command = transport === "stdio" ? window.prompt("启动命令", "python -m mcp_server_example") ?? "" : "";
    const url = transport === "http" ? window.prompt("服务地址", "http://127.0.0.1:9000/mcp") ?? "" : "";
    await api.post("/agent/mcp/servers", { name, transport, command, url });
    load();
  };

  return (
    <div className="split">
      <div className="card">
        <h3>已注册 MCP 服务（{servers.length}）</h3>
        {servers.map((server) => (
          <div key={server.name} className="card" style={{ background: "var(--panel-2)" }}>
            <div className="row">
              <b>{server.display || server.name}</b>
              <span className="chip">{server.transport}</span>
              <span className={`chip ${server.enabled ? "accepted" : "rejected"}`}>{server.enabled ? "已启用" : "已停用"}</span>
              <span className="grow" />
              <button
                className="mini ghost"
                onClick={async () => {
                  await api.post(`/agent/mcp/servers/${server.name}/enabled${qs({ enabled: !server.enabled })}`);
                  load();
                }}
              >
                {server.enabled ? "停用" : "启用"}
              </button>
              <button
                className="mini ghost"
                onClick={async () => {
                  await api.del(`/agent/mcp/servers/${server.name}`);
                  load();
                }}
              >
                移除
              </button>
            </div>
            <div className="muted">{server.description}</div>
            {server.tools.map((tool) => (
              <div key={tool.qualified} className="row" style={{ marginTop: 4 }}>
                <button className="mini ghost mono" onClick={() => setCall({ server: server.name, tool: tool.name, arguments: "{}" })}>
                  {tool.qualified}
                </button>
                <span className="muted">{tool.description}</span>
              </div>
            ))}
          </div>
        ))}
        <button onClick={addServer}>添加外部 MCP 服务</button>
      </div>
      <div className="card">
        <h3>调用</h3>
        <div className="col">
          <input value={call.server} onChange={(event) => setCall({ ...call, server: event.target.value })} placeholder="server" />
          <input value={call.tool} onChange={(event) => setCall({ ...call, tool: event.target.value })} placeholder="tool" />
          <textarea value={call.arguments} onChange={(event) => setCall({ ...call, arguments: event.target.value })} placeholder='{"a": 1}' />
          <button className="primary" onClick={invoke}>执行</button>
          {error && <div className="error">{error}</div>}
          {output && <pre className="trace">{output}</pre>}
        </div>
      </div>
    </div>
  );
}

function SandboxPanel() {
  const [code, setCode] = useState(
    "import json, statistics\nrates = [0.0435, 0.0465, 0.049]\nprint(json.dumps({'mean': statistics.fmean(rates), 'max': max(rates)}, ensure_ascii=False))",
  );
  const [output, setOutput] = useState("");
  const [config, setConfig] = useState<SandboxConfig | null>(null);

  useEffect(() => {
    api.get<SandboxConfig>("/agent/sandbox/config").then(setConfig).catch(() => setConfig(null));
  }, []);

  const run = async () => {
    const result = await api.post<SandboxRun>("/agent/sandbox",
      { code },
    );
    setOutput(
      [
        result.ok ? "✓ 成功" : `✗ 失败 (exit ${result.exit_code})`,
        `耗时 ${result.elapsed_ms}ms`,
        result.violations.length ? `违规：${result.violations.join("；")}` : "",
        result.stdout,
        result.stderr,
      ]
        .filter(Boolean)
        .join("\n"),
    );
  };

  return (
    <div className="split">
      <div className="card">
        <h3>受限 Python 执行（AST 白名单 + 子进程隔离 + 超时）</h3>
        <textarea rows={12} value={code} className="mono" onChange={(event) => setCode(event.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" onClick={run}>运行</button>
          <button
            onClick={async () => {
              const result = await api.post<{ ok: boolean; violations: string[] }>("/agent/sandbox/validate", { code });
              setOutput(result.ok ? "静态校验通过" : `拦截：${result.violations.join("；")}`);
            }}
          >
            仅静态校验
          </button>
          {config && (
            <span className="muted">
              {config.enabled ? `超时 ${config.timeout_s}s` : "已在配置中禁用"}
            </span>
          )}
        </div>
        {config && (
          <div style={{ marginTop: 8 }}>
            {config.allowed_imports.map((item) => (
              <span key={item} className="chip read">{item}</span>
            ))}
          </div>
        )}
      </div>
      <div className="card">
        <h3>输出</h3>
        <pre className="trace">{output || "（尚无输出）"}</pre>
      </div>
    </div>
  );
}
