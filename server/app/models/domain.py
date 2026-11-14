"""领域模型：19 张业务表 + 1 张向量背书表。

设计原则
--------
1. **一切按空间隔离**：类型、属性、关联、约束、提案、Wiki、图谱都挂 ``space_id``，
   所以「加一个领域」不需要动任何表结构（见 docs/ARCHITECTURE.md「领域包」一节）。
2. **结构只能通过提案改变**：``ObjectType``/``TypeLink``/``Constraint`` 没有对外的写接口，
   唯一入口是 ``core.ontology.service.accept_proposal``。
3. **版本不可变**：``WikiRevision`` 只追加不修改；回滚 = 追加一个新版本。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
# 1. 本体空间
# --------------------------------------------------------------------------- #
class OntologySpace(TimestampMixin, Base):
    __tablename__ = "ontology_spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    iri: Mapped[str] = mapped_column(String(300), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    #: 领域包 key —— 「领域如何生效」的唯一挂载点
    domain: Mapped[str] = mapped_column(String(60), index=True, default="general")
    status: Mapped[str] = mapped_column(String(20), default="active")

    object_types: Mapped[list["ObjectType"]] = relationship(
        back_populates="space", cascade="all, delete-orphan", passive_deletes=True
    )


# --------------------------------------------------------------------------- #
# 2. 对象类型
# --------------------------------------------------------------------------- #
class ObjectType(TimestampMixin, Base):
    __tablename__ = "object_types"
    __table_args__ = (UniqueConstraint("space_id", "label", name="uq_object_type_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(120))
    label_en: Mapped[str] = mapped_column(String(160), default="")
    definition: Mapped[str] = mapped_column(Text, default="")
    #: class / agreement / role / document / metric / event / organization / asset / process
    kind: Mapped[str] = mapped_column(String(40), default="class")
    is_abstract: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_types.id", ondelete="SET NULL"), nullable=True
    )
    business_domain: Mapped[str] = mapped_column(String(80), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    #: pack / rag / agent / manual
    source: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="active")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)
    iri: Mapped[str] = mapped_column(String(300), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    space: Mapped[OntologySpace] = relationship(back_populates="object_types")
    parent: Mapped["ObjectType | None"] = relationship(remote_side="ObjectType.id")


# --------------------------------------------------------------------------- #
# 3-4. 数据属性与「类型 ↔ 属性」绑定
# --------------------------------------------------------------------------- #
class DataPropertyDef(TimestampMixin, Base):
    __tablename__ = "data_properties"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_data_property_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(120))
    #: string / integer / decimal / boolean / date / datetime / uri / enum
    data_type: Mapped[str] = mapped_column(String(20), default="string")
    unit: Mapped[str] = mapped_column(String(40), default="")
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    enum_values: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")


class TypeDataProperty(TimestampMixin, Base):
    __tablename__ = "type_data_properties"
    __table_args__ = (UniqueConstraint("type_id", "property_id", name="uq_type_prop"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("data_properties.id", ondelete="CASCADE"), index=True
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- #
# 5-6. 关联类型与类型间连线
# --------------------------------------------------------------------------- #
class RelationType(TimestampMixin, Base):
    __tablename__ = "relation_types"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_relation_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(120))
    #: taxonomy / participant / reference / derivation / regulatory / part_of / causal
    category: Mapped[str] = mapped_column(String(40), default="reference")
    inverse_key: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class TypeLink(TimestampMixin, Base):
    __tablename__ = "type_links"
    __table_args__ = (
        UniqueConstraint("source_type_id", "target_type_id", "relation_id", name="uq_type_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    source_type_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    target_type_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    relation_id: Mapped[int] = mapped_column(
        ForeignKey("relation_types.id", ondelete="CASCADE"), index=True
    )
    cardinality: Mapped[str] = mapped_column(String(20), default="1..*")
    label: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- #
# 7. SHACL 风格约束
# --------------------------------------------------------------------------- #
class Constraint(TimestampMixin, Base):
    __tablename__ = "constraints"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    type_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_properties.id", ondelete="SET NULL"), nullable=True
    )
    #: range / codependency / cardinality
    kind: Mapped[str] = mapped_column(String(30), default="range")
    #: violation / warning / info
    severity: Mapped[str] = mapped_column(String(20), default="violation")
    expression: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    regulation_ref: Mapped[str] = mapped_column(String(160), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- #
# 8. 证据链
# --------------------------------------------------------------------------- #
class Evidence(TimestampMixin, Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_proposals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True
    )
    #: 「文档 §3.2 第 2 段」这类人类可读定位
    locator: Mapped[str] = mapped_column(String(300), default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")

    proposal: Mapped["ChangeProposal | None"] = relationship(back_populates="evidence")


# --------------------------------------------------------------------------- #
# 9. 变更提案（人工确认闸门）
# --------------------------------------------------------------------------- #
class ChangeProposal(TimestampMixin, Base):
    __tablename__ = "change_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    #: create_type / update_type / create_link / create_constraint / update_property
    action: Mapped[str] = mapped_column(String(40), default="create_type")
    target_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_types.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    #: pending / accepted / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    #: rag / agent / manual
    author: Mapped[str] = mapped_column(String(20), default="rag")
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(60), default="")
    decision_comment: Mapped[str] = mapped_column(Text, default="")

    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", passive_deletes=True
    )


# --------------------------------------------------------------------------- #
# 10-11. 文档与片段
# --------------------------------------------------------------------------- #
class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300), default="")
    mime: Mapped[str] = mapped_column(String(120), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    language: Mapped[str] = mapped_column(String(20), default="zh")
    #: 文种：制度 / 标准 / 合同 / 手册 / 论文 / 规程
    doc_type: Mapped[str] = mapped_column(String(40), default="制度")
    issuer: Mapped[str] = mapped_column(String(160), default="")
    jurisdiction: Mapped[str] = mapped_column(String(80), default="")
    #: 权威度 1-5，检索时的结构化过滤维度
    authority: Mapped[int] = mapped_column(Integer, default=3)
    source_uri: Mapped[str] = mapped_column(String(500), default="")
    object_key: Mapped[str] = mapped_column(String(300), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="indexed")

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(TimestampMixin, Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    locator: Mapped[str] = mapped_column(String(300), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class ChunkVector(Base):
    """向量库背书表：``sqlite_numpy`` 实现把向量存成 float32 BLOB。"""

    __tablename__ = "chunk_vectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), unique=True, index=True
    )
    dim: Mapped[int] = mapped_column(Integer, default=384)
    model: Mapped[str] = mapped_column(String(120), default="local_hash")
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# 12-13. Wiki 页面与版本链
# --------------------------------------------------------------------------- #
class WikiPage(TimestampMixin, Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("space_id", "slug", name="uq_wiki_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(200), index=True)
    #: index / type / domain / relations / quality
    page_type: Mapped[str] = mapped_column(String(30), default="type")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="published")
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"
    __table_args__ = (UniqueConstraint("page_id", "version", name="uq_revision_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    #: 内容摘要，用于快速判断「有没有真变化」
    digest: Mapped[str] = mapped_column(String(64), default="")
    #: compiler / manual / rollback
    author: Mapped[str] = mapped_column(String(20), default="compiler")
    message: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# 14. 问答轨迹
# --------------------------------------------------------------------------- #
class QueryTrace(Base):
    __tablename__ = "query_traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(40), default="general")
    sub_questions: Mapped[list] = mapped_column(JSON, default=list)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(80), default="mock")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# 15-16. Agent 运行与步骤
# --------------------------------------------------------------------------- #
class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    skill: Mapped[str] = mapped_column(String(60), default="doc-extraction")
    goal: Mapped[str] = mapped_column(Text, default="")
    #: running / succeeded / failed / budget_exceeded
    status: Mapped[str] = mapped_column(String(30), default="running")
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    proposal_ids: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    thought: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(80), default="")
    action_input: Mapped[dict] = mapped_column(JSON, default=dict)
    observation: Mapped[dict] = mapped_column(JSON, default=dict)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- #
# 17. 长期记忆
# --------------------------------------------------------------------------- #
class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    #: fact / preference / insight
    kind: Mapped[str] = mapped_column(String(20), default="fact")
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(String(300), default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- #
# 18. MCP 服务登记
# --------------------------------------------------------------------------- #
class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    #: inproc / stdio / http
    transport: Mapped[str] = mapped_column(String(20), default="inproc")
    command: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(300), default="")
    tools: Mapped[list] = mapped_column(JSON, default=list)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# 19. 沙箱运行记录
# --------------------------------------------------------------------------- #
class SandboxRun(Base):
    __tablename__ = "sandbox_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(
        ForeignKey("ontology_spaces.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# 20. 账号与会话（企业落地：认证 / 授权 / 会话吊销）
# --------------------------------------------------------------------------- #
class User(TimestampMixin, Base):
    """用户与角色。角色只有四级，够用且好解释（见 security.ROLE_LEVELS）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    #: viewer / editor / approver / admin
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SessionToken(Base):
    """会话令牌：库里只存 SHA-256 指纹，明文只在登录响应里出现一次。"""

    __tablename__ = "session_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip: Mapped[str] = mapped_column(String(60), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# 21. 审计日志（谁、在什么时间、对什么、做了什么、结果如何）
# --------------------------------------------------------------------------- #
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60), default="system", index=True)
    role: Mapped[str] = mapped_column(String(20), default="")
    #: login / logout / accept_proposal / create_type / edit_wiki / rollback / ingest / delete / activate_component / backup ...
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    #: ok / denied / failed
    status: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(60), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    request_id: Mapped[str] = mapped_column(String(40), default="", index=True)


__all__ = [
    "Base",
    "utcnow",
    "OntologySpace",
    "ObjectType",
    "DataPropertyDef",
    "TypeDataProperty",
    "RelationType",
    "TypeLink",
    "Constraint",
    "Evidence",
    "ChangeProposal",
    "Document",
    "DocumentChunk",
    "ChunkVector",
    "WikiPage",
    "WikiRevision",
    "QueryTrace",
    "AgentRun",
    "AgentStep",
    "Memory",
    "MCPServer",
    "SandboxRun",
    "User",
    "SessionToken",
    "AuditLog",
]
