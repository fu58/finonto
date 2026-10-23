import { useCallback, useEffect, useState } from "react";
import { api, qs } from "../api";
import { useApp } from "../App";
import type { PackDescriptor, PackInstallReport, SampleFileRow } from "../types";

const DOMAIN_LIMIT = 6;

const PACK_ICON: Record<string, string> = {
  finance: "金",
  maintenance: "维",
  ai: "AI",
  network: "网",
};

function bytes(value: number): string {
  return value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}

function reportLines(report: PackInstallReport): string[] {
  const lines = [
    `已定位空间 #${report.space_id} ${report.space_name}`,
    "created" in report.seed
      ? `种子类型 ${report.seed.created} 个 · 空间内类型共 ${report.seed.types} 个`
      : `基线本体跳过：${report.seed.skipped}`,
  ];
  if (report.samples) {
    lines.push(
      `入库文档 ${report.samples.ingested.length} 篇 · 跳过同名 ${report.samples.skipped.length} 篇 · 片段共 ${report.samples.total_chunks} 块`,
    );
  }
  if (report.wiki) {
    lines.push(
      `Wiki 页面新建 ${report.wiki.created} 个 · 更新 ${report.wiki.updated} 个 · 未变 ${report.wiki.unchanged} 个${
        report.wiki.skipped_locked ? ` · 人工锁定跳过 ${report.wiki.skipped_locked} 个` : ""
      }`,
    );
  }
  lines.push(
    `当前空间统计：类型 ${report.stats.object_types} · 关联 ${report.stats.links} · 属性 ${report.stats.data_properties} · 约束 ${report.stats.constraints}`,
  );
  return lines;
}

export default function PacksView() {
  const { spaceId, setSpaceId, refresh } = useApp();
  const [packs, setPacks] = useState<PackDescriptor[]>([]);
  const [corpus, setCorpus] = useState<SampleFileRow[]>([]);
  const [error, setError] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, string[]>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [loadSamples, setLoadSamples] = useState(true);
  const [compileWiki, setCompileWiki] = useState(true);

  const load = useCallback(async () => {
    try {
      const [packData, corpusData] = await Promise.all([
        api.get<{ packs: PackDescriptor[] }>("/ontology/packs"),
        api.get<SampleFileRow[]>("/system/samples"),
      ]);
      setPacks(packData.packs);
      setCorpus(corpusData);
      setError("");
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const install = async (pack: PackDescriptor) => {
    setBusyKey(pack.key);
    try {
      const report = await api.post<PackInstallReport>(
        `/ontology/packs/${pack.key}/install${qs({ load_samples: loadSamples, compile_wiki: compileWiki })}`,
      );
      setReports((prev) => ({ ...prev, [pack.key]: reportLines(report) }));
      setOpen((prev) => ({ ...prev, [pack.key]: true }));
      setSpaceId(report.space_id);
      await load();
      refresh();
      setError("");
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="col">
      <div className="card">
        <h3>领域包（Domain Pack）</h3>
        <div className="muted">
          每个领域包打包了一个领域的受控词表、基线对象类型、关联断言与示例语料。安装即「建空间 → 灌基线本体 →
          示例文档入库 → 编译 Wiki」，全程幂等，可反复点击。装入后请在顶栏切换本体空间，问答与本体工作室会跟随当前领域。
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <label className="muted">
            <input
              type="checkbox"
              checked={loadSamples}
              onChange={(event) => setLoadSamples(event.target.checked)}
              style={{ marginRight: 6 }}
            />
            装入示例语料
          </label>
          <label className="muted">
            <input
              type="checkbox"
              checked={compileWiki}
              onChange={(event) => setCompileWiki(event.target.checked)}
              style={{ marginRight: 6 }}
            />
            编译 Wiki
          </label>
          <span className="grow" />
          <span className="stat">领域包<b>{packs.length}</b></span>
          <span className="stat">已装空间<b>{packs.filter((item) => item.installed).length}</b></span>
          <span className="stat">示例语料<b>{corpus.length}</b></span>
          <button className="mini ghost" onClick={load} disabled={busyKey !== null}>
            刷新目录
          </button>
        </div>
        {error && <div className="error">{error}</div>}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))", gap: 14 }}>
        {packs.map((pack) => {
          const domains = pack.business_domains.slice(0, DOMAIN_LIMIT);
          const rest = pack.business_domains.length - domains.length;
          const lines = reports[pack.key];
          const busy = busyKey === pack.key;
          const space = pack.space;
          return (
            <div key={pack.key} className="card" style={{ marginBottom: 0 }}>
              <div className="row">
                <span className="chip mono">{PACK_ICON[pack.key] ?? pack.label.slice(0, 1)}</span>
                <b>{pack.label}</b>
                <span className="chip mono">{pack.key}</span>
                {pack.installed && <span className="chip accepted">已安装</span>}
                {space?.id === spaceId && <span className="chip write">当前空间</span>}
              </div>
              <div className="muted" style={{ margin: "6px 0" }}>{pack.summary}</div>
              <div className="muted mono">空间名：{pack.space_name}</div>
              <div className="row" style={{ margin: "8px 0" }}>
                <span className="stat">基线类型<b>{pack.type_count}</b></span>
                <span className="stat">关联断言<b>{pack.link_count}</b></span>
                <span className="stat">标准/规范<b>{pack.standards.length}</b></span>
                <span className="stat">对象分类<b>{Object.keys(pack.kinds).length}</b></span>
                <span className="stat">注解维度<b>{Object.keys(pack.facets).length}</b></span>
                <span className="stat">可用语料<b>{pack.samples_available}</b></span>
              </div>
              <div>
                {domains.map((item) => (
                  <span key={item} className="chip">{item}</span>
                ))}
                {rest > 0 && <span className="chip mono">+{rest}</span>}
              </div>

              {pack.samples.length > 0 && (
                <>
                  <div className="title-sm">示例语料</div>
                  <div className="muted mono">
                    {pack.samples.map((name) => {
                      const file = pack.sample_files.find((item) => item.filename === name);
                      return (
                        <div key={name}>
                          {name}
                          {file ? ` · ${file.doc_type} · ${bytes(file.size_bytes)}` : " · 文件缺失"}
                        </div>
                      );
                    })}
                  </div>
                </>
              )}

              {space && (
                <>
                  <div className="title-sm">已装空间</div>
                  <div className="row">
                    <span className="mono">#{space.id}</span>
                    <b>{space.name}</b>
                    <button className="mini ghost" onClick={() => setSpaceId(space.id)} disabled={spaceId === space.id}>
                      {spaceId === space.id ? "已在此空间" : "切到该空间"}
                    </button>
                  </div>
                  <div className="row" style={{ marginTop: 4 }}>
                    <span className="stat">类型<b>{space.stats.object_types}</b></span>
                    <span className="stat">active<b>{space.stats.active_types}</b></span>
                    <span className="stat">关联<b>{space.stats.links}</b></span>
                    <span className="stat">属性<b>{space.stats.data_properties}</b></span>
                    <span className="stat">约束<b>{space.stats.constraints}</b></span>
                    <span className="stat">待确认提案<b>{space.stats.pending_proposals}</b></span>
                  </div>
                </>
              )}

              <div className="row" style={{ marginTop: 10 }}>
                <button
                  className={pack.installed ? "ghost" : "primary"}
                  onClick={() => install(pack)}
                  disabled={busyKey !== null}
                >
                  {busy ? "安装中…" : pack.installed ? "重新安装（幂等）" : "安装 / 装入空间"}
                </button>
                {lines && (
                  <button className="mini ghost" onClick={() => setOpen((prev) => ({ ...prev, [pack.key]: !prev[pack.key] }))}>
                    {open[pack.key] ? "收起结果" : "安装结果"}
                  </button>
                )}
              </div>

              {lines && open[pack.key] && (
                <div style={{ marginTop: 8 }}>
                  {lines.map((line, index) => (
                    <div key={index} className="ok-text">
                      {line}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {packs.length === 0 && !error && <div className="empty">正在读取领域包目录…</div>}
      </div>

      <div className="card">
        <h3>全部示例语料（跨领域包合并）</h3>
        <div className="scroll-y">
          <table className="grid">
            <thead>
              <tr>
                <th>领域</th>
                <th>文件</th>
                <th>文档类型</th>
                <th>发布机构</th>
                <th>辖区</th>
                <th>权威</th>
                <th>大小</th>
              </tr>
            </thead>
            <tbody>
              {corpus.map((row) => (
                <tr key={`${row.pack}/${row.filename}`}>
                  <td>
                    <span className="chip">{row.pack_label}</span>
                  </td>
                  <td className="mono">{row.filename}</td>
                  <td>{row.doc_type}</td>
                  <td className="muted">{row.issuer || "—"}</td>
                  <td className="muted">{row.jurisdiction || "—"}</td>
                  <td className="mono">{Number(row.authority).toFixed(2)}</td>
                  <td className="mono">{bytes(row.size_bytes)}</td>
                </tr>
              ))}
              {corpus.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty">
                    没有可用的示例语料文件。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
