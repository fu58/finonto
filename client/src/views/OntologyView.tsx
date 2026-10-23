import { useCallback, useEffect, useMemo, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import type { ObjectTypeRow, TypeDetail, Vocab } from "../types";

const EMPTY_FORM = { label: "", label_en: "", definition: "", kind: "class", business_domain: "", confidence: 0.7 };

export default function OntologyView() {
  const { spaceId, refresh } = useApp();
  const [rows, setRows] = useState<ObjectTypeRow[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<TypeDetail | null>(null);
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [issues, setIssues] = useState<{ code: string; level: string; message: string }[]>([]);
  const [validateIssues, setValidateIssues] = useState<{ code: string; level: string; message: string; subject: string }[]>([]);

  const load = useCallback(async () => {
    setError("");
    try {
      const result = await api.get<{ types: ObjectTypeRow[]; total: number }>(
        `/ontology/${spaceId}/types${qs({ q: query, status: statusFilter, limit: 500 })}`,
      );
      setRows(result.types);
      setVocab(await api.get<Vocab>(`/ontology/vocab${qs({ space_id: spaceId })}`));
      if (!result.types.some((row) => row.id === selected)) {
        setSelected(result.types[0]?.id ?? null);
      }
    } catch (exc) {
      setError(String(exc));
    }
  }, [query, spaceId, statusFilter, selected]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (selected == null) {
      setDetail(null);
      return;
    }
    api
      .get<TypeDetail>(`/ontology/types/${selected}`)
      .then(setDetail)
      .catch((exc) => setError(String(exc)));
  }, [selected]);

  const domainOptions = useMemo(() => vocab?.business_domains ?? [], [vocab]);

  const submitCreate = async () => {
    setIssues([]);
    try {
      const created = await api.post<{ type: TypeDetail; issues: typeof issues }>(`/ontology/${spaceId}/types`, {
        ...form,
        space_id: spaceId,
      });
      setIssues(created.issues);
      if (!created.issues.some((issue) => issue.level === "violation")) {
        setSelected(created.type.id);
        setShowCreate(false);
        setForm({ ...EMPTY_FORM });
        await load();
        refresh();
      }
    } catch (exc) {
      setIssues([{ code: "http_error", level: "violation", message: String(exc) }]);
    }
  };

  const runValidate = async () => {
    const result = await api.get<{ issues: typeof validateIssues }>(`/ontology/${spaceId}/validate`);
    setValidateIssues(result.issues);
  };

  const removeType = async () => {
    if (!detail || !window.confirm(`确认删除对象类型「${detail.label}」？其关联与页面会一并失效。`)) return;
    await api.del(`/ontology/types/${detail.id}`);
    setSelected(null);
    await load();
    refresh();
  };

  const patchDefinition = async (definition: string) => {
    if (!detail) return;
    const result = await api.patch<{ type: TypeDetail }>(`/ontology/types/${detail.id}`, { definition });
    setDetail(result.type);
    await load();
  };

  return (
    <div className="split">
      <div>
        <div className="card">
          <h3>对象类型（{rows.length}）</h3>
          {vocab && (
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="chip">{vocab.pack.label}</span>
              <span className="muted">{vocab.pack.summary}</span>
            </div>
          )}
          <div className="row">
            <input
              className="grow"
              placeholder="搜索名称 / 定义 / 别名"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
              <option value="">全部状态</option>
              <option value="active">active</option>
              <option value="draft">draft</option>
              <option value="deprecated">deprecated</option>
            </select>
            <button className="primary" onClick={() => setShowCreate((value) => !value)}>
              新建类型
            </button>
            <button onClick={runValidate}>空间校验</button>
          </div>
          {showCreate && (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="col">
                <div className="row">
                  <input className="grow" placeholder="中文名称（必填）" value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} />
                  <input className="grow" placeholder="英文名称" value={form.label_en} onChange={(event) => setForm({ ...form, label_en: event.target.value })} />
                </div>
                <textarea placeholder="定义（建议含业务口径与监管依据）" value={form.definition} onChange={(event) => setForm({ ...form, definition: event.target.value })} />
                <div className="row">
                  <select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}>
                    {Object.entries(vocab?.object_kinds ?? {}).map(([key, label]) => (
                      <option key={key} value={key}>{key} — {label}</option>
                    ))}
                  </select>
                  <select value={form.business_domain} onChange={(event) => setForm({ ...form, business_domain: event.target.value })}>
                    <option value="">业务域（可选）</option>
                    {domainOptions.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </select>
                  <label className="muted">
                    置信度
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={form.confidence}
                      style={{ width: 80 }}
                      onChange={(event) => setForm({ ...form, confidence: Number(event.target.value) })}
                    />
                  </label>
                  <button className="ok" onClick={submitCreate}>提交</button>
                </div>
                {issues.map((issue, index) => (
                  <div key={index} className={issue.level === "violation" ? "error" : "muted"}>
                    [{issue.level}] {issue.code}：{issue.message}
                  </div>
                ))}
              </div>
            </div>
          )}
          {validateIssues.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="title-sm">校验结果（{validateIssues.length}）</div>
              <div className="scroll-y">
                {validateIssues.map((issue, index) => (
                  <div key={index} className={issue.level === "violation" ? "error" : "muted"}>
                    [{issue.level}] {issue.subject} · {issue.code}：{issue.message}
                  </div>
                ))}
              </div>
            </div>
          )}
          {error && <div className="error">{error}</div>}
        </div>

        <div className="card">
          <div className="scroll-y">
            {rows.map((row) => (
              <div key={row.id} className={`list-item${row.id === selected ? " active" : ""}`} onClick={() => setSelected(row.id)}>
                <span className="name">
                  {row.label} <span className="muted mono">{row.label_en}</span>
                </span>
                <span className="muted">{row.definition.slice(0, 76)}</span>
                <span>
                  <span className="chip">{row.kind}</span>
                  <span className={`chip ${row.status}`}>{row.status}</span>
                  <span className="chip">{row.origin}</span>
                  <span className="chip">v{row.version_no}</span>
                </span>
              </div>
            ))}
            {rows.length === 0 && <div className="empty">没有匹配的对象类型。</div>}
          </div>
        </div>
      </div>

      <div>
        {!detail && <div className="empty">选择左侧类型查看详情与证据链。</div>}
        {detail && (
          <>
            <div className="card">
              <h2>{detail.label}</h2>
              <div className="muted mono">{detail.iri}</div>
              <div className="row" style={{ margin: "8px 0" }}>
                <span className="chip">{detail.kind}</span>
                <span className={`chip ${detail.status}`}>{detail.status}</span>
                <span className="chip">{detail.business_domain || "未分域"}</span>
                <span className="chip">置信度 {detail.confidence}</span>
                {detail.abstract && <span className="chip">抽象类</span>}
                {detail.parent && <span className="chip">父类：{detail.parent.label}</span>}
                {detail.tags.map((tag) => (
                  <span key={tag} className="chip">#{tag}</span>
                ))}
              </div>
              <DefinitionEditor value={detail.definition} onSave={patchDefinition} />
              {detail.aliases.length > 0 && <div className="muted">别名：{detail.aliases.join("、")}</div>}
              <div className="row" style={{ marginTop: 10 }}>
                <button className="mini danger" onClick={removeType}>删除类型</button>
              </div>
            </div>

            <div className="card">
              <h3>数据属性（{detail.properties.length}）</h3>
              {detail.properties.length === 0 && <div className="empty">无数据属性。</div>}
              <table className="grid">
                <tbody>
                  {detail.properties.map((property, index) => (
                    <tr key={index}>
                      <td>{property.label}</td>
                      <td className="mono muted">{property.datatype}{property.unit ? ` · ${property.unit}` : ""}</td>
                      <td>
                        {property.required && <span className="chip write">必填</span>}
                        {property.enum.length > 0 && <span className="chip">枚举：{property.enum.join("/")}</span>}
                      </td>
                      <td className="muted">{property.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card">
              <h3>关联（{detail.links.length}）</h3>
              {detail.links.length === 0 && <div className="empty">无关联。</div>}
              {detail.links.map((link) => (
                <div key={link.link_id} className="row" style={{ marginBottom: 4 }}>
                  <span className={`chip ${link.category}`}>{link.direction === "out" ? "→" : "←"} {link.relation}</span>
                  <b>{link.target_label}</b>
                  <span className="muted">{link.cardinality} · {link.description}</span>
                </div>
              ))}
            </div>

            <div className="card">
              <h3>约束（{detail.constraints.length}）</h3>
              {detail.constraints.length === 0 && <div className="empty">无 SHACL 约束。</div>}
              {detail.constraints.map((constraint) => (
                <div key={constraint.id} style={{ marginBottom: 6 }}>
                  <b>{constraint.name}</b> <span className={`chip ${constraint.severity}`}>{constraint.severity}</span>
                  {constraint.reg_ref && <span className="chip read">{constraint.reg_ref}</span>}
                  <div className="mono muted">{constraint.expression}</div>
                  <div className="muted">{constraint.description}</div>
                </div>
              ))}
            </div>

            <div className="card">
              <h3>证据链（{detail.evidence.length}）</h3>
              {detail.evidence.length === 0 && <div className="empty">该类型暂无原文证据。</div>}
              {detail.evidence.map((evidence) => (
                <div key={evidence.id} className="step">
                  <div className="head">
                    <span>{evidence.doc_name || (evidence.doc_id ? `文档 #${evidence.doc_id}` : "人工确认记录")}</span>
                    <span className="muted mono">{evidence.locator}</span>
                    {evidence.proposal_id && <span className="chip">提案 #{evidence.proposal_id}</span>}
                    {evidence.field_path && <span className="chip">{evidence.field_path}</span>}
                  </div>
                  <div className="obs">{evidence.quote}</div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DefinitionEditor({ value, onSave }: { value: string; onSave: (next: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  useEffect(() => setDraft(value), [value]);

  if (!editing) {
    return (
      <div
        style={{ margin: "8px 0", cursor: "pointer" }}
        title="点击编辑定义"
        onClick={() => setEditing(true)}
      >
        {value || <span className="muted">（无定义，点击补充）</span>}
      </div>
    );
  }
  return (
    <div className="col" style={{ margin: "8px 0" }}>
      <textarea value={draft} rows={4} onChange={(event) => setDraft(event.target.value)} />
      <div className="row">
        <button
          className="ok mini"
          onClick={async () => {
            await onSave(draft);
            setEditing(false);
          }}
        >
          保存（版本 +1）
        </button>
        <button className="mini ghost" onClick={() => { setDraft(value); setEditing(false); }}>
          取消
        </button>
      </div>
    </div>
  );
}
