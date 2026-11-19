"""Read a PowerCenter export (XML) into :mod:`pwc2dbt.model` objects.

The only module that touches XML.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping as MappingABC
from xml.etree import ElementTree

from .model import (
    Column,
    Connector,
    FieldDependency,
    Group,
    Instance,
    Kind,
    Mapping,
    MappingVariable,
    Port,
    Repository,
    Table,
    Transformation,
)
from .validate import ParseError, validate_envelope

__all__ = ["parse_file", "parse_string", "ParseError"]

_WHITESPACE = re.compile(r"\s+")
#: A single-quoted literal, with '' as an escaped quote, as the tokenizer reads it.
_LITERAL = re.compile(r"('(?:[^']|'')*')")


# ---------------------------------------------------------------------------
# Attribute readers
# ---------------------------------------------------------------------------


def _text(element: ElementTree.Element, name: str, default: str = "") -> str:
    value = element.get(name)
    return default if value is None else value.strip()


def _int(element: ElementTree.Element, name: str, default: int = 0) -> int:
    raw = element.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _yes(element: ElementTree.Element, name: str, default: bool = False) -> bool:
    raw = element.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().upper() in {"YES", "TRUE", "1"}


def _normalise(value: str | None) -> str | None:
    """Collapse whitespace, returning None for an empty result.

    Expressions are written across several lines in the XML, arriving with
    embedded CR/LF. Collapsing them here means the expression parser never
    sees a line break.

    Quoted literals are left exactly as written: ``'A  B'`` is data, and
    collapsing it to ``'A B'`` would change what the expression compares to.
    """
    if value is None:
        return None
    pieces = _LITERAL.split(value)
    # split() with a capturing group alternates: outside, literal, outside...
    collapsed = "".join(
        piece if index % 2 else _WHITESPACE.sub(" ", piece)
        for index, piece in enumerate(pieces)
    ).strip()
    return collapsed or None


def _table_attributes(element: ElementTree.Element) -> MappingProxyType:
    """Read ``<TABLEATTRIBUTE>`` children into a read-only dict.

    Empty values are dropped, because ``Sql Query=""`` means "no override"
    rather than "an override that is blank".
    """
    attrs: dict[str, str] = {}
    for attr in element.findall("TABLEATTRIBUTE"):
        name = _text(attr, "NAME")
        value = _normalise(attr.get("VALUE"))
        if name and value is not None:
            attrs[name] = value
    return MappingProxyType(attrs)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _parse_columns(element: ElementTree.Element, tag: str) -> tuple[Column, ...]:
    columns = [
        Column(
            name=_text(field_el, "NAME"),
            datatype=_text(field_el, "DATATYPE"),
            precision=_int(field_el, "PRECISION"),
            scale=_int(field_el, "SCALE"),
            nullable=_text(field_el, "NULLABLE", "NULL").upper() != "NOTNULL",
            key_type=_text(field_el, "KEYTYPE", "NOT A KEY"),
            field_number=_int(field_el, "FIELDNUMBER"),
            referenced_table=_text(field_el, "REFERENCEDTABLE") or None,
            referenced_field=_text(field_el, "REFERENCEDFIELD") or None,
        )
        for field_el in element.findall(tag)
    ]
    # Document order does not always match FIELDNUMBER, and column order ends
    # up in the generated SQL.
    return tuple(sorted(columns, key=lambda c: (c.field_number, c.name)))


def _parse_source(element: ElementTree.Element) -> Table:
    return Table(
        name=_text(element, "NAME"),
        columns=_parse_columns(element, "SOURCEFIELD"),
        database_type=_text(element, "DATABASETYPE"),
        connection_name=_text(element, "DBDNAME"),
        schema=_text(element, "OWNERNAME"),
    )


def _parse_target(element: ElementTree.Element) -> Table:
    # No OWNERNAME on a TARGET; the schema comes from the target instance's
    # "Pre SQL" TRUNCATE statement instead.
    return Table(
        name=_text(element, "NAME"),
        columns=_parse_columns(element, "TARGETFIELD"),
        database_type=_text(element, "DATABASETYPE"),
    )


# ---------------------------------------------------------------------------
# Transformations
# ---------------------------------------------------------------------------


def _parse_port(element: ElementTree.Element) -> Port:
    return Port(
        name=_text(element, "NAME"),
        datatype=_text(element, "DATATYPE"),
        precision=_int(element, "PRECISION"),
        scale=_int(element, "SCALE"),
        port_type=_text(element, "PORTTYPE", "INPUT"),
        expression=_normalise(element.get("EXPRESSION")),
        expression_type=_text(element, "EXPRESSIONTYPE") or None,
        group=_text(element, "GROUP") or None,
        ref_field=_text(element, "REF_FIELD") or None,
        is_sort_key=_yes(element, "ISSORTKEY"),
        sort_direction=_text(element, "SORTDIRECTION") or None,
    )


def _parse_group(element: ElementTree.Element) -> Group:
    return Group(
        name=_text(element, "NAME"),
        type=_text(element, "TYPE"),
        order=_int(element, "ORDER"),
        expression=_normalise(element.get("EXPRESSION")),
    )


def _parse_transformation(element: ElementTree.Element) -> Transformation:
    raw_type = _text(element, "TYPE")
    return Transformation(
        name=_text(element, "NAME"),
        kind=Kind.classify(raw_type, _text(element, "TEMPLATENAME") or None),
        raw_type=raw_type,
        ports=tuple(_parse_port(p) for p in element.findall("TRANSFORMFIELD")),
        attributes=_table_attributes(element),
        groups=tuple(_parse_group(g) for g in element.findall("GROUP")),
        field_dependencies=tuple(
            FieldDependency(
                input_field=_text(fd, "INPUTFIELD"),
                output_field=_text(fd, "OUTPUTFIELD"),
            )
            for fd in element.findall("FIELDDEPENDENCY")
        ),
    )


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def _parse_instance(
    element: ElementTree.Element,
    definitions: MappingABC[str, Transformation],
) -> Instance:
    definition_name = _text(element, "TRANSFORMATION_NAME") or _text(element, "NAME")

    # An INSTANCE records TRANSFORMATION_TYPE but not TEMPLATENAME, so a Union
    # looks like an unrecognised custom transformation here. Take the kind from
    # the definition, which has both.
    definition = definitions.get(definition_name)
    kind = (
        definition.kind
        if definition is not None
        else Kind.classify(_text(element, "TRANSFORMATION_TYPE"))
    )

    return Instance(
        name=_text(element, "NAME"),
        definition_name=definition_name,
        kind=kind,
        instance_type=_text(element, "TYPE", "TRANSFORMATION"),
        connection_name=_text(element, "DBDNAME"),
        associated_sources=tuple(
            _text(src, "NAME") for src in element.findall("ASSOCIATED_SOURCE_INSTANCE")
        ),
        attributes=_table_attributes(element),
    )


def _parse_connector(element: ElementTree.Element) -> Connector:
    return Connector(
        from_instance=_text(element, "FROMINSTANCE"),
        from_field=_text(element, "FROMFIELD"),
        to_instance=_text(element, "TOINSTANCE"),
        to_field=_text(element, "TOFIELD"),
        from_instance_type=_text(element, "FROMINSTANCETYPE"),
        to_instance_type=_text(element, "TOINSTANCETYPE"),
    )


def _parse_mapping(element: ElementTree.Element) -> Mapping:
    transformations = {}
    for trans_el in element.findall("TRANSFORMATION"):
        transformation = _parse_transformation(trans_el)
        transformations[transformation.name] = transformation

    instances = {}
    for inst_el in element.findall("INSTANCE"):
        instance = _parse_instance(inst_el, transformations)
        instances[instance.name] = instance

    load_order = {
        _text(tlo, "TARGETINSTANCE"): _int(tlo, "ORDER")
        for tlo in element.findall("TARGETLOADORDER")
    }

    return Mapping(
        name=_text(element, "NAME"),
        transformations=MappingProxyType(transformations),
        instances=MappingProxyType(instances),
        connectors=tuple(_parse_connector(c) for c in element.findall("CONNECTOR")),
        variables=tuple(
            MappingVariable(
                name=_text(var, "NAME"),
                datatype=_text(var, "DATATYPE"),
                default_value=_text(var, "DEFAULTVALUE"),
            )
            for var in element.findall("MAPPINGVARIABLE")
        ),
        target_load_order=MappingProxyType(load_order),
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _parse_root(root: ElementTree.Element) -> Repository:
    repository_el, folder_el = validate_envelope(root)

    sources: dict[str, Table] = {}
    targets: dict[str, Table] = {}
    mappings: list[Mapping] = []

    # Walk children in document order: SOURCE, TARGET and MAPPING blocks are
    # interleaved rather than grouped.
    for child in folder_el:
        if child.tag == "SOURCE":
            source = _parse_source(child)
            sources[source.name] = source
        elif child.tag == "TARGET":
            target = _parse_target(child)
            targets[target.name] = target
        elif child.tag == "MAPPING":
            mappings.append(_parse_mapping(child))

    # SESSION, WORKFLOW and CONFIG are skipped: they describe scheduling and
    # database connections, not the transformation itself. The run order they
    # encode is implied by the generated models' ref() dependencies.

    return Repository(
        name=_text(repository_el, "NAME"),
        folder_name=_text(folder_el, "NAME"),
        database_type=_text(repository_el, "DATABASETYPE"),
        sources=MappingProxyType(sources),
        targets=MappingProxyType(targets),
        mappings=tuple(mappings),
    )


def parse_string(xml: str | bytes) -> Repository:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ParseError(f"malformed XML: {exc}") from exc
    return _parse_root(root)


def parse_file(path: str | Path) -> Repository:
    path = Path(path)
    if not path.is_file():
        raise ParseError(f"no such file: {path}")
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        raise ParseError(f"malformed XML in {path}: {exc}") from exc
    return _parse_root(tree.getroot())
