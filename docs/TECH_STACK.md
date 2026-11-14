# FinOnto 技术选型说明（Python 单栈版）

> 本文回答三件事：**为什么这样选**、**和原方案差在哪**、**边界与风险在哪**。
> 配套文档：[LEARNING.md](LEARNING.md)（非 Python 技术要学什么）、[ARCHITECTURE.md](ARCHITECTURE.md)（模块与数据流）。

---

## 1. 一句话

**一种语言（Python 3.11+）、一个进程、一条命令。** 前端不是 SPA，而是服务端渲染的 HTML
+ 一小段原生 JavaScript；没有 Node、没有 npm、没有打包器、没有前端三方库。

```
┌──────────────────────────── 浏览器（HTML + CSS + 原生 JS，无框架）────────────────────────────┐
│  9 个页签 · 表单原生提交 · fetch 局部换新 · EventSource 接 SSE · 原生 SVG 图谱                │
└───────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                            │  HTTP / SSE（同源，无 CORS、无代理）
┌───────────────────────────────────────────▼──────────────────────────────────────────────────┐
│  FastAPI（单进程，uvicorn）                                                                    │
│  ├── api/          REST + SSE 端点，全部返回 JSON 或 HTML 片段                                 │
│  ├── web/          Jinja2 模板渲染（整页 + HTMX 风格的片段）                                    │
│  ├── core/rag      query_understanding → bm25+向量 → RRF → rerank → MMR → 流式生成 → 引用绑定   │
│  ├── core/agent    ReAct 循环 + 工具注册表 + 技能包 + 沙箱 + 记忆                               │
│  ├── core/ontology 领域包(Domain Pack) / 提案确认 / 唯一 IRI / SHACL 校验 / OWL 导出            │
│  ├── core/wiki    增量编译 / 版本链 / 回滚 / 图谱投影与洞察                                     │
│  └── providers/    10 类组件的可插拔适配层（默认全本地实现，零密钥）                            │
└───────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                            │  SQLAlchemy 2.0
┌───────────────────────────────────────────▼──────────────────────────────────────────────────┐
│  SQLite（WAL）19 张表 + 本地对象存储 var/objects + var/components.json                        │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

启动命令只有一条：

```powershell
python -m app.main            # 或 powershell -File scripts\dev.ps1
```

---

## 2. 技术栈清单

| 层 | 选型 | 版本 | 是否必需 | 备注 |
| --- | --- | --- | --- | --- |
| 运行时 | Python | ≥3.11（实测 3.12.4） | 是 | 只用一个语言 |
| Web | FastAPI | 0.162 | 是 | 自带 OpenAPI、SSE 友好、依赖注入 |
| ASGI | uvicorn | 0.54 | 是 | 单进程即可 |
| 模板 | Jinja2 | 3.1 | 是 | 服务端渲染整页与片段 |
| ORM | SQLAlchemy | 2.0 | 是 | `Mapped[]` 类型化模型 |
| 数据库 | SQLite | 内置 | 是 | WAL 模式，单机单写者 |
| 数值 | numpy | 1.26 | 是 | 向量打分 + **力导向布局迭代**（原在 JS 里算，移到 Python） |
| 测试 | pytest | 7.4 | 开发 | 端到端冒烟 + HTTP 层 |
| 浏览器端 | HTML5 + CSS3 + ES2020 | — | 是 | **原生，无任何三方库** |

**刻意不引入的东西**（对照原 README 逐项消除）：

| 原依赖 | 原用途 | 现在怎么做 | 收益 |
| --- | --- | --- | --- |
| React 18 + Vite 5 + TS | 前端 SPA | Jinja2 模板 + 原生 JS | 去掉 Node/npm、构建步骤、~300KB JS |
| `pydantic-settings` | `.env` 解析 | `config.py` 里 60 行 `os.environ` + `.env` 解析 | 少一个依赖，配置行为透明 |
| `python-multipart` | 文件上传 | 自写 multipart 解析（stdlib `email`） | 少一个依赖；同时支持 raw body 上传 |
| `python-docx` | DOCX 解析 | `zipfile` + `xml.etree` 读 `word/document.xml` | 少一个依赖，解析逻辑可控 |
| `pypdf` | PDF 解析 | 内置 FlateDecode 文本层抽取；有 pypdf 时自动优先 | 扫描件仍需外部 OCR（同原限制） |
| `openpyxl` | XLSX 解析 | `zipfile` + `sharedStrings.xml` 轻量读取 | 可选，缺省也能读 |
| 前端图表库 | 图谱 | **服务端 SVG**：numpy 迭代布局 → 输出坐标 → 模板拼 `<svg>` | 前后端同一套坐标，可截图、可 diff |

---

## 3. 关键决策记录（ADR）

### ADR-001：放弃 SPA，改用服务端渲染 + 渐进增强

**背景**：原方案 9 个页签、力导向图、Markdown 渲染全部在 React 里，导致必须维持一条 Node 工具链。
**决策**：页面整页 SSR；交互点用「服务端返回 HTML 片段，浏览器换进去」的模型。
**理由**：
1. 本项目是**内部工具型应用**，页面数固定、无离线需求、无复杂客户端状态——SPA 的收益（路由切换不刷新、组件复用）在这里不成立。
2. 服务端渲染让 **Wiki 页面、图谱子图、提案详情天生可分享**（一个 URL 就是一页），这是原 SPA 需要额外做的工作。
3. 调试面从「浏览器 + 打包产物 + 类型系统」缩成「浏览器 + Python 栈」，**个人项目最怕的是链路长**。
**代价**：每次交互一次往返（局域网内 <10ms，可接受）；复杂交互（拖拽、实时预览）需要自己写 JS。
**否决的替代方案**：
- *Streamlit*：每次交互全量 rerun，无真路由，SSE 逐 token 与细粒度刷新的控制力不够；
- *NiceGUI*：基于 websocket 的隐式状态，排查成本高，企业交付时"黑盒"是负债；
- *Flask + HTMX 官方推荐栈*：FastAPI 的 SSE、依赖注入、OpenAPI 更适合本项目（Agent 工具面需要结构化签名）。

### ADR-002：交互协议用「data-* 属性 + 事件委托」，不引 HTMX

**决策**：`ui.js` 里实现一套 8 条规则的微型协议，全站复用：

```html
<form data-post="/api/ontology/proposals/12/accept" data-target="#proposal-12" data-swap="outer">
  <button>确认写入</button>
</form>
<div data-poll="/api/system/health" data-interval="5000"></div>
<button data-post="/api/wiki/pages/3/lock">锁定</button>
```

`ui.js` 只做四件事：① 拦截 `[data-post]` 表单提交 → `fetch` → 替换 `data-target`；
② `[data-poll]` 定时拉片段；③ `[data-dialog]` 开关 `<dialog>`；④ 统一错误 Toast。
**理由**：协议只有 4 个动作，手写约 120 行；引入 HTMX 虽省事，但会让「前端到底发生了什么」变得不可见，
而本项目明确要求**零前端三方库**。代价是 SSE 与图谱交互需要自己写（见 `stream.js` / `graph.js`）。

### ADR-003：图谱布局放在 Python，浏览器只做绘制与交互

原方案在 TS 里跑力导向。现在：`core/wiki/graph.py` 用 numpy 做固定迭代次数的 Fruchterman-Reingold，
**布局结果确定性**（同一图同一次编译坐标一致），这带来两个企业级好处：
1. Wiki 页面里的图谱快照可以进版本 diff（坐标变了说明结构变了）；
2. 服务端可导出 PNG/SVG 报告，不需要浏览器。

浏览器端 `graph.js` 只负责：`<g>` 组的 pan/zoom、节点点击跳详情、边高亮——约 120 行。

### ADR-004：人工确认闸门在服务层，不在 UI 层

`accept_proposal()` 是**唯一**改变本体结构（并触发 Wiki 增量编译）的函数，位置在
`core/ontology/service.py`，API、Agent、脚本都只能调它。UI 层不做权限判断——企业里 UI 一定会被绕过
（curl、定时任务、批处理），闸门必须钉在服务层。

### ADR-005：零密钥默认实现是「一等公民」，不是 mock 补丁

`providers/` 下每个组件协议都有本地实现，且**功能完整**（不是抛 `NotImplementedError` 的占位）：
`mock` LLM 能真的抽取术语并生成结构化草案，`local_hash` embedding 能真的做语义近邻，
`heuristic` reranker 能真的按查询词覆盖度重排。这样「一条命令跑通」才是真跑通，
而不是跑到一半提示"请配置 API Key"。

---

## 4. 目录结构

```
docs/
  TECH_STACK.md      # 本文
  LEARNING.md        # 非 Python 技术学习清单
  ARCHITECTURE.md    # 模块、数据流、扩展点
server/
  app/
    main.py          # 建表 → 冷启动 → 路由 → 静态与模板挂载
    config.py        # 环境变量 + .env（无 pydantic-settings）
    db.py            # 引擎（WAL）+ session_scope
    models/domain.py # 19 张表
    core/text.py     # 中英混合分词 / 句子切分 / 术语抽取
    core/rag/        # 查询理解 · 切块 · 入库 · BM25 · 混合检索 · 重排 · 生成 · 引用
    core/agent/      # ReAct · 工具 · 技能 · 沙箱 · 记忆 · MCP
    core/ontology/   # 领域包 base + finance/maintenance/ai/network/general · 提案确认 · 校验 · OWL 导出
    core/wiki/       # 增量编译 · 版本与回滚 · 图谱投影与洞察
    providers/       # 10 类组件的协议 + 本地实现 + 注册表
    api/             # system · ontology · rag · ingest · agent · wiki · components · sse
    web/             # Jinja2 环境 + 片段渲染助手
    templates/       # 9 个页签 + 片段模板
    static/          # app.css · ui.js · stream.js · graph.js · editor.js · md.css
  data/samples/<pack>/
  scripts/           # smoke.py（离线端到端）· http_smoke.py（HTTP 层）
  requirements.txt · .env.example
tests/               # pytest 包装
scripts/dev.ps1|dev.sh
```

---

## 5. 运行时约束（企业落地视角）

| 维度 | 现状 | 边界与对策 |
| --- | --- | --- |
| 并发 | 单进程 uvicorn，SQLite WAL | 内部工具量级（<50 并发）够用；要横向扩，改 `FINONTO_DATABASE_URL` 指向 Postgres 并把副本数设为 1 |
| 鉴权 | 无账号体系 | 定位为「内网可信用户」；对外暴露必须在外层加网关（OIDC/反代 Basic Auth），参见第 7 节 |
| 上传体积 | 默认 25MB/文件 | `FINONTO_MAX_UPLOAD_MB` 可调；对象存储在 `var/objects`，可切 OSS |
| 外呼 | 默认零外呼 | `mock`/`local_hash`/`sqlite_numpy`/`local_fs`/`none` 全部无出网；只有切到真实厂商时才需要密钥 |
| 数据留存 | 全在 `server/var/` | 备份 = 拷 `var/`；`.gitignore` 已排除 |
| 可观测 | `/api/health`、结构化日志、`query_traces` 表 | 未接 Prometheus；接入方式见 `docs/ARCHITECTURE.md` |

---

## 6. 单栈带来的具体收益（可度量）

| 指标 | 原方案 | 现在 |
| --- | --- | --- |
| 启动进程数 | 2（uvicorn + vite） | **1** |
| 首次跑起来需要的命令 | 5 条（venv、pip、npm i、uvicorn、vite） | **1 条** |
| 安装体积 | ~180MB（node_modules） | **0**（无前端依赖） |
| 浏览器下行 JS | ~300KB（压缩后） | **~28KB**（自写 4 个文件） |
| 需要熟悉的技术 | Python + TS + React + Vite + CSS | **Python + HTML + CSS + 少量 JS** |
| 新增一个领域要改的文件 | 后端 3 个 | **后端 3 个（前端 0 个）** |
| 新增一个页签要改的文件 | 4 个（view/api/router/App） | **2 个（模板 + 路由 + 一行 NAV）** |

---

## 7. 部署形态

| 形态 | 做法 | 说明 |
| --- | --- | --- |
| 本机开发 | `scripts\dev.ps1` | 单进程，热重载可选 `--reload` |
| 单机容器 | `docker compose up -d` | 单阶段镜像，只暴露 8000 |
| Kubernetes | `kubectl apply -f deploy/k8s/finonto.yaml` | `Recreate` 策略 + PVC（SQLite 单写者） |
| 内网隔离 | 全部 provider 用本地实现 | 零外呼，模型可接自建 `openai_compat` |
| 反向代理后 | Nginx 前置，`/` 转发到 8000 | SSE 需关闭 `proxy_buffering`；示例配置见 `deploy/nginx.conf` |

---

## 8. 企业化叠加层（0.2 版新增）

第一版是「能跑通、能演示」；企业落地补的是「能交给别人用、出事能复盘」这一层。
关键点是：**没有引入任何新三方依赖**，全用标准库实现。

| 叠加层 | 实现方式 | 为什么不用现成库 |
| --- | --- | --- |
| 认证 / 会话 | hashlib.scrypt + secrets + 服务端会话表 | passlib 已停止维护、python-jose 有 CVE 史；自己写 120 行更好审 |
| RBAC | 两级校验：中间件路径规则 + 接口依赖 | 引入 Casbin 之类会让「谁能做什么」藏进策略文件，反而不利于评审 |
| 审计 | 一张表 + 旁路写入函数 | 结构化日志系统（ELK）不解决「业务语义留痕」，两者互补而非替代 |
| 指标 | 进程内计数器 + Prometheus 文本格式 | prometheus_client 只差一个文件，但当前形态能直接替换 |
| 备份 | sqlite3.Connection.backup() + zipfile | 官方 API 保证 WAL 一致性，比 cp 正确 |
| 自检 | 一个函数返回结构化检查项，三处复用（CLI/页面/API） | 避免「文档写一套、脚本查一套」的漂移 |

工程约束：认证层只依赖 security.py 与 uth_service.py，
业务代码（RAG/本体/Wiki/Agent）**一行没改**——这是分层带来的直接收益。

## 9. 已知取舍（写清楚，避免过度承诺）

1. **没有前端框架**：列表筛选、无限滚动这类交互要自己写；本项目用「服务端过滤 + 分页片段」规避。
2. **SQLite 单写者**：并发写入会串行；提案确认、Wiki 编译都是短事务，实测无感，但不是高并发方案。
3. **PDF 仅文本层**：扫描件、复杂表格版式还原不做，需要 OCR 时接外部服务（`doc_parser` 可插拔）。
4. **mock LLM 是抽取式**：能跑通全链路与演示，但生成质量不等同真实模型；正式使用请切真实厂商（配置 4 行）。
5. **无账号体系**：按「本机/内网可信用户」假设设计，见第 5 节。
