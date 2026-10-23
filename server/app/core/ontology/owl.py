"""本体导出：OWL Turtle / SHACL 形状 / SKOS 词表 / JSON Schema。

导出面向既有语义网与数据集成两条路径：前者可载入 Protégé / Apache Jena，
后者可直接给数据中台做表结构对齐。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    Constraint,
    DataPropertyDef,
    Enumeration,
    ObjectType,
    RelationType,
    Space,
    TypeDataProperty,
    TypeLink,
)
from .kinds import datatype_iri

PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sh": "http://www.w3.org/ns/shacl#",
    "dcterms": "http://purl.org/dc/terms/",
    "onto": "https://finonto.example.com/vocab/",
}


def _q(value: str, lang: str = "zh") -> str:
    text = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    text = re.sub(r"\s+", " ", text).strip()
    return f'"{text}"@{lang}' if lang else f'"{text}"'


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace('"', "'")).strip()


def _prefix_lines(space: Space) -> list[str]:
    lines = [f"@prefix {p}: <{u}> ." for p, u in PREFIXES.items()]
    lines.append(f"@prefix : <{space.iri}> .")
    return lines


def _subject(subject: str, triples: list[str]) -> list[str]:
    body = " ;\n    ".join(t for t in triples if t)
    return [f"{subject} {body} .", ""]


def to_turtle(session: Session, space_id: int, type_ids: list[int] | None = None) -> str:
    space = session.get(Space, space_id)
    if space is None:
        raise KeyError(f"空间 {space_id} 不存在")
    base = space.iri
    stmt = select(ObjectType).where(ObjectType.space_id == space_id)
    if type_ids:
        stmt = stmt.where(ObjectType.id.in_(type_ids))
    types = list(session.scalars(stmt.order_by(ObjectType.id)).all())
    by_id = {t.id: t for t in types}

    def pname(iri: str) -> str:
        return ":" + iri[len(base) :] if iri and iri.startswith(base) else f"<{iri or 'onto:unknown'}>"

    out: list[str] = _prefix_lines(space)
    out += _subject(
        f"<{base}>",
        [
            "a owl:Ontology",
            f"rdfs:label {_q(space.name)}",
            f"rdfs:comment {_q(space.description)}",
            f"dcterms:subject {_q(space.domain)}",
            'owl:versionInfo "0.1"',
        ],
    )

    for t in types:
        triples = [
            "a owl:Class" + (" , owl:AbstractClass" if t.abstract else ""),
            f"rdfs:label {_q(t.label)}",
        ]
        if t.label_en:
            triples.append(f'rdfs:label "{_plain(t.label_en)}"@en')
        if t.plural_label:
            triples.append(f"skos:altLabel {_q(t.plural_label)}")
        triples += [f"skos:altLabel {_q(str(a))}" for a in (t.aliases or [])[:8]]
        if t.definition:
            triples.append(f"rdfs:comment {_q(t.definition)}")
        if t.parent_id and t.parent_id in by_id:
            triples.append(f"rdfs:subClassOf {pname(by_id[t.parent_id].iri)}")
        triples.append(f"onto:objectKind onto:{t.kind}")
        triples.append(f"onto:businessDomain {_q(t.business_domain or '通用')}")
        ann = t.annotations or {}
        for key in ("data_classification", "risk_dimension", "reg_refs", "identifier_scheme", "lifecycle"):
            if key in ann:
                value = ann[key]
                rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
                triples.append(f"onto:{key} {_q(rendered, '')}")
        triples += [f"dcterms:subject {_q(str(tag))}" for tag in (t.tags or [])[:6]]
        triples.append(f"onto:status {_q(t.status, '')}")
        triples.append(f'onto:confidence "{round(float(t.confidence or 0), 3)}"')
        triples.append(f"onto:origin {_q(t.origin, '')}")
        out += _subject(pname(t.iri), triples)

    props = list(session.scalars(select(DataPropertyDef).where(DataPropertyDef.space_id == space_id)).all())
    for p in props:
        triples = [
            "a owl:DatatypeProperty",
            f"rdfs:label {_q(p.label)}",
            f"rdfs:comment {_q(p.description)}" if p.description else "",
            f"rdfs:range {datatype_iri(p.datatype)}",
            f"onto:unit {_q(p.unit, '')}" if p.unit else "",
            f"onto:pattern {_q(p.pattern, '')}" if p.pattern else "",
            f"onto:formatRef {_q(p.format_ref, '')}" if p.format_ref else "",
        ]
        out += _subject(pname(p.iri), triples)

    slots = list(
        session.scalars(
            select(TypeDataProperty).join(ObjectType, ObjectType.id == TypeDataProperty.object_type_id).where(
                ObjectType.space_id == space_id
            )
        ).all()
    )
    for slot in slots:
        definition = session.get(DataPropertyDef, slot.property_id)
        owner = session.get(ObjectType, slot.object_type_id)
        if not definition or not owner:
            continue
        card = "minCardinality" if (slot.required or slot.min_card) else "maxCardinality"
        value = slot.min_card if (slot.required or slot.min_card) else slot.max_card
        restriction = f"[ a owl:Restriction ; owl:onProperty {pname(definition.iri)} ; owl:{card} {value} ]"
        out += _subject(pname(owner.iri), [f"rdfs:subClassOf {restriction}"])

    rels = list(session.scalars(select(RelationType).where(RelationType.space_id == space_id)).all())
    for r in rels:
        triples = [
            "a owl:ObjectProperty"
            + (", owl:SymmetricProperty" if r.symmetric else "")
            + (", owl:TransitiveProperty" if r.transitive else "")
            + (", owl:FunctionalProperty" if r.functional else ""),
            f"rdfs:label {_q(r.label)}",
            f"rdfs:comment {_q(r.description)}" if r.description else "",
            f"owl:inverseOf onto:{r.inverse_iri}" if r.inverse_iri else "",
            f"onto:relationCategory onto:{r.category}",
        ]
        out += _subject(pname(r.iri), triples)

    for link in session.scalars(select(TypeLink).where(TypeLink.space_id == space_id)).all():
        if link.source_id not in by_id or link.target_id not in by_id:
            continue
        rel = session.get(RelationType, link.relation_id)
        predicate = pname(rel.iri) if rel else "onto:associatedWith"
        line = f"{pname(by_id[link.source_id].iri)} {predicate} {pname(by_id[link.target_id].iri)} ."
        if link.description:
            line += f"  # {_plain(link.description)}"
        out.append(line)
    out.append("")

    for c in session.scalars(select(Constraint).where(Constraint.space_id == space_id)).all():
        subject = session.get(ObjectType, c.subject_type_id) if c.subject_type_id else None
        component = {
            "required": "sh:MinCountConstraintComponent",
            "cardinality": "sh:MaxCountConstraintComponent",
            "range": "sh:IntervalConstraintComponent",
            "pattern": "sh:PatternConstraintComponent",
            "enum": "sh:InConstraintComponent",
            "unique": "sh:UniqueValueConstraintComponent",
            "codependency": "sh:ExpressionConstraintComponent",
            "consistency": "sh:ExpressionConstraintComponent",
            "regulatory": "onto:RegulatoryConstraintComponent",
        }.get(c.kind, "sh:ConstraintComponent")
        severity = {"violation": "sh:Violation", "warning": "sh:Warning", "info": "sh:Info"}.get(c.severity, "sh:Violation")
        out += _subject(
            f":shape/{c.id}",
            [
                "a sh:NodeShape",
                f"sh:targetClass {pname(subject.iri) if subject else 'owl:Thing'}",
                f"sh:component {component}",
                f"sh:severity {severity}",
                f"rdfs:comment {_q(c.expression or c.description)}",
                f"onto:regRef {_q(c.reg_ref, '')}" if c.reg_ref else "",
            ],
        )

    return "\n".join(out)


def to_skos(session: Session, space_id: int) -> str:
    space = session.get(Space, space_id)
    if space is None:
        raise KeyError(str(space_id))
    out = _prefix_lines(space) + ["", f":conceptScheme a skos:ConceptScheme ; rdfs:label {_q(space.name)} .", ""]
    types = list(session.scalars(select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.id)))
    by_id = {t.id: t for t in types}
    for t in types:
        out.append(f":concept/{t.id} a skos:Concept ;")
        out.append(f"    skos:inScheme :conceptScheme ;")
        out.append(f"    skos:prefLabel {_q(t.label)} ;")
        if t.label_en:
            out.append(f'    skos:prefLabel "{_plain(t.label_en)}"@en ;')
        for alias in (t.aliases or [])[:6]:
            out.append(f"    skos:altLabel {_q(str(alias))} ;")
        out.append(f"    skos:definition {_q(t.definition)} ;")
        if t.parent_id and t.parent_id in by_id:
            out.append(f"    skos:broader :concept/{by_id[t.parent_id].id} ;")
        for child in [c for c in types if c.parent_id == t.id]:
            out.append(f"    skos:narrower :concept/{child.id} ;")
        out[-1] = out[-1].rstrip(";") + " ."
        out.append("")
    return "\n".join(out)


_JSON_TYPE = {
    "string": "string",
    "text": "string",
    "identifier": "string",
    "uri": "string",
    "integer": "integer",
    "decimal": "number",
    "money": "number",
    "percentage": "number",
    "boolean": "boolean",
    "date": "string",
    "datetime": "string",
    "duration": "string",
    "json": "object",
    "enum": "string",
}


def to_json_schema(session: Session, space_id: int, type_id: int) -> dict[str, Any]:
    """对象类型 -> JSON Schema，供数据集成 / 主数据落表使用。"""
    row = session.get(ObjectType, type_id)
    if row is None:
        raise KeyError(str(type_id))
    slots = session.scalars(select(TypeDataProperty).where(TypeDataProperty.object_type_id == type_id)).all()
    properties: dict[str, Any] = {}
    required: list[str] = []
    for slot in slots:
        definition = session.get(DataPropertyDef, slot.property_id)
        if definition is None:
            continue
        key = re.sub(r"[^0-9A-Za-z_]", "", definition.iri.rsplit("/", 1)[-1]) or definition.label
        schema: dict[str, Any] = {
            "type": _JSON_TYPE.get(definition.datatype, "string"),
            "title": definition.label,
            "description": definition.description or slot.description,
            "x-iri": definition.iri,
        }
        if definition.format_ref:
            schema["format"] = definition.format_ref
        elif definition.datatype in ("date", "datetime"):
            schema["format"] = definition.datatype
        if definition.pattern:
            schema["pattern"] = definition.pattern
        if definition.datatype == "enum":
            enum = session.get(Enumeration, definition.enum_id) if definition.enum_id else None
            if enum:
                schema["enum"] = enum.members
        if slot.unique_flag:
            schema["x-unique"] = True
        if slot.key_identifier:
            schema["x-key"] = True
        properties[key] = schema
        if slot.required:
            required.append(key)

    links = session.scalars(select(TypeLink).where(TypeLink.source_id == type_id)).all()
    for link in links:
        rel = session.get(RelationType, link.relation_id)
        target = session.get(ObjectType, link.target_id)
        if not rel or not target:
            continue
        key = f"rel{re.sub(r'[^0-9A-Za-z]', '', rel.label_en or rel.iri.rsplit('/', 1)[-1]).title()}"
        properties[key] = {
            "type": "array" if link.cardinality.startswith("n") else "object",
            "title": f"{rel.label} -> {target.label}",
            "description": link.description,
            "x-target-iri": target.iri,
            "items": {"type": "string", "format": "uri"} if link.cardinality.startswith("n") else None,
        }
        if properties[key]["items"] is None:
            properties[key].pop("items")

    payload: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": row.iri,
        "title": row.label,
        "description": row.definition,
        "type": "object",
        "properties": properties,
        "x-finonto": {
            "kind": row.kind,
            "status": row.status,
            "businessDomain": row.business_domain,
            "aliases": row.aliases,
            "tags": row.tags,
            "annotations": row.annotations,
            "confidence": row.confidence,
        },
    }
    if required:
        payload["required"] = required
    constraints = session.scalars(
        select(Constraint).where(Constraint.subject_type_id == type_id)
    ).all()
    if constraints:
        payload["x-constraints"] = [
            {"name": c.name, "kind": c.kind, "expression": c.expression, "severity": c.severity, "regRef": c.reg_ref}
            for c in constraints
        ]
    return payload


def export_bundle(session: Session, space_id: int) -> dict[str, str]:
    """一次导出全套序列化产物（文件名 -> 文本内容）。"""
    space = session.get(Space, space_id)
    if space is None:
        raise KeyError(str(space_id))
    turtle = to_turtle(session, space_id)
    skos = to_skos(session, space_id)
    schemas = {
        str(t.id): json.dumps(to_json_schema(session, space_id, t.id), ensure_ascii=False, indent=2)
        for t in session.scalars(select(ObjectType).where(ObjectType.space_id == space_id)).all()
    }
    return {
        "spaces/space-{}-{}.ttl".format(space_id, space.name): turtle,
        "spaces/space-{}-{}.skos.ttl".format(space_id, space.name): skos,
        **{f"spaces/jsonschema/{k}.json": v for k, v in schemas.items()},
    }
