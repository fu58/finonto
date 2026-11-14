# FinOnto · 多领域本体生成平台（Python 单栈版）

以 **RAG 问答** 与 **Agent 推理** 为主要入口，引导式地从文档与制度条款中生成领域本体
（对象类型 / 数据属性 / 关联 / SHACL 约束 / 证据链），自动编译成可编辑、可版本回滚的
Wiki 与知识图谱。

领域不写死在代码里：**金融、设备维保、人工智能、计算机网络** 各自是一个可插拔的「领域包」，
新增一个领域只需放一个模块。10 类外部依赖（模型厂商、Embedding、向量库、对象存储、文档格式、
数据源、网络搜索、IM、重排序、部署目标）全部可插拔；**不配任何密钥时全部落到本地实现**，
一条命令即可端到端演示。

> 技术栈已从「FastAPI + React 18 + Vite 5 + TypeScript」整体换成 **Python 单栈**：
> 前端是服务端渲染的 Jinja2 模板 + 543 行原生 JavaScript，**没有 npm、没有打包器、没有前端三方库**。
> 为什么这样选、你要额外学什么，见 [docs/TECH_STACK.md](docs/TECH_STACK.md) 与 [docs/LEARNING.md](docs/LEARNING.md)。

---

## 1. 三十秒跑起来（零密钥）

环境：**Python 3.11+**（实测 3.12.4）。不需要 Node。

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

```bash
# macOS / Linux
bash scripts/dev.sh
```

也可以手动跑：

```bash
cd server
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000** —— 只有一个进程、一个端口，页面与接口同源。

首次访问会跳转登录页，内置管理员 **`admin` / `admin123`**（登录后**强制改密**）。
想跳过登录做纯离线演示，设 `FINONTO_AUTH_ENABLED=false`（页面顶部会显示「演示模式」横幅）。

首次启动会自动冷启动（`FINONTO_AUTO_BOOTSTRAP=true`）：把**全部内置领域包**逐个装进各自的本体空间
（金融 13 / 设备维保 11 / 人工智能 12 / 计算机网络 14 个基线对象类型，外加一个不预设领域的
「通用本体域」，合计 **5 个空间 / 50 个类型**）→ 解析并入库 8 份示例语料（143 个片段）→
构建 BM25 + 向量双索引 → 首次编译 Wiki（**87 页**）。

只想装某几个领域时把 `FINONTO_AUTO_BOOTSTRAP` 关掉，用
`POST /api/system/bootstrap?pack_keys=network,ai`，或在「领域包」页签里点安装。

---

## 2. 这一版的技术栈（对照原方案）

| 层 | 原方案 | 现在 | 收益 |
| --- | --- | --- | --- |
| 前端框架 | React 18 + TypeScript | **无框架**，Jinja2 服务端渲染 | 去掉组件状态与构建链 |
| 前端构建 | Vite 5 + npm | **无** | 首次跑起来从 5 条命令降到 1 条 |
| 浏览器依赖 | React + 自写图表/Markdown | **0 个三方库**（自写 4 个 JS 文件） | 下行 JS 从 ~300KB 到 ~28KB |
| 交互 | SPA 路由 + fetch | 整页 SSR + `data-*` 协议 + `EventSource` | Wiki 页面天生可分享，调试只看 Python |
| 图表 | 手写 TS 力导向 | **Python + numpy 布局** → 服务端 SVG | 坐标确定，可进版本 diff、可归档 |
| 后端 | FastAPI + SQLAlchemy + SQLite(WAL) | **不变** | 业务代码零改动 |
| 依赖 | +pydantic-settings / python-multipart / pypdf / python-docx | **全部去掉**，标准库替代 | 安装体积更小，配置行为透明 |

启动进程数 **1**，安装体积 **0 个 node_modules**，需要熟悉的技术从「Python + TS + React + Vite」
收敛到「Python + HTML + CSS + 少量 JS」（约 3 天，见 [docs/LEARNING.md](docs/LEARNING.md)）。

---

## 3. 主流程：从一句话到本体里的对象类型

| 页签 | 做什么 | 关键接口 |
| --- | --- | --- |
| **问答 · RAG** | 流式问答 + 可点击引用 `[c1]`，查询理解芯片（意图/子问题/过滤条件/类型对齐），一键「按本题生成本体草案」 | `GET /api/rag/stream`、`POST /api/ontology/{space}/draft` |
| **Agent 推理** | 选技能 → ReAct 多步推理，实时展示 Thought/Action/Observation、token 与步数预算；沙箱、MCP、长期记忆 | `GET /api/agent/stream`、`POST /api/agent/sandbox`、`/api/agent/mcp/*` |
| **本体工作室** | 类型检索/筛选、手工新建、详情（属性/关联/约束/证据链）、内联改定义（版本 +1）、空间校验、删除 | `/api/ontology/{space}/types`、`PATCH /api/ontology/types/{id}` |
| **提案审核** | 所有写入都必须在这里人工确认；确认后立即写本体 + 增量重编译 Wiki + IM 通知 | `POST /api/ontology/proposals/{id}/accept` |
| **Wiki 与版本** | 页面列表（类型/关键词/dirty 过滤）、全量或增量编译、人工编辑与锁定、版本链 diff、非破坏式回滚 | `/api/wiki/pages*`、`POST …/rollback` |
| **知识图谱** | 全图 / 邻域子图 / 两点路径三种模式，节点详情与洞察（枢纽、域分布、缺证据类型、孤立节点） | `/api/wiki/graph`、`…/subgraph`、`…/path`、`…/insights` |
| **文档入库** | 上传（文种/发布机构/辖区/权威度）、纯文本入库、片段预览、重建索引、删除 | `/api/documents*` |
| **领域包** | 画廊（基线类型数/子域/标准/示例语料/已装空间）、一键幂等安装 | `GET /api/ontology/packs`、`POST …/packs/{key}/install` |
| **组件中心** | 10 类组件目录与切换、连通性探针、密钥状态、部署清单 | `/api/components*` |
| **运维 · 备份** | 11 项上线自检、在线备份、指标快照、恢复步骤、审计清理 | `GET /ops`、`POST /api/system/backup`、`GET /api/metrics` |
| **用户与角色** | 用户增删改停、四级角色、重置密码、会话统计、强制首登改密 | `/api/auth/users*` |
| **审计日志** | 22 类动作留痕、三级结果、按动作/操作人/关键字筛选、CSV 导出 | `GET /audit`、`GET /api/audit/export.csv` |

一条被验证过的完整链路（`smoke.py`、`http_smoke.py` 与浏览器都跑通过）：

```
文档解析 → 分块 → 嵌入 → BM25+向量双索引
   → 查询理解 → 混合检索(RRF) → 重排 → MMR 去冗 → 流式生成 → 引用绑定
   → ReAct（rag.search → doc.list → ontology.list_types → find_similar → propose_type）
   → ChangeProposal + Evidence
   → 人工确认 accept → ObjectType/Link/Constraint 落库（版本 +1，唯一 IRI）
   → 增量重编译 Wiki（dirty 页面）→ IM 通知
   → 人工编辑/锁定 → 回滚（追加新版本，不删历史）→ 图谱与路径查询
```

**人工确认是硬闸门**：RAG 与 Agent 只能产出提案（`change_proposals`）与证据（`evidence`），
没有任何自动写本体的旁路；`accept_proposal` 是唯一改变本体结构（并触发 Wiki 编译）的入口。

---

## 4. 目录结构

```
server/
  app/
    main.py               # 建表 → 幂等冷启动 → 路由 → 挂载静态资源（单端口）
    config.py             # FINONTO_* 环境变量 + .env（不依赖 pydantic-settings）
    models/domain.py      # 23 张表
    db.py                 # SQLAlchemy 引擎（WAL）与 session_scope
    core/
      text.py             # 中英混合分词、切句、术语抽取、slug
      rag/                # query_understanding / chunking / bm25 / retriever / reranker
                          # generator / citations / ingest / pipeline
      agent/              # react / tools(15 个) / skills(6 个) / sandbox / memory / mcp_client
      ontology/           # service(提案确认与唯一 IRI) / kinds / owl / packs(base+5 个领域)
      wiki/               # compiler(增量) / versions(回滚与 diff) / graph(投影+布局+洞察)
      audit.py            # 审计（旁路、脱敏、CSV 导出）
      auth_service.py     # 账号 / 会话 / 角色 / 初始管理员
      backup.py           # SQLite 在线备份 + 恢复说明
      diagnostics.py      # 11 项自检（/api/ready、/ops、preflight 共用）
    providers/            # 10 类组件：llm / embedding / vectorstore / objectstore
                          # docparser / datasource / search / im / rerank / registry
    api/                  # system / auth / ontology / rag / ingest / agent / wiki
                          # components / ops(指标·诊断·备份·审计) / sse
    middleware.py         # request-id → 会话 → 认证 → CSRF → 授权 → 限流 → 指标/日志
    security.py           # scrypt 密码 / 会话令牌 / 角色层级 / 限流器
    observability.py      # 请求指标 + Prometheus 文本输出
    web/                  # render(Jinja2 环境) / markdown(零依赖渲染器)
                          # pages(业务页面) / pages_auth(登录·用户·审计·运维)
    templates/            # 12 个页签 + 登录/错误页
    static/css/app.css    # 手写 CSS（无框架）
    static/js/*.js        # ui / stream / graph / editor（合计 543 行）
  data/samples/<pack>/    # 随领域包发布的示例语料（4 个领域 × 2 份）
  scripts/smoke.py        # 服务层端到端冒烟（89 项断言）
  scripts/http_smoke.py   # HTTP 层冒烟（87 项检查，含登录）
  scripts/auth_smoke.py   # 安全边界冒烟（69 项检查）
  scripts/preflight.py    # 上线前检查（退出码可接 CI）
  scripts/backup.py       # 命令行备份 / 列备份 / 恢复提示
docs/                     # TECH_STACK / LEARNING / ARCHITECTURE / OPERATIONS
tests/                    # pytest（42 项单元测试）
deploy/                   # Dockerfile / compose / k8s / nginx
scripts/dev.ps1|dev.sh
```

运行期数据都在 `server/var/`（SQLite、对象存储、上传件、沙箱工作区、`components.json`），已被 `.gitignore` 排除。

---

## 5. 能力实现要点

### 5.1 RAG 问答
- **查询理解**（`core/rag/query_understanding.py`）：规则 + 可选 LLM 混合，产出意图
  （definition / quantitative / comparison / ontology_generation / general）、同义扩展、
  子问题拆分、结构化过滤（文种/辖区/最低权威度）、与已有对象类型的对齐结果。
  过滤条件只在**明确的筛选句式**下生效（避免「信贷合同」被误判成"只看合同类文档"）。
- **混合检索**（`retriever.py`）：BM25（自实现，`bm25.py`）+ 向量召回并行，RRF 或加权融合；
  随后同文档配额 → 图感知扩展（命中已有类型则加权）→ MMR 多样化。每条结果带
  `signals`（来源、RRF 分、图加权、权威度），写进问答轨迹。
- **重排序**（`providers/rerank.py`）：`heuristic`（默认，零依赖，每分可解释）/ `llm` pointwise /
  `cross_encoder`（缺依赖时自动退回启发式）。
- **流式与引用**（`generator.py` + `citations.py`）：上下文带 `[cN]`，逐 token 推送；
  结束时把答案中真实出现的 `[cN]` 绑定成引用（文档名、定位、原文引句、得分），
  **未被引用的片段不会出现在引用列表里**。

### 5.2 Agent 推理
- **ReAct**（`react.py`）：Thought→Action→Observation，步数与 token 预算可配
  （`FINONTO_AGENT_MAX_STEPS` / `AGENT_STEP_TOKEN_BUDGET`）；三重防转圈：步数上限、
  同工具同参数重复检测、无法解析动作即退出。运行与步骤落库，可在 UI 回放。
- **工具面**：15 个内置工具（`rag.*`、`doc.*`、`ontology.*`、`graph.*`、`wiki.*`、`memory.*`、
  `sandbox.run_python`、`search.web`、`datasource.probe`），每个带风险分级
  `read / write / exec / network`；Agent 写本体的唯一方式是 `propose_type`。
- **技能包**（`skills.py`）：`doc-extraction`、`clause-mapping`、`relation-discovery`、
  `def-refine`、`graph-completion`、`web-research` —— 系统提示 + 工具白名单 + 输出契约 + 步数上限；
  技能本身领域中立，领域词表由当前空间的领域包注入。
- **MCP**：`inproc` / `stdio` / `http`；内置 3 个进程内服务（`finonto-kb`、`finonto-ontology`、
  `finonto-export`，共 8 个工具），可在 UI 启停与直连调用。
- **沙箱**（`sandbox.py`）：独立子进程 + AST 白名单（只允许 `json/math/statistics/re/datetime/…`，
  禁 `os/sys/subprocess/socket/pathlib`）+ 超时 + 输出上限 + 下划线属性拦截；
  `POST /api/agent/sandbox/validate` 可先看拦截结果。
- **长期记忆**（`memory.py`）：条目化存储 + 关键词召回 + 运行后自动归纳 + 时间衰减 + 清理。

### 5.3 本体模型
`ObjectType`（class/agreement/role/document/metric/event/organization/asset/process、抽象类、父子分类、
业务域、置信度、来源、别名、版本）→ `DataPropertyDef`/`TypeDataProperty`（类型、单位、必填、枚举）→
`RelationType`/`TypeLink`（taxonomy / participant / reference / derivation / regulatory / part_of / causal）→
`Constraint`（SHACL 风格：range / codependency / cardinality，带 severity 与监管文号）→
`Evidence`（文档、片段、定位器、引句、提案号）。
- **唯一 IRI**：中文标签派生局部名可能撞车（两个概念都映射到 `Loan`），追加数字后缀（`Loan2`）而非覆盖。
- **查重**：`find_similar` 用别名 + 术语重叠提示疑似重复，避免同义建类。
- **校验**：`validate_space` 产出 `violation / warning / info` 三级问题（无定义、无属性、未分域、
  约束表达式不可解析、循环父子、悬空关联）。
- **导出**：`GET /api/ontology/{space}/export?format=turtle|skos|json`，Turtle 里同时含
  `owl:Class`/`owl:DatatypeProperty` 与 `sh:NodeShape`。

### 5.4 自动 Wiki 与知识图谱
- 编译器按五类页面生成 Markdown：索引页 `index`、类型页 `type-<id>`、业务域页 `domain-<域>`、
  关联语义目录、本体质量报告；正文带来源注释与 `[[slug]]` 链接。提案确认后只重编译 dirty 页面，
  人工锁定页跳过。
- 版本服务：每次编译或人工编辑追加一条 `wiki_revisions`（含 digest）；`diff` 给行级 +
  并排视图；`rollback` **追加**新版本而不是删历史。
- 图谱投影（`wiki/graph.py`）：节点 `t:<type_id>` / `p:<prop_id>` / `d:<doc_id>`，
  边分 taxonomy / association / attribute / evidence 四类；提供全图、邻域子图、两点最短路径
  与洞察（枢纽排名、域分布、无证据类型、孤立节点、连通分量、密度、平均度）。
- **布局在 Python**（numpy 迭代 Fruchterman-Reingold，固定种子），所以坐标确定、可进版本、可导出 SVG。

### 5.5 多领域与领域包
- **挂载点是 `Space.domain`**：每个本体空间属于一个领域包，类型、属性、关联、约束、提案、
  Wiki、图谱、导出本来就按 `space_id` 隔离，所以扩领域不需要任何表结构变更。
- **`DomainPack`**（`core/ontology/packs/base.py`）把一个领域的东西打包声明：空间名/IRI/描述、
  `business_domains` 受控子域、基线 `object_types` 与 `links`、随包发布的 `samples`、词表扩展
  （`kinds` / `relation_categories` / `facets` / `classification` / `standards`）、离线抽取线索
  （`term_suffixes` / `clause_markers` / `domain_cues` / `tag_cues` / `synonyms`）、
  `sample_questions`（问答页签的起手问题，切空间即换）与 `agent_hint`。
- **领域如何生效**：`vocab_for(domain)` 给前端受控词表；`lexicon_for(domain)` 把同义扩展表与
  术语后缀交给查询理解；`prompt_context_for(domain)` 注入草案服务与 ReAct 的系统提示；
  Mock LLM 直接解析提示里的「子域线索 / 标签线索 / 术语后缀」，所以零密钥路径也跟随当前领域。
- **安装即幂等**：`install_pack` = 建空间 → 灌基线本体 → 示例语料入库 → 证据链落库 → 编译 Wiki，
  反复执行只会跳过已存在的类型、同名文档与相同哈希。

---

## 6. 企业落地能力

> 完整操作手册见 [docs/OPERATIONS.md](docs/OPERATIONS.md)：权限矩阵、上线检查单、备份恢复、监控接入、故障速查。

| 能力 | 实现 | 入口 |
| --- | --- | --- |
| **身份认证** | 内置用户表 + scrypt 加盐密码 + 服务端会话（Cookie 只存令牌，库里只存 SHA-256 指纹）+ 强制首登改密 | `/login`、`/profile` |
| **四级 RBAC** | 只读 / 编辑 / 审批 / 管理员；**路径级（中间件）+ 接口级（依赖）双层校验**，越权写审计 | `/users` |
| **职责分离** | 提案由 approver 确认，editor 只能产出；最后一个管理员不可降级/停用/删除 | `core/auth_service.py` |
| **审计日志** | 22 类动作（登录、改密、提案确认、类型增删改、Wiki 回滚、文档入库删除、组件切换、备份…），密钥字段脱敏，CSV 导出，180 天保留 | `/audit` |
| **登录防爆破** | 失败计数 + 临时锁定（默认 5 次 / 15 分钟）+ 强口令规则 | `security.py` |
| **CSRF 兜底** | SameSite=Lax + Origin/Host 比对（跨站 unsafe 请求直接 403） | `middleware.py` |
| **限流** | 按用户/IP 的滑动窗口，只作用于 `/api/rag/*`、`/api/agent/*`，超限 429 + `Retry-After` | `security.RateLimiter` |
| **可观测** | `/api/health`（存活）、`/api/ready`（依赖就绪）、`/api/metrics`（Prometheus 文本）、访问日志带 `request-id` 与用户角色 | `/ops` |
| **上线自检** | 11 项检查（Python/依赖/目录/DB/向量维度/认证/审计/语料/沙箱/内容/密钥），可命令行执行 | `scripts/preflight.py` |
| **在线备份** | SQLite 官方 backup API（WAL 下一致，无需停服）+ 对象存储 + 组件配置，包内附 `RESTORE.md` | `/ops`、`scripts/backup.py` |
| **配置校验** | `.env` 解析容错、非法值退回默认、密钥只回显布尔 | `config.py` |

**四个数字**（本机实测，全套自动化）：

| 套件 | 覆盖 | 结果 |
| --- | --- | --- |
| `scripts/smoke.py` | 服务层业务链路（10 阶段） | **89/89** |
| `scripts/http_smoke.py` | HTTP 层（页面 + API + SSE + 边界） | **87/87** |
| `scripts/auth_smoke.py` | 安全边界（未登录/登录失败/强制改密/四级角色/审计/限流/备份） | **69/69** |
| `pytest tests` | 单元测试（文本、检索、沙箱、安全、诊断） | **42/42** |

上线前：

```bash
cd server
.venv/Scripts/python.exe scripts/preflight.py --strict   # 退出码非 0 即不可上线
.venv/Scripts/python.exe scripts/backup.py --note "上线前基线" --keep 14
```

---

## 7. 组件矩阵与切换

组件中心按类别列出全部实现，并区分「免密钥可用」与「需要配置」：

| 类别 | 免密钥默认实现 | 其他可选实现 |
| --- | --- | --- |
| 对话/推理模型 `llm` | `mock`（抽取式确定性推理，能完整跑通问答/本体/Agent/Wiki） | 通义千问(百炼)、OpenAI、DeepSeek、Kimi、智谱 GLM、火山豆包、自建 vLLM、本机 Ollama（均为 OpenAI 兼容） |
| 向量化 `embedding` | `local_hash`（特征哈希，384 维） | `openai_compat`（任意 OpenAI 兼容 embeddings） |
| 向量数据库 `vector_store` | `sqlite_numpy`（SQLite 存向量 + numpy 算分） | `pgvector`、`memory` |
| 对象存储 `object_store` | `local_fs`（`var/objects`） | `s3`（阿里云 OSS / MinIO / S3 兼容） |
| 文档格式 `doc_parser` | `default_multi_format`：txt/md/csv/tsv/json/jsonl/html/xml/pdf/docx/xlsx/log | — |
| 数据源 `data_source` | 按 URI 前缀自动路由：`local://`、`file://`、`sqlite://`、`http(s)://`、`mysql+pymysql://` | file / sqlite / http_json / jdbc 四个适配器 |
| 网络搜索 `web_search` | `none`（关闭） | `duckduckgo`（免密钥）、`tavily`、`bocha`、`serper` |
| IM 渠道 `im_channel` | `none`（通知打到服务日志） | `dingtalk`、`feishu`、`wecom`、`slack` |
| 重排序 `rerank` | `heuristic` | `llm`、`cross_encoder` |
| 部署目标 `deploy` | `local` | `docker`、`k8s`、`serverless`、`edge` |

两种切换方式：① UI「组件中心 → 设为生效」（写入 `var/components.json`，重启后仍生效）；
② 环境变量 `FINONTO_*`（见 `server/.env.example`）。密钥永远只回显「是否已配置」。

**接真实模型的最小改动**（以百炼为例）：

```bash
cp server/.env.example server/.env
# 编辑 server/.env
FINONTO_LLM_PROVIDER=openai_compat:dashscope
FINONTO_LLM_API_KEY=sk-xxxx
FINONTO_LLM_MODEL=qwen-plus
FINONTO_EMBEDDING_PROVIDER=openai_compat
FINONTO_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
FINONTO_EMBEDDING_API_KEY=sk-xxxx
FINONTO_EMBEDDING_DIM=1024
FINONTO_RERANK_PROVIDER=llm
```

Embedding 换实现后请到「文档入库」逐篇「重建索引」（维度/语义空间不同，不能混用旧向量）。
列表型配置（`FINONTO_CORS_ORIGINS`）必须写成 JSON 数组。

---

## 8. 验收与自检

```bash
cd server
.venv/Scripts/python.exe scripts/smoke.py         # 服务层端到端：10 个阶段 / 89 项断言
.venv/Scripts/python.exe scripts/http_smoke.py    # HTTP 层：页面 + API + SSE / 87 项检查
.venv/Scripts/python.exe scripts/auth_smoke.py    # 安全边界：认证/角色/审计/限流/备份 / 69 项
.venv/Scripts/python.exe scripts/preflight.py     # 上线前 11 项检查（--strict 更严格）
cd .. && python -m pytest tests -q                # 单元测试：42 项
```

`smoke.py` 覆盖：五个领域包幂等冷启动 → 各领域语料规模核对 → RAG（必须产生引用）→
ReAct（必须收敛并产出提案）→ 人工确认（类型 +1、唯一 IRI、触发重编译、重复确认被拒）→
校验三级结构 → Wiki 编译/编辑/锁定/回滚/diff（并验证回滚后内容与目标版本一致）→
图谱（全图/子图/路径/洞察）→ **多领域交叉验证**（维保/AI/网络各自检索、词表随空间切换、导出产物）→
组件面板与沙箱（拦截 `os` 导入并可解释）。

`http_smoke.py` 用 `TestClient` 打真实接口（以默认管理员登录后执行），包含未登录 401、
12 个页面 200、SSE 帧数与 content-type、沙箱越权拦截、MCP 目录与直连调用、密钥不回显、
领域包安装幂等与未知领域包 404、非法导出格式 422、空文件 400、相同内容重复入库等。

`auth_smoke.py` 专测安全边界：未登录跳转、错误密码、**强制首登改密**、弱密码拒绝、
四级角色的允许/拒绝矩阵、`role_denied` 审计、审计 CSV、备份 zip、诊断无 fail 项、限流 429。

`preflight.py` 把「上线前必须确认的事」做成可执行检查（11 项，退出码可接 CI），
与页面 `/ops` 和 `GET /api/system/diagnostics` 共用同一份逻辑。

当前状态：**smoke 89/89、http 87/87、auth 69/69、pytest 42/42，共 287 项检查全绿**。

---

## 9. 部署形态

| 形态 | 怎么做 | 说明 |
| --- | --- | --- |
| 本机开发 | `scripts\dev.ps1` / `scripts/dev.sh` | 单进程，可选 `--reload` |
| 单机容器 | `docker compose up -d` | 单阶段镜像，只暴露 8000；数据落 `./data/finonto` |
| 单机 + pgvector | `docker compose --profile pgvector up -d` 并设 `FINONTO_VECTOR_STORE=pgvector` | 需自行 `pip install "psycopg[binary]"` |
| Kubernetes | `kubectl apply -f deploy/k8s/finonto.yaml` | Deployment + Service + PVC，`/api/health` 作探针；`Recreate` 避免抢写 SQLite |
| 内网隔离（零外呼） | 全部 provider 用本地实现，模型走自建 `openai_compat:vllm` / `ollama`，网搜与 IM 置 `none` | 数据不出网；`mock` 本身无任何外部调用 |
| Serverless | `FINONTO_DATABASE_URL` 指向外部 Postgres、`FINONTO_OBJECT_STORE=s3`、`FINONTO_DATA_DIR` 挂 NAS | 沙箱需要可执行子进程，受限运行时置 `FINONTO_SANDBOX_ENABLED=false` |
| 反向代理 | 参考 `deploy/nginx.conf` | **SSE 必须关闭 `proxy_buffering`**，否则 token 会被攒着下发 |

### 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `scripts\dev.ps1` 报 `Unexpected token` | 脚本是 UTF-8 带 BOM 保存的；若你手工改过并另存为无 BOM，Windows PowerShell 5.1 会把中文注释读坏。用 VS Code 重新保存为「UTF-8 with BOM」，或改用 `pwsh`（PowerShell 7）。 |
| 端口被占用 | `powershell -File scripts\dev.ps1 -Port 8080` |
| `python -m venv` 失败 / 没有 pip | 脚本会自动回退到系统 Python；也可手工 `pip install -r server\requirements.txt` 后用 `python -m uvicorn app.main:app`。 |
| 改了代码没生效 | `uvicorn --reload` 在 Windows 上偶尔不换 worker，直接重启进程。 |
| 页面正常但流式回答没有逐字出现 | 反向代理缓冲了 SSE，见 `deploy/nginx.conf` 的 `proxy_buffering off`。 |
| 换 embedding 后检索变差 | 旧向量维度/语义空间已失效，到「文档入库」逐篇「重建索引」。 |

---

## 10. 扩展指南

- **加一个领域（最常用）**：三步，服务层/接口/前端都不用改。
  1. 在 `server/app/core/ontology/packs/` 新建 `medical.py`，声明
     `PACK = DomainPack(key, label, summary, space_name, space_iri, space_description,
     business_domains, object_types, links, samples=[SampleDoc(…)], term_suffixes, clause_markers,
     domain_cues, tag_cues, synonyms, standards, classification, facets, sample_questions, agent_hint)`；
  2. 示例语料放进 `server/data/samples/medical/`，文件名与 `SampleDoc.filename` 对齐；
  3. 在 `packs/__init__.py` 的 `PACKS` 里加一行 `medical.PACK`。
  然后 `POST /api/ontology/packs/medical/install`（或重启，冷启动会装入全部领域包），
  该空间的词表、检索扩词、草案子域、Agent 提示随之切换。只想试概念时把
  `object_types`/`links` 留空即可（参考 `general.py`）。
- **加一个模型厂商**：OpenAI 兼容的，在 `providers/llm.py` 的 `VENDORS` 加一条
  `{key, display, base_url, model}` 即出现在组件中心；非兼容协议则实现 `providers/base.py`
  的 `LLMProvider`（`chat` / `chat_stream` / `info`），再在 `registry._build_llm()` 分支返回。
- **加一个向量库/对象存储/IM/网搜/数据源**：按 `providers/base.py` 对应 `Protocol` 实现，
  在 registry 注册目录项与构造函数；`DataSource` 加一条 URI 前缀匹配即可。
- **加一个 Agent 工具**：在 `core/agent/tools.py` 的 `_register_builtin()` 里调用
  `R.register(name, description, parameters, handler, category=…, risk=…)`；handler 只返回结构化
  observation。若它会改本体，请让它产出提案（`risk="write"`）而不是直接写库。
- **加一个技能**：在 `core/agent/skills.py` 的 `SKILLS` 里追加一个 `Skill(...)`。
- **加一类 Wiki 页面**：在 `core/wiki/compiler.py` 增加 builder 与 slug 策略，增量编译与版本链自动适用。
- **加一个页签**：`templates/<x>.html` + `web/pages.py` 一个路由 + `web/render.py` 的 `NAV` 一行。

---

## 11. 已知限制

- 默认 `mock` LLM 是抽取式启发实现：术语抽取用后缀线索 + 定义句式 + 标记过滤，能避免跨词截断，
  但仍可能产出偏短语化的候选名，因此设计了人工确认闸门。
- 向量召回为 numpy 暴力算分，适合十万级片段；更大规模请换 `pgvector`。
- PDF/DOCX 解析为基础版（文本层提取、无版面还原），扫描件需要外部 OCR。
- SQLite 为单写者模型，适合单机；多副本部署请用外部 Postgres 并把副本数设为 1
  （多副本还会导致会话表、限流窗口各算各的，需一并迁移到共享存储）。
- **账号是内置的**：无 LDAP / SSO / MFA，也没有多租户隔离（按「一个部门一套实例」部署）。
  要接企业目录，优先在网关层做 OIDC，再把 `auth_service.resolve_session` 换成网关注入的身份。
- **限流是进程内的**：单进程足够，多副本需换 Redis（切换点只有 `security.RateLimiter`）。
- **审计不防篡改**：有脱敏与保留策略，但没有 WORM 存储或签名链；强合规场景需把 CSV 定期归档到不可变存储。
- **无字段级加密**：数据目录里的 SQLite 与对象文件是明文，请依赖磁盘/卷加密。
- 前端零框架的代价：复杂表格交互（拖拽排序、虚拟滚动）需要自己写；本项目用「服务端过滤 + 分页片段」规避。



