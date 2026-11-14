# 架构说明

> 配套：[TECH_STACK.md](TECH_STACK.md)（为什么这样选）、[LEARNING.md](LEARNING.md)（要学什么）。

---

## 1. 分层与依赖方向

```
浏览器（HTML + CSS + 543 行自写 JS）
   │  GET 整页 / POST 表单 / fetch 片段 / EventSource 收 SSE
   ▼
middleware.py ── request-id → 会话解析 → 认证闸门 → CSRF → 路径级授权 → 限流 → 指标/日志
   │
   ├─ web/pages.py + web/pages_auth.py ── Jinja2 渲染，表单成功后 303 重定向（无 JS 也能用）
   └─ api/*.py ── JSON + SSE 接口（前端增强路径、外部系统集成路径）
   │
   ▼
core/  ── 业务逻辑，唯一可信层
   ├ rag/       查询理解 → 混合检索 → 重排 → 生成 → 引用
   ├ agent/     ReAct / 工具 / 技能 / 沙箱 / 记忆 / MCP
   ├ ontology/  领域包 / 提案 / 唯一 IRI / 校验 / 导出
   ├ wiki/      编译 / 版本 / 图谱
   ├ audit.py      审计（旁路，永不阻断业务）
   ├ auth_service.py 账号与会话
   ├ backup.py     在线备份（SQLite backup API + zip）
   └ diagnostics.py 11 项自检（/api/ready、/ops、preflight 共用）
   │
   ▼
providers/ ── 10 类组件的协议与实现（本地实现为零密钥默认值）
   │
   ▼
models/ + db.py ── SQLAlchemy 2.0 / SQLite(WAL) / 23 张表
```

**依赖只能向下**：`core` 不 import `api` 或 `web`；`providers` 不 import `core`（唯一的例外是
`core.text` 的分词工具，因为它纯函数、无状态）。这条规矩让小工具（脚本、定时任务）可以只依赖
`core + providers` 就跑起来。

**中间件顺序不是随意的**：request-id 必须最先（后续日志与审计都靠它串联）；
授权必须在限流之前（越权请求不该占用配额，更不该被记成「流量过大」）；
限流必须在 `call_next` 之前（否则昂贵接口已经跑完才被拒）。

---

## 2. 一条链路上的六个关键决策点

```
文档入库
  parse(filename, bytes) → ParsedDocument(text, pages, warnings)
    · doc_parser 按扩展名路由；PDF 无文本层时 warnings 会进库
  chunk_text(text, size, overlap) → [Chunk(text, locator, tokens)]
    · 按标题/「第X条」分段，句子边界切分，尾部重叠
  embedding.embed(texts) → [[float]]
  vector_store.upsert([(chunk_id, vector)])
  bm25 索引缓存失效（retriever.invalidate_cache）

提问
  query_understanding.understand(...)
    · intent / 同义扩展 / 子问题 / 过滤条件 / 与已有类型对齐
  retriever.retrieve(...)
    · BM25(40) + 向量(40) → RRF(k=60) → 图感知加权 → 同文档配额 → MMR
  reranker.rerank(...)          · heuristic：覆盖度+密度+权威度，每分可解释
  generator.build_messages(...) · 【问题】【意图】【领域】【资料】小节
  llm.chat_stream(...)          · 逐 token
  citations.bind(answer, tags)  · 只保留答案真的引用过的片段
  QueryTrace 落库

提案
  draft_proposals(...) → N × ChangeProposal + Evidence（status=pending）
  accept_proposal(...)  ← 唯一写本体入口
    ├ 落库 ObjectType/属性/约束/关联（版本 +1，唯一 IRI）
    ├ 相关 Wiki 页置 dirty
    ├ 增量重编译（跳过 locked）
    └ IM 通知（失败不影响主流程）
```

---

## 3. 数据模型（20 张表）

| # | 表 | 作用 | 关键约束 |
| --- | --- | --- | --- |
| 1 | `ontology_spaces` | 本体空间，`domain` 挂领域包 | `key`/`iri` 唯一 |
| 2 | `object_types` | 对象类型 | `(space_id,label)` 唯一、`iri` 唯一 |
| 3 | `data_properties` | 数据属性定义 | `(space_id,key)` 唯一 |
| 4 | `type_data_properties` | 类型↔属性 | `(type_id,property_id)` 唯一 |
| 5 | `relation_types` | 关联语义 | `(space_id,key)` 唯一 |
| 6 | `type_links` | 类型间关联 | `(source,target,relation)` 唯一 |
| 7 | `constraints` | SHACL 风格约束 | 带 `severity` 与 `regulation_ref` |
| 8 | `evidence` | 证据链 | 指向文档/片段，`note`=类型标签 |
| 9 | `change_proposals` | 提案（人工闸门） | `status` 三态，`decided_at/by` |
| 10 | `documents` | 文档元数据 | `sha256` 去重 |
| 11 | `document_chunks` | 片段 | `(document_id, ordinal)` |
| 12 | `chunk_vectors` | 向量背书 | `chunk_id` 唯一，float32 BLOB |
| 13 | `wiki_pages` | Wiki 页面 | `(space_id,slug)` 唯一 |
| 14 | `wiki_revisions` | 版本链（只追加） | `(page_id,version)` 唯一 |
| 15 | `query_traces` | 问答轨迹 | 含检索信号，用于复盘 |
| 16 | `agent_runs` | Agent 运行 | 步数/token/提案 id |
| 17 | `agent_steps` | 单步 Thought/Action/Observation | `run_id` 级联删除 |
| 18 | `memories` | 长期记忆 | 权重+命中数，支持衰减 |
| 19 | `mcp_servers` | MCP 服务登记 | `name` 唯一 |
| 20 | `sandbox_runs` | 沙箱执行记录 | 保留 stdout/stderr |
| 21 | `users` | 账号与角色 | `username` 唯一，密码 scrypt |
| 22 | `session_tokens` | 会话（只存 SHA-256 指纹） | 过期/吊销/用户停用三重校验 |
| 23 | `audit_logs` | 审计日志 | 22 类动作，按 request-id 串联 |

设计取舍：

* **一切按 `space_id` 隔离**，所以「加领域」不需要改表结构；
* **版本不可变**：`wiki_revisions` 只插入；回滚 = 追加新版本；
* **提案是唯一写入口**：`object_types` 等表没有对外的写接口；
* **会话只存指纹**：库泄露 ≠ 会话泄露；改密/停用/删除用户会级联吊销。

---

## 4. 扩展点（按改动成本排序）

| 想做 | 改哪里 | 成本 |
| --- | --- | --- |
| 加一个领域 | `core/ontology/packs/<new>.py` + `data/samples/<new>/` + `packs/__init__.py` 一行 | 只写数据 |
| 加一个模型厂商 | `providers/llm.py` 的 `VENDORS` 加一条（OpenAI 兼容）；非兼容则实现 `LLMProvider` | 10 行 |
| 加一个向量库 | 实现 `VectorStore` 协议 + registry 注册 | 1 个类 |
| 加一个 Agent 工具 | `core/agent/tools.py` 里 `R.register(...)` | 10 行 |
| 加一个技能 | `core/agent/skills.py` 的 `SKILLS` 追加 | 1 个 dataclass |
| 加一类 Wiki 页面 | `core/wiki/compiler.py` 加 builder + `PAGE_TYPES` | 1 个函数 |
| 加一个页签 | `templates/<x>.html` + `web/pages.py` 一个路由 + `render.NAV` 一行 | 2 个文件 |

---

## 5. 安全边界

| 边界 | 措施 | 位置 |
| --- | --- | --- |
| 写本体 | 只能经 `accept_proposal`，且必须人工点确认 | `core/ontology/service.py` |
| 身份认证 | 服务端会话 + HttpOnly Cookie + 失效三重校验 | `middleware.py`、`core/auth_service.py` |
| 越权访问 | 路径级规则（中间件）+ 接口级 `require_role()` 双层校验 | `middleware.py`、`api/*` |
| 密码爆破 | 失败计数 + 临时锁定 + 强口令规则 + scrypt 加盐 | `security.py` |
| CSRF | SameSite=Lax + Origin/Host 比对 | `middleware.py` |
| 滥用与配额 | 按用户/IP 的滑动窗口限流（只限昂贵接口） | `security.RateLimiter` |
| 归因与追溯 | 审计旁路记录 + request-id 贯穿访问日志 | `core/audit.py` |
| Agent 越权调用工具 | 技能白名单校验，越权返回结构化错误 | `core/agent/react.py` |
| 代码执行 | AST 白名单 + 子进程 + 超时 + 输出上限 + 包列表禁读 `os/sys/socket` | `core/agent/sandbox.py` |
| XSS | 所有输出先 HTML 转义再放行白名单标签；Markdown 渲染屏蔽 `javascript:` | `web/markdown.py` |
| SQL 注入 | 全部走 SQLAlchemy 参数化；唯一的动态表名做了 `isalnum()` 白名单 | `providers/datasource.py` |
| 密钥泄露 | 接口只回显布尔；`.env` 不入库、不进备份；审计对密钥字段写 `***` | `config.py`、`core/audit.py` |
| 上传体积 | `FINONTO_MAX_UPLOAD_MB` 硬限制，超限 413 | `api/ingest.py` |
| 重复入库 | 同空间同 `sha256` 直接跳过 | `core/rag/ingest.py` |

---

## 6. 性能画像（本机实测，单进程 SQLite）

| 操作 | 规模 | 耗时 |
| --- | --- | --- |
| 冷启动装 5 个领域包（50 类型 / 8 文档 / 87 页） | — | ~1.2s |
| BM25 + 向量检索（RRF+MMR） | 100 片段 | ~3ms |
| 单次问答（mock，含检索与引用） | 8 片段上下文 | ~10ms |
| Agent 6 步（每步一次检索/工具） | — | ~30ms |
| Wiki 全量编译 | 87 页 | ~120ms |
| 图谱布局（180 次迭代） | 50 节点 | ~15ms |

瓶颈与对策：SQLite 单写者 → 换 Postgres；numpy 暴力向量 → 换 pgvector；
mock 抽取式 → 换真实模型（质量上限，不是速度问题）。

---

## 7. 与「传统」实现的差异（为什么这样组织）

1. **API 与页面共用服务层**：页面处理器直接调 `core`，不绕自己的 HTTP 接口——
   少一次序列化，也避免「内部调用还要拼 URL」。
2. **SSE 而不是 WebSocket**：单向推送 + 自动重连 + 代理友好；代价是不能中途发消息（本项目不需要）。
3. **图谱布局在服务端**：确定性坐标让「图谱快照可进版本」，浏览器只做 pan/zoom。
4. **模板里不做业务判断**：所有 `if` 都是展示层分支；数据准备在 `web/pages.py`。
5. **零三方前端库**：交互协议收敛成 4 个 `data-*` 属性和 150 行 `ui.js`，
   降级路径（原生表单提交）始终可用。


