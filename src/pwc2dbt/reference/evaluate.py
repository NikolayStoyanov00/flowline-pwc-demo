"""Evaluate an Informatica expression against a row of data.

The converter *translates* expressions into SQL. This module *computes* them,
reusing the same parser. It exists so the reference implementation in
:mod:`pwc2dbt.simulate` can run a mapping row by row.

Semantics follow Informatica's documentation, with two choices worth naming:

* NULL propagates through arithmetic and makes a comparison neither true nor
  false, which is what makes ``IIF(ISNULL(x), ...)`` the idiom it is.
* Decimal ports are computed as floats. The session in this export sets
  ``Enable high precision = NO``, which is what PowerCenter does in that mode.
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Any
from collections.abc import Mapping, Sequence

from ..naming import is_date, is_integer, is_numeric
from ..expressions import (
    date_mask_to_strftime,
    date_part,
    iif_default,
    Binary,
    Call,
    ExpressionError,
    Node,
    Number,
    Parameter,
    Reference,
    Star,
    Text,
    Unary,
    parse_expression,
)

__all__ = ["EvalError", "evaluate", "AGGREGATES", "coerce"]

AGGREGATES = frozenset({"SUM", "COUNT", "MIN", "MAX", "AVG"})


class EvalError(Exception):
    """An expression cannot be evaluated."""


# ---------------------------------------------------------------------------
# Reading values out of source data
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)


def coerce(value: Any, datatype: str) -> Any:
    """Turn a CSV string into the type its PowerCenter datatype implies."""
    if value is None or value == "":
        return None
    if is_numeric(datatype):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if is_integer(datatype) else number
    if is_date(datatype):
        if isinstance(value, (datetime, date)):
            return value
        for pattern in _TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(str(value), pattern)
            except ValueError:
                continue
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    """Informatica treats NULL as not true, and 0 as false."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return coerce(value, "timestamp")


_DATE_PART_SECONDS = {
    "day": 86400.0,
    "hour": 3600.0,
    "minute": 60.0,
    "second": 1.0,
}

def _truncate(value: Any, part: str) -> Any:
    moment = _as_datetime(value)
    if moment is None:
        return None
    if part == "year":
        return datetime(moment.year, 1, 1)
    if part == "month":
        return datetime(moment.year, moment.month, 1)
    if part == "day":
        return datetime(moment.year, moment.month, moment.day)
    if part == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    if part == "minute":
        return moment.replace(second=0, microsecond=0)
    return moment.replace(microsecond=0)


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

#: Operators whose Python equivalent is a plain binary function. Division is
#: handled separately because Informatica yields NULL rather than raising.
_ARITHMETIC = {"+": operator.add, "-": operator.sub, "*": operator.mul}

_COMPARISONS = {
    "=": operator.eq,
    "<>": operator.ne,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}

#: One-argument functions that propagate NULL and need no other handling.
_SCALARS = {
    "UPPER": lambda v: str(v).upper(),
    "LOWER": lambda v: str(v).lower(),
    "LTRIM": lambda v: str(v).lstrip(),
    "RTRIM": lambda v: str(v).rstrip(),
    "ABS": abs,
    "LENGTH": lambda v: len(str(v)),
}


class _Evaluator:
    def __init__(
        self,
        row: Mapping[str, Any],
        variables: Mapping[str, Any],
        parameters: Mapping[str, Any],
        result_datatype: str,
    ) -> None:
        self.row = row
        self.variables = variables
        self.parameters = parameters
        self.result_datatype = (result_datatype or "").lower()

    def eval(self, node: Node) -> Any:
        if isinstance(node, Number):
            text = node.text
            return int(text) if "." not in text else float(text)
        if isinstance(node, Text):
            return node.value
        if isinstance(node, Reference):
            if node.name in self.variables:
                return self.variables[node.name]
            if node.name in self.row:
                return self.row[node.name]
            raise EvalError(f"nothing supplies {node.name!r}")
        if isinstance(node, Parameter):
            if node.name not in self.parameters:
                raise EvalError(f"no value for parameter {node.name}")
            return self.parameters[node.name]
        if isinstance(node, Star):
            return 1
        if isinstance(node, Unary):
            return self.eval_unary(node)
        if isinstance(node, Binary):
            return self.eval_binary(node)
        if isinstance(node, Call):
            return self.eval_call(node)
        raise EvalError(f"cannot evaluate {type(node).__name__}")

    def eval_unary(self, node: Unary) -> Any:
        value = self.eval(node.operand)
        if node.operator == "NOT":
            return None if value is None else not _truthy(value)
        if value is None:
            return None
        return -value if node.operator == "-" else value

    def eval_binary(self, node: Binary) -> Any:
        operator = node.operator

        # Three-valued logic: these can resolve without both sides.
        if operator in {"AND", "OR"}:
            left = self.eval(node.left)
            right = self.eval(node.right)
            if operator == "AND":
                if left is False or right is False:
                    return False
                if left is None or right is None:
                    return None
                return _truthy(left) and _truthy(right)
            if _truthy(left) or _truthy(right):
                return True
            if left is None or right is None:
                return None
            return False

        left = self.eval(node.left)
        right = self.eval(node.right)
        if left is None or right is None:
            return None

        if operator == "||":
            return f"{left}{right}"
        if operator == "/":
            # Informatica logs a row error and emits NULL rather than failing.
            return None if right == 0 else left / right
        if operator in _ARITHMETIC:
            return _ARITHMETIC[operator](left, right)
        if operator in _COMPARISONS:
            return _COMPARISONS[operator](*_align(left, right))

        raise EvalError(f"unsupported operator {operator!r}")

    def eval_call(self, node: Call) -> Any:
        name = node.name
        args = node.arguments

        if name == "IIF":
            if len(args) not in {2, 3}:
                raise EvalError(f"IIF takes 2 or 3 arguments, got {len(args)}")
            if _truthy(self.eval(args[0])):
                return self.eval(args[1])
            if len(args) == 3:
                return self.eval(args[2])
            # No false branch: 0, '' or NULL by result type, as in the SQL.
            return iif_default(self.result_datatype)

        if name == "ISNULL":
            return self.eval(args[0]) is None

        if name == "TRUNC":
            part = "day" if len(args) == 1 else _part(self.eval(args[1]))
            return _truncate(self.eval(args[0]), part)

        if name == "DATE_DIFF":
            # DATE_DIFF(a, b, part) is a - b.
            later, earlier = _as_datetime(self.eval(args[0])), _as_datetime(
                self.eval(args[1])
            )
            if later is None or earlier is None:
                return None
            part = _part(self.eval(args[2]))
            if part in _DATE_PART_SECONDS:
                return (later - earlier).total_seconds() / _DATE_PART_SECONDS[part]
            if part == "month":
                return (later.year - earlier.year) * 12 + later.month - earlier.month
            if part == "year":
                return later.year - earlier.year
            raise EvalError(f"unsupported DATE_DIFF part {part!r}")

        if name == "TO_CHAR":
            moment = _as_datetime(self.eval(args[0]))
            if moment is None:
                return None
            return moment.strftime(date_mask_to_strftime(str(self.eval(args[1]))))

        if name == "TO_DATE":
            text = self.eval(args[0])
            if text is None:
                return None
            return datetime.strptime(str(text), date_mask_to_strftime(str(self.eval(args[1]))))

        if name in _SCALARS:
            # All of these take one argument and propagate NULL.
            value = self.eval(args[0])
            return None if value is None else _SCALARS[name](value)

        if name == "ROUND":
            value = self.eval(args[0])
            digits = self.eval(args[1]) if len(args) > 1 else 0
            return None if value is None else round(value, int(digits or 0))

        if name in AGGREGATES:
            raise EvalError(
                f"{name}() is an aggregate; it is evaluated over a group, "
                "not over a single row"
            )

        raise EvalError(f"unsupported function {name}()")


def _align(left: Any, right: Any) -> tuple[Any, Any]:
    """Make two values comparable.

    Source data arrives as text, so a port compared against a number may hold
    the string "1". Informatica coerces; so do we.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return _truthy(left), _truthy(right)
    if isinstance(left, (int, float)) and isinstance(right, str):
        try:
            return left, float(right)
        except ValueError:
            return str(left), right
    if isinstance(left, str) and isinstance(right, (int, float)):
        try:
            return float(left), right
        except ValueError:
            return left, str(right)
    if isinstance(left, datetime) or isinstance(right, datetime):
        return _as_datetime(left), _as_datetime(right)
    return left, right


def _part(value: Any) -> str:
    """The date part a mask names.

    The mask tables live in :mod:`pwc2dbt.expressions`. The two
    implementations are deliberately independent in *behaviour*, but a mask
    means the same thing on both sides, so the tables are shared.
    """
    part = date_part(str(value))
    if part is None:
        raise EvalError(f"unsupported date part {value!r}")
    return part


def evaluate(
    expression: str | Node,
    row: Mapping[str, Any],
    *,
    variables: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
    result_datatype: str = "",
) -> Any:
    """Compute one Informatica expression for one row."""
    tree = expression if isinstance(expression, Node) else parse_expression(expression)
    evaluator = _Evaluator(row, variables or {}, parameters or {}, result_datatype)
    try:
        return evaluator.eval(tree)
    except ExpressionError as exc:
        raise EvalError(str(exc)) from exc


def aggregate(name: str, values: Sequence[Any]) -> Any:
    """Reduce a group, ignoring NULLs as Informatica does."""
    present = [v for v in values if v is not None]
    if name == "COUNT":
        return len(present)
    if not present:
        return None
    if name == "SUM":
        return sum(present)
    if name == "MIN":
        return min(present)
    if name == "MAX":
        return max(present)
    if name == "AVG":
        return sum(present) / len(present)
    raise EvalError(f"unsupported aggregate {name}()")
