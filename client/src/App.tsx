import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, qs } from "./api";
import type { Counts, Space, StatusPayload } from "./types";
import ChatView from "./views/ChatView";
import OntologyView from "./views/OntologyView";
import ProposalsView from "./views/ProposalsView";
import AgentView from "./views/AgentView";
import WikiView from "./views/WikiView";
import GraphView from "./views/GraphView";
import DocumentsView from "./views/DocumentsView";
import ComponentsView from "./views/ComponentsView";
import PacksView from "./views/PacksView";

type Ctx = {
  spaceId: number;
  setSpaceId: (id: number) => void;
  spaces: Space[];
  counts: Counts | null;
  refresh: () => void;
  offline: boolean;
};

const AppContext = createContext<Ctx>({
  spaceId: 1,
  setSpaceId: () => {},
  spaces: [],
  counts: null,
  refresh: () => {},
  offline: true,
});

export const useApp = () => useContext(AppContext);

const TABS = [
  { key: "chat", label: "问答 · RAG", view: ChatView },
  { key: "agent", label: "Agent 推理", view: AgentView },
  { key: "packs", label: "领域包", view: PacksView },
  { key: "ontology", label: "本体工作室", view: OntologyView },
  { key: "proposals", label: "提案审核", view: ProposalsView },
  { key: "wiki", label: "Wiki 与版本", view: WikiView },
  { key: "graph", label: "知识图谱", view: GraphView },
  { key: "documents", label: "文档入库", view: DocumentsView },
  { key: "components", label: "组件中心", view: ComponentsView },
] as const;

export default function App() {
  const [tab, setTab] = useState<string>("chat");
  const [spaceId, setSpaceId] = useState(1);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [offline, setOffline] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [status, spaceList, info] = await Promise.all([
        api.get<StatusPayload>(`/system/status`),
        api.get<{ spaces: Space[] }>(`/ontology/spaces`),
        api.get<{ offline_ready: boolean }>(`/system/info`),
      ]);
      setCounts(status.counts);
      setSpaces(spaceList.spaces);
      setOffline(info.offline_ready);
      if (spaceList.spaces.length && !spaceList.spaces.some((s) => s.id === spaceId)) {
        setSpaceId(spaceList.spaces[0].id);
      }
      setError("");
    } catch (exc) {
      setError(String(exc));
    }
  }, [spaceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const bootstrap = async () => {
    setBusy(true);
    try {
      await api.post(`/system/bootstrap${qs({ load_samples: true })}`);
      await refresh();
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const context = useMemo(
    () => ({ spaceId, setSpaceId, spaces, counts, refresh, offline }),
    [spaceId, spaces, counts, refresh, offline],
  );
  const Active = TABS.find((t) => t.key === tab) ?? TABS[0];

  return (
    <AppContext.Provider value={context}>
      <div className="app">
        <header className="topbar">
          <div className="brand">
            FinOnto <small>多领域本体生成平台 · 领域包（金融 / 维保 / AI / 网络）→ 文档 → 检索 → 推理 → 提案 → 人工确认 → Wiki</small>
          </div>
          <div className="stats">
            {counts && (
              <>
                <span className="stat">本体类型<b>{counts.object_types}</b></span>
                <span className="stat">文档<b>{counts.documents}</b></span>
                <span className="stat">片段<b>{counts.chunks}</b></span>
                <span className="stat">待确认提案<b>{counts.pending_proposals}</b></span>
                <span className="stat">Wiki 页<b>{counts.wiki_pages}</b></span>
                <span className="stat">Agent 运行<b>{counts.agent_runs}</b></span>
                <span className="stat">记忆<b>{counts.memory_items}</b></span>
              </>
            )}
            <span className={`stat ${offline ? "" : "ok-text"}`}>{offline ? "离线模式（无需密钥）" : "已配置远程模型"}</span>
          </div>
        </header>
        <nav className="tabs">
          {TABS.map((item) => (
            <button key={item.key} className={`tab${tab === item.key ? " active" : ""}`} onClick={() => setTab(item.key)}>
              {item.label}
            </button>
          ))}
          <span className="grow" />
          <select
            value={spaceId}
            onChange={(event) => setSpaceId(Number(event.target.value))}
            title="本体空间（每个领域包一个空间）"
            style={{ margin: "4px 8px" }}
          >
            {spaces.map((space) => (
              <option key={space.id} value={space.id}>
                #{space.id} {space.name}
              </option>
            ))}
          </select>
          <button className="mini ghost" onClick={bootstrap} disabled={busy} title="幂等：全部内置领域包的基线本体 + 示例语料 + 首次编译">
            {busy ? "载入中…" : "载入示例数据"}
          </button>
        </nav>
        {error && (
          <div className="error" style={{ padding: "8px 18px", background: "#2a1215" }}>
            {error} —— 请确认后端已在 8000 端口启动：<span className="mono">python -m uvicorn app.main:app --port 8000</span>
          </div>
        )}
        <main className="layout">
          <div className="main">
            <Active.view />
          </div>
        </main>
      </div>
    </AppContext.Provider>
  );
}
