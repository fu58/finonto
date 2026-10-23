"""本体读写服务：对象类型、属性、关联、约束、变更提案的确认与落库。

Agent / 问答链路只能产出 ChangeProposal（草稿）；人工确认后由本服务写入正式本体，
并把受影响 Wiki 页标记为 dirty 以触发增量重编译。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...models import (
    ChangeProposal,
    Constraint,
    DataPropertyDef,
    DocumentRecord,
    Enumeration,
    Evidence,
    ObjectType,
    RelationType,
    Space,
    TypeDataProperty,
    TypeLink,
    WikiPage,
)

# 中文术语 -> 英文 IRI 局部名：通用词 + 各内置领域包高频词，用于生成可读的 Turtle 局部名
GLOSSARY: dict[str, str] = {
    "客户": "Customer",
    "个人客户": "IndividualCustomer",
    "对公客户": "CorporateCustomer",
    "产品": "Product",
    "合约": "Contract",
    "协议": "Agreement",
    "条款": "Clause",
    "账户": "Account",
    "交易": "Transaction",
    "支付": "Payment",
    "清算": "Clearing",
    "结算": "Settlement",
    "收单": "Acquiring",
    "贷款": "Loan",
    "授信": "Credit",
    "额度": "Quota",
    "质押": "Pledge",
    "抵押": "Mortgage",
    "担保": "Guarantee",
    "评级": "Rating",
    "限额": "Limit",
    "逾期": "Delinquency",
    "违约": "Default",
    "不良": "NonPerforming",
    "保单": "Policy",
    "理赔": "Claim",
    "承保": "Underwriting",
    "保费": "Premium",
    "基金": "Fund",
    "资管": "AssetManagement",
    "净值": "NetAssetValue",
    "份额": "Share",
    "组合": "Portfolio",
    "利率": "InterestRate",
    "费率": "FeeRate",
    "期限": "Tenor",
    "金额": "Amount",
    "币种": "Currency",
    "敞口": "Exposure",
    "风险": "Risk",
    "信用风险": "CreditRisk",
    "市场风险": "MarketRisk",
    "操作风险": "OperationalRisk",
    "合规": "Compliance",
    "监管": "Regulation",
    "反洗钱": "AntiMoneyLaundering",
    "信息披露": "Disclosure",
    "机构": "Institution",
    "对手方": "Counterparty",
    "银行": "Bank",
    "事件": "Event",
    "凭证": "Voucher",
    "单据": "Document",
    "指标": "Metric",
    "模型": "Model",
    "参数": "Parameter",
    "要素": "Element",
    "分类": "Category",
    "标准": "Standard",
    "资产": "Asset",
    "负债": "Liability",
    "权益": "Equity",
    "市场": "Market",
    "债券": "Bond",
    "票据": "Note",
    "抵押品": "Collateral",
    "日期": "Date",
    "状态": "Status",
    "类型": "Type",
    "编号": "Id",
    "名称": "Name",
    # 设备维保
    "设备": "Equipment",
    "部件": "Part",
    "零件": "Component",
    "工单": "WorkOrder",
    "检修": "Overhaul",
    "维护": "Maintenance",
    "保养": "Upkeep",
    "点检": "Inspection",
    "巡检": "Patrol",
    "润滑": "Lubrication",
    "故障": "Failure",
    "失效": "Invalidation",
    "缺陷": "Defect",
    "备件": "SparePart",
    "库存": "Inventory",
    "仓储": "Warehouse",
    "物料": "Material",
    "规程": "Procedure",
    "作业": "Task",
    "班组": "Crew",
    "人员": "Person",
    "技师": "Technician",
    "资质": "Qualification",
    "持证": "Certification",
    "许可": "Permit",
    "工具": "Tool",
    "仪表": "Instrument",
    "校准": "Calibration",
    "检定": "Verification",
    "寿命": "LifeSpan",
    "停机": "Downtime",
    "运行": "Operation",
    "改造": "Retrofit",
    "报废": "Disposal",
    "验收": "Acceptance",
    "处置": "Disposition",
    # 人工智能
    "算法": "Algorithm",
    "架构": "Architecture",
    "数据集": "Dataset",
    "样本": "Sample",
    "语料": "Corpus",
    "标注": "Annotation",
    "特征": "Feature",
    "超参数": "Hyperparameter",
    "训练": "Training",
    "微调": "FineTuning",
    "推理": "Inference",
    "评估": "Evaluation",
    "基准": "Benchmark",
    "算力": "Compute",
    "智能体": "Agent",
    "提示词": "Prompt",
    "模板": "Template",
    "部署": "Deployment",
    "版本": "Version",
    "量化": "Quantization",
    "蒸馏": "Distillation",
    "对齐": "Alignment",
    "幻觉": "Hallucination",
    "服务": "Service",
    "治理": "Governance",
    # 计算机网络
    "网络": "Network",
    "路由": "Routing",
    "路由器": "Router",
    "交换机": "Switch",
    "防火墙": "Firewall",
    "链路": "Link",
    "接口": "Interface",
    "端口": "Port",
    "带宽": "Bandwidth",
    "时延": "Latency",
    "抖动": "Jitter",
    "丢包": "PacketLoss",
    "子网": "Subnet",
    "网关": "Gateway",
    "地址": "Address",
    "分段": "Segment",
    "隧道": "Tunnel",
    "域名": "Domain",
    "解析": "Resolution",
    "证书": "Certificate",
    "策略": "Policy",
    "拓扑": "Topology",
    "通信协议": "Protocol",
    "拥塞": "Congestion",
    "广播": "Broadcast",
    "组播": "Multicast",
    "镜像": "Mirror",
    "日志": "Log",
    "告警": "Alarm",
    "监控": "Monitoring",
    "集群": "Cluster",
    "主机": "Host",
    "终端": "Terminal",
}

_SNAKE = re.compile(r"[^a-zA-Z0-9]+")


def to_local_name(label: str, label_en: str = "") -> str:
    """生成可读的 IRI 局部名：优先英文标签，其次术语表逐词映射，最后哈希兜底。"""
    if label_en and re.search(r"[A-Za-z]", label_en):
        return _SNAKE.sub(" ", label_en).strip().title().replace(" ", "") or "Class"
    if label and re.fullmatch(r"[A-Za-z0-9_ .\-]+", label):
        return _SNAKE.sub("", label.title()) or "Class"
    pieces: list[str] = []
    rest = label or ""
    while rest:
        matched = ""
        for size in (5, 4, 3, 2):
            token = rest[:size]
            if token in GLOSSARY:
                matched = token
                break
        if matched:
            pieces.append(GLOSSARY[matched])
            rest = rest[len(matched) :]
        else:
            ch = rest[0]
            if ch.isascii() and (ch.isalnum() or ch in " _-."):
                pieces.append(ch)
            rest = rest[1:]
    joined = "".join(pieces).replace(" ", "")
    if len(re.sub(r"[^A-Za-z]", "", joined)) >= 3:
        return joined[:70]
    import zlib

    return f"C{zlib.crc32((label or '').encode('utf-8')) & 0xFFFFFF:06x}"


def _ascii_slug(text: str, maxlen: int = 40) -> str:
    slug = _SNAKE.sub("-", (text or "").strip()).strip("-").lower()
    return slug[:maxlen]


@dataclass
class ValidationIssue:
    code: str
    level: str
    message: str
    path: str = ""


@dataclass
class ProposalResult:
    proposal_id: int
    created_types: list[int] = field(default_factory=list)
    created_links: list[int] = field(default_factory=list)
    dirty_pages: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)


class OntologyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # =============================================================== 空间
    def create_space(
        self, *, name: str, domain: str = "general", description: str = "", iri: str = ""
    ) -> Space:
        space = Space(
            name=name,
            domain=domain,
            description=description,
            iri=iri or f"https://onto.example.com/ontology/{_ascii_slug(name) or 'space'}#",
        )
        self.session.add(space)
        self.session.flush()
        self._seed_space_vocabulary(space)
        return space

    def list_spaces(self) -> list[Space]:
        return list(self.session.scalars(select(Space).order_by(Space.id)).all())

    def get_space(self, space_id: int) -> Space | None:
        return self.session.get(Space, space_id)

    def space_stats(self, space_id: int) -> dict[str, Any]:
        def count(model) -> int:  # noqa: ANN001
            return int(self.session.scalar(select(func.count()).select_from(model).where(model.space_id == space_id)) or 0)

        by_status = dict(
            self.session.execute(
                select(ObjectType.status, func.count()).where(ObjectType.space_id == space_id).group_by(ObjectType.status)
            ).all()
        )
        by_domain = dict(
            self.session.execute(
                select(ObjectType.business_domain, func.count())
                .where(ObjectType.space_id == space_id)
                .group_by(ObjectType.business_domain)
            ).all()
        )
        by_kind = dict(
            self.session.execute(
                select(ObjectType.kind, func.count()).where(ObjectType.space_id == space_id).group_by(ObjectType.kind)
            ).all()
        )
        return {
            "object_types": count(ObjectType),
            "active_types": by_status.get("active", 0),
            "draft_types": by_status.get("draft", 0),
            "deprecated_types": by_status.get("deprecated", 0),
            "data_properties": count(DataPropertyDef),
            "relation_types": count(RelationType),
            "links": count(TypeLink),
            "constraints": count(Constraint),
            "enumerations": count(Enumeration),
            "pending_proposals": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(ChangeProposal)
                    .where(ChangeProposal.space_id == space_id, ChangeProposal.status == "pending")
                )
                or 0
            ),
            "documents": int(
                self.session.scalar(select(func.count()).select_from(DocumentRecord)) or 0
            ),
            "by_status": by_status,
            "by_domain": by_domain,
            "by_kind": by_kind,
        }

    def _seed_space_vocabulary(self, space: Space) -> None:
        """每个空间预置一批通用数据属性与关联语义，避免 Agent 重复发明轮子。"""
        common_props = [
            ("identifier", "标识", "identifier", "主键/业务唯一标识", ""),
            ("name", "名称", "string", "业务名称", ""),
            ("code", "编码", "string", "内部分类或外部标准编码", ""),
            ("status", "状态", "enum", "生命周期状态", ""),
            ("effectiveDate", "生效日期", "date", "开始生效的日期", ""),
            ("expiryDate", "到期日期", "date", "失效日期", ""),
            ("amount", "金额", "money", "货币金额", "CNY"),
            ("currency", "币种", "enum", "ISO 4217 币种", ""),
            ("tenor", "期限", "duration", "以月/日表示的期限", ""),
            ("riskLevel", "风险等级", "enum", "内部分级（口径见领域包）", ""),
            ("dataClassification", "数据分级", "enum", "敏感分级，取值由所属领域包定义", ""),
            ("regRef", "规则依据", "string", "引用的制度、标准或规范条款", ""),
        ]
        for iri, label, datatype, description, unit in common_props:
            if not self.session.scalar(
                select(DataPropertyDef).where(DataPropertyDef.space_id == space.id, DataPropertyDef.iri.endswith(iri))
            ):
                self.session.add(
                    DataPropertyDef(
                        space_id=space.id,
                        iri=f"{space.iri}dataprop/{iri}",
                        label=label,
                        datatype=datatype,
                        unit=unit,
                        description=description,
                    )
                )
        common_relations = [
            ("subClassOf", "是一种", "taxonomy", "rdfs:subClassOf", False, False, True),
            ("partOf", "属于…的一部分", "part_of", "hasPart", False, False, False),
            ("references", "引用", "reference", "referencedBy", False, False, False),
            ("isDefinedBy", "由…定义", "regulatory", "defines", False, False, False),
            ("hasParticipant", "参与方", "participant", "participatesIn", False, False, False),
            ("triggers", "触发", "causal", "triggeredBy", False, False, False),
            ("derivedFrom", "派生自", "derivation", "derives", False, True, False),
            ("associatedWith", "关联", "association", "associatedWith", True, False, False),
        ]
        for iri, label, category, inverse, symmetric, transitive, _ in common_relations:
            if not self.session.scalar(
                select(RelationType).where(RelationType.space_id == space.id, RelationType.iri.endswith(iri))
            ):
                self.session.add(
                    RelationType(
                        space_id=space.id,
                        iri=f"{space.iri}prop/{iri}",
                        label=label,
                        label_en=iri,
                        inverse_iri=inverse,
                        symmetric=symmetric,
                        transitive=transitive,
                        category=category,
                    )
                )
        self.session.flush()

    # ========================================================== 对象类型
    def list_types(
        self,
        space_id: int,
        *,
        q: str | None = None,
        status: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[ObjectType]:
        stmt = select(ObjectType).where(ObjectType.space_id == space_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(ObjectType.label.like(like), ObjectType.definition.like(like), ObjectType.label_en.like(like))
            )
        if status:
            stmt = stmt.where(ObjectType.status == status)
        if domain:
            stmt = stmt.where(ObjectType.business_domain == domain)
        if kind:
            stmt = stmt.where(ObjectType.kind == kind)
        return list(self.session.scalars(stmt.order_by(ObjectType.label).limit(limit)).all())

    def get_type(self, type_id: int) -> ObjectType | None:
        return self.session.get(ObjectType, type_id)

    def find_type_by_label(self, space_id: int, label: str) -> ObjectType | None:
        label = (label or "").strip()
        if not label:
            return None
        row = self.session.scalar(
            select(ObjectType).where(ObjectType.space_id == space_id, ObjectType.label == label)
        )
        if row:
            return row
        return self.session.scalar(
            select(ObjectType).where(
                ObjectType.space_id == space_id, func.lower(ObjectType.label_en) == label.lower()
            )
        )

    def unique_iri(self, space_id: int, space: Space | None, local: str) -> str:
        """中文标签派生的局部名可能撞车（两个不同概念映射到同一个词）。

        撞车时不覆盖既有类，而是加数字后缀，保持「同 IRI = 同类」的语义。
        """
        base = f"{space.iri if space else ''}class/"
        candidate = f"{base}{local}"
        index = 2
        while self.session.scalar(
            select(ObjectType).where(ObjectType.space_id == space_id, ObjectType.iri == candidate)
        ):
            candidate = f"{base}{local}{index}"
            index += 1
        return candidate

    def similar_types(self, space_id: int, label: str, threshold: float = 0.55) -> list[dict[str, Any]]:
        """去重提示：完全同名 / 互为子串 / 强别名命中。"""
        label = (label or "").strip()
        out: list[dict[str, Any]] = []
        for t in self.list_types(space_id, limit=2000):
            names = [t.label, t.label_en, *(t.aliases or [])]
            for name in names:
                if not name:
                    continue
                if name.strip() == label:
                    out.append({"id": t.id, "label": t.label, "status": t.status, "matched": name, "reason": "同名"})
                    break
                if len(label) >= 3 and (label in name or name in label):
                    out.append({"id": t.id, "label": t.label, "status": t.status, "matched": name, "reason": "包含关系"})
                    break
        return out[:12]

    def create_type(
        self,
        space_id: int,
        payload: dict[str, Any],
        *,
        origin: str = "manual",
        status: str = "active",
        confidence: float | None = None,
    ) -> ObjectType:
        label = (payload.get("label") or payload.get("name") or "").strip()
        if not label:
            raise ValueError("对象类型缺少 label")
        label_en = (payload.get("label_en") or "").strip()
        existing = self.find_type_by_label(space_id, label)
        if existing:
            return self.update_type(existing.id, payload)
        space = self.session.get(Space, space_id)
        parent = None
        if payload.get("parent_label"):
            parent = self.find_type_by_label(space_id, payload["parent_label"])
        local = to_local_name(label, label_en)
        iri = self.unique_iri(space_id, space, local)
        local = iri.rsplit("/", 1)[-1]
        row = ObjectType(
            space_id=space_id,
            iri=iri,
            label=label,
            label_en=label_en or local,
            plural_label=payload.get("plural_label", ""),
            definition=(payload.get("definition") or "").strip(),
            parent_id=parent.id if parent else payload.get("parent_id"),
            kind=payload.get("kind", "class"),
            abstract=bool(payload.get("abstract", False)),
            status=payload.get("status", status),
            origin=origin,
            confidence=float(confidence if confidence is not None else payload.get("confidence", 0.0)),
            business_domain=payload.get("business_domain", "通用"),
            tags=list(payload.get("tags") or []),
            aliases=list(payload.get("aliases") or []),
            annotations=dict(payload.get("annotations") or {}),
        )
        self.session.add(row)
        self.session.flush()
        self._apply_type_extras(row, payload)
        return row

    def _apply_type_extras(self, row: ObjectType, payload: dict[str, Any]) -> None:
        space_id = row.space_id
        for prop in payload.get("properties") or []:
            self.attach_property(row, prop)
        for rel in payload.get("relations") or payload.get("links") or []:
            target_label = rel.get("target") or rel.get("target_label")
            if not target_label:
                continue
            target = self.find_type_by_label(space_id, target_label) or self.create_type(
                space_id, {"label": target_label, "definition": rel.get("target_definition", ""), "kind": "class"},
                origin=row.origin or "agent",
                status="draft",
                confidence=0.3,
            )
            self.link(space_id, row.id, target.id, rel.get("relation", "关联"), description=rel.get("description", ""), cardinality=rel.get("cardinality", "n:m"), origin=row.origin, confidence=float(rel.get("confidence", 0.5) or 0.5))
        for con in payload.get("constraints") or []:
            self.session.add(
                Constraint(
                    space_id=space_id,
                    name=con.get("name") or f"{row.label} 约束",
                    subject_type_id=row.id,
                    subject_property_iri=con.get("property", ""),
                    kind=con.get("kind", "range"),
                    expression=con.get("expression", ""),
                    severity=con.get("severity", "warning"),
                    reg_ref=con.get("reg_ref", ""),
                    description=con.get("description", ""),
                )
            )
        for enum_payload in payload.get("enumerations") or []:
            self.session.add(
                Enumeration(
                    space_id=space_id,
                    iri=f"{row.iri}enum/{to_local_name(enum_payload.get('label', 'enum'))}",
                    label=enum_payload.get("label", "枚举"),
                    members=list(enum_payload.get("members") or []),
                    description=enum_payload.get("description", ""),
                )
            )
        self.session.flush()

    def attach_property(self, row: ObjectType, prop: dict[str, Any]) -> TypeDataProperty:
        space_id = row.space_id
        label = (prop.get("label") or prop.get("name") or "").strip()
        if not label:
            raise ValueError("属性缺少 label")
        datatype = prop.get("datatype", "string")
        iri_local = prop.get("iri") or to_local_name(label, prop.get("label_en", ""))
        definition = self.session.scalar(
            select(DataPropertyDef).where(DataPropertyDef.space_id == space_id, DataPropertyDef.label == label)
        )
        if definition is None:
            definition = DataPropertyDef(
                space_id=space_id,
                iri=f"{row.iri.rsplit('class/', 1)[0] if 'class/' in row.iri else ''}dataprop/{iri_local}",
                label=label,
                datatype=datatype,
                unit=prop.get("unit", ""),
                pattern=prop.get("pattern", ""),
                description=prop.get("description", ""),
                enum_id=prop.get("enum_id"),
                format_ref=prop.get("format_ref", ""),
                annotations=dict(prop.get("annotations") or {}),
            )
            self.session.add(definition)
            self.session.flush()
        slot = self.session.scalar(
            select(TypeDataProperty).where(
                TypeDataProperty.object_type_id == row.id, TypeDataProperty.property_id == definition.id
            )
        )
        if slot is None:
            slot = TypeDataProperty(
                object_type_id=row.id,
                property_id=definition.id,
                required=bool(prop.get("required", False)),
                unique_flag=bool(prop.get("unique", False)),
                min_card=int(prop.get("min_card", 1 if prop.get("required") else 0)),
                max_card=int(prop.get("max_card", 1)),
                description=prop.get("description", ""),
                key_identifier=bool(prop.get("key_identifier", False)),
                annotations=dict(prop.get("annotations") or {}),
            )
            self.session.add(slot)
        else:
            for key in ("required", "unique_flag", "min_card", "max_card", "description", "key_identifier"):
                if key in prop:
                    setattr(slot, key, prop[key])
        self.session.flush()
        return slot

    def update_type(self, type_id: int, patch: dict[str, Any]) -> ObjectType:
        row = self.session.get(ObjectType, type_id)
        if row is None:
            raise KeyError(f"对象类型 {type_id} 不存在")
        scalar_fields = {
            "label",
            "label_en",
            "plural_label",
            "definition",
            "kind",
            "abstract",
            "status",
            "business_domain",
            "confidence",
        }
        for key in scalar_fields & set(patch):
            setattr(row, key, patch[key])
        if "parent_label" in patch:
            parent = self.find_type_by_label(row.space_id, patch["parent_label"])
            row.parent_id = parent.id if parent else None
        if "parent_id" in patch:
            row.parent_id = patch["parent_id"]
        for key in ("tags", "aliases", "annotations"):
            if key in patch:
                merged = dict(patch[key]) if key == "annotations" else list(patch[key])
                setattr(row, key, merged)
        row.version_no += 1
        self.session.flush()
        extras = {k: patch[k] for k in ("properties", "relations", "constraints", "enumerations") if k in patch}
        if extras:
            self._apply_type_extras(row, extras)
        self.mark_pages_dirty(row.space_id, type_id)
        return row

    def set_status(self, type_id: int, status: str) -> ObjectType:
        row = self.update_type(type_id, {"status": status})
        return row

    def delete_type(self, type_id: int) -> None:
        row = self.session.get(ObjectType, type_id)
        if row is None:
            return
        for child in self.session.scalars(select(ObjectType).where(ObjectType.parent_id == type_id)):
            child.parent_id = row.parent_id
        for link in self.session.scalars(
            select(TypeLink).where(or_(TypeLink.source_id == type_id, TypeLink.target_id == type_id))
        ):
            self.session.delete(link)
        for con in self.session.scalars(select(Constraint).where(Constraint.subject_type_id == type_id)):
            self.session.delete(con)
        self.session.delete(row)
        self.session.flush()

    def detail(self, type_id: int) -> dict[str, Any]:
        row = self.session.get(ObjectType, type_id)
        if row is None:
            raise KeyError(str(type_id))
        space = self.session.get(Space, row.space_id)
        slots = self.session.scalars(
            select(TypeDataProperty).where(TypeDataProperty.object_type_id == row.id)
        ).all()
        props = []
        for slot in slots:
            definition = self.session.get(DataPropertyDef, slot.property_id)
            enum = self.session.get(Enumeration, definition.enum_id) if definition and definition.enum_id else None
            props.append(
                {
                    "id": definition.id if definition else None,
                    "slot_id": slot.id,
                    "iri": definition.iri if definition else "",
                    "label": definition.label if definition else "",
                    "datatype": definition.datatype if definition else "string",
                    "unit": definition.unit if definition else "",
                    "pattern": definition.pattern if definition else "",
                    "required": slot.required,
                    "unique": slot.unique_flag,
                    "min_card": slot.min_card,
                    "max_card": slot.max_card,
                    "key_identifier": slot.key_identifier,
                    "description": slot.description or (definition.description if definition else ""),
                    "enum": (enum.members if enum else []),
                }
            )
        out_links = self.session.scalars(select(TypeLink).where(TypeLink.source_id == row.id)).all()
        in_links = self.session.scalars(select(TypeLink).where(TypeLink.target_id == row.id)).all()

        def link_view(link: TypeLink, direction: str) -> dict[str, Any]:
            other = link.target if direction == "out" else link.source
            return {
                "link_id": link.id,
                "relation": link.relation.label,
                "relation_iri": link.relation.iri,
                "category": link.relation.category,
                "direction": direction,
                "target_id": other.id,
                "target_label": other.label,
                "cardinality": link.cardinality,
                "description": link.description,
                "confidence": link.confidence,
                "status": link.status,
            }

        parent = self.session.get(ObjectType, row.parent_id) if row.parent_id else None
        children = self.session.scalars(select(ObjectType).where(ObjectType.parent_id == row.id)).all()
        constraints = self.session.scalars(select(Constraint).where(Constraint.subject_type_id == row.id)).all()
        proposals = self.session.scalars(
            select(ChangeProposal).where(
                ChangeProposal.space_id == row.space_id, ChangeProposal.status != "rejected"
            )
        ).all()
        related_evidence = []
        for proposal in proposals:
            payload = proposal.payload or {}
            if payload.get("label") == row.label:
                for ev in proposal.evidences:
                    related_evidence.append(self.evidence_view(ev))
        return {
            "id": row.id,
            "space_id": row.space_id,
            "space_name": space.name if space else "",
            "iri": row.iri,
            "label": row.label,
            "label_en": row.label_en,
            "plural_label": row.plural_label,
            "definition": row.definition,
            "kind": row.kind,
            "abstract": row.abstract,
            "status": row.status,
            "origin": row.origin,
            "confidence": row.confidence,
            "business_domain": row.business_domain,
            "tags": row.tags or [],
            "aliases": row.aliases or [],
            "annotations": row.annotations or {},
            "version_no": row.version_no,
            "parent": {"id": parent.id, "label": parent.label} if parent else None,
            "children": [{"id": c.id, "label": c.label, "status": c.status} for c in children],
            "properties": props,
            "links": [link_view(l, "out") for l in out_links] + [link_view(l, "in") for l in in_links],
            "constraints": [
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": c.kind,
                    "expression": c.expression,
                    "severity": c.severity,
                    "reg_ref": c.reg_ref,
                    "description": c.description,
                    "property": c.subject_property_iri,
                }
                for c in constraints
            ],
            "evidence": related_evidence,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }

    def evidence_view(self, ev: Evidence) -> dict[str, Any]:
        doc = self.session.get(DocumentRecord, ev.doc_id) if ev.doc_id else None
        return {
            "id": ev.id,
            "proposal_id": ev.proposal_id,
            "answer_id": ev.answer_id,
            "chunk_id": ev.chunk_id,
            "doc_id": ev.doc_id,
            "doc_name": doc.name if doc else "",
            "locator": ev.locator,
            "quote": ev.quote,
            "field_path": ev.field_path,
            "score": ev.score,
        }

    # ============================================================= 关联
    def ensure_relation_type(self, space_id: int, label: str, *, category: str = "association", description: str = "") -> RelationType:
        label = (label or "关联").strip()
        row = self.session.scalar(
            select(RelationType).where(RelationType.space_id == space_id, RelationType.label == label)
        )
        if row:
            return row
        space = self.session.get(Space, space_id)
        row = RelationType(
            space_id=space_id,
            iri=f"{space.iri if space else ''}prop/{to_local_name(label)}",
            label=label,
            label_en=to_local_name(label),
            category=category,
            description=description,
            inverse_iri="associatedWith",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def link(
        self,
        space_id: int,
        source_id: int,
        target_id: int,
        relation: str | int,
        *,
        description: str = "",
        cardinality: str = "n:m",
        origin: str = "manual",
        confidence: float = 0.6,
        status: str = "active",
    ) -> TypeLink:
        if source_id == target_id:
            raise ValueError("不允许自环关联，请用属性表达")
        rel = (
            self.session.get(RelationType, relation)
            if isinstance(relation, int)
            else self.ensure_relation_type(space_id, relation)
        )
        row = self.session.scalar(
            select(TypeLink).where(
                TypeLink.source_id == source_id,
                TypeLink.target_id == target_id,
                TypeLink.relation_id == rel.id,
            )
        )
        if row:
            row.description = description or row.description
            row.confidence = max(row.confidence, confidence)
            self.session.flush()
            self.mark_pages_dirty(space_id, source_id, target_id)
            return row
        row = TypeLink(
            space_id=space_id,
            source_id=source_id,
            target_id=target_id,
            relation_id=rel.id,
            description=description,
            cardinality=cardinality,
            origin=origin,
            confidence=confidence,
            status=status,
        )
        self.session.add(row)
        self.session.flush()
        self.mark_pages_dirty(space_id, source_id, target_id)
        return row

    def list_relation_types(self, space_id: int) -> list[RelationType]:
        return list(self.session.scalars(select(RelationType).where(RelationType.space_id == space_id)).all())

    def list_properties(self, space_id: int) -> list[DataPropertyDef]:
        return list(self.session.scalars(select(DataPropertyDef).where(DataPropertyDef.space_id == space_id)).all())

    def list_constraints(self, space_id: int, type_id: int | None = None) -> list[Constraint]:
        stmt = select(Constraint).where(Constraint.space_id == space_id)
        if type_id:
            stmt = stmt.where(Constraint.subject_type_id == type_id)
        return list(self.session.scalars(stmt).all())

    # ========================================================= 变更提案
    def create_proposal(
        self,
        space_id: int,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str = "",
        confidence: float = 0.5,
        evidences: list[dict[str, Any]] | None = None,
        run_id: int | None = None,
        title: str = "",
    ) -> ChangeProposal:
        title = title or f"新增对象类型「{payload.get('label', '未命名')}」"
        proposal = ChangeProposal(
            space_id=space_id,
            kind=kind,
            title=title,
            payload=payload,
            rationale=rationale,
            confidence=float(confidence),
            run_id=run_id,
        )
        self.session.add(proposal)
        self.session.flush()
        for ev in evidences or []:
            self.session.add(
                Evidence(
                    proposal_id=proposal.id,
                    answer_id=ev.get("answer_id", ""),
                    chunk_id=ev.get("chunk_id"),
                    doc_id=ev.get("doc_id"),
                    locator=ev.get("locator", ""),
                    quote=ev.get("quote", ""),
                    field_path=ev.get("field_path", ""),
                    score=float(ev.get("score", 0.0) or 0.0),
                )
            )
        self.session.flush()
        return proposal

    def list_proposals(self, space_id: int | None, status: str | None = None, limit: int = 100) -> list[ChangeProposal]:
        stmt = select(ChangeProposal)
        if space_id:
            stmt = stmt.where(ChangeProposal.space_id == space_id)
        if status:
            stmt = stmt.where(ChangeProposal.status == status)
        return list(self.session.scalars(stmt.order_by(ChangeProposal.id.desc()).limit(limit)).all())

    def proposal_view(self, proposal: ChangeProposal) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "space_id": proposal.space_id,
            "run_id": proposal.run_id,
            "kind": proposal.kind,
            "title": proposal.title,
            "payload": proposal.payload or {},
            "rationale": proposal.rationale,
            "status": proposal.status,
            "confidence": proposal.confidence,
            "review_note": proposal.review_note,
            "applied_ref": proposal.applied_ref,
            "created_at": proposal.created_at.isoformat() if proposal.created_at else "",
            "evidence": [self.evidence_view(ev) for ev in proposal.evidences],
            "duplicates": self.similar_types(proposal.space_id, (proposal.payload or {}).get("label", "")),
        }

    def accept_proposal(self, proposal_id: int, *, reviewer: str = "human", patch: dict | None = None) -> ProposalResult:
        proposal = self.session.get(ChangeProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"提案 {proposal_id} 不存在")
        payload = dict(proposal.payload or {})
        payload.update(patch or {})
        result = ProposalResult(proposal_id=proposal_id)
        if proposal.kind == "object_type":
            row = self.create_type(
                proposal.space_id,
                payload,
                origin=payload.get("origin", "agent"),
                status="active",
                confidence=float(payload.get("confidence", proposal.confidence) or 0.7),
            )
            result.created_types = [row.id]
            result.dirty_pages = self.mark_pages_dirty(proposal.space_id, row.id)
            proposal.applied_ref = f"object_type:{row.id}"
        elif proposal.kind == "link":
            source = self.find_type_by_label(proposal.space_id, payload.get("source", ""))
            target = self.find_type_by_label(proposal.space_id, payload.get("target", ""))
            if not source or not target:
                raise ValueError("关联提案的源或目标对象类型不存在")
            link = self.link(
                proposal.space_id,
                source.id,
                target.id,
                payload.get("relation", "关联"),
                description=payload.get("description", ""),
                cardinality=payload.get("cardinality", "n:m"),
                origin="agent",
                confidence=float(payload.get("confidence", 0.7)),
            )
            result.created_links = [link.id]
            result.dirty_pages = self.mark_pages_dirty(proposal.space_id, source.id, target.id)
            proposal.applied_ref = f"type_link:{link.id}"
        elif proposal.kind == "constraint":
            subject = self.find_type_by_label(proposal.space_id, payload.get("subject", ""))
            constraint = Constraint(
                space_id=proposal.space_id,
                name=payload.get("name", "约束"),
                subject_type_id=subject.id if subject else None,
                subject_property_iri=payload.get("property", ""),
                kind=payload.get("kind", "regulatory"),
                expression=payload.get("expression", ""),
                severity=payload.get("severity", "warning"),
                reg_ref=payload.get("reg_ref", ""),
                description=payload.get("description", ""),
            )
            self.session.add(constraint)
            self.session.flush()
            proposal.applied_ref = f"constraint:{constraint.id}"
            result.dirty_pages = self.mark_pages_dirty(proposal.space_id, subject.id) if subject else []
        elif proposal.kind == "property":
            subject = self.find_type_by_label(proposal.space_id, payload.get("subject", ""))
            if not subject:
                raise ValueError("属性提案缺少已存在的 subject 对象类型")
            slot = self.attach_property(subject, payload)
            proposal.applied_ref = f"type_data_property:{slot.id}"
            result.dirty_pages = self.mark_pages_dirty(proposal.space_id, subject.id)
        else:
            raise ValueError(f"未知提案类型 {proposal.kind}")
        proposal.status = "accepted"
        proposal.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        proposal.review_note = (proposal.review_note + f" [{reviewer} 确认]").strip()
        self.session.flush()
        self.session.add(
            Evidence(
                proposal_id=proposal.id,
                field_path="review",
                quote=f"{reviewer} 于 {proposal.reviewed_at.isoformat()} 确认提案",
            )
        )
        self.session.flush()
        return result

    def reject_proposal(self, proposal_id: int, *, note: str = "", reviewer: str = "human") -> ChangeProposal:
        proposal = self.session.get(ChangeProposal, proposal_id)
        if proposal is None:
            raise KeyError(str(proposal_id))
        proposal.status = "rejected"
        proposal.review_note = note or "人工否决"
        proposal.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.session.flush()
        return proposal

    def update_proposal(self, proposal_id: int, patch: dict[str, Any]) -> ChangeProposal:
        """人工在确认前直接修订草稿字段。"""
        proposal = self.session.get(ChangeProposal, proposal_id)
        if proposal is None:
            raise KeyError(str(proposal_id))
        payload = dict(proposal.payload or {})
        payload.update(patch)
        proposal.payload = payload
        proposal.confidence = float(payload.get("confidence", proposal.confidence))
        self.session.flush()
        return proposal

    # ============================================================ Wiki 脏
    def mark_pages_dirty(self, space_id: int, *type_ids: int) -> list[str]:
        slugs: list[str] = []
        for type_id in type_ids:
            row = self.session.get(ObjectType, type_id)
            if row is None:
                continue
            slug = f"type-{row.id}"
            page = self.session.scalar(select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.slug == slug))
            if page:
                page.dirty = True
                if row.id == type_id:
                    page.title = row.label
                slugs.append(slug)
        index = self.session.scalar(
            select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.slug == "index")
        )
        if index:
            index.dirty = True
            slugs.append(index.slug)
        self.session.flush()
        return slugs

    # ============================================================ 校验
    def validate_space(self, space_id: int) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        types = self.list_types(space_id, limit=5000)
        by_id = {t.id: t for t in types}
        for t in types:
            if not t.definition or len(t.definition) < 6:
                issues.append(ValidationIssue("definition_missing", "warning", f"「{t.label}」缺少充分定义（少于 6 字）", f"type:{t.id}.definition"))
            if not t.business_domain or t.business_domain == "通用":
                issues.append(ValidationIssue("domain_unassigned", "info", f"「{t.label}」未指派业务域", f"type:{t.id}.business_domain"))
            slots = self.session.scalars(select(TypeDataProperty).where(TypeDataProperty.object_type_id == t.id)).all()
            if not slots:
                issues.append(ValidationIssue("no_property", "info", f"「{t.label}」尚未定义任何数据属性", f"type:{t.id}.properties"))
            if t.parent_id and t.parent_id not in by_id:
                issues.append(ValidationIssue("dangling_parent", "violation", f"「{t.label}」的父类型引用失效", f"type:{t.id}.parent_id"))
            # subClassOf 环检测
            seen: set[int] = set()
            cursor: int | None = t.parent_id
            while cursor is not None:
                if cursor in seen:
                    issues.append(ValidationIssue("taxonomy_cycle", "violation", f"「{t.label}」的上位分类存在环", f"type:{t.id}.parent_id"))
                    break
                seen.add(cursor)
                cursor = by_id[cursor].parent_id if cursor in by_id else None
        for link in self.session.scalars(select(TypeLink).where(TypeLink.space_id == space_id)).all():
            if link.source_id not in by_id or link.target_id not in by_id:
                issues.append(ValidationIssue("dangling_link", "violation", f"关联 #{link.id} 端点缺失", f"link:{link.id}"))
        return issues

    def validate_type_payload(self, payload: dict[str, Any], space_id: int | None = None) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        label = (payload.get("label") or "").strip()
        if not label:
            issues.append(ValidationIssue("label_required", "violation", "对象类型必须有中文标签", "label"))
        if len(label) > 30:
            issues.append(ValidationIssue("label_too_long", "warning", "标签超过 30 字，建议改为短语", "label"))
        if not (payload.get("definition") or "").strip():
            issues.append(ValidationIssue("definition_required", "warning", "缺少定义，入库后 Wiki 会出现空章节", "definition"))
        if space_id:
            duplicates = self.similar_types(space_id, label)
            if any(d["reason"] == "同名" for d in duplicates):
                issues.append(ValidationIssue("duplicate_label", "violation", f"已存在同名对象类型「{label}」，将被合并更新", "label"))
            elif duplicates:
                issues.append(ValidationIssue("similar_exists", "warning", "存在相近类型，建议复用或明确区分: " + "、".join(d["label"] for d in duplicates[:3]), "label"))
        for idx, prop in enumerate(payload.get("properties") or []):
            if not (prop.get("label") or "").strip():
                issues.append(ValidationIssue("prop_label", "violation", f"第 {idx + 1} 个属性缺少标签", f"properties[{idx}]"))
            if prop.get("datatype") not in PROPERTY_DATATYPES_SET:
                issues.append(ValidationIssue("prop_datatype", "warning", f"属性「{prop.get('label')}」的数据类型不在标准词表内", f"properties[{idx}].datatype"))
        for idx, rel in enumerate(payload.get("relations") or []):
            if not (rel.get("target") or rel.get("target_label") or "").strip():
                issues.append(ValidationIssue("rel_target", "violation", f"第 {idx + 1} 条关联缺少目标对象类型", f"relations[{idx}]"))
        return issues


PROPERTY_DATATYPES_SET = {
    "string",
    "text",
    "integer",
    "decimal",
    "money",
    "percentage",
    "date",
    "datetime",
    "duration",
    "boolean",
    "enum",
    "identifier",
    "uri",
    "json",
}
