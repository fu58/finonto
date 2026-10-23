# FinOnto · 多领域本体生成平台

以 **RAG 问答** 与 **Agent 推理** 为主要入口，引导式地从文档与制度条款中生成领域本体
（对象类型 / 数据属性 / 关联 / SHACL 约束 / 证据链），自动编译成可编辑、可版本回滚的
Wiki 与知识图谱。领域不写死在代码里：**金融、设备维保、人工智能、计算机网络** 各自是一个
可插拔的「领域包」（Domain Pack），新增一个领域只需放一个模块。所有外部依赖（模型厂商、
Embedding、向量库、对象存储、文档格式、数据源、网络搜索、IM、重排序、部署目标）都是可插拔
组件；**不配任何密钥时全部落到本地可跑实现**，一条命令即可端到端演示。

技术栈：FastAPI + SQLAlchemy 2.0 + SQLite(WAL) ｜ React 18 + Vite 5 + TypeScript（客户端只有
浏览器，无 UI/图表/Markdown 三方库，SVG 力导向图与 Markdown 渲染均为内置实现）。

---

## 1. 三十秒跑起来（零密钥）

环境：Python 3.11+、Node 18+。

```bash
# macOS / Linux
bash scripts/dev.sh

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

或者手动分两个终端：

```bash
# ① 后端
cd server
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt            # macOS / Linux
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ② 前端（/api 已代理到 8000）
cd client
npm install
npm run dev            # 打开 http://localhost:5173
```

首次启动会自动完成冷启动（`FINONTO_AUTO_BOOTSTRAP=true`）：把 **全部内置领域包** 逐个装进
各自的本体空间（金融 23 / 设备维保 24 / 人工智能 20 / 计算机网络 23 个基线对象类型，外加一个
不预设领域的「通用本体域」，合计 5 个空间 / 90 个类型）→ 解析并入库 10 份示例语料（44 个片段）
→ 构建 BM25 + 向量双索引 → 首次编译 Wiki（136 页）。之后打开浏览器直接可用；只想装某几个领域
时把 `FINONTO_AUTO_BOOTSTRAP` 关掉，用 `POST /api/system/bootstrap?pack_keys=network,ai` 或在
「领域包」页签里点安装。

**单端口模式**：`cd client && npm run build` 生成 `client/dist` 后，FastAPI 会自动托管它，
只访问 `http://127.0.0.1:8000` 即可（Docker / 生产部署走这条路径）。

> 注意：`uvicorn --reload` 在 Windows 上偶尔不会真正换掉 worker，改动没生效时请重启进程。

---

## 2. 主流程：从一句话到本体里的对象类型

| 页签 | 做什么 | 关键接口 |
| --- | --- | --- |
| 领域包 | 领域包画廊（基线类型数 / 子域 / 标准 / 示例语料可用性 / 已装空间）、一键幂等安装到空间、合并示例清单 | `GET /api/ontology/packs`、`POST /api/ontology/packs/{key}/install`、`GET /api/system/packs`、`GET /api/system/samples` |
| 问答 · RAG | 流式问答 + 可点击引用 `[c1]`，查询理解芯片（意图/子问题/过滤条件/已有类型对齐），一键「按本题生成本体草案」 | `GET /api/rag/stream`、`POST /api/ontology/{space}/draft`、`POST …/draft/propose` |
| Agent 推理 | 选技能 → ReAct 多步推理，实时展示 Thought/Action/Observation、token 与步数预算；可运行沙箱代码、管理 MCP 服务与长期记忆 | `GET /api/agent/stream`、`POST /api/agent/sandbox`、`/api/agent/mcp/*`、`/api/agent/memory/*` |
| 本体工作室 | 对象类型检索/筛选、手工新建、详情（数据属性/关联/约束/证据链）、内联改定义（版本 +1）、空间校验、删除 | `/api/ontology/{space}/types`、`PATCH /api/ontology/types/{id}`、`GET …/validate` |
| 提案审核 | 所有写入都必须在这里人工确认；确认后立即写本体 + 增量重编译 Wiki + IM 通知 | `POST /api/ontology/proposals/{id}/accept`、`…/reject` |
| Wiki 与版本 | 页面列表（按类型/关键词/dirty 过滤）、全量或增量编译、人工编辑与锁定、版本链 diff（并排/行内）、非破坏式回滚 | `/api/wiki/pages*`、`…/revisions`、`…/diff`、`POST …/rollback` |
| 知识图谱 | 全图 / 邻域子图 / 两点路径三种模式，节点详情与邻居关系，图谱洞察（枢纽、域分布、缺证据类型、孤立节点） | `/api/wiki/graph`、`…/subgraph`、`…/path`、`…/insights` |
| 文档入库 | 上传（含文种/发布机构/辖区/权威度元数据）、纯文本入库、切块预览、片段查看、重建索引、删除 | `/api/documents*` |
| 组件中心 | 10 类组件目录与切换、连通性探针、健康检查、IM 试发、网搜试调、数据源结构探测、部署清单 | `/api/components*` |

一条被验证过的完整链路（`scripts/smoke.py` 与浏览器都跑通过）：

```
文档解析 → 分块 → 嵌入 → BM25+向量双索引
   → 查询理解 → 混合检索(RRF) → 重排 → MMR 去冗 → 流式生成 → 引用绑定
   → ReAct（rag.search → doc.list → ontology.list_types → find_similar → propose_type → wiki.draft_page）
   → ChangeProposal + Evidence
   → 人工确认 accept → ObjectType/Link/Constraint 落库（版本 +1）
   → 增量重编译 Wiki（dirty 页面）→ IM 通知
   → 人工编辑/锁定 → 回滚（追加新版本，不删历史）→ 图谱与路径查询
```

**人工确认是硬闸门**：RAG 与 Agent 只能产出提案（`change_proposals`）与证据（`evidence`），
没有任何自动写本体的旁路；`accept_proposal` 是唯一改变本体结构（并触发 Wiki 编译）的入口。

---

## 3. 目录结构

```
server/
  app/
    main.py               # 建表 → 幂等冷启动 → 路由 → 托管 client/dist
    config.py             # FINONTO_* 环境变量（pydantic-settings）
    models/domain.py      # 19 张表：空间/类型/属性/关联/约束/提案/证据/文档/片段/页面/版本/运行/步骤/记忆…
    db.py                 # SQLAlchemy 引擎与 session_scope
    core/
      text.py             # 中英混合分词、句子切分、术语抽取（含边界与句式过滤）
      rag/                # query_understanding / bm25 / retriever(混合+图感知+MMR) / reranker / generator / citations / ingest / chunking / pipeline
      agent/              # react / tools(25 个内置) / skills(6 个技能包) / mcp_client / sandbox / memory
      ontology/           # service(提案确认与唯一 IRI) / kinds / owl(Turtle·SKOS·JSON Schema) / packs(领域包：base/install/finance/maintenance/ai/network/general)
      wiki/               # compiler(增量) / versions(回滚与 diff) / graph(投影与洞察)
    providers/            # 组件适配层：llm / embedding / vectorstore / objectstore / docparser / datasource / search / im / registry
    api/                  # system / ontology / rag / ingest / agent / wiki / components / sse
  data/samples/<pack>/    # 随领域包发布的示例语料（finance / maintenance / ai / network 共 10 份）
  scripts/smoke.py        # 端到端离线冒烟（10 个阶段，含多领域交叉验证）
  scripts/http_smoke.py   # HTTP 层冒烟（TestClient，61 项检查）
client/
  src/App.tsx             # 9 个页签 + 全局空间/刷新上下文
  src/api.ts              # fetch 封装 + streamSSE 异步生成器
  src/views/*.tsx         # PacksView / ChatView / AgentView / OntologyView / ProposalsView / WikiView / GraphView / DocumentsView / ComponentsView
  src/Markdown.tsx        # 零依赖 Markdown 渲染（含 [[slug]] 链接）
docker-compose.yml  Dockerfile  deploy/k8s/finonto.yaml  scripts/dev.sh|dev.ps1
```

运行期数据都在 `server/var/`（SQLite、对象存储、上传件、沙箱工作区、`components.json`
组件选择状态），已被 `.gitignore` 排除。

---

## 4. 能力实现要点

### 4.1 RAG 问答
- **查询理解**（`core/rag/query_understanding.py`）：规则 + 可选 LLM 混合，产出意图分类
  （definition / quantitative / comparison / ontology_generation / general）、同义扩展、
  子问题拆分、结构化过滤（文种/辖区/最低权威度）以及与已有对象类型的对齐结果。
- **混合检索**（`retriever.py`）：BM25（自实现，`bm25.py`）+ 向量召回并行，RRF 或加权融合；
  随后重复折叠 → 同文档配额 → 图感知扩展（命中已有类型则加权，并补邻域片段）→ MMR 多样化。
- **重排序**（`reranker.py`）：`heuristic`（默认，零依赖）/ `llm` pointwise / `cross_encoder`，
  接口相同，按 `.env` 或组件中心切换。
- **流式生成与引用**（`generator.py` + `citations.py`）：上下文块带 `[cN]` 标记，生成时逐 token
  推送；结束时把答案中出现的 `[cN]` 绑定成引用列表（文档名、定位器、原文引句、页码、得分），
  未被引用的候选不会伪装成来源；`answer_id` 与完整检索轨迹写入 `query_traces` 便于复盘。

### 4.2 Agent 推理
- **ReAct**（`react.py`）：Thought→Action→Observation 循环，步数与 token 预算可配
  （`FINONTO_AGENT_MAX_STEPS` / `AGENT_STEP_TOKEN_BUDGET`），每次运行的步骤、提案、结论落库，
  可在 UI 回放；支持 SSE 流式与非流式两种调用。
- **工具面**：25 个内置工具（`rag.*`、`doc.*`、`ontology.*`、`graph.*`、`wiki.*`、`memory.*`、
  `sandbox.run_python`、`search.*`、`datasource.*`），每个带风险分级
  `read / write / exec / network`；Agent 写本体的唯一方式是产出提案。
- **技能包**（`skills.py`）：`doc-extraction`、`clause-mapping`、`relation-discovery`、
  `def-refine`、`graph-completion`、`web-research` —— 系统提示 + 工具白名单 +
  输出契约 + 步数上限；技能本身领域中立，领域词表由当前空间的领域包注入。
- **MCP**：`inproc` / `stdio` / `http` 三种传输；内置 3 个进程内服务
  （`finonto-kb`、`finonto-ontology`、`finonto-export`，共 8 个工具），可在 UI 增删、启停、直连调用。
- **沙箱**（`sandbox.py`）：独立子进程 + AST 白名单（只允许 `json/math/statistics/re/datetime/…`）+
  超时 + 输出上限 + 禁止越权导入；`POST /api/agent/sandbox/validate` 可先看拦截结果。
- **长期记忆**（`memory.py`）：条目化存储 + 关键词召回 + 运行后自动归纳
  （`consolidate`）、时间衰减（`decay`）与清理（`prune`）。

### 4.3 本体模型
OWL 风格骨架 + 金融领域扩展：`ObjectType`（class/agreement/role/…、抽象类、父子分类、
业务域、置信度、来源、状态、别名、版本）→ `DataPropertyDef`/`TypeDataProperty`（数据类型、
单位、必填、枚举）→ `RelationType`/`TypeLink`（taxonomy / participant / reference / derivation /
regulatory / part_of / causal）→ `Constraint`（SHACL 风格：range / codependency / cardinality，
带 severity 与监管文号）→ `Evidence`（文档、片段、定位器、引句、提案号）。
- **唯一 IRI**：中文标签派生的局部名可能撞车（两个概念都映射到 `Loan`），此时追加数字后缀
  （`Loan2`）而不是覆盖既有类，保持「同 IRI = 同类」。
- **查重**：`ontology.find_similar` 与 `/api/ontology/{space}/similar` 用别名 + 术语重叠度
  提示疑似重复，避免同义建类。
- **校验**：`validate_space` 产出 `violation / warning / info` 三级问题（无定义、无属性、
  未分域、约束表达式不可解析、循环父子等），新建类型时同步返回。
- **导出**：`GET /api/ontology/{space}/export` 返回 Turtle、SKOS、逐类型 JSON Schema 等产物。

### 4.4 自动 Wiki 与知识图谱
- 编译器按五类页面生成 Markdown：索引页 `index`、类型页 `type-<id>`、业务域页
  `domain-<域>`、关联语义目录、本体质量报告；正文带来源注释与 `[[slug]]` 链接。提案确认后只
  重编译 dirty 页面（`only_dirty`），人工锁定页跳过。
- 版本服务：每次编译或人工编辑追加一条 `wiki_revisions`（含 digest）；`diff` 给行级 +
  并排两种视图；`rollback` **追加**新版本而不是删历史，从而保住可审计性。
- 图谱投影（`graph.py`）：节点 `t:<type_id>` / `p:<prop_id>` / `d:<doc_id>`，边分
  taxonomy / association / attribute / evidence 四类；提供全图、邻域子图、两点最短路径
  与洞察（枢纽排名、域分布、无证据类型、孤立节点、连通分量、密度、平均度）。

### 4.5 多领域与领域包
- **挂载点是 `Space.domain`**：每个本体空间属于一个领域包，类型、属性、关联、约束、提案、
  Wiki、图谱、导出本来就按 `space_id` 隔离，所以扩领域不需要任何表结构变更。
- **`DomainPack`**（`core/ontology/packs/base.py`）把一个领域的东西打包声明：空间名/IRI/描述、
  `business_domains` 受控子域、基线 `object_types` 与 `links`、随包发布的 `samples`、词表扩展
  （`extra_kinds` / `extra_relation_categories` / `facets` / `classification` / `standards`）、
  离线抽取线索（`term_suffixes` / `clause_markers` / `domain_cues` / `tag_cues` / `synonyms`）、
  `sample_questions`（问答页签的起手问题，切空间即换）与 `agent_hint`。内置 finance / maintenance /
  ai / network 四个真领域 + `general` 空白域（新建空间与「认不出 domain」时的回落，不预设任何词表）。
- **领域如何生效**：`vocab_for(space_id)` 给前端受控词表；`lexicon_for(space_id)` 把同义扩展表与
  术语后缀交给查询理解；`prompt_context()` 注入草案服务与 ReAct 的系统提示，`OntologyDraftService._align`
  再把模型给出的子域/标签收敛回受控取值；Mock LLM 直接解析提示里的「子域线索 / 标签线索 / 术语后缀」，
  所以零密钥路径也跟随当前领域。
- **安装即幂等**：`install_pack` = 建空间 → 灌基线本体 → 示例语料入库 → 编译 Wiki，反复执行只会跳过
  已存在的类型与同名文档；`GET /api/ontology/packs` 返回画廊（是否已装、空间统计、示例文件可用性）。

---

## 5. 组件矩阵与切换

组件中心按类别列出全部实现，并区分「免密钥可用」与「需要配置」：

| 类别 | 免密钥默认实现 | 其他可选实现 |
| --- | --- | --- |
| 对话/推理模型 `llm` | `mock`（抽取式确定性推理，能完整跑通问答/本体/Agent/Wiki） | 通义千问(百炼)、OpenAI、DeepSeek、Kimi、智谱 GLM、火山豆包、自建 vLLM、本机 Ollama（均为 OpenAI 兼容） |
| 向量化 `embedding` | `local_hash`（特征哈希，384 维） | `openai_compat`（任意 OpenAI 兼容 embeddings） |
| 向量数据库 `vector_store` | `sqlite_numpy`（SQLite 存向量 + numpy 算分） | `pgvector`、`dashvector`、`memory` |
| 对象存储 `object_store` | `local_fs`（`var/objects`） | `oss`（阿里云 OSS / S3 兼容） |
| 文档格式 `doc_parser` | `default_multi_format`：txt/md/csv/tsv/json/jsonl/html/xml/pdf/docx/xlsx/log，按扩展名自动路由 | — |
| 数据源 `data_source` | 按 URI 前缀自动路由：`local://`、`file://`、`sqlite://`、`http(s)://`、`mysql+pymysql://` 等 | file / sqlite / http_json / jdbc 四个适配器 |
| 网络搜索 `web_search` | `none`（关闭） | `duckduckgo`（免密钥）、`tavily`、`bocha`、`serper` |
| IM 渠道 `im_channel` | `none`（通知打到服务日志） | `dingtalk`、`feishu`、`wecom`、`slack` |
| 重排序 `rerank` | `heuristic` | `llm`、`cross_encoder` |
| 部署目标 `deploy` | `local` | `docker`、`k8s`、`serverless`、`edge` |

两种切换方式：① UI「组件中心 → 设为生效」（写入 `var/components.json`，重启后仍生效）；
② 环境变量 `FINONTO_*`（见 `server/.env.example`，`cp server/.env.example server/.env`）。
密钥永远只回显「是否已配置」，接口不会把密钥原文返回给浏览器。

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
FINONTO_RERANK_PROVIDER=llm
```

Embedding 换实现后请到「文档入库」逐篇「重建索引」（维度/语义空间不同，不能混用旧向量）。

列表型配置（`FINONTO_CORS_ORIGINS`）必须写成 JSON 数组，逗号分隔会让服务启动报错；
`.env.example` 的每个键都与 `Settings` 字段一一对应，可直接照抄。

---

## 6. 验收与自检

```bash
cd server
.venv/Scripts/python.exe scripts/smoke.py         # 离线端到端：10 个阶段，全链路断言
.venv/Scripts/python.exe scripts/http_smoke.py    # HTTP 层：前端要调的每条链路，62 项检查
```

`smoke.py` 覆盖：五个领域包幂等冷启动 → 各领域语料规模核对 → RAG（必须产生引用）→ ReAct（必须收敛并产出提案）→
人工确认 → 校验 → Wiki 编译/编辑/锁定/回滚/diff/幂等 → 图谱（全图/子图/路径/洞察）→
**多领域交叉验证**（维保 / 人工智能 / 计算机网络各自检索、词表随空间切换、导出产物数）→
Turtle 导出 → 组件面板。`http_smoke.py` 用 `TestClient` 打真实接口，包含沙箱越权导入被
拦截、SSE 帧数、MCP 目录、密钥不回显、领域包安装幂等与未知领域包 400/404 等安全项。
当前两者均全绿（http 62/62）。

浏览器侧已人工验证：RAG 流式回答与可点击引用、问答驱动草案卡、ReAct 8 步运行与提案、
确认写入 + IM 通知、Wiki 编辑→回滚→diff、图谱三种模式与洞察、文档预览/入库/重建索引/删除、
组件激活持久化与 8 类探针、本体工作室新建/内联改定义/校验/删除。多领域部分：顶栏切到
「计算机网络与运维」空间后，问答页签的起手问题、本体工作室的受控词表与摘要、图谱投影
（23 节点 / 35 关系）与导出都随之切换；「领域包」页签的五个卡片显示各自的基线类型数、
子域、标准数、示例语料与已装空间统计。

---

## 7. 部署形态

| 形态 | 怎么做 | 说明 |
| --- | --- | --- |
| 本机开发 | `scripts/dev.sh` / `scripts\dev.ps1` | 后端 8000 + Vite 5173，`/api` 走代理 |
| 单机容器 | `docker compose up -d` | 多阶段镜像：前端产物由 FastAPI 托管，只暴露 8000；数据落在 `./data/finonto` |
| 单机 + pgvector | `docker compose --profile pgvector up -d` 并设置 `FINONTO_VECTOR_STORE=pgvector`、`FINONTO_DATABASE_URL=postgresql+psycopg://…` | 需自行 `pip install "psycopg[binary]"`（镜像默认未装） |
| Kubernetes | `kubectl apply -f deploy/k8s/finonto.yaml` | Deployment + Service + PVC，`/api/health` 作就绪/存活探针；`strategy: Recreate` 避免两个副本抢写同一份 SQLite |
| 内网隔离（零外呼） | 全部 provider 用本地实现，模型走自建 `openai_compat:vllm` / `ollama`，网搜与 IM 置 `none` | 数据不出网；`mock` 实现本身就无任何外部调用 |
| Serverless / 函数计算 | 把 `FINONTO_DATABASE_URL` 指向外部 Postgres、`FINONTO_OBJECT_STORE=oss`、`FINONTO_DATA_DIR` 挂 NAS | Agent 沙箱需要可执行子进程，受限运行时请置 `FINONTO_SANDBOX_ENABLED=false` |

---

## 8. 扩展指南

- **加一个领域（最常用）**：三步，服务层/接口/前端都不用改。
  1. 在 `server/app/core/ontology/packs/` 新建 `medical.py`，声明
     `PACK = DomainPack(key, label, summary, space_name, space_iri, space_description,
     business_domains, object_types, links, samples=[SampleDoc(…)], term_suffixes, clause_markers,
     domain_cues, tag_cues, synonyms, standards, classification, facets, sample_questions, agent_hint)`；
     `object_types` 的字段与「本体工作室」新建类型一致（`label/label_en/definition/kind/parent_label/
     aliases/tags/annotations/properties/relations/constraints`）。
  2. 把示例语料放进 `server/data/samples/<key>/`，文件名与 `SampleDoc.filename` 对齐
     （文件不在盘上时，画廊的示例列表会标「文件缺失」，安装自动跳过）。
  3. 在 `packs/__init__.py` 的 `PACKS` 里加一行 `medical.PACK`。
  然后 `POST /api/ontology/packs/medical/install`（或重启，冷启动会装入全部领域包）、
  前端「领域包」页签点安装，该空间的词表、检索扩词、草案子域、Agent 提示随之切换。
  只想试概念而不写基线类型时，把 `object_types`/`links` 留空即可（参考 `general.py`）。
- **加一个模型厂商**：无需写代码——在 `providers/registry.py` 的 `LLM_VENDORS` 里加一条
  `{key, display, base_url, model}` 即可出现在组件中心；非 OpenAI 兼容协议则实现
  `providers/base.py` 的 `LLMProvider` 协议（`chat` / `chat_stream` / `info`），再在
  `registry.build_llm()` 里按 `FINONTO_LLM_PROVIDER` 分支返回。
- **加一个向量库/对象存储/IM/网搜/数据源**：同样按 `providers/base.py` 中对应
  `Protocol`（`VectorStore`、`ObjectStore`、`IMChannel`、`WebSearch`、`DataSource`）实现，
  在 registry 注册目录项与构造函数；`DataSource` 通过 URI 前缀自动路由，加一条前缀匹配即可。
- **加一个 Agent 工具**：在 `core/agent/tools.py` 的 `_register_builtin()` 里调用
  `R.register(name, description, parameters, handler, category=…, risk=…)`；handler 只返回结构化
  observation。若它会改本体，请让它产出提案（`risk="write"`）而不是直接写库。
- **加一个技能**：在 `core/agent/skills.py` 的 `SKILLS` 列表追加
  `Skill(name, display, description, system_prompt, allowed_tools, output_contract, max_steps)`。
- **加一类 Wiki 页面**：在 `core/wiki/compiler.py` 的页面生成规则里增加 page_type 与 slug 策略，
  增量编译与版本链会自动适用。

---

## 9. 已知限制

- 默认 `mock` LLM 是抽取式启发实现：术语抽取用中文 n-gram + 边界助词 + 句式标记过滤，能避免
  跨词截断片段（如「款人的风险状况」），但仍可能产出偏短语化的候选名，因此设计了人工确认闸门。
- 向量召回为 numpy 暴力算分，适合十万级片段；更大规模请换 `pgvector` / `dashvector`。
- PDF/DOCX 解析为基础版（文本层提取、无版面还原），扫描件需要外部 OCR。
- SQLite 为单写者模型，适合单机；多副本部署请使用外部 Postgres 并把副本数设为 1 或换 PVC(RWO) 方案。
- 未包含账号体系：所有操作按「本机可信用户」假设，接入企业环境请在外层加网关鉴权后再暴露。
