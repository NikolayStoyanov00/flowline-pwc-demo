"""Translating PowerCenter names into dbt names."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "model_name",
    "schema_from_pre_sql",
    "layer_for_schema",
    "project_name",
    "Layer",
    "LAYERS",
    "source_name",
    "sql_type",
    "is_string",
    "is_date",
    "is_numeric",
    "is_integer",
]

_TRUNCATE = re.compile(
    r"^\s*TRUNCATE\s+TABLE\s+(?:(?P<schema>[\w$#]+)\s*\.\s*)?(?P<table>[\w$#]+)",
    re.IGNORECASE,
)

@dataclass(frozen=True, slots=True)
class Layer:
    """A dbt model subdirectory and how its models are materialized.

    The two belong together: a layer with a directory but no materialization
    silently falls back to a view, and a shared model built as a view is
    re-executed once per consumer.
    """

    directory: str
    materialized: str


#: Staging is a view so consumers always read fresh source data, per dbt
#: convention. Marts are tables because every PowerCenter target is loaded
#: truncate-and-insert. Intermediates are tables so the work they hold is
#: computed once rather than once per consumer.
STAGING = Layer("staging", "view")
INTERMEDIATE = Layer("intermediate", "table")
MARTS = Layer("marts", "table")

#: Every layer, in the order the generated project should configure them.
LAYERS = (STAGING, INTERMEDIATE, MARTS)

# Oracle schema -> layer.
_SCHEMA_LAYERS = {
    "STG": STAGING,
    "MART": MARTS,
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
    return _SCHEMA_LAYERS.get((schema or "").upper(), MARTS).directory


def project_name(folder_name: str) -> str:
    """dbt project name for a PowerCenter folder."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", folder_name.strip().lower()).strip("_")
    return cleaned or "converted"


def source_name(schema: str) -> str:
    """Name of the dbt source group for a schema, e.g. ``RAW`` -> ``raw``."""
    return schema.strip().lower()


# PowerCenter datatype names, grouped in one place. Several modules ask "is
# this a string?" — the SQL emitter to decide a cast and an IIF fallback, the
# evaluator to parse a CSV value, the reference run to initialise a variable.
# They have to agree: whether PERISHABLE is a string decides whether
# `PERISHABLE = 'Y'` is a string comparison.
_TEXT_TYPES = frozenset(
    {"varchar2", "varchar", "char", "nchar", "nvarchar2", "string", "nstring", "text"}
)
_DATE_TYPES = frozenset({"date", "timestamp", "date/time", "datetime"})
_NUMBER_TYPES = frozenset({"decimal", "double", "integer", "real", "float"})


def _kind(datatype: str) -> str:
    return (datatype or "").strip().lower()


def is_string(datatype: str) -> bool:
    return _kind(datatype) in _TEXT_TYPES


def is_date(datatype: str) -> bool:
    return _kind(datatype) in _DATE_TYPES


def is_numeric(datatype: str) -> bool:
    kind = _kind(datatype)
    # "number", "number(p,s)" and the fixed names all count.
    return kind.startswith("number") or kind in _NUMBER_TYPES


def is_integer(datatype: str) -> bool:
    return _kind(datatype) == "integer"


def sql_type(datatype: str) -> str:
    """The SQL type a PowerCenter source column should be read as.

    Source columns are cast explicitly rather than left to the warehouse,
    because the mapping's behaviour depends on the declared type. The session
    in this export has ``Enable high precision = NO``, which means PowerCenter
    computes decimals as doubles, so numerics are read as ``double`` rather
    than as a scaled decimal.
    """
    if is_string(datatype):
        return "varchar"
    if is_date(datatype):
        return "timestamp"
    if is_integer(datatype):
        return "bigint"
    if is_numeric(datatype):
        return "double"
    return "varchar"
