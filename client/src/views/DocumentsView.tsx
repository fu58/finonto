import { useCallback, useEffect, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import type { ChunkRow, DocumentRow, DocumentsPayload, IngestReport } from "../types";

type PreviewChunk = { ordinal: number; locator: string; tokens: number; text: string };

const DOC_TYPES = ["监管制度", "合同模板", "产品说明", "业务手册", "报表数据", "会议纪要", "数据字典", "手工录入"];

export default function DocumentsView() {
  const { spaceId, refresh } = useApp();
  const [rows, setRows] = useState<DocumentRow[]>([]);
  const [formats, setFormats] = useState<string[]>([]);
  const [totalChunks, setTotalChunks] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [meta, setMeta] = useState({ doc_type: "监管制度", issuer: "", jurisdiction: "", effective_date: "", authority: 0.75 });

  const [inlineName, setInlineName] = useState("手工录入条目.md");
  const [inlineText, setInlineText] = useState("");
  const [preview, setPreview] = useState<PreviewChunk[] | null>(null);

  const [openDoc, setOpenDoc] = useState<number | null>(null);
  const [chunks, setChunks] = useState<ChunkRow[]>([]);

  const load = useCallback(async () => {
    try {
      const data = await api.get<DocumentsPayload>(`/documents${qs({ space_id: spaceId, limit: 200 })}`);
      setRows(data.documents);
      setFormats(data.formats);
      setTotalChunks(data.total_chunks);
      setError("");
    } catch (exc) {
      setError(String(exc));
    }
  }, [spaceId]);

  useEffect(() => {
    load();
  }, [load]);

  const openChunks = async (docId: number) => {
    setBusy(true);
    try {
      if (openDoc === docId) {
        setOpenDoc(null);
        setChunks([]);
        return;
      }
      const data = await api.get<{ document: DocumentRow; chunks: ChunkRow[] }>(`/documents/${docId}/chunks${qs({ limit: 120 })}`);
      setOpenDoc(docId);
      setChunks(data.chunks);
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const upload = async () => {
    if (!file) {
      setError("请先选择文件。");
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("space_id", String(spaceId));
      form.append("doc_type", meta.doc_type);
      form.append("issuer", meta.issuer);
      form.append("jurisdiction", meta.jurisdiction);
      form.append("effective_date", meta.effective_date);
      form.append("authority", String(meta.authority));
      const result = await api.upload<{ report: IngestReport; document: DocumentRow | null }>("/documents/upload", form);
      const report = result.report;
      setNotice(
        `已入库 ${report.name}：解析 ${report.doc_format} · ${report.chunks} 块 · ${report.chars} 字 · ${report.elapsed_ms}ms` +
          (report.warnings.length ? ` · 提示：${report.warnings.join("；")}` : ""),
      );
      setFile(null);
      await load();
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const ingestInline = async () => {
    if (!inlineText.trim()) {
      setError("文本内容为空。");
      return;
    }
    setBusy(true);
    try {
      const result = await api.post<{ report: IngestReport }>("/documents/text", {
        text: inlineText,
        name: inlineName,
        doc_format: inlineName.endsWith(".txt") ? "txt" : "md",
        space_id: spaceId,
        doc_type: meta.doc_type,
        authority: meta.authority,
      });
      setNotice(`文本已入库：${result.report.chunks} 块 · ${result.report.chars} 字`);
      setInlineText("");
      setPreview(null);
      await load();
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const previewChunks = async () => {
    if (!inlineText.trim()) {
      setError("先粘贴一段文本再预览分块。");
      return;
    }
    setBusy(true);
    try {
      const data = await api.post<{ chunks: PreviewChunk[] }>("/documents/preview-chunks", { text: inlineText });
      setPreview(data.chunks);
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const reindex = async (docId: number) => {
    setBusy(true);
    try {
      const result = await api.post<{ embedded: number; lexical_documents: number; namespace: string }>(
        `/documents/${docId}/reindex`,
      );
      setNotice(`重新向量化 ${result.embedded} 块 · 词法索引 ${result.lexical_documents} 篇 · 命名空间 ${result.namespace}`);
      await load();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (docId: number) => {
    setBusy(true);
    try {
      await api.del(`/documents/${docId}`);
      setNotice(`文档 #${docId} 已删除，向量与索引同步清理。`);
      if (openDoc === docId) {
        setOpenDoc(null);
        setChunks([]);
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
      <div style={{ minWidth: 0 }}>
        <div className="card">
          <h3>知识库文档（解析 → 分块 → 向量化 → 词法索引）</h3>
          <div className="row">
            <span className="stat">文档<b>{rows.length}</b></span>
            <span className="stat">片段<b>{totalChunks}</b></span>
            <span className="grow" />
            <span className="muted">支持格式：</span>
            {formats.map((format) => (
              <span key={format} className="chip">{format}</span>
            ))}
          </div>
          {error && <div className="error">{error}</div>}
          {notice && <div className="ok-text">{notice}</div>}
        </div>

        <div className="card">
          <table className="grid">
            <thead>
              <tr>
                <th>#</th>
                <th>名称</th>
                <th>类型</th>
                <th>格式</th>
                <th>发布机构</th>
                <th>权威</th>
                <th>块数</th>
                <th>状态</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((doc) => (
                <tr key={doc.id}>
                  <td className="mono">{doc.id}</td>
                  <td>
                    <div>{doc.name}</div>
                    <div className="muted mono">{doc.effective_date || "—"} · {doc.char_count} 字</div>
                  </td>
                  <td>{doc.doc_type || "—"}</td>
                  <td className="mono">{doc.doc_format}</td>
                  <td className="muted">{doc.issuer || "—"}</td>
                  <td className="mono">{Number(doc.authority).toFixed(2)}</td>
                  <td className="mono">{doc.chunks}</td>
                  <td>
                    <span className={`chip ${doc.status === "indexed" ? "accepted" : "pending"}`}>{doc.status}</span>
                    {doc.error && <span className="chip rejected">{doc.error.slice(0, 24)}</span>}
                  </td>
                  <td className="right">
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      <button className="mini ghost" onClick={() => openChunks(doc.id)} disabled={busy}>
                        {openDoc === doc.id ? "收起" : "分块"}
                      </button>
                      <button className="mini ghost" onClick={() => reindex(doc.id)} disabled={busy}>
                        重建索引
                      </button>
                      <button className="mini danger" onClick={() => remove(doc.id)} disabled={busy}>
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty">
                    还没有文档。可上传文件、粘贴文本，或点顶部「载入示例数据」。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {openDoc && (
          <div className="card">
            <h3>分块明细（文档 #{openDoc}）</h3>
            <div className="scroll-y">
              {chunks.map((chunk) => (
                <div key={chunk.id} className="step">
                  <div className="head">
                    <b>#{chunk.ordinal}</b>
                    <span className="muted mono">{chunk.locator}</span>
                    <span className="chip">≈{chunk.token_estimate} token</span>
                  </div>
                  <div className="obs">{chunk.text}</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <div>
        <div className="card">
          <h3>上传文件</h3>
          <div className="row">
            <input
              type="file"
              className="grow"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <select value={meta.doc_type} onChange={(event) => setMeta({ ...meta, doc_type: event.target.value })}>
              {DOC_TYPES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <input placeholder="发布机构" value={meta.issuer} onChange={(event) => setMeta({ ...meta, issuer: event.target.value })} />
            <input placeholder="辖区" value={meta.jurisdiction} onChange={(event) => setMeta({ ...meta, jurisdiction: event.target.value })} />
            <input
              placeholder="生效日期"
              value={meta.effective_date}
              onChange={(event) => setMeta({ ...meta, effective_date: event.target.value })}
            />
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <label className="muted">
              权威度 {Number(meta.authority).toFixed(2)}
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={meta.authority}
                onChange={(event) => setMeta({ ...meta, authority: Number(event.target.value) })}
              />
            </label>
            <span className="grow" />
            <button className="primary" onClick={upload} disabled={busy || !file}>
              解析并入库
            </button>
          </div>
          <div className="muted" style={{ marginTop: 6 }}>权威度参与检索加权：监管制度建议 0.8+，内部纪要建议 0.4 左右。</div>
        </div>

        <div className="card">
          <h3>粘贴文本入库</h3>
          <div className="row">
            <input className="grow" value={inlineName} onChange={(event) => setInlineName(event.target.value)} />
          </div>
          <textarea
            rows={10}
            style={{ marginTop: 8 }}
            placeholder="粘贴制度条款、产品要素或字段说明，标题会作为分块定位信息。"
            value={inlineText}
            onChange={(event) => setInlineText(event.target.value)}
          />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="mini ghost" onClick={previewChunks} disabled={busy}>
              预览分块（不写库）
            </button>
            <span className="grow" />
            <button onClick={ingestInline} disabled={busy}>
              文本入库
            </button>
          </div>
          {preview && (
            <>
              <div className="title-sm">分块预览（{preview.length} 块）</div>
              <div className="scroll-y" style={{ maxHeight: 260 }}>
                {preview.map((chunk) => (
                  <div key={chunk.ordinal} className="step">
                    <div className="head">
                      <b>#{chunk.ordinal}</b>
                      <span className="muted mono">{chunk.locator}</span>
                      <span className="chip">≈{chunk.tokens} token</span>
                    </div>
                    <div className="obs">{chunk.text}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h3>入库后能做什么</h3>
          <ul className="muted">
            <li>问答：在「问答 · RAG」里直接检索这些片段并生成带引用的回答。</li>
            <li>生成本体：在问答里点「由此题生成本体草案」，或在「Agent 推理」里让 Agent 读文档后提提案。</li>
            <li>确认写入：只有「提案审核」里的确认动作才会修改本体，并触发 Wiki 增量重编译。</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
