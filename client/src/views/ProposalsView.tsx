import { useCallback, useEffect, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import type { Proposal } from "../types";

const KIND_LABEL: Record<string, string> = {
  object_type: "新增对象类型",
  link: "新增关联",
  constraint: "新增约束",
  update: "修订已有类型",
};

const STATUS_LABEL: Record<string, string> = { pending: "待确认", accepted: "已确认", rejected: "已驳回", "": "全部" };

export default function ProposalsView() {
  const { spaceId, refresh } = useApp();
  const [status, setStatus] = useState("pending");
  const [rows, setRows] = useState<Proposal[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [definitionPatch, setDefinitionPatch] = useState("");
  const [result, setResult] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ proposals: Proposal[] }>(
        `/ontology/${spaceId}/proposals${qs({ status, limit: 200 })}`,
      );
      setRows(data.proposals);
      if (!data.proposals.some((row) => row.id === selected)) setSelected(data.proposals[0]?.id ?? null);
      setError("");
    } catch (exc) {
      setError(String(exc));
    }
  }, [selected, spaceId, status]);

  useEffect(() => {
    load();
  }, [load]);

  const current = rows.find((row) => row.id === selected) ?? null;

  useEffect(() => {
    const payload = (current?.payload ?? {}) as Record<string, unknown>;
    setDefinitionPatch(String(payload.definition ?? ""));
    setNote("");
    setResult("");
  }, [current?.id]);

  const act = async (action: "accept" | "reject" | "patch") => {
    if (!current) return;
    setBusy(true);
    setError("");
    try {
      if (action === "patch") {
        await api.patch(`/ontology/proposals/${current.id}`, { definition: definitionPatch });
        setResult("草稿字段已修订，尚未写入本体。");
      } else if (action === "accept") {
        const accepted = await api.post<{
          result: { created_types: number[]; created_links: number[]; dirty_pages: string[]; issues: { code: string; message: string }[] };
          side_effects: { compile: Record<string, number>; notify: unknown };
        }>(`/ontology/proposals/${current.id}/accept`, {
          reviewer: "human",
          note,
          patch: definitionPatch !== String((current.payload as Record<string, unknown>).definition ?? "")
            ? { definition: definitionPatch }
            : null,
          notify: true,
        });
        const created = accepted.result.created_types.length + accepted.result.created_links.length;
        const compiled = accepted.side_effects.compile;
        setResult(
          `已写入本体（新增 ${created} 项）· Wiki 重编译 新建 ${compiled.created} / 更新 ${compiled.updated} / 跳过锁定 ${compiled.skipped_locked} · 已推送 IM 通知`,
        );
      } else {
        await api.post(`/ontology/proposals/${current.id}/reject`, { note, reviewer: "human" });
        setResult("已驳回。");
      }
      await load();
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="split">
      <div>
        <div className="card">
          <h3>提案队列（Agent 与问答只能产生提案，只有这里能写入本体）</h3>
          <div className="row">
            {["pending", "accepted", "rejected", ""].map((value) => (
              <button key={value || "all"} className={status === value ? "primary" : "ghost"} onClick={() => setStatus(value)}>
                {STATUS_LABEL[value]}
              </button>
            ))}
            <span className="grow" />
            <span className="muted">共 {rows.length} 条</span>
          </div>
          {error && <div className="error">{error}</div>}
        </div>
        <div className="card scroll-y">
          {rows.map((row) => (
            <div key={row.id} className={`list-item${row.id === selected ? " active" : ""}`} onClick={() => setSelected(row.id)}>
              <span className="name">
                #{row.id} {row.title || (KIND_LABEL[row.kind] ?? row.kind)}
              </span>
              <span className="muted">{row.rationale.slice(0, 90)}</span>
              <span>
                <span className={`chip ${row.status}`}>{row.status}</span>
                <span className="chip">{KIND_LABEL[row.kind] ?? row.kind}</span>
                <span className="chip">置信 {Number(row.confidence).toFixed(2)}</span>
                {row.run_id && <span className="chip read">Agent 运行 #{row.run_id}</span>}
                <span className="chip">{row.evidence.length} 证据</span>
              </span>
            </div>
          ))}
          {rows.length === 0 && <div className="empty">该状态下没有提案。</div>}
        </div>
      </div>

      <div>
        {!current && <div className="empty">选择左侧提案查看差异与证据。</div>}
        {current && (
          <>
            <div className="card">
              <h2>#{current.id} {KIND_LABEL[current.kind] ?? current.kind}</h2>
              <div className="muted">{current.created_at} · 置信度 {current.confidence} · 状态 <span className={`chip ${current.status}`}>{current.status}</span></div>
              <div className="hr" />
              <div className="title-sm">理由</div>
              <div>{current.rationale || "（无）"}</div>
              <div className="title-sm">载荷</div>
              <pre className="trace">{JSON.stringify(current.payload, null, 2)}</pre>
              {current.duplicates.length > 0 && (
                <>
                  <div className="title-sm">疑似重复（建议先合并而不是新建）</div>
                  {current.duplicates.map((item) => (
                    <span key={item.id} className="chip write">#{item.id} {item.label} · {Number(item.score).toFixed(2)}</span>
                  ))}
                </>
              )}
            </div>

            <div className="card">
              <h3>原文证据（{current.evidence.length}）</h3>
              {current.evidence.length === 0 && <div className="empty">该提案没有绑定原文，建议驳回或补充依据。</div>}
              {current.evidence.map((evidence) => (
                <div key={evidence.id} className="step">
                  <div className="head">
                    <b>{evidence.doc_name || `文档 #${evidence.doc_id}`}</b>
                    <span className="muted mono">{evidence.locator}</span>
                    {evidence.field_path && <span className="chip">{evidence.field_path}</span>}
                    <span className="chip">得分 {Number(evidence.score).toFixed(3)}</span>
                  </div>
                  <div className="obs">{evidence.quote}</div>
                </div>
              ))}
            </div>

            {current.status === "pending" && (
              <div className="card">
                <h3>确认操作</h3>
                <div className="title-sm">定义（可先修订再确认）</div>
                <textarea rows={3} value={definitionPatch} onChange={(event) => setDefinitionPatch(event.target.value)} />
                <div className="row" style={{ marginTop: 8 }}>
                  <button className="mini ghost" onClick={() => act("patch")} disabled={busy}>仅修订草稿</button>
                </div>
                <div className="title-sm">审核备注</div>
                <input className="grow" value={note} onChange={(event) => setNote(event.target.value)} placeholder="写入口径依据、合并意见等" />
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="ok" onClick={() => act("accept")} disabled={busy}>确认写入本体</button>
                  <button className="danger" onClick={() => act("reject")} disabled={busy}>驳回</button>
                </div>
              </div>
            )}
            {result && (
              <div className="card">
                <h3>处理结果</h3>
                <div className="ok-text">{result}</div>
              </div>
            )}
            {current.applied_ref && (
              <div className="card">
                <h3>落地结果</h3>
                <div className="mono">{current.applied_ref}</div>
                {current.review_note && <div className="muted">备注：{current.review_note}</div>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
