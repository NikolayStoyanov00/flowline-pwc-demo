"""Translate the Informatica expression language into SQL.

Informatica expressions look like SQL but are not: ``IIF`` instead of ``CASE``,
``ISNULL(x)`` instead of ``x IS NULL``, ``=`` for equality, and local variables
that are not columns at all.

Translation happens in three steps rather than by rewriting text, because
``IIF`` nests 4 deep in this export and text substitution cannot see structure:

    tokenise  ->  parse into a tree  ->  emit SQL

An unknown function raises :class:`ExpressionError`. Guessing would produce SQL
that runs and returns the wrong answer, which is worse than failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Iterable, Mapping as MappingABC, Sequence

from .model import Port

__all__ = [
    "ExpressionError",
    "translate",
    "parse_expression",
    "order_variable_ports",
    "stateful_variable_ports",
    "SUPPORTED_FUNCTIONS",
]


class ExpressionError(Exception):
    """An expression uses something we cannot translate."""


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TokenType(Enum):
    NUMBER = auto()
    STRING = auto()
    IDENT = auto()
    PARAM = auto()
    OPERATOR = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    EOF = auto()


@dataclass(frozen=True, slots=True)
class Token:
    type: TokenType
    text: str
    position: int


_TOKEN_PATTERN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<number>\d+\.\d*|\.\d+|\d+)
    | (?P<string>'(?:[^']|'')*')
    | (?P<param>\$\$[A-Za-z_][A-Za-z_0-9]*)
    | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<operator><>|!=|>=|<=|\|\||[-+*/=<>])
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<comma>,)
    """,
    re.VERBOSE,
)

_WORD_OPERATORS = {"AND", "OR", "NOT"}


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN_PATTERN.match(text, position)
        if match is None:
            raise ExpressionError(
                f"unexpected character {text[position]!r} at position {position} "
                f"in {text!r}"
            )
        kind = match.lastgroup
        value = match.group()
        position = match.end()

        if kind == "space":
            continue
        if kind == "ident" and value.upper() in _WORD_OPERATORS:
            tokens.append(Token(TokenType.OPERATOR, value.upper(), match.start()))
        else:
            tokens.append(Token(TokenType[kind.upper()], value, match.start()))

    tokens.append(Token(TokenType.EOF, "", len(text)))
    return tokens


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


class Node:
    """Base class for expression tree nodes."""


@dataclass(frozen=True, slots=True)
class Number(Node):
    text: str


@dataclass(frozen=True, slots=True)
class Text(Node):
    """A string literal, quotes already stripped."""

    value: str


@dataclass(frozen=True, slots=True)
class Reference(Node):
    """A port, column or local variable name."""

    name: str


@dataclass(frozen=True, slots=True)
class Parameter(Node):
    """A ``$$`` mapping variable."""

    name: str


@dataclass(frozen=True, slots=True)
class Star(Node):
    """The ``*`` of ``COUNT(*)``."""


@dataclass(frozen=True, slots=True)
class Unary(Node):
    operator: str
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary(Node):
    operator: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Call(Node):
    name: str
    arguments: tuple[Node, ...]


# ---------------------------------------------------------------------------
# Parser
#
# Precedence, loosest first: OR, AND, NOT, comparison, ||, + -, * /, unary.
# ---------------------------------------------------------------------------

_COMPARISONS = {"=", "<>", "!=", ">", ">=", "<", "<="}


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0

    # -- token helpers ----------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def accept_operator(self, *options: str) -> str | None:
        token = self.current
        if token.type is TokenType.OPERATOR and token.text.upper() in options:
            self.advance()
            return token.text.upper()
        return None

    def expect(self, token_type: TokenType, label: str) -> Token:
        if self.current.type is not token_type:
            raise ExpressionError(
                f"expected {label} at position {self.current.position} "
                f"in {self.text!r}, found {self.current.text!r}"
            )
        return self.advance()

    # -- grammar ----------------------------------------------------------
    def parse(self) -> Node:
        node = self.parse_or()
        if self.current.type is not TokenType.EOF:
            raise ExpressionError(
                f"unexpected {self.current.text!r} at position "
                f"{self.current.position} in {self.text!r}"
            )
        return node

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.accept_operator("OR"):
            node = Binary("OR", node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self.accept_operator("AND"):
            node = Binary("AND", node, self.parse_not())
        return node

    def parse_not(self) -> Node:
        if self.accept_operator("NOT"):
            return Unary("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        node = self.parse_concat()
        operator = self.accept_operator(*_COMPARISONS)
        if operator:
            return Binary(operator, node, self.parse_concat())
        return node

    def parse_concat(self) -> Node:
        node = self.parse_additive()
        while self.accept_operator("||"):
            node = Binary("||", node, self.parse_additive())
        return node

    def parse_additive(self) -> Node:
        node = self.parse_multiplicative()
        while operator := self.accept_operator("+", "-"):
            node = Binary(operator, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self) -> Node:
        node = self.parse_unary()
        while operator := self.accept_operator("*", "/"):
            node = Binary(operator, node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        if operator := self.accept_operator("-", "+"):
            return Unary(operator, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Node:
        token = self.current

        if token.type is TokenType.NUMBER:
            self.advance()
            return Number(token.text)

        if token.type is TokenType.STRING:
            self.advance()
            # Informatica escapes a quote by doubling it.
            return Text(token.text[1:-1].replace("''", "'"))

        if token.type is TokenType.PARAM:
            self.advance()
            return Parameter(token.text)

        if token.type is TokenType.LPAREN:
            self.advance()
            node = self.parse_or()
            self.expect(TokenType.RPAREN, "')'")
            return node

        if token.type is TokenType.OPERATOR and token.text == "*":
            self.advance()
            return Star()

        if token.type is TokenType.IDENT:
            self.advance()
            if self.current.type is TokenType.LPAREN:
                self.advance()
                return Call(token.text.upper(), tuple(self.parse_arguments()))
            return Reference(token.text)

        raise ExpressionError(
            f"unexpected {token.text!r} at position {token.position} "
            f"in {self.text!r}"
        )

    def parse_arguments(self) -> Iterable[Node]:
        if self.current.type is TokenType.RPAREN:
            self.advance()
            return
        while True:
            yield self.parse_or()
            if self.current.type is TokenType.COMMA:
                self.advance()
                continue
            self.expect(TokenType.RPAREN, "')'")
            return


def parse_expression(text: str) -> Node:
    """Parse an Informatica expression into a tree."""
    return _Parser(text).parse()


# ---------------------------------------------------------------------------
# Emitting SQL
# ---------------------------------------------------------------------------

#: Maps an Informatica date format mask onto a strftime directive. Longest
#: first, so MONTH is matched before MON and HH24 before HH.
_DATE_MASK = [
    ("YYYY", "%Y"),
    ("MONTH", "%B"),
    ("HH24", "%H"),
    ("MON", "%b"),
    ("MM", "%m"),
    ("DDD", "%j"),
    ("DD", "%d"),
    ("YY", "%y"),
    ("HH", "%I"),
    ("MI", "%M"),
    ("SS", "%S"),
    ("AM", "%p"),
    ("PM", "%p"),
]

#: Informatica date-part names, as used by DATE_DIFF and TRUNC.
_DATE_PARTS = {
    "D": "day",
    "DD": "day",
    "DDD": "day",
    "J": "day",
    "HH": "hour",
    "HH12": "hour",
    "HH24": "hour",
    "MI": "minute",
    "MM": "month",
    "MON": "month",
    "MONTH": "month",
    "SS": "second",
    "Y": "year",
    "YY": "year",
    "YYY": "year",
    "YYYY": "year",
}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _date_mask_to_strftime(mask: str) -> str:
    out: list[str] = []
    position = 0
    upper = mask.upper()
    while position < len(mask):
        for informatica, directive in _DATE_MASK:
            if upper.startswith(informatica, position):
                out.append(directive)
                position += len(informatica)
                break
        else:
            out.append(mask[position])
            position += 1
    return "".join(out)


def _literal_text(node: Node, function: str, what: str) -> str:
    if not isinstance(node, Text):
        raise ExpressionError(
            f"{function} needs a literal {what}, got {type(node).__name__}"
        )
    return node.value


Resolver = Callable[[str], str]


def _default_resolver(name: str) -> str:
    return name.lower()


# How tightly each operator binds. Used to add brackets only where they change
# the meaning, so the generated SQL stays readable.
_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "NOT": 3,
    "=": 4,
    "<>": 4,
    "!=": 4,
    ">": 4,
    ">=": 4,
    "<": 4,
    "<=": 4,
    "||": 5,
    "+": 6,
    "-": 6,
    "*": 7,
    "/": 7,
}

#: Anything self-delimiting: a literal, a name, ``f(...)``, ``case ... end``.
_ATOM = 100

#: ``x is null`` binds like a comparison, not like an atom.
_COMPARISON_FUNCTIONS = {"ISNULL"}


def _precedence(node: Node) -> int:
    if isinstance(node, Binary):
        return _PRECEDENCE[node.operator]
    if isinstance(node, Unary):
        return 3 if node.operator == "NOT" else 8
    if isinstance(node, Call) and node.name in _COMPARISON_FUNCTIONS:
        return 4
    return _ATOM


#: PowerCenter datatypes that must reach SQL as a timestamp, not a bare value.
_DATE_TYPES = {"date/time", "date", "timestamp"}
_STRING_TYPES = {"string", "nstring", "text"}


class _Emitter:
    def __init__(
        self,
        resolver: Resolver,
        result_datatype: str,
        parameters: MappingABC[str, str] | None = None,
    ) -> None:
        self.resolve = resolver
        self.result_datatype = (result_datatype or "").lower()
        self.parameters = parameters or {}

    def emit_parameter(self, node: Parameter) -> str:
        """A ``$$`` variable becomes a dbt ``var()``.

        The quoting matters. ``{{ var('run_date') }}`` on its own renders to a
        bare ``2026-04-01``, which SQL reads as the arithmetic 2026 - 4 - 1.
        A date variable therefore has to be quoted and cast.
        """
        jinja = '{{ var("' + node.name.lstrip("$").lower() + '") }}'
        datatype = (self.parameters.get(node.name) or "").lower()
        if datatype in _DATE_TYPES:
            return f"cast('{jinja}' as timestamp)"
        if datatype and datatype not in _STRING_TYPES:
            return jinja
        # Strings, and anything undeclared: a quoted literal is the safe
        # reading, since an unquoted one can parse as something else entirely.
        return f"'{jinja}'"

    # -- dispatch ---------------------------------------------------------
    def emit(self, node: Node, minimum: int = 0) -> str:
        """Emit ``node``, bracketing it if it binds more loosely than ``minimum``."""
        sql = self._emit(node)
        if _precedence(node) < minimum:
            return f"({sql})"
        return sql

    def _emit(self, node: Node) -> str:
        if isinstance(node, Number):
            return node.text
        if isinstance(node, Text):
            return _quote(node.value)
        if isinstance(node, Reference):
            return self.resolve(node.name)
        if isinstance(node, Parameter):
            return self.emit_parameter(node)
        if isinstance(node, Star):
            return "*"
        if isinstance(node, Unary):
            return self.emit_unary(node)
        if isinstance(node, Binary):
            return self.emit_binary(node)
        if isinstance(node, Call):
            return self.emit_call(node)
        raise ExpressionError(f"cannot emit {type(node).__name__}")

    def emit_unary(self, node: Unary) -> str:
        if node.operator == "NOT":
            # NOT ISNULL(x) reads far better as "x is not null".
            inner = node.operand
            if isinstance(inner, Call) and inner.name == "ISNULL":
                if len(inner.arguments) != 1:
                    raise ExpressionError(
                        f"ISNULL takes 1 argument, got {len(inner.arguments)}"
                    )
                return f"{self.emit(inner.arguments[0], 4)} is not null"
            return f"not {self.emit(inner, 3)}"
        return f"{node.operator}{self.emit(node.operand, 8)}"

    def emit_binary(self, node: Binary) -> str:
        operator = {"!=": "<>", "AND": "and", "OR": "or"}.get(
            node.operator, node.operator
        )
        own = _PRECEDENCE[node.operator]
        # The right side needs one level more, so that a - (b - c) keeps its
        # brackets while a - b - c does not gain any.
        return (
            f"{self.emit(node.left, own)} {operator} "
            f"{self.emit(node.right, own + 1)}"
        )

    def emit_call(self, node: Call) -> str:
        handler = _FUNCTIONS.get(node.name)
        if handler is None:
            raise ExpressionError(
                f"unsupported function {node.name}(); supported: "
                + ", ".join(sorted(_FUNCTIONS))
            )
        return handler(self, node.arguments)

    # -- functions --------------------------------------------------------
    def fn_iif(self, args: Sequence[Node]) -> str:
        """``IIF(condition, true_value [, false_value])`` -> ``CASE WHEN``.

        With no false branch Informatica returns an empty string for a
        string-typed result and NULL otherwise, so the result datatype decides.
        """
        if len(args) == 3:
            return (
                f"case when {self.emit(args[0])} then {self.emit(args[1])} "
                f"else {self.emit(args[2])} end"
            )
        if len(args) == 2:
            body = f"case when {self.emit(args[0])} then {self.emit(args[1])} end"
            if self.result_datatype in {"string", "nstring", "text"}:
                return f"coalesce({body}, '')"
            return body
        raise ExpressionError(f"IIF takes 2 or 3 arguments, got {len(args)}")

    def fn_isnull(self, args: Sequence[Node]) -> str:
        if len(args) != 1:
            raise ExpressionError(f"ISNULL takes 1 argument, got {len(args)}")
        return f"{self.emit(args[0], 4)} is null"

    def fn_trunc(self, args: Sequence[Node]) -> str:
        """``TRUNC`` on a date, truncating to a date part.

        Informatica uses one name for both date and number truncation, and the
        export does not record which is meant. Every use in this export is a
        date, so the date reading is taken; see the README for the risk.
        """
        if len(args) == 1:
            return f"date_trunc('day', {self.emit(args[0])})"
        if len(args) == 2:
            part = _date_part(_literal_text(args[1], "TRUNC", "date part"))
            return f"date_trunc('{part}', {self.emit(args[0])})"
        raise ExpressionError(f"TRUNC takes 1 or 2 arguments, got {len(args)}")

    def fn_date_diff(self, args: Sequence[Node]) -> str:
        """``DATE_DIFF(a, b, part)`` is ``a - b``.

        SQL's ``date_diff(part, start, end)`` is ``end - start``, so the two
        operands swap over. Getting this backwards silently flips every sign.
        """
        if len(args) != 3:
            raise ExpressionError(f"DATE_DIFF takes 3 arguments, got {len(args)}")
        part = _date_part(_literal_text(args[2], "DATE_DIFF", "date part"))
        return f"date_diff('{part}', {self.emit(args[1])}, {self.emit(args[0])})"

    def fn_to_char(self, args: Sequence[Node]) -> str:
        if len(args) != 2:
            raise ExpressionError(f"TO_CHAR takes 2 arguments, got {len(args)}")
        mask = _date_mask_to_strftime(_literal_text(args[1], "TO_CHAR", "format mask"))
        return f"strftime({self.emit(args[0])}, {_quote(mask)})"

    def fn_to_date(self, args: Sequence[Node]) -> str:
        if len(args) != 2:
            raise ExpressionError(f"TO_DATE takes 2 arguments, got {len(args)}")
        mask = _date_mask_to_strftime(_literal_text(args[1], "TO_DATE", "format mask"))
        return f"strptime({self.emit(args[0])}, {_quote(mask)})"

    def simple(self, sql_name: str, arity: int) -> Callable[[Sequence[Node]], str]:
        def handler(args: Sequence[Node]) -> str:
            if len(args) != arity:
                raise ExpressionError(
                    f"{sql_name.upper()} takes {arity} argument(s), got {len(args)}"
                )
            return f"{sql_name}({', '.join(self.emit(a) for a in args)})"

        return handler

    def aggregate(self, sql_name: str) -> Callable[[Sequence[Node]], str]:
        """An aggregate, with Informatica's optional filter argument.

        ``SUM(x, condition)`` only totals rows matching the condition, which is
        ``sum(case when condition then x end)`` in SQL.
        """

        def handler(args: Sequence[Node]) -> str:
            if len(args) == 1:
                return f"{sql_name}({self.emit(args[0])})"
            if len(args) == 2:
                return (
                    f"{sql_name}(case when {self.emit(args[1])} "
                    f"then {self.emit(args[0])} end)"
                )
            raise ExpressionError(
                f"{sql_name.upper()} takes 1 or 2 arguments, got {len(args)}"
            )

        return handler


def _date_part(mask: str) -> str:
    part = _DATE_PARTS.get(mask.strip().upper())
    if part is None:
        raise ExpressionError(
            f"unsupported date part {mask!r}; supported: "
            + ", ".join(sorted(set(_DATE_PARTS)))
        )
    return part


def _build_function_table() -> dict[str, Callable[[_Emitter, Sequence[Node]], str]]:
    table: dict[str, Callable[[_Emitter, Sequence[Node]], str]] = {
        "IIF": lambda e, a: e.fn_iif(a),
        "ISNULL": lambda e, a: e.fn_isnull(a),
        "TRUNC": lambda e, a: e.fn_trunc(a),
        "DATE_DIFF": lambda e, a: e.fn_date_diff(a),
        "TO_CHAR": lambda e, a: e.fn_to_char(a),
        "TO_DATE": lambda e, a: e.fn_to_date(a),
    }
    for name, sql_name, arity in (
        ("UPPER", "upper", 1),
        ("LOWER", "lower", 1),
        ("LTRIM", "ltrim", 1),
        ("RTRIM", "rtrim", 1),
        ("ABS", "abs", 1),
        ("LENGTH", "length", 1),
        ("ROUND", "round", 2),
    ):
        table[name] = (
            lambda e, a, sql_name=sql_name, arity=arity: e.simple(sql_name, arity)(a)
        )
    for name in ("SUM", "COUNT", "MIN", "MAX", "AVG"):
        sql_name = name.lower()
        table[name] = lambda e, a, sql_name=sql_name: e.aggregate(sql_name)(a)
    return table


_FUNCTIONS = _build_function_table()

#: Informatica functions this module can translate.
SUPPORTED_FUNCTIONS = frozenset(_FUNCTIONS)


def translate(
    expression: str,
    *,
    resolver: Resolver | None = None,
    result_datatype: str = "",
    parameters: MappingABC[str, str] | None = None,
) -> str:
    """Translate one Informatica expression into SQL.

    ``resolver`` turns a bare name into SQL, which is how local variables get
    substituted and how columns get table-qualified later. It lowercases by
    default.
    """
    tree = parse_expression(expression)
    emitter = _Emitter(resolver or _default_resolver, result_datatype, parameters)
    return emitter.emit(tree)


# ---------------------------------------------------------------------------
# Local variables
# ---------------------------------------------------------------------------


def _references(node: Node) -> set[str]:
    if isinstance(node, Reference):
        return {node.name}
    if isinstance(node, Unary):
        return _references(node.operand)
    if isinstance(node, Binary):
        return _references(node.left) | _references(node.right)
    if isinstance(node, Call):
        return set().union(*(_references(a) for a in node.arguments)) if node.arguments else set()
    return set()


def stateful_variable_ports(ports: Iterable[Port]) -> tuple[Port, ...]:
    """Local variables that reference themselves.

    PowerCenter evaluates variable ports once per row and keeps their value
    between rows, so a variable may read its own previous value::

        v_ORDER_SEQUENCE = IIF(v_PREV_CUSTOMER_ID = CUSTOMER_ID,
                               v_ORDER_SEQUENCE + 1, 1)

    That is a running counter, i.e. a hand-rolled window function. There is no
    expression that computes it, so it cannot be translated as one; it needs
    ``row_number() over (...)``, which in turn needs the ordering from the
    upstream Sorter.
    """
    stateful = []
    for port in ports:
        if not (port.is_variable and port.expression):
            continue
        if port.name in _references(parse_expression(port.expression)):
            stateful.append(port)
    return tuple(stateful)


def order_variable_ports(
    ports: Iterable[Port], *, allow_stateful: bool = False
) -> tuple[Port, ...]:
    """Order local variable ports so each is defined before it is used.

    They are declared *after* the outputs that consume them and can reference
    each other, so document order is not usable.

    Raises on a self-referencing variable unless ``allow_stateful`` is set,
    because no ordering makes such a variable work as an expression. Callers
    that handle them as window functions opt in explicitly, so the case cannot
    be met by accident.
    """
    variables = [p for p in ports if p.is_variable]
    by_name = {p.name: p for p in variables}

    if not allow_stateful:
        stateful = stateful_variable_ports(variables)
        if stateful:
            raise ExpressionError(
                "local variable(s) "
                + ", ".join(p.name for p in stateful)
                + " read their own previous value, which is a running "
                "counter rather than an expression; translate with a window "
                "function instead"
            )

    needs = {
        p.name: {
            name
            for name in _references(parse_expression(p.expression))
            if name in by_name and name != p.name
        }
        for p in variables
        if p.expression
    }
    for port in variables:
        needs.setdefault(port.name, set())

    ordered: list[Port] = []
    remaining = dict(needs)
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise ExpressionError(
                "local variables reference each other in a cycle: "
                + ", ".join(sorted(remaining))
            )
        for name in ready:
            ordered.append(by_name[name])
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)

    return tuple(ordered)
