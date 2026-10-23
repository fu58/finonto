import { useCallback, useEffect, useRef, useState } from "react";
import { api, qs, streamSSE } from "../api";
import { useApp } from "../App";
import type { Candidate, Citation, Proposal, Understood, Vocab } from "../types";

type Turn = {
  question: string;
  answer: string;
  understood: Understood | null;
  candidates: Candidate[];
  citations: Citation[];
  strategy: string;
  coverage: Record<string, number>;
  answerId: string;
  provider: string;
  running: boolean;
  error: string;
};

// 金融空间的起手问题；其它领域由各自领域包的 sample_questions 覆盖
const SUGGESTIONS = [
  "互联网贷款业务中，单户在银行的信用贷款总额度上限是多少？",
  "联合贷款的风险分担有什么监管要求？",
  "逾期多少天必须计入不良资产？",
  "反洗钱客户尽职调查需要留存哪些资料？",
];

/** 把回答中的 [cN] 标记渲染成可点击引用。 */
function renderAnswer(text: string, onJump: (ref: string) => void) {
  const parts = text.split(/(\[c\d+\])/g);
  return parts.map((part, index) => {
    const match = /^\[(c\d+)\]$/.exec(part);
    if (match) {
      return (
        <span key={index} className="ref" onClick={() => onJump(match[1])}>
          [{match[1]}]
        </span>
      );
    }
    return <span key={index}>{part}</span>;
  });
}

export default function ChatView() {
  const { spaceId, refresh } = useApp();
  const [question, setQuestion] = useState(SUGGESTIONS[0]);
  const [suggestions, setSuggestions] = useState<string[]>(SUGGESTIONS);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);
  useEffect(() => {
    let alive = true;
    api
      .get<Vocab>(`/ontology/vocab${qs({ space_id: spaceId })}`)
      .then((vocab) => {
        if (!alive || !vocab.sample_questions?.length) return;
        setSuggestions(vocab.sample_questions);
        setQuestion((current) => (SUGGESTIONS.includes(current) ? vocab.sample_questions[0] : current));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [spaceId]);

  const patchTurn = (index: number, patch: Partial<Turn>) =>
    setTurns((prev) => prev.map((turn, i) => (i === index ? { ...turn, ...patch } : turn)));

  const ask = useCallback(async () => {
    const text = question.trim();
    if (!text || busy) return;
    setBusy(true);
    const turnIndex = turns.length;
    setTurns((prev) => [
      ...prev,
      {
        question: text,
        answer: "",
        understood: null,
        candidates: [],
        citations: [],
        strategy: "",
        coverage: {},
        answerId: "",
        provider: "",
        running: true,
        error: "",
      },
    ]);
    const controller = new AbortController();
    abortRef.current = controller;
    let answer = "";
    try {
      for await (const event of streamSSE(`/rag/stream${qs({ question: text, space_id: spaceId })}`, controller.signal)) {
        const type = String(event.type);
        if (type === "stage" && event.stage === "understood") {
          patchTurn(turnIndex, { understood: event.understood as Understood, answerId: String(event.answer_id ?? "") });
        } else if (type === "stage" && event.stage === "retrieved") {
          patchTurn(turnIndex, {
            candidates: (event.candidates as Candidate[]) ?? [],
            strategy: String(event.strategy ?? ""),
          });
        } else if (type === "token") {
          answer += String(event.text ?? "");
          patchTurn(turnIndex, { answer });
        } else if (type === "citations") {
          patchTurn(turnIndex, { citations: (event.citations as Citation[]) ?? [] });
        } else if (type === "done") {
          patchTurn(turnIndex, {
            answer: String(event.answer ?? answer) || answer,
            coverage: (event.coverage as Record<string, number>) ?? {},
            provider: String(event.provider ?? ""),
            running: false,
          });
        } else if (type === "error") {
          patchTurn(turnIndex, { error: String(event.message ?? "未知错误"), running: false });
        }
      }
    } catch (exc) {
      patchTurn(turnIndex, { error: String(exc), running: false });
    } finally {
      patchTurn(turnIndex, { running: false });
      setBusy(false);
      abortRef.current = null;
    }
  }, [busy, question, spaceId, turns.length]);

  const [draft, setDraft] = useState<{ candidates: Record<string, unknown>[]; question: string } | null>(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [draftError, setDraftError] = useState("");

  const toDraft = async (text: string) => {
    setDraftBusy(true);
    setDraftError("");
    try {
      const result = await api.post<{ draft: { object_types: Record<string, unknown>[] } }>(
        `/ontology/${spaceId}/draft${qs({ })}`,
        { question: text, space_id: spaceId },
      );
      setDraft({ candidates: result.draft.object_types ?? [], question: text });
    } catch (exc) {
      setDraftError(String(exc));
    } finally {
      setDraftBusy(false);
    }
  };

  const commitDraft = async () => {
    if (!draft) return;
    try {
      const created = await api.post<{ proposals: Proposal[] }>(`/ontology/${spaceId}/draft/propose`, {
        question: draft.question,
        space_id: spaceId,
      });
      setDraft({ ...draft, candidates: [] });
      setDraftError(`已提交 ${created.proposals.length} 个提案，等待人工确认`);
      refresh();
    } catch (exc) {
      setDraftError(String(exc));
    }
  };

  return (
    <div className="split">
      <div>
        <div className="card">
          <h3>查询理解 → 混合检索 → 重排 → 流式生成 → 引用</h3>
          <div className="row">
            <textarea
              className="grow"
              value={question}
              rows={2}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) ask();
              }}
              placeholder="就本地资料提问，Ctrl+Enter 发送"
            />
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" onClick={ask} disabled={busy}>
              {busy ? "推理中…" : "提问"}
            </button>
            <button className="ghost" onClick={() => setTurns([])} disabled={busy || !turns.length}>
              清空
            </button>
            <span className="grow" />
            {suggestions.map((item) => (
              <button key={item} className="mini ghost" onClick={() => setQuestion(item)} disabled={busy}>
                {item.slice(0, 12)}…
              </button>
            ))}
          </div>
        </div>

        {!turns.length && <div className="empty">还没有提问。左侧输入问题，或直接点上面的示例。</div>}

        {turns.map((turn, index) => (
          <div className="card" key={index}>
            <h2>{turn.question}</h2>
            {turn.understood && (
              <div className="row" style={{ marginBottom: 8 }}>
                <span className="chip">{turn.understood.intent}</span>
                <span className="chip">{turn.understood.answer_shape}</span>
                {turn.understood.expansions.slice(0, 4).map((item) => (
                  <span key={item} className="chip">{item}</span>
                ))}
                {turn.understood.matched_types?.slice(0, 3).map((item) => (
                  <span key={item} className="chip write">已有类型：{item}</span>
                ))}
              </div>
            )}
            <div className="answer">
              {turn.answer ? renderAnswer(turn.answer, (ref) => {
                const target = document.getElementById(`cite-${ref}`);
                target?.scrollIntoView({ behavior: "smooth", block: "center" });
                target?.classList.add("hit");
              }) : turn.running ? <span className="muted">正在生成…</span> : <span className="muted">（无内容）</span>}
            </div>
            {turn.error && <div className="error">{turn.error}</div>}
            <div className="row muted" style={{ marginTop: 10, justifyContent: "space-between" }}>
              <span>
                {turn.strategy && <span className="chip read">{turn.strategy}</span>}
                候选 {turn.candidates.length} · 引用 {turn.citations.length} · 覆盖度{" "}
                {Math.round((turn.coverage?.coverage ?? 0) * 100)}% · {turn.provider}
              </span>
              <button className="mini ghost" onClick={() => toDraft(turn.question)} disabled={draftBusy}>
                由此题生成本体草案 →
              </button>
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div>
        {draft && (
          <div className="card">
            <h3>本体草案（未写入，需确认）</h3>
            <div className="muted" style={{ marginBottom: 8 }}>来源问题：{draft.question}</div>
            {draft.candidates.length === 0 && <div className="empty">草案已提交或没有新候选。</div>}
            {draft.candidates.map((item, index) => (
              <div key={index} style={{ marginBottom: 8 }}>
                <b>{String(item.label)}</b>
                <div className="muted">{String(item.definition ?? "")}</div>
              </div>
            ))}
            {draft.candidates.length > 0 && (
              <button className="primary" onClick={commitDraft}>提交为待确认提案</button>
            )}
            {draftError && <div className="ok-text">{draftError}</div>}
          </div>
        )}

        <div className="card">
          <h3>引用来源</h3>
          {turns.length === 0 || turns[turns.length - 1].citations.length === 0 ? (
            <div className="empty">暂无引用。</div>
          ) : (
            turns[turns.length - 1].citations.map((citation) => (
              <div key={citation.ref} id={`cite-${citation.ref}`} className="step" style={{ marginBottom: 6 }}>
                <div className="head">
                  <b className="ref">[{citation.ref}]</b>
                  <span>{citation.doc_name}</span>
                  <span className="chip">{citation.doc_type}</span>
                  <span className="chip">{citation.strength}</span>
                </div>
                <div className="muted mono">{citation.locator}</div>
                <div className="obs">{citation.quote}</div>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <h3>召回轨迹（{turns.length ? turns[turns.length - 1].candidates.length : 0} 候选）</h3>
          {turns.length === 0 ? (
            <div className="empty">提问后这里会显示 BM25 / 向量 / RRF / 重排的全过程。</div>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>ref</th>
                  <th>片段</th>
                  <th className="right">BM25</th>
                  <th className="right">向量</th>
                  <th className="right">融合</th>
                  <th>命中理由</th>
                </tr>
              </thead>
              <tbody>
                {turns[turns.length - 1].candidates.map((candidate) => (
                  <tr key={candidate.chunk_id}>
                    <td className="mono">{candidate.ref}</td>
                    <td>
                      <div className="muted">{candidate.doc_name} · {candidate.locator}</div>
                      <div style={{ maxWidth: 320 }}>{candidate.preview}</div>
                    </td>
                    <td className="right mono">{candidate.lexical_rank ?? "-"}</td>
                    <td className="right mono">{candidate.dense_rank ?? "-"}</td>
                    <td className="right mono">{candidate.final_score.toFixed(4)}</td>
                    <td className="muted">{candidate.reasons.join("；") || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <HistoryPanel spaceId={spaceId} onPick={(text) => setQuestion(text)} />
      </div>
    </div>
  );
}

function HistoryPanel({ spaceId, onPick }: { spaceId: number; onPick: (question: string) => void }) {
  const [traces, setTraces] = useState<{ id: number; answer_id: string; question: string; coverage: number; citation_count: number; elapsed_ms: number }[]>([]);

  useEffect(() => {
    api
      .get<{ traces: typeof traces }>(`/rag/traces${qs({ space_id: spaceId, limit: 12 })}`)
      .then((result) => setTraces(result.traces))
      .catch(() => setTraces([]));
  }, [spaceId]);

  return (
    <div className="card">
      <h3>历史问答轨迹</h3>
      {traces.length === 0 && <div className="empty">还没有记录。</div>}
      {traces.map((trace) => (
        <div key={trace.id} className="list-item" onClick={() => onPick(trace.question)}>
          <span className="name">{trace.question.slice(0, 46)}</span>
          <span className="muted mono">
            {trace.answer_id} · 覆盖 {Math.round((trace.coverage ?? 0) * 100)}% · 引用 {trace.citation_count} · {trace.elapsed_ms}ms
          </span>
        </div>
      ))}
    </div>
  );
}
