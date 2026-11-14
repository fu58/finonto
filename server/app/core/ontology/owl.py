"""本体导出：Turtle（OWL + SHACL）、SKOS 词表、逐类型 JSON Schema。

导出不是「顺便做一下」：企业里本体要能被下游系统消费（数据治理平台、图数据库、
表单引擎），所以三种格式各解决一个问题——
* Turtle/OWL：类与属性的形式化语义，可导入 GraphDB/Neo4j(neosemantics)；
* SHACL：可执行的约束，能做数据质量校验；
* JSON Schema：前端表单与 API 校验直接可用。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from ...models import Constraint, DataPropertyDef, ObjectType, OntologySpace, RelationType, TypeDataProperty, TypeLink
from ..text import slugify

XSD_MAP = {
    "string": "xsd:string",
    "integer": "xsd:integer",
    "decimal": "xsd:decimal",
    "boolean": "xsd:boolean",
    "date": "xsd:date",
    "datetime": "xsd:dateTime",
    "uri": "xsd:anyURI",
    "enum": "xsd:string",
}

JSON_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "decimal": "number",
    "boolean": "boolean",
    "date": "string",
    "datetime": "string",
    "uri": "string",
    "enum": "string",
}


def _ttl(text: str) -> str:
    """Turtle 字符串转义（含换行）。"""
    cleaned = (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{cleaned}"@zh'


def _local(label: str) -> str:
    """IRI 局部名：ASCII 优先，中文走稳定短码（与 slugify 一致）。"""
    slug = slugify(label, fallback="Concept")
    slug = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
    return slug or "Concept"


def export_turtle(session, space_id: int) -> str:
    space = session.get(OntologySpace, space_id)
    if space is None:
        return "# 空间不存在\n"

    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.label)
    ).scalars().all()
    by_id = {item.id: item for item in types}

    relations = session.execute(
        select(RelationType).where(RelationType.space_id == space_id)
    ).scalars().all()
    relation_by_id = {item.id: item for item in relations}

    props = session.execute(
        select(DataPropertyDef).where(DataPropertyDef.space_id == space_id)
    ).scalars().all()

    lines: list[str] = [
        f"@prefix : <{space.iri}#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        f"<{space.iri}> a owl:Ontology ;",
        f"    rdfs:label {_ttl(space.name)} ;",
        f"    rdfs:comment {_ttl(space.description)} .",
        "",
        "# ----------------------------------------------------------------",
        "# 对象类型（owl:Class）",
        "# ----------------------------------------------------------------",
    ]

    for item in types:
        local = _local(item.label)
        lines.append(f":{local} a owl:Class ;")
        lines.append(f"    rdfs:label {_ttl(item.label)} ;")
        if item.definition:
            lines.append(f"    rdfs:comment {_ttl(item.definition)} ;")
        if item.aliases:
            lines.append(f"    skos:altLabel {', '.join(_ttl(a) for a in item.aliases)} ;")
        if item.business_domain:
            lines.append(f"    :businessDomain {_ttl(item.business_domain)} ;")
        if item.parent_id and item.parent_id in by_id:
            lines.append(f"    rdfs:subClassOf :{_local(by_id[item.parent_id].label)} ;")
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("")

    lines.extend(["# ----------------------------------------------------------------", "# 数据属性", "# ----------------------------------------------------------------"])
    prop_links = session.execute(
        select(TypeDataProperty.type_id, DataPropertyDef).join(
            DataPropertyDef, DataPropertyDef.id == TypeDataProperty.property_id
        )
    ).all()
    for prop in props:
        local = _local(prop.label)
        lines.append(f":{local} a owl:DatatypeProperty ;")
        lines.append(f"    rdfs:label {_ttl(prop.label)} ;")
        lines.append(f"    rdfs:range {XSD_MAP.get(prop.data_type, 'xsd:string')} .")
        lines.append("")

    for type_id, prop in prop_links:
        if type_id not in by_id:
            continue
        type_local = _local(by_id[type_id].label)
        prop_local = _local(prop.label)
        if prop.is_required:
            lines.append(f":{type_local} rdfs:subClassOf [ a owl:Restriction ;")
            lines.append(f"    owl:onProperty :{prop_local} ;")
            lines.append("    owl:minCardinality 1 ] .")
    lines.append("")

    lines.extend(["# ----------------------------------------------------------------", "# 对象属性（类型间关联）", "# ----------------------------------------------------------------"])
    links = session.execute(select(TypeLink).where(TypeLink.space_id == space_id)).scalars().all()
    seen_relations: set[int] = set()
    for link in links:
        relation = relation_by_id.get(link.relation_id)
        if relation is None:
            continue
        local = _local(relation.label)
        if relation.id not in seen_relations:
            seen_relations.add(relation.id)
            lines.append(f":{local} a owl:ObjectProperty ;")
            lines.append(f"    rdfs:label {_ttl(relation.label)} ;")
            lines.append(f"    :relationCategory {_ttl(relation.category)} .")
        source = by_id.get(link.source_type_id)
        target = by_id.get(link.target_type_id)
        if source and target:
            lines.append(
                f":{_local(source.label)} rdfs:subClassOf [ a owl:Restriction ; "
                f"owl:onProperty :{local} ; owl:allValuesFrom :{_local(target.label)} ] ."
            )
    lines.append("")

    lines.extend(["# ----------------------------------------------------------------", "# SHACL 约束", "# ----------------------------------------------------------------"])
    constraints = session.execute(
        select(Constraint).where(Constraint.space_id == space_id, Constraint.is_active.is_(True))
    ).scalars().all()
    for constraint in constraints:
        item = by_id.get(constraint.type_id)
        if item is None:
            continue
        shape = f"{_local(item.label)}Shape"
        severity = {
            "violation": "sh:Violation",
            "warning": "sh:Warning",
            "info": "sh:Info",
        }.get(constraint.severity, "sh:Violation")
        lines.append(f":{shape} a sh:NodeShape ;")
        lines.append(f"    sh:targetClass :{_local(item.label)} ;")
        lines.append(f"    sh:severity {severity} ;")
        expression = (constraint.expression or "").replace('"', "'")
        lines.append(f'    sh:message "{constraint.message or expression}"@zh .')
        lines.append(f"# 原始表达式：{expression}")
        if constraint.regulation_ref:
            lines.append(f'# 依据：{constraint.regulation_ref}')
        lines.append("")

    return "\n".join(lines)


def export_skos(session, space_id: int) -> str:
    space = session.get(OntologySpace, space_id)
    if space is None:
        return "# 空间不存在\n"
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.label)
    ).scalars().all()
    by_id = {item.id: item for item in types}
    lines = [
        f"@prefix : <{space.iri}#> .",
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
        f"<{space.iri}/scheme> a skos:ConceptScheme ;",
        f"    skos:prefLabel {_ttl(space.name)} .",
        "",
    ]
    for item in types:
        local = _local(item.label)
        lines.append(f":{local} a skos:Concept ;")
        lines.append(f"    skos:prefLabel {_ttl(item.label)} ;")
        lines.append(f"    skos:inScheme <{space.iri}/scheme> ;")
        if item.definition:
            lines.append(f"    skos:definition {_ttl(item.definition)} ;")
        for alias in item.aliases or []:
            lines.append(f"    skos:altLabel {_ttl(alias)} ;")
        if item.parent_id and item.parent_id in by_id:
            lines.append(f"    skos:broader :{_local(by_id[item.parent_id].label)} ;")
        children = [child for child in types if child.parent_id == item.id]
        for child in children:
            lines.append(f"    skos:narrower :{_local(child.label)} ;")
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("")
    return "\n".join(lines)


def export_json_schemas(session, space_id: int) -> dict[str, Any]:
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.label)
    ).scalars().all()
    output: dict[str, Any] = {}
    for item in types:
        props = session.execute(
            select(DataPropertyDef, TypeDataProperty)
            .join(TypeDataProperty, TypeDataProperty.property_id == DataPropertyDef.id)
            .where(TypeDataProperty.type_id == item.id)
            .order_by(TypeDataProperty.ordinal)
        ).all()
        constraints = session.execute(
            select(Constraint).where(Constraint.type_id == item.id, Constraint.is_active.is_(True))
        ).scalars().all()
        schema: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": item.iri,
            "title": item.label,
            "description": item.definition or "",
            "type": "object",
            "properties": {},
            "required": [],
            "x-ontology": {
                "kind": item.kind,
                "business_domain": item.business_domain,
                "confidence": item.confidence,
                "version": item.version,
                "aliases": item.aliases or [],
            },
        }
        for prop, link in props:
            entry: dict[str, Any] = {
                "title": prop.label,
                "type": JSON_TYPE_MAP.get(prop.data_type, "string"),
            }
            if prop.data_type == "enum" and prop.enum_values:
                entry["enum"] = list(prop.enum_values)
            if prop.unit:
                entry["x-unit"] = prop.unit
            if prop.description:
                entry["description"] = prop.description
            schema["properties"][prop.key] = entry
            if prop.is_required or link.is_required:
                schema["required"].append(prop.key)

        shacl: list[dict[str, Any]] = []
        for constraint in constraints:
            shacl.append(
                {
                    "kind": constraint.kind,
                    "severity": constraint.severity,
                    "expression": constraint.expression,
                    "message": constraint.message,
                    "regulation_ref": constraint.regulation_ref,
                }
            )
        if shacl:
            schema["x-shacl"] = shacl
        output[item.label] = schema
    return output


def export_bundle(session, space_id: int) -> dict[str, Any]:
    space = session.get(OntologySpace, space_id)
    if space is None:
        return {"status": "missing"}
    return {
        "status": "ok",
        "space": {
            "id": space.id,
            "key": space.key,
            "name": space.name,
            "iri": space.iri,
            "domain": space.domain,
        },
        "turtle": export_turtle(session, space_id),
        "skos": export_skos(session, space_id),
        "json_schema": export_json_schemas(session, space_id),
    }
