import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type {
  ComponentCatalog,
  ComponentGroup,
  ComponentHealth,
  DatasourceDescribe,
  DeployPayload,
  ProbeResult,
  SourceTable,
  WebSearchResult,
} from "../types";

const KIND_LABEL: Record<string, string> = {
  llm: "对话/推理模型",
  embedding: "向量化模型",
  vector_store: "向量数据库",
  object_store: "对象存储",
  doc_parser: "文档格式解析",
  data_source: "数据源",
  web_search: "网络搜索",
  im_channel: "IM 通知渠道",
  rerank: "重排序",
  deploy: "部署目标",
};

const PROBE_KINDS = ["llm", "embedding", "vector_store", "object_store", "doc_parser", "web_search", "im_channel", "rerank"];

function probeText(result: ProbeResult): string {
  const parts = Object.entries(result)
    .filter(([key]) => !["kind", "elapsed_ms", "ok", "error"].includes(key))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : String(value)}`);
  return `${result.ok ? "可用" : "失败"} · ${result.elapsed_ms}ms${parts.length ? ` · ${parts.join(" · ")}` : ""}`;
}

export default function ComponentsView() {
  const [catalog, setCatalog] = useState<ComponentCatalog | null>(null);
  const [health, setHealth] = useState<ComponentHealth | null>(null);
  const [deploy, setDeploy] = useState<DeployPayload | null>(null);
  const [expanded, setExpanded] = useState<string>("llm");
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const [imText, setImText] = useState("提案 #1 已确认写入本体，Wiki 已增量重编译。");
  const [searchQuery, setSearchQuery] = useState("本体 领域建模 规范");
  const [searchResults, setSearchResults] = useState<WebSearchResult[] | null>(null);
  const [dsUri, setDsUri] = useState("");
  const [dsTables, setDsTables] = useState<SourceTable[] | null>(null);
  const [dsAdapter, setDsAdapter] = useState("");
  const [dsError, setDsError] = useState("");

  const load = useCallback(async () => {
    try {
      const [catalogData, healthData, deployData] = await Promise.all([
        api.get<ComponentCatalog>("/components"),
        api.get<ComponentHealth>("/components/health"),
        api.get<DeployPayload>("/components/deploy"),
      ]);
      setCatalog(catalogData);
      setHealth(healthData);
      setDeploy(deployData);
      setError("");
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const activate = async (kind: string, name: string) => {
    setBusy(true);
    try {
      const result = await api.post<{ kind: string; active: string; health: ComponentHealth }>("/components/active", {
        kind,
        name,
      });
      setHealth(result.health);
      setNotice(`${KIND_LABEL[kind] ?? kind} 已切换为 ${result.active}（写入 var/components.json，重启后依然生效）`);
      await load();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const probe = async (kind: string) => {
    setBusy(true);
    setProbes((prev) => ({ ...prev, [kind]: { kind, elapsed_ms: 0, ok: undefined, error: "探测中…" } }));
    try {
      const result = await api.post<ProbeResult>("/components/probe", { kind });
      setProbes((prev) => ({ ...prev, [kind]: result }));
    } catch (exc) {
      setProbes((prev) => ({ ...prev, [kind]: { kind, elapsed_ms: 0, ok: false, error: String(exc).slice(0, 160) } }));
    } finally {
      setBusy(false);
    }
  };

  const sendIm = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ channel: string; result: Record<string, unknown> }>("/components/im/test", {
        text: imText,
        title: "FinOnto 测试通知",
      });
      setNotice(`已通过 ${result.channel} 发送：${JSON.stringify(result.result).slice(0, 160)}`);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const runSearch = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ query: string; results: WebSearchResult[] }>("/components/search", {
        query: searchQuery,
        top_k: 5,
      });
      setSearchResults(result.results);
      setNotice(`网搜返回 ${result.results.length} 条（未配置密钥时为本地占位结果）`);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const describeSource = async () => {
    setBusy(true);
    try {
      const result = await api.post<DatasourceDescribe>("/components/datasource/describe", { uri: dsUri });
      setDsTables(result.tables);
      setDsAdapter(result.adapter);
      setDsError(result.error ?? "");
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const groups: ComponentGroup[] = catalog?.groups ?? [];
  const settings = catalog?.settings ?? {};

  return (
    <div className="split">
      <div style={{ minWidth: 0 }}>
        <div className="card">
          <h3>组件中心：10 类可插拔实现（本地实现零密钥可跑，真实适配器通过 .env 激活）</h3>
          {error && <div className="error">{error}</div>}
          {notice && <div className="ok-text">{notice}</div>}
          <div className="row" style={{ marginTop: 6 }}>
            {PROBE_KINDS.map((kind) => (
              <button key={kind} className="mini ghost" onClick={() => probe(kind)} disabled={busy}>
                探针·{KIND_LABEL[kind]}
              </button>
            ))}
          </div>
          {Object.keys(probes).length > 0 && (
            <table className="grid" style={{ marginTop: 10 }}>
              <tbody>
                {Object.values(probes).map((result) => (
                  <tr key={result.kind}>
                    <td>{KIND_LABEL[result.kind] ?? result.kind}</td>
                    <td className={result.ok === false ? "error" : "ok-text"}>{probeText(result)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {groups.map((group) => (
          <div className="card" key={group.kind}>
            <div className="row" onClick={() => setExpanded(expanded === group.kind ? "" : group.kind)} style={{ cursor: "pointer" }}>
              <h3 className="grow" style={{ margin: 0 }}>
                {group.label}
              </h3>
              <span className="chip read">生效：{group.active}</span>
              <span className="muted">{group.count} 个可选</span>
            </div>
            {expanded === group.kind && (
              <table className="grid" style={{ marginTop: 10 }}>
                <thead>
                  <tr>
                    <th>实现</th>
                    <th>说明</th>
                    <th>配置要求</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {group.items.map((item) => (
                    <tr key={item.name}>
                      <td>
                        <div>{item.display}</div>
                        <div className="mono muted">{item.name}</div>
                        {item.local_fallback && <span className="chip network">本地实现</span>}
                        {!item.local_fallback && item.configured && <span className="chip accepted">密钥已配置</span>}
                        {!item.local_fallback && !item.configured && <span className="chip pending">需要密钥</span>}
                      </td>
                      <td className="muted">{item.description}</td>
                      <td className="mono muted">
                        {Object.entries(item.config_hint).map(([key, value]) => (
                          <div key={key}>
                            {key} = {value}
                          </div>
                        ))}
                        {item.capabilities.length > 0 && <div>能力：{item.capabilities.join(" / ")}</div>}
                      </td>
                      <td className="right">
                        <button
                          className={item.name === group.active ? "mini" : "mini primary"}
                          disabled={busy || item.name === group.active}
                          onClick={() => activate(group.kind, item.name)}
                        >
                          {item.name === group.active ? "当前生效" : "设为生效"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>

      <div>
        <div className="card">
          <h3>当前生效组件</h3>
          {health && (
            <table className="grid">
              <tbody>
                {Object.entries(health).map(([key, value]) => (
                  <tr key={key}>
                    <td>{KIND_LABEL[key] ?? key}</td>
                    <td className="mono">
                      {Object.entries(value)
                        .map(([field, cell]) => `${field}=${String(cell)}`)
                        .join("  ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="title-sm">密钥状态（只显示是否存在，不回传明文）</div>
          <div>
            {Object.entries(settings).map(([key, value]) => (
              <span key={key} className={`chip ${key.endsWith("_present") ? (value ? "accepted" : "") : ""}`}>
                {key}={String(value)}
              </span>
            ))}
          </div>
        </div>

        <div className="card">
          <h3>IM 通知测试</h3>
          <textarea rows={3} value={imText} onChange={(event) => setImText(event.target.value)} />
          <div className="row" style={{ marginTop: 8 }}>
            <button onClick={sendIm} disabled={busy}>
              发送测试消息
            </button>
            <span className="muted">提案确认时会自动复用该渠道。</span>
          </div>
        </div>

        <div className="card">
          <h3>网络搜索测试</h3>
          <div className="row">
            <input className="grow" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} />
            <button onClick={runSearch} disabled={busy}>
              搜索
            </button>
          </div>
          {searchResults && (
            <div className="scroll-y" style={{ marginTop: 8, maxHeight: 220 }}>
              {searchResults.map((result) => (
                <div key={result.url} className="step">
                  <a href={result.url} target="_blank" rel="noreferrer">
                    {result.title || result.url}
                  </a>
                  <div className="muted">{result.snippet}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <h3>数据源探测（按 URI 前缀自动路由 file / sqlite / http / jdbc）</h3>
          <div className="row">
            <input
              className="grow"
              placeholder="例如 sqlite:///./var/finonto.db 或 file:///C:/data/loan.csv"
              value={dsUri}
              onChange={(event) => setDsUri(event.target.value)}
            />
            <button onClick={describeSource} disabled={busy || !dsUri.trim()}>
              列出表结构
            </button>
          </div>
          {dsError && <div className="error">{dsError}</div>}
          {dsTables && dsTables.length > 0 && (
            <>
              <div className="muted" style={{ marginTop: 6 }}>适配器：{dsAdapter}</div>
              {dsTables.map((table) => (
                <div key={table.name} style={{ marginTop: 8 }}>
                  <div className="title-sm">
                    {table.name} · {table.columns.length} 列 · {table.rows.length} 行
                  </div>
                  <table className="grid">
                    <thead>
                      <tr>
                        {table.columns.map((column) => (
                          <th key={column}>{column}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {table.rows.slice(0, 5).map((row, index) => (
                        <tr key={index}>
                          {row.map((cell, cellIndex) => (
                            <td key={cellIndex} className="mono muted">
                              {String(cell ?? "")}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </>
          )}
        </div>

        {deploy && (
          <div className="card">
            <h3>部署目标</h3>
            {deploy.targets.map((target) => (
              <div key={target.name} style={{ marginBottom: 8 }}>
                <b>{target.display}</b> <span className="mono muted">{target.name}</span>
                <div className="muted">{target.description}</div>
              </div>
            ))}
            <div className="hr" />
            <div className="title-sm">当前运行参数</div>
            <pre className="trace">
              {`app_name      = ${deploy.current.app_name}
data_dir      = ${deploy.current.data_dir}
database_url  = ${deploy.current.database_url}
cors_origins  = ${deploy.current.cors_origins.join(", ")}`}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
