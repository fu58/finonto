import { useCallback, useEffect, useMemo, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import { Markdown } from "../Markdown";
import type { CompileReport, DiffPayload, RevisionRow, WikiPageDetail, WikiPageRow } from "../types";

type EditResult = { changed: boolean; page: WikiPageDetail; revision: RevisionRow | null; message: string };
type RollbackResult = {
  rolled_back: boolean;
  message?: string;
  page_id?: number;
  from_version?: number;
  to_content_version?: number;
  new_version?: number;
};

const PAGE_TYPE_LABEL: Record<string, string> = {
  index: "空间首页",
  type: "对象类型",
  domain: "业务域",
  link: "关联类型",
  constraint: "约束",
  document: "文档",
  provenance: "证据溯源",
  changelog: "变更日志",
};

export default function WikiView() {
  const { spaceId, refresh } = useApp();
  const [pages, setPages] = useState<WikiPageRow[]>([]);
  const [keyword, setKeyword] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [onlyDirty, setOnlyDirty] = useState(false);
  const [pageId, setPageId] = useState<number | null>(null);
  const [detail, setDetail] = useState<WikiPageDetail | null>(null);
  const [tab, setTab] = useState<"read" | "edit" | "history" | "diff">("read");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [keepLock, setKeepLock] = useState(true);

  const [leftVersion, setLeftVersion] = useState<number | null>(null);
  const [rightVersion, setRightVersion] = useState<number | null>(null);
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<number | null>(null);

  const loadPages = useCallback(async () => {
    try {
      const data = await api.get<{ total: number; pages: WikiPageRow[] }>(`/wiki/pages${qs({ space_id: spaceId })}`);
      setPages(data.pages);
      setPageId((current) => {
        if (current && data.pages.some((page) => page.id === current)) return current;
        return data.pages[0]?.id ?? null;
      });
      setError("");
      return data.pages;
    } catch (exc) {
      setError(String(exc));
      return [];
    }
  }, [spaceId]);

  useEffect(() => {
    loadPages();
  }, [loadPages]);

  const loadDetail = useCallback(async (id: number) => {
    try {
      const data = await api.get<WikiPageDetail>(`/wiki/pages/${id}`);
      setDetail(data);
      setDraft(data.body_md);
      const versions = data.revisions.map((revision) => revision.version_no);
      setLeftVersion(versions.length > 1 ? versions[1] : versions[0] ?? null);
      setRightVersion(versions[0] ?? null);
      setRollbackTarget(null);
      setDiff(null);
      setError("");
      return data;
    } catch (exc) {
      setError(String(exc));
      return null;
    }
  }, []);

  useEffect(() => {
    setNotice("");
    if (pageId) loadDetail(pageId);
    else setDetail(null);
  }, [pageId, loadDetail]);

  const visible = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return pages.filter((page) => {
      if (typeFilter && page.page_type !== typeFilter) return false;
      if (onlyDirty && !page.dirty && !page.locked_by_human) return false;
      if (!needle) return true;
      return `${page.title} ${page.slug}`.toLowerCase().includes(needle);
    });
  }, [keyword, onlyDirty, pages, typeFilter]);

  const recompile = async (mode: "dirty" | "all" | "force") => {
    setBusy(true);
    try {
      const report = await api.post<CompileReport>(
        `/wiki/spaces/${spaceId}/compile${qs({
          only_dirty: mode === "dirty" ? true : undefined,
          force: mode === "force" ? true : undefined,
        })}`,
      );
      setNotice(
        `重编译完成：新建 ${report.created} · 更新 ${report.updated} · 未变 ${report.unchanged} · 因人工锁跳过 ${report.skipped_locked}`,
      );
      const rows = await loadPages();
      if (pageId) await loadDetail(pageId);
      else if (rows[0]) setPageId(rows[0].id);
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const result = await api.put<EditResult>(`/wiki/pages/${detail.id}`, {
        body_md: draft,
        note,
        author: "human",
        lock: keepLock,
      });
      setNotice(result.message);
      if (result.revision) {
        setLeftVersion(result.page.current_version > 1 ? result.page.current_version - 1 : result.page.current_version);
        setRightVersion(result.page.current_version);
      }
      // 保存响应里的 page 不含 revisions，重新拉详情才能看到完整版本链
      await loadDetail(detail.id);
      await loadPages();
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const toggleLock = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      await api.post<WikiPageRow>(`/wiki/pages/${detail.id}/lock${qs({ locked: !detail.locked_by_human })}`);
      setNotice(detail.locked_by_human ? "已解锁：后续重编译会覆盖本页。" : "已加锁：自动重编译将跳过本页。");
      await loadDetail(detail.id);
      await loadPages();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const showDiff = async () => {
    if (!detail || leftVersion === null) return;
    setBusy(true);
    try {
      const data = await api.get<DiffPayload>(`/wiki/pages/${detail.id}/diff${qs({ left: leftVersion, right: rightVersion ?? undefined })}`);
      setDiff(data);
      setTab("diff");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const rollback = async () => {
    if (!detail || rollbackTarget === null) return;
    setBusy(true);
    try {
      const result = await api.post<RollbackResult>(`/wiki/pages/${detail.id}/rollback`, {
        to_version: rollbackTarget,
        actor: "human",
        note,
      });
      setNotice(
        result.rolled_back
          ? `已回滚：v${result.from_version} → 采用 v${result.to_content_version} 内容，追加为新版本 v${result.new_version}（历史未被删除）`
          : (result.message ?? "无需回滚"),
      );
      await loadDetail(detail.id);
      await loadPages();
      refresh();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const dropRevision = async (versionNo: number) => {
    if (!detail) return;
    setBusy(true);
    try {
      await api.del(`/wiki/pages/${detail.id}/revisions/${versionNo}`);
      setNotice(`已删除版本记录 v${versionNo}`);
      await loadDetail(detail.id);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  const followWikiLink = (slug: string) => {
    const target = pages.find((page) => page.slug === slug || page.slug.endsWith(`/${slug}`));
    if (target) {
      setPageId(target.id);
      setTab("read");
    } else {
      setError(`页面 ${slug} 尚不存在，可在「本体工作室」确认相关提案后重编译。`);
    }
  };

  const versions = detail?.revisions ?? [];

  return (
    <div className="split">
      <div>
        <div className="card">
          <h3>页面与重编译</h3>
          <div className="row">
            <input
              className="grow"
              placeholder="按标题或 slug 过滤"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
            <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
              <option value="">全部类型</option>
              {Object.entries(PAGE_TYPE_LABEL).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="mini ghost" onClick={() => recompile("dirty")} disabled={busy}>
              增量重编译（仅脏页）
            </button>
            <button className="mini" onClick={() => recompile("all")} disabled={busy}>
              全量重编译
            </button>
            <button className="mini danger" onClick={() => recompile("force")} disabled={busy}>
              强制（忽略人工锁）
            </button>
            <label className="muted" style={{ marginLeft: "auto" }}>
              <input type="checkbox" checked={onlyDirty} onChange={(event) => setOnlyDirty(event.target.checked)} /> 只看脏页/锁定
            </label>
          </div>
          <div className="muted" style={{ marginTop: 6 }}>共 {pages.length} 页 · 脏 {pages.filter((p) => p.dirty).length} · 锁定 {pages.filter((p) => p.locked_by_human).length}</div>
          {error && <div className="error">{error}</div>}
          {notice && <div className="ok-text">{notice}</div>}
        </div>
        <div className="card scroll-y">
          {visible.map((page) => (
            <div key={page.id} className={`list-item${page.id === pageId ? " active" : ""}`} onClick={() => setPageId(page.id)}>
              <span className="name">{page.title}</span>
              <span className="mono muted">{page.slug}</span>
              <span>
                <span className="chip">{PAGE_TYPE_LABEL[page.page_type] ?? page.page_type}</span>
                <span className="chip">v{page.current_version}</span>
                <span className="chip">{page.revision_count} 版本</span>
                {page.dirty && <span className="chip pending">待重编译</span>}
                {page.locked_by_human && <span className="chip lock">人工锁定</span>}
              </span>
            </div>
          ))}
          {visible.length === 0 && <div className="empty">没有匹配页面。可先点「全量重编译」。</div>}
        </div>
      </div>

      <div>
        {!detail && <div className="empty">选择左侧页面查看内容、编辑与版本。</div>}
        {detail && (
          <>
            <div className="card">
              <div className="row">
                <h2 className="grow">{detail.title}</h2>
                {detail.object_type_id && <span className="chip read">对象类型 #{detail.object_type_id}</span>}
                <span className="chip">v{detail.current_version}</span>
                {detail.locked_by_human && <span className="chip lock">已锁定</span>}
                {detail.dirty && <span className="chip pending">脏</span>}
              </div>
              <div className="muted mono">{detail.slug} · 作者 {detail.author || "auto"} · {detail.updated_at}</div>
              {detail.change_note && <div className="muted">版本说明：{detail.change_note}</div>}
              <div className="row" style={{ marginTop: 10 }}>
                {(["read", "edit", "history", "diff"] as const).map((value) => (
                  <button key={value} className={tab === value ? "primary" : "ghost"} onClick={() => setTab(value)}>
                    {value === "read" ? "阅读" : value === "edit" ? "人工编辑" : value === "history" ? `版本历史 (${versions.length})` : "版本对比"}
                  </button>
                ))}
                <span className="grow" />
                <button className="mini ghost" onClick={toggleLock} disabled={busy}>
                  {detail.locked_by_human ? "解除锁定" : "锁定本页"}
                </button>
              </div>
            </div>

            {tab === "read" && (
              <div className="card">
                <Markdown source={detail.body_md} onWikiLink={(slug) => followWikiLink(slug)} />
              </div>
            )}

            {tab === "edit" && (
              <div className="card">
                <h3>人工编辑（保存即产生新版本，并自动加锁防止被重编译覆盖）</h3>
                <textarea rows={20} value={draft} onChange={(event) => setDraft(event.target.value)} />
                <div className="row" style={{ marginTop: 8 }}>
                  <input
                    className="grow"
                    placeholder="版本说明，例如：补充口径出处"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />
                  <label className="muted">
                    <input type="checkbox" checked={keepLock} onChange={(event) => setKeepLock(event.target.checked)} /> 保存后加锁
                  </label>
                  <button className="primary" onClick={save} disabled={busy || draft.trim() === ""}>
                    保存为新版本
                  </button>
                  <button className="mini ghost" onClick={() => setDraft(detail.body_md)} disabled={busy}>
                    放弃修改
                  </button>
                </div>
              </div>
            )}

            {tab === "history" && (
              <div className="card">
                <h3>版本历史（回滚采用「追加新版本」，不会抹掉中间历史）</h3>
                <table className="grid">
                  <thead>
                    <tr>
                      <th>版本</th>
                      <th>作者</th>
                      <th>说明</th>
                      <th>摘要</th>
                      <th>字数</th>
                      <th>时间</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((revision) => (
                      <tr key={revision.id}>
                        <td>
                          v{revision.version_no}
                          {revision.version_no === detail.current_version && <span className="chip accepted"> 当前</span>}
                        </td>
                        <td>{revision.author}</td>
                        <td>{revision.change_note}</td>
                        <td className="mono muted">{String(revision.digest).slice(0, 12)}</td>
                        <td className="mono">{revision.chars}</td>
                        <td className="mono muted">{revision.created_at}</td>
                        <td className="right">
                          <div className="row" style={{ justifyContent: "flex-end" }}>
                            <button className="mini ghost" onClick={() => { setRollbackTarget(revision.version_no); setTab("history"); }}>
                              选为回滚目标
                            </button>
                            {revision.version_no !== detail.current_version && (
                              <button className="mini danger" onClick={() => dropRevision(revision.version_no)} disabled={busy}>
                                删除
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {rollbackTarget !== null && (
                  <div className="row" style={{ marginTop: 10 }}>
                    <span className="chip write">回滚 v{detail.current_version} → v{rollbackTarget} 内容</span>
                    <input className="grow" placeholder="回滚原因（写入版本说明）" value={note} onChange={(event) => setNote(event.target.value)} />
                    <button className="ok" onClick={rollback} disabled={busy}>
                      执行回滚
                    </button>
                    <button className="mini ghost" onClick={() => setRollbackTarget(null)} disabled={busy}>
                      取消
                    </button>
                  </div>
                )}
              </div>
            )}

            {tab === "diff" && (
              <div className="card">
                <h3>版本对比</h3>
                <div className="row">
                  <label className="muted">左</label>
                  <select value={leftVersion ?? ""} onChange={(event) => setLeftVersion(Number(event.target.value))}>
                    {versions.map((revision) => (
                      <option key={revision.id} value={revision.version_no}>
                        v{revision.version_no} · {revision.author}
                      </option>
                    ))}
                  </select>
                  <label className="muted">右</label>
                  <select value={rightVersion ?? ""} onChange={(event) => setRightVersion(Number(event.target.value))}>
                    {versions.map((revision) => (
                      <option key={revision.id} value={revision.version_no}>
                        v{revision.version_no} · {revision.author}
                      </option>
                    ))}
                  </select>
                  <button onClick={showDiff} disabled={busy || leftVersion === null}>
                    生成差异
                  </button>
                </div>
                {diff && (
                  <>
                    <div className="muted" style={{ marginTop: 8 }}>
                      v{diff.left.version_no} → v{diff.right.version_no}：+{diff.added_lines} 行 / −{diff.removed_lines} 行
                    </div>
                    <table className="grid" style={{ marginTop: 8 }}>
                      <tbody>
                        {diff.side_by_side.map((row, index) => (
                          <tr
                            key={index}
                            style={
                              row.kind === "insert"
                                ? { background: "rgba(78,201,138,.08)" }
                                : row.kind === "delete"
                                  ? { background: "rgba(242,104,111,.08)" }
                                  : row.kind === "replace"
                                    ? { background: "rgba(224,164,88,.08)" }
                                    : undefined
                            }
                          >
                            <td className="mono" style={{ width: "50%", whiteSpace: "pre-wrap" }}>{row.left}</td>
                            <td className="mono" style={{ width: "50%", whiteSpace: "pre-wrap" }}>{row.right}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <div className="title-sm">unified diff</div>
                    <pre className="diff">
                      {(diff.unified_diff || "").split("\n").map((line, index) => (
                        <div
                          key={index}
                          className={line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "ctx"}
                        >
                          {line || " "}
                        </div>
                      ))}
                    </pre>
                  </>
                )}
                {!diff && <div className="empty">选择两个版本后点「生成差异」。</div>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
