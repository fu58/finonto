# 学习清单：这个项目里你必须会的「非 Python」技术

> 结论先行：整个项目 **约 87% 的代码是 Python**。剩下的 3% 只有四样东西——HTML、CSS、原生
> JavaScript、SVG，加起来约 **1900 行**（其中 JS 只有 543 行），按下面的顺序学，**约 3 天**可以从零读到能改。
>
> 每一节都给出三件事：**它在本项目哪个文件里**、**最小知识清单（只学这些就够）**、
> **一个可自检的练习**（做完就等于掌握了对应功能）。

---

## 0. 前置：Python 要会到什么程度

不需要异步高手，但要能看懂：

| 要求 | 为什么 | 30 分钟自检 |
| --- | --- | --- |
| 类型注解 + `@dataclass` | `core/ontology/packs/base.py` 全是声明式数据 | 能读懂 `PACK = DomainPack(key=..., object_types=[...])` |
| 上下文管理器 `with` | `db.session_scope()` 每个请求一个事务 | 能说出 `with session_scope() as s:` 里抛异常会怎样 |
| 生成器 `yield` | SSE 流式回答逐 token 推送 | 能写出一个 `for x in gen()` 的消费端 |
| `async def` 基础 | FastAPI 路由与 `StreamingResponse` | 分得清 `def` 与 `async def` 路由的差别（谁在事件循环里跑） |
| 列表/字典推导式 | 数据整形到处是 | 无 |

**不用提前学的**：SQLAlchemy 的 `select()` 高级用法、Pydantic v2 校验器、Alembic 迁移、
Kubernetes、Docker 多阶段构建——这些在本项目里都有现成写法可抄。

---

## 1. HTML —— 约 3 小时

**在哪用**：`../finonto/server/app/templates/*.html`（9 个页签模板 + 若干片段模板），共约 600 行。

**最小知识清单**（只需要这些标签）：

| 标签/属性 | 本项目里的用途 |
| --- | --- |
| `<form method="post" action="...">` | 所有写操作（确认提案、新建类型、编辑 Wiki） |
| `<input>` / `<select>` / `<textarea>` | 表单控件；`type="file"` 用于文档上传 |
| `<button>`、`<dialog>` | 弹窗（新建类型、图谱节点详情） |
| `<table>` / `<details>` / `<summary>` | 属性表、约束表、可折叠的证据链（**原生折叠，不用 JS**） |
| `<nav>` + `<a href>` | 9 个页签的导航 |
| `<template>` / `data-*` 属性 | `data-post`、`data-target`、`data-poll`——自定义交互协议 |
| `<pre>` / `<code>` | Markdown 渲染结果、Turtle 导出预览 |

**不用学的**：`<canvas>`、`<video>`、表单校验伪类、无障碍 ARIA 全量规范、Web Components。

**练习（做完能改一个功能）**：
1. 打开 `templates/proposals.html`，找到「确认写入」按钮；
2. 把它从 `data-post` 改成普通 `<form method="post">`，刷新页面提交；
3. 观察：没有 JS 也能工作（服务端返回重定向）——这就是「渐进增强」，也是企业内网老旧浏览器下的兜底。

---

## 2. CSS —— 约 4 小时

**在哪用**：`server/static/css/app.css` 一个文件，约 700 行；`md.css` 约 120 行（Markdown 排版）。

**最小知识清单**：

| 主题 | 本项目里的用途 |
| --- | --- |
| 盒模型 `box-sizing: border-box` | 全局重置 |
| Flexbox | 顶栏、按钮组、卡片行 |
| Grid `grid-template-columns` | 三栏布局（侧栏 / 主区 / 详情抽屉）、表单两列 |
| CSS 变量 `--fg`、`--bg`、`--accent` | 主题（含 `[data-theme="dark"]` 切换） |
| `position: sticky` | 顶栏与 Wiki 目录 |
| `:hover` / `:focus-visible` / `[data-state]` | 交互态与「已锁定/编译中」状态色 |
| `@media (max-width: 900px)` | 单栏降级 |
| `overflow` / `text-overflow: ellipsis` | 长定义文本截断 |

**不用学的**：Tailwind（**故意不用**）、Sass/Less、CSS-in-JS、动画库、`@container` 查询、
复杂 `transform: 3d`——图谱缩放只用 `transform: scale()` 一行。

**练习**：
1. 在 `app.css` 末尾加 `:root[data-theme="dark"] { --bg: #111; --fg: #eee; }`；
2. 在 `base.html` 的 `<html>` 上加 `data-theme="dark"`，刷新看效果；
3. 理解「一个变量改全站」比改 50 个 class 划算——这就是为什么不用 Tailwind。

---

## 3. JavaScript —— 约 1.5 天（最大的一块）

**在哪用**：`server/static/js/` 四个文件，合计约 420 行：

| 文件 | 行数 | 职责 | 你必须掌握的语言特性 |
| --- | --- | --- | --- |
| `ui.js` | 191 | 事件委托：`[data-post]` 提交、`[data-poll]` 轮询、`[data-dialog]` 弹窗、Toast | `querySelector`、`addEventListener`、`fetch`、`FormData`、`classList`、`dataset` |
| `stream.js` | 226 | SSE 接流式回答，逐 token 追加 | `EventSource`、`innerHTML` 的安全用法、`scrollIntoView` |
| `graph.js` | 86 | 图谱 pan/zoom、节点点击、边高亮 | `addEventListener('pointerdown')`、`transform` 属性、`getBoundingClientRect` |
| `editor.js` | 40 | Wiki 编辑器：Tab 缩进、Ctrl+S 保存、脏标记 | `keydown`、`preventDefault`、`beforeunload` |

**分四块学，每块都有对应文件**：

### 3.1 `fetch` + `FormData`（半天）
```js
const res = await fetch('/api/ontology/proposals/3/accept', { method: 'POST' });
const html = await res.text();          // 服务端返回的是 HTML 片段，不是 JSON
document.querySelector('#proposal-3').outerHTML = html;
```
要理解：**本项目大部分接口返回 HTML 片段**（供直接换进去），只有 `/api/*` 数据接口返回 JSON。
这是与 SPA 最大的思维差异。

### 3.2 DOM 操作（半天）
`querySelector` / `closest` / `dataset` / `classList` / `textContent` vs `innerHTML`（**必须理解 XSS**：
服务端已转义，前端只对可信片段用 `innerHTML`）。

### 3.3 `EventSource`（2 小时）
```js
const es = new EventSource('/api/rag/stream?q=设备维保的预防性维护周期是多少');
es.onmessage = e => { const d = JSON.parse(e.data); if (d.type === 'token') out.append(d.text); };
```
要理解：SSE 是**单向**的（服务端→浏览器），所以本项目不用 WebSocket；`event: done` 后必须 `es.close()`。

### 3.4 原生 SVG 操作（半天）
不是为了画图（**画图在 Python 里**），而是为了缩放：
```js
viewport.setAttribute('transform', `translate(${tx},${ty}) scale(${k})`);
```
要理解 SVG 坐标系、`viewBox`、`<g>` 分组。

**不用学的**：React/Vue 任何 API、`Promise` 手写实现、`Proxy`/`Reflect`、模块打包、TypeScript 类型体操、
`localStorage` 状态管理（本项目状态都在服务端）。

**练习（做完能改一个功能）**：
1. 打开 `ui.js`，找到 `[data-poll]` 的分支；
2. 给「组件中心」的探针卡片加 `data-poll="/api/components/probe" data-interval="3000"`；
3. 刷新页面，看到探针每 3 秒自动更新——你就掌握了本项目的交互模型。

---

## 4. SVG —— 约 3 小时

**在哪用**：
- `core/wiki/graph.py` 生成坐标（Python）；
- `templates/_graph.svg.html` 拼 `<svg>`（Jinja2）；
- `graph.js` 做交互。

**最小知识清单**：`<svg viewBox>`、`<g>`、`<circle>`、`<line>`、`<text>`、`<path d="...">`（贝塞尔边）、
`stroke-dasharray`（虚线的证据链边）、`<marker>`（箭头）。

**练习**：手写一个 3 节点 2 边的 `<svg>`，节点 `<circle cx="100" cy="100" r="18">`，
边用 `<line>` 加 `marker-end` 箭头。做完就能看懂 `_graph.svg.html`。

---

## 5. 可选加分项（不学也能跑，但会让你改起来更顺）

| 技术 | 用在哪 | 学到什么程度 | 时间 |
| --- | --- | --- | --- |
| SQL 基础 | 排查 `var/finonto.db`，写报表 | `SELECT ... WHERE ... JOIN ... GROUP BY` | 2 小时 |
| 正则 | `core/text.py` 术语抽取、`citations.py` 引用标记 | 会读 `r"\[c(\d+)\]"` 这级 | 1 小时 |
| Git | 版本回滚本项目的代码（不是 Wiki 回滚） | `add/commit/diff/log` | 1 小时 |
| Markdown | 读本仓库所有文档、Wiki 页面 | 标题/列表/代码块/表格 | 30 分钟 |

---

## 6. 领域知识（不是技术栈，但不懂就读不懂代码）

这五个概念在项目里各占一个模块，**每个附一句"用 Python 话怎么说"**：

| 概念 | 一句话 | 代码位置 | 读多久 |
| --- | --- | --- | --- |
| **RAG** 检索增强生成 | 先查资料，再把资料塞进提示词让模型回答 | `core/rag/pipeline.py` | 1 小时 |
| **BM25 / 向量检索 / RRF** | 关键词打分与语义近邻两路召回，用倒数排名融合 | `core/rag/bm25.py`、`retriever.py` | 2 小时 |
| **本体（OWL）/ SHACL** | 用「类—属性—关系—约束」把业务概念写成机器可校验的模型 | `core/ontology/` | 3 小时 |
| **ReAct** | 让模型循环「想一步→调工具→看结果」，直到收敛 | `core/agent/react.py` | 1 小时 |
| **领域包（Domain Pack）** | 把"某个领域的词表、样例、术语线索"打成一个 Python 模块 | `core/ontology/packs/*.py` | 30 分钟 |

---

## 7. 建议的学习顺序与检查点

```
Day 1  上午  HTML（3h）      → 检查点：能改一个表单，理解"无 JS 也能提交"
       下午  CSS（4h）       → 检查点：能加一套 dark 主题变量
Day 2  上午  fetch + DOM（4h）→ 检查点：能看懂 ui.js 的数据流
       下午  EventSource（2h）→ 检查点：能改流式回答的追加逻辑
Day 3  上午  SVG + graph.js（3h）→ 检查点：能给图谱加"高亮邻居"
       下午  领域知识（3h）  → 检查点：能解释"提案为什么必须人工确认"
```

**三天后你能独立完成的事**：加一个页签、改一个交互、加一个领域包、调一个 RAG 参数。

---

## 8. 明确不需要学的东西（省下来的时间）

| 不需要 | 原因 |
| --- | --- |
| React / Vue / Svelte | 项目不用框架 |
| TypeScript | 前端没有类型系统，只有 4 个 JS 文件 |
| Vite / Webpack / npm / pnpm | 没有构建步骤 |
| Tailwind / Sass | 一个手写 CSS 文件，CSS 变量足够 |
| WebSocket | 本项目全用 SSE（单向、够用、穿透代理更容易） |
| Kubernetes 细节 | 只要会 `kubectl apply -f`，yaml 已写好 |
| Alembic 迁移 | 冷启动自动建表 + 幂等安装；生产改表再学不迟 |
| JWT / OAuth | 无账号体系，接入时在外层网关做 |






