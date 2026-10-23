"""领域数据模型：本体空间、对象类型、属性、关系、约束、变更提案、文档、Agent、Wiki、记忆。"""

from __future__ import annotations

from datetime import datetime, timezone

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

from ..db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Space(Base):
    """一个本体空间（ontology space），例如「零售信贷」「设备维保」「计算机网络」。

    domain 存放所属领域包的 key，用于把词表、提示词与推理线索定位到正确的领域。
    """

    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    iri: Mapped[str] = mapped_column(String(256), default="")
    domain: Mapped[str] = mapped_column(String(64), default="general", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    object_types: Mapped[list[ObjectType]] = relationship(
        back_populates="space", cascade="all, delete-orphan"
    )


class ObjectType(Base):
    """OWL 风格 owl:Class —— 本体对象类型。"""

    __tablename__ = "object_types"
    __table_args__ = (UniqueConstraint("space_id", "iri", name="uq_object_type_iri"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    iri: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(128), index=True)
    label_en: Mapped[str] = mapped_column(String(128), default="")
    plural_label: Mapped[str] = mapped_column(String(128), default="")
    definition: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="class")
    abstract: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    origin: Mapped[str] = mapped_column(String(24), default="manual")
    business_domain: Mapped[str] = mapped_column(String(64), default="", index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    # 领域扩展注解：标准/条款引用、数据分级、风险维度、标识体系、生命周期等，取值随领域包变化
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    space: Mapped[Space] = relationship(back_populates="object_types")
    data_props: Mapped[list["TypeDataProperty"]] = relationship(
        back_populates="object_type", cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        return self.label


class DataPropertyDef(Base):
    """owl:DatatypeProperty —— 可复用的数据类型定义。"""

    __tablename__ = "data_property_defs"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    iri: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(128))
    datatype: Mapped[str] = mapped_column(String(32), default="string")
    unit: Mapped[str] = mapped_column(String(32), default="")
    pattern: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    enum_id: Mapped[int | None] = mapped_column(ForeignKey("enumerations.id", ondelete="SET NULL"))
    format_ref: Mapped[str] = mapped_column(String(64), default="")
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)


class TypeDataProperty(Base):
    """对象类型上的数据属性槽位（domain 绑定）。"""

    __tablename__ = "type_data_properties"
    __table_args__ = (UniqueConstraint("object_type_id", "property_id", name="uq_type_dataprop"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    object_type_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("data_property_defs.id", ondelete="CASCADE"), index=True
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    unique_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    min_card: Mapped[int] = mapped_column(Integer, default=0)
    max_card: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    key_identifier: Mapped[bool] = mapped_column(Boolean, default=False)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)

    object_type: Mapped[ObjectType] = relationship(back_populates="data_props")
    property: Mapped[DataPropertyDef] = relationship()


class RelationType(Base):
    """owl:ObjectProperty —— 对象类型之间的关联语义。"""

    __tablename__ = "relation_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    iri: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(128))
    label_en: Mapped[str] = mapped_column(String(128), default="")
    inverse_iri: Mapped[str] = mapped_column(String(256), default="")
    symmetric: Mapped[bool] = mapped_column(Boolean, default=False)
    transitive: Mapped[bool] = mapped_column(Boolean, default=False)
    functional: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="association")
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)


class TypeLink(Base):
    """被断言的关联（RelationType 在两个具体对象类型上的实例化）= 知识图谱的边。"""

    __tablename__ = "type_links"
    __table_args__ = (
        UniqueConstraint("source_id", "relation_id", "target_id", name="uq_type_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), index=True
    )
    relation_id: Mapped[int] = mapped_column(ForeignKey("relation_types.id", ondelete="CASCADE"))
    cardinality: Mapped[str] = mapped_column(String(16), default="n:m")
    description: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(24), default="manual")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="active")
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[ObjectType] = relationship(foreign_keys=[source_id])
    target: Mapped[ObjectType] = relationship(foreign_keys=[target_id])
    relation: Mapped[RelationType] = relationship()


class Enumeration(Base):
    __tablename__ = "enumerations"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    iri: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(128))
    members: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")


class Constraint(Base):
    """SHACL 风格约束，用于表达合规义务、SLA 与运维阈值等硬性规则。"""

    __tablename__ = "constraints"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    subject_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_types.id", ondelete="CASCADE"), nullable=True, index=True
    )
    subject_property_iri: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(32), default="range")
    expression: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="violation")
    reg_ref: Mapped[str] = mapped_column(String(160), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("spaces.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(256))
    source_uri: Mapped[str] = mapped_column(String(512), default="")
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    media_type: Mapped[str] = mapped_column(String(96), default="")
    doc_format: Mapped[str] = mapped_column(String(32), default="txt")
    doc_type: Mapped[str] = mapped_column(String(48), default="")
    jurisdiction: Mapped[str] = mapped_column(String(32), default="")
    issuer: Mapped[str] = mapped_column(String(128), default="")
    effective_date: Mapped[str] = mapped_column(String(32), default="")
    authority: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    space_id: Mapped[int | None] = mapped_column(Integer, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    locator: Mapped[str] = mapped_column(String(64), default="")
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    vector_model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[DocumentRecord] = relationship(back_populates="chunks")


class ChangeProposal(Base):
    """Agent / 问答生成的本体变更草稿，等待人工确认后才落库。"""

    __tablename__ = "change_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), default="object_type")
    title: Mapped[str] = mapped_column(String(256), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    review_note: Mapped[str] = mapped_column(Text, default="")
    applied_ref: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    evidences: Mapped[list["Evidence"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )


class Evidence(Base):
    """证据链：提案字段 -> 文档 chunk 的原文引用。"""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_proposals.id", ondelete="CASCADE"), index=True
    )
    answer_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    chunk_id: Mapped[int | None] = mapped_column(ForeignKey("chunks.id", ondelete="SET NULL"))
    doc_id: Mapped[int | None] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(64), default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    field_path: Mapped[str] = mapped_column(String(128), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)

    proposal: Mapped["ChangeProposal | None"] = relationship(back_populates="evidences")
    chunk: Mapped["Chunk | None"] = relationship()


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("spaces.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(256), default="")
    skill: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="SET NULL")
    )
    space_id: Mapped[int | None] = mapped_column(Integer, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(24), default="react")
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    final_answer: Mapped[str] = mapped_column(Text, default="")
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    thought: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(128), default="")
    action_input: Mapped[dict] = mapped_column(JSON, default=dict)
    observation: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="ok")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryItem(Base):
    """长期记忆：跨会话可召回的事实/偏好/模式/决策。"""

    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), default="global", index=True)
    kind: Mapped[str] = mapped_column(String(24), default="fact")
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    salience: Mapped[float] = mapped_column(Float, default=1.0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source_run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WikiPage(Base):
    __tablename__ = "wiki_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(191), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    page_type: Mapped[str] = mapped_column(String(32), default="auto")
    object_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_types.id", ondelete="SET NULL"), nullable=True
    )
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    locked_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    revisions: Mapped[list["WikiRevision"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="WikiRevision.version_no"
    )


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    body_md: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(64), default="system")
    change_note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    page: Mapped[WikiPage] = relationship(back_populates="revisions")


class QueryTrace(Base):
    """一次 RAG 问答的完整可观测记录。"""

    __tablename__ = "query_traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    space_id: Mapped[int | None] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(Text)
    understood: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieval: Mapped[list] = mapped_column(JSON, default=list)
    rerank: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
