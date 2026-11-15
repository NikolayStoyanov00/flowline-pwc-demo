"""Translating PowerCenter names into dbt names."""

from __future__ import annotations

import re

__all__ = [
    "model_name",
    "schema_from_pre_sql",
    "layer_for_schema",
    "source_name",
]

_TRUNCATE = re.compile(
    r"^\s*TRUNCATE\s+TABLE\s+(?:(?P<schema>[\w$#]+)\s*\.\s*)?(?P<table>[\w$#]+)",
    re.IGNORECASE,
)

# Oracle schema -> dbt model subdirectory.
_LAYERS = {
    "STG": "staging",
    "MART": "marts",
}


def model_name(table_name: str) -> str:
    """dbt model name for a target table.

    Lowercasing is the whole rule: the export already uses names like
    ``STG_ORDER_ITEMS`` and ``STG_LOCATIONS`` that match dbt conventions.
    """
    return table_name.strip().lower()


def schema_from_pre_sql(pre_sql: str) -> str | None:
    """Read the schema out of a target's ``TRUNCATE TABLE SCHEMA.TABLE``.

    Target definitions carry no owner, so this statement is the only place a
    target's schema appears.
    """
    match = _TRUNCATE.match(pre_sql or "")
    if match is None:
        return None
    schema = match.group("schema")
    return schema.upper() if schema else None


def layer_for_schema(schema: str | None) -> str:
    """dbt subdirectory for an Oracle schema, e.g. ``MART`` -> ``marts``."""
    return _LAYERS.get((schema or "").upper(), "marts")


def source_name(schema: str) -> str:
    """Name of the dbt source group for a schema, e.g. ``RAW`` -> ``raw``."""
    return schema.strip().lower()


_TEXT_TYPES = {"varchar2", "varchar", "char", "nchar", "nvarchar2", "string", "text"}
_DATE_TYPES = {"date", "timestamp", "date/time", "datetime"}


def sql_type(datatype: str) -> str:
    """The SQL type a PowerCenter source column should be read as.

    Source columns are cast explicitly rather than left to the warehouse,
    because the mapping's behaviour depends on the declared type. The session
    in this export has ``Enable high precision = NO``, which means PowerCenter
    computes decimals as doubles, so numerics are read as ``double`` rather
    than as a scaled decimal.
    """
    kind = (datatype or "").strip().lower()
    if kind in _TEXT_TYPES:
        return "varchar"
    if kind in _DATE_TYPES:
        return "timestamp"
    if kind == "integer":
        return "bigint"
    if kind.startswith("number") or kind in {"decimal", "double", "real", "float"}:
        return "double"
    return "varchar"
