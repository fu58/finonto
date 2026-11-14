# 企业落地手册（运维 / 安全 / 合规）

> 面向「要把这套东西装进内网、交给别人用、出事能复盘」的场景。
> 读完你应该能回答四个问题：**谁能做什么**、**上线前检查什么**、**坏了怎么恢复**、**出问题怎么定位**。

---

## 1. 角色与权限矩阵

四级角色，数值越大权限越高（`security.ROLE_LEVELS`）。权限在**两层**校验：
中间件按路径拦一次（`middleware.WRITE_RULES`），接口内用 `require_role()` 再拦一次。
页面隐藏按钮只是体验优化，**不作为安全边界**。

| 操作 | 路径 / 接口 | 只读 viewer | 编辑 editor | 审批 approver | 管理员 admin |
| --- | --- | :-: | :-: | :-: | :-: |
| 问答、Agent 流式推理 | `GET /api/rag/*`、`/api/agent/stream` | ✅ | ✅ | ✅ | ✅ |
| 浏览本体 / Wiki / 图谱 | `GET /ontology` `/wiki` `/graph` | ✅ | ✅ | ✅ | ✅ |
| 导出本体（Turtle/SKOS/JSON） | `GET /api/ontology/{id}/export` | ✅ | ✅ | ✅ | ✅ |
| 文档入库 / 删除 / 重建索引 | `POST /documents*` | ❌ | ✅ | ✅ | ✅ |
| 手工建类型 / 改定义 / 删类型 | `POST /ontology/*` | ❌ | ✅ | ✅ | ✅ |
| Wiki 编辑 / 锁定 / 回滚 / 编译 | `POST /wiki/*` | ❌ | ✅ | ✅ | ✅ |
| 生成草案（问答驱动） | `POST /api/ontology/{id}/draft` | ❌ | ✅ | ✅ | ✅ |
| **确认 / 驳回提案** | `POST /proposals/{id}/accept` | ❌ | ❌ | ✅ | ✅ |
| 查看审计日志 / 导出 CSV | `GET /audit`、`/api/audit*` | ❌ | ❌ | ✅ | ✅ |
| 安装领域包 | `POST /packs/*/install` | ❌ | ❌ | ❌ | ✅ |
| 切换组件 / 探针 | `/components`、`/api/components*` | ❌ | ❌ | ❌ | ✅ |
| 用户与角色管理 | `/users`、`/api/auth/users*` | ❌ | ❌ | ❌ | ✅ |
| 运维页面 / 诊断 / 备份 | `/ops`、`/api/system/*` | ❌ | ❌ | ❌ | ✅ |
| 清理审计 | `POST /api/audit/purge` | ❌ | ❌ | ❌ | ✅ |

设计取舍：

* **编辑者不能确认自己的提案** —— 提案确认统一给 approver，形成「产出」与「采纳」的职责分离；
* **回滚也是编辑者**，但每次都写审计（谁在什么时候把哪个页面退回到哪一版）；
* **最后一个管理员不可降级/停用/删除**（服务层强制），避免把自己锁在门外。

---

## 2. 认证与会话

| 项 | 现状 | 配置 |
| --- | --- | --- |
| 账号来源 | 内置用户表（无 LDAP/SSO） | 需要对接时在外层网关做 OIDC，或改 `auth_service` |
| 密码存储 | `scrypt`（n=16384, r=8, p=1）+ 随机盐 | `security.hash_password` |
| 初始管理员 | `admin` / `admin123`（**强制首登改密**） | `FINONTO_ADMIN_USERNAME` / `FINONTO_ADMIN_PASSWORD` |
| 会话 | 服务端会话表 + HttpOnly Cookie（SameSite=Lax） | `FINONTO_SESSION_TTL_HOURS`（默认 12） |
| 会话吊销 | 改密 / 停用 / 删除用户 / 主动退出即时吊销 | `revoke_user_sessions` |
| 登录防爆破 | 失败计数 + 临时锁定 | `FINONTO_AUTH_LOGIN_MAX_ATTEMPTS` / `FINONTO_AUTH_LOCK_MINUTES` |
| CSRF | SameSite=Lax + Origin/Host 比对 | `middleware.handle` |
| HTTPS 场景 | Cookie 加 `Secure` | `FINONTO_COOKIE_SECURE=true` |

两种模式：

```bash
# 生产（默认）：必须登录
FINONTO_AUTH_ENABLED=true

# 演示 / 单机离线：所有请求按 admin 处理，页面顶部有明显「演示模式」横幅
FINONTO_AUTH_ENABLED=false
```

> 关闭认证只适合「本机 + 不对外暴露」。一旦有人能访问端口，就等于把本体写权限交给所有人。

---

## 3. 审计日志

记录字段：`时间 / 操作人 / 角色 / 动作 / 对象 / 结果 / IP / request-id / 详情`。

* **脱敏**：字段名含 `password / secret / token / api_key / webhook` 一律写 `***`，字符串截断 400 字；
* **旁路**：`audit.record()` 内部吞掉所有异常，审计失败不影响业务（但会打 warning 日志）；
* **归因**：任何一条业务日志都能用 `request-id` 关联到访问日志与审计记录；
* **保留**：默认 180 天（`FINONTO_AUDIT_RETENTION_DAYS`），清理动作本身也会被记录；
* **导出**：`GET /api/audit/export.csv`（审批者以上），字段为中文，可直接进合规报表。

已覆盖的动作（`core/audit.ACTIONS`）：
登录 / 登录失败 / 退出 / 改密 / 用户增删改 / 重置密码 / 确认提案 / 驳回提案 / 新建·修改·删除类型 /
文档入库 / 删除文档 / 重建索引 / 编辑 Wiki / 锁定 / 回滚 / 编译 / 安装领域包 / 切换组件 /
备份 / 清理审计 / 导出本体 / 沙箱执行 / 越权被拒（`role_denied`）。

---

## 4. 上线检查单（必做）

```bash
cd server
.venv/Scripts/python.exe scripts/preflight.py --strict
```

11 项检查，任何 `fail` 都会让退出码非 0：

| 检查 | 通过标准 | 常见不通过原因 |
| --- | --- | --- |
| Python 版本 | ≥ 3.11 | 用了系统自带 3.9 |
| 运行依赖 | fastapi / sqlalchemy / jinja2 / numpy / uvicorn 齐全 | venv 没装全 |
| 数据目录可写 | 能创建并删除探针文件，剩余空间 > 1GB | 目录权限 / 磁盘满 |
| 数据库连接 | 能查询对象类型表 | 路径错、被占用 |
| 向量维度一致 | 向量库维度 == 当前 embedding 维度 | 换过 embedding 没重建索引 |
| 认证与初始密码 | 至少一个启用管理员；无人仍用初始密码 | 还没登录改密 |
| 审计日志 | 有记录（登录一次后） | 认证被关闭 |
| 示例语料 | 领域包语料文件齐全 | 部署时漏拷 `data/samples` |
| 沙箱 | 白名单代码放行、`import os` 被拦 | 沙箱被改坏 |
| 内容规模 | 仅为提示（文档/片段/待编译/待确认提案计数） | — |
| 密钥与外部依赖 | mock 零外呼，或已配置密钥且在安全白名单内 | 误开外呼 |

人工确认清单（脚本查不到的）：

- [ ] 管理员初始密码已改，`admin123` 不再可用
- [ ] 服务端口只对需要的网段开放（或前置网关鉴权）
- [ ] `FINONTO_COOKIE_SECURE=true`（HTTPS 场景）
- [ ] 备份计划已配置（见第 5 节），并**实际恢复演练过一次**
- [ ] 监控已抓取 `/api/metrics`，`/api/ready` 已接到负载均衡健康检查
- [ ] 日志已接入集中采集（stdout 是 JSON 行，带 request-id）
- [ ] 审计 CSV 导出给合规方确认过字段

---

## 5. 备份与恢复

```bash
cd server
python scripts/preflight.py            # 先确认数据目录可写
.venv/Scripts/python.exe scripts/backup.py --note "上线前基线" --keep 14
.venv/Scripts/python.exe scripts/backup.py --list
```

备份内容：`finonto.db`（SQLite 在线备份 API，WAL 下一致）+ `objects/` + `components.json`
+ `RESTORE.md` + `manifest.json`。落在 `server/var/backups/`。

**恢复（必须停服）**

1. `mv server/var server/var.before-restore`
2. 把备份包解压到 `server/var`
3. 启动服务 → `/api/ready` 返回 `ready`
4. 抽查 `/api/spaces` 数量、Wiki 版本号连续性
5. 如果换过 embedding，逐篇「重建索引」

计划任务示例（Windows）：

```powershell
schtasks /Create /SC DAILY /TN "FinOnto-Backup" /ST 02:00 /TR ^
  "C:\path\to\server\.venv\Scripts\python.exe C:\path\to\server\scripts\backup.py --keep 14"
```

> 只备份不演练 = 没有备份。第一次上线前请完整走一遍恢复流程。

---

## 6. 可观测性

| 手段 | 入口 | 说明 |
| --- | --- | --- |
| 健康检查（存活） | `GET /api/health` | 进程活着就 200 |
| 就绪检查（依赖） | `GET /api/ready` | 数据库 + 向量库不可用则 503 语义（`status=not_ready`） |
| Prometheus 指标 | `GET /api/metrics` | 请求数、5xx、慢请求、延迟、按路径模板计数，以及文档/类型/页面/待确认提案等业务 gauge |
| 诊断 | `GET /api/system/diagnostics`（admin）或 `/ops` 页面 | 11 项检查 + 运行时配置 + 会话统计 + 指标快照 |
| 访问日志 | stdout | 每行 JSON：`request_id / method / path / status / ms / user / role / ip / ua` |
| 指标命名 | `finonto_*` | 换 `prometheus_client` 只需改 `observability.py` |

抓取示例（prometheus.yml）：

```yaml
scrape_configs:
  - job_name: finonto
    metrics_path: /api/metrics
    static_configs:
      - targets: ["finonto.internal:80"]
```

> `/api/metrics` 是公开的（Prometheus 通常不便带会话）。它只暴露计数与规模，不含任何用户、文档内容
> 或密钥。若不合规，可在网关层限制来源网段。

---

## 7. 限流与容量

* 默认 **120 次 / 分钟 / 用户**（未登录按 IP），只作用于 `/api/rag/*` 与 `/api/agent/*`；
* 超限返回 `429` + `Retry-After`，并写一条 `denied` 审计；
* 健康检查、静态资源、页面渲染不受限；
* 限流是**进程内滑动窗口**：多副本时需要改成 Redis（`security.RateLimiter` 是唯一切换点）。

容量参考（本机实测，单进程 SQLite）：检索 ~3ms、问答 ~10ms（mock）、Agent 6 步 ~30ms、
Wiki 全量编译 87 页 ~120ms。瓶颈通常在真实 LLM 的延迟，而不是本服务。

---

## 8. 故障排查速查

| 症状 | 先看什么 | 处理 |
| --- | --- | --- |
| 503 / not_ready | `/api/ready` 的 `checks` | 数据库或向量库不可用 |
| 401 循环跳登录 | Cookie 是否被代理丢掉；`FINONTO_COOKIE_SECURE` 与协议是否匹配 | HTTPS 下开 Secure，HTTP 下必须关 |
| 403 且审计有 `role_denied` | 当前账号角色 vs `WRITE_RULES` | 提权或换账号 |
| 登录一直失败 | 审计 `login_failed` 的 `reason` | `locked` 说明触发锁定，等 15 分钟或管理员重置 |
| 改了密码还是旧密码能登 | 是否多副本共享同一 SQLite | 单副本或换 Postgres |
| 流式回答一次性出现 | 反向代理缓冲 | `proxy_buffering off`（见 `deploy/nginx.conf`） |
| 备份失败 | `/ops` 页面或 `backup.py` 报错 | 磁盘空间、目录权限、SQLite 被独占 |
| 检索结果突然变差 | `preflight` 的向量维度检查 | 换 embedding 后重建索引 |
| 页面 500 且日志有 request-id | 用 request-id 检索访问日志与堆栈 | 异常已被中间件捕获并记录 |

---

## 9. 合规与数据边界

* **数据不出网**：默认 `mock / local_hash / sqlite_numpy / local_fs / none`，全部零外呼；
* **密钥不回显**：接口只返回「是否已配置」，`.env` 不入库、不进备份包（备份只含 `components.json` 与数据）；
* **审计可导出**：CSV 字段固定，便于对接合规系统；
* **删除即删除**：删文档会同时删片段、向量与对象存储文件（`delete_document`），审计留痕；
* **无自建邮件通道**：忘记密码由管理员重置，减少一条对外攻击面；
* **已知边界**：无 LDAP/SSO、无多租户隔离（按「一个部门一套实例」部署）、无字段级加密。
  需要时优先在网关层补齐，而不是把复杂度塞进单体应用。

---

## 10. 与「个人项目」的差异清单（自检）

| 维度 | 之前 | 现在 |
| --- | --- | --- |
| 身份 | 无账号 | 用户表 + 会话 + 四级角色 + 强制改密 |
| 授权 | 无 | 路径级 + 接口级双层校验，越权写审计 |
| 审计 | 无 | 22 类动作、脱敏、CSV 导出、保留策略 |
| 可观测 | 只有 /api/health | /api/ready、/api/metrics、request-id 贯穿日志、诊断 11 项 |
| 防滥用 | 无 | 按用户/IP 限流 + Retry-After |
| 幂等 | 部分 | 领域包安装、文档入库、重复确认提案、重复建用户全部幂等 |
| 备份 | 手工拷贝 | 在线备份 API + 恢复步骤 + 保留策略 |
| 上线 | 手感 | 可执行 preflight + 人工检查单 |
| 测试 | 2 个脚本 | 4 套（服务层 89 / HTTP 87 / 安全 69 / 单测 42） |
