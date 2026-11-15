"""Tests for the Informatica expression translator.

Three layers, strongest last:

1. The translated text matches, for the hardest real expressions in the export.
2. The translated SQL is valid, checked by parsing all 205 of them with sqlglot.
3. The translated SQL **computes the right answer**, checked by running it in
   DuckDB and comparing against Informatica's documented semantics.

Layer 3 is the one that matters: the text being as expected proves nothing if
the semantics are wrong, and the two cases where Informatica and SQL disagree
(``DATE_DIFF`` operand order, ``IIF`` with no false branch) are invisible at
the text level.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pytest
import sqlglot

from pwc2dbt.expressions import (
    ExpressionError,
    order_variable_ports,
    parse_expression,
    stateful_variable_ports,
    translate,
)
from pwc2dbt.model import Port, Repository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def duck():
    connection = duckdb.connect()
    yield connection
    connection.close()


def evaluate(
    connection,
    expression: str,
    result_datatype: str = "",
    types: dict[str, str] | None = None,
    **columns,
):
    """Translate an Informatica expression and run it over one row of input.

    ``types`` casts a column explicitly, which is needed when its value is
    None: an untyped NULL parameter leaves DuckDB unable to pick an overload,
    whereas a real model column always has a declared type.
    """
    sql = translate(expression, result_datatype=result_datatype)
    if not columns:
        return connection.execute(f"select {sql}").fetchone()[0]

    types = types or {}
    names = list(columns)
    row = ", ".join(
        f"?::{types[name]} as {name.lower()}" if name in types
        else f"? as {name.lower()}"
        for name in names
    )
    statement = f"with r as (select {row}) select {sql} from r"
    return connection.execute(statement, [columns[n] for n in names]).fetchone()[0]


def all_expressions(repo: Repository) -> list[tuple[str, str, str]]:
    """Every expression in the export, as (label, text, datatype)."""
    found = []
    for mapping in repo.mappings:
        for transformation in mapping.transformations.values():
            for port in transformation.ports:
                if port.expression and not port.is_passthrough:
                    found.append(
                        (
                            f"{transformation.name}.{port.name}",
                            port.expression,
                            port.datatype,
                        )
                    )
            for group in transformation.groups:
                if group.expression:
                    found.append(
                        (f"{transformation.name}.{group.name}", group.expression, "")
                    )
            for key in ("Filter Condition", "Join Condition"):
                condition = transformation.attribute(key)
                if condition:
                    found.append((f"{transformation.name}.{key}", condition, ""))
    return found


# ---------------------------------------------------------------------------
# 1. Against the real export
# ---------------------------------------------------------------------------


class TestRealExport:
    def test_every_expression_in_the_export_translates(self, repo) -> None:
        failures = []
        for label, text, datatype in all_expressions(repo):
            try:
                translate(text, result_datatype=datatype)
            except ExpressionError as exc:
                failures.append(f"{label}: {text!r} -> {exc}")
        assert failures == []

    def test_the_export_contains_the_expressions_we_think_it_does(self, repo) -> None:
        # Guards the claim the rest of the suite is sized against.
        assert len(all_expressions(repo)) == 205

    def test_translated_sql_is_valid_sql(self, repo) -> None:
        """Every translation parses as DuckDB SQL.

        dbt's `{{ var() }}` is Jinja, not SQL, so it is stubbed out first.
        """
        failures = []
        for label, text, datatype in all_expressions(repo):
            sql = translate(text, result_datatype=datatype)
            probe = sql.replace("'{{ var(\"run_date\") }}'", "current_date")
            try:
                sqlglot.parse_one(f"select {probe}", dialect="duckdb")
            except sqlglot.ParseError as exc:
                failures.append(f"{label}: {probe!r} -> {exc}")
        assert failures == []

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            # Nested IIF, four deep.
            (
                "IIF(COUNT_LIFETIME_ORDERS=0,'PROSPECT',"
                "IIF(RFM_SCORE=333,'CHAMPION',"
                "IIF(R_SCORE=3 AND F_SCORE>=2 AND M_SCORE>=2,'LOYAL',"
                "IIF(R_SCORE=1 AND F_SCORE>=2,'AT_RISK','STANDARD'))))",
                "case when count_lifetime_orders = 0 then 'PROSPECT' "
                "else case when rfm_score = 333 then 'CHAMPION' "
                "else case when r_score = 3 and f_score >= 2 and m_score >= 2 "
                "then 'LOYAL' "
                "else case when r_score = 1 and f_score >= 2 then 'AT_RISK' "
                "else 'STANDARD' end end end end",
            ),
            # Function nesting, and brackets that must survive.
            (
                "IIF( UPPER(LTRIM(RTRIM(TYPE))) = 'JAFFLE', 1, 0 )",
                "case when upper(ltrim(rtrim(type))) = 'JAFFLE' then 1 else 0 end",
            ),
            (
                "IIF(PRODUCT_PRICE = 0, 0, "
                "(PRODUCT_PRICE - v_SUPPLY_COST_CL) / PRODUCT_PRICE)",
                "case when product_price = 0 then 0 "
                "else (product_price - v_supply_cost_cl) / product_price end",
            ),
            # ISNULL in its two forms.
            ("NOT ISNULL(LOCATION_ID)", "location_id is not null"),
            (
                "IIF(ISNULL(TAX_RATE), 0, TAX_RATE)",
                "case when tax_rate is null then 0 else tax_rate end",
            ),
            # Informatica's filtered aggregate idiom.
            (
                "SUM(IIF(ORDER_VALUE_BAND = 'HIGH', 1, 0))",
                "sum(case when order_value_band = 'HIGH' then 1 else 0 end)",
            ),
            ("COUNT(*)", "count(*)"),
            # Dates.
            ("TRUNC(OPENED_AT)", "date_trunc('day', opened_at)"),
            (
                "TO_DATE(TO_CHAR(ORDERED_AT, 'DD-MM-YY'), 'DD-MM-YY')",
                "strptime(strftime(ordered_at, '%d-%m-%y'), '%d-%m-%y')",
            ),
            # A mapping variable becomes a dbt var, quoted so it cannot be
            # read as arithmetic, and DATE_DIFF reverses its operands.
            (
                "DATE_DIFF(TRUNC($$RUN_DATE), TRUNC(LAST_ORDERED_AT), 'DD')",
                "date_diff('day', date_trunc('day', last_ordered_at), "
                "date_trunc('day', '{{ var(\"run_date\") }}'))",
            ),
            # OR, and a disjunction inside a condition.
            (
                "IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)",
                "case when perishable = 'Y' or perishable = '1' then 1 else 0 end",
            ),
            ("REGION = 'APAC'", "region = 'APAC'"),
            ("ORDER_TOTAL >= 50", "order_total >= 50"),
            ("PRICE/100", "price / 100"),
        ],
    )
    def test_hard_real_expressions(self, expression: str, expected: str) -> None:
        assert translate(expression) == expected


# ---------------------------------------------------------------------------
# 2. Executed semantics
# ---------------------------------------------------------------------------


class TestExecutedSemantics:
    """Run the translated SQL and check the answer, not the text."""

    def test_date_diff_keeps_informatica_operand_order(self, duck) -> None:
        """``DATE_DIFF(a, b, 'DD')`` is ``a - b``.

        SQL's ``date_diff(part, start, end)`` is ``end - start``, so the
        operands swap during translation. If they did not, every recency in
        CUSTOMER_360 would come out negative.
        """
        days = evaluate(
            duck,
            "DATE_DIFF(RUN_DATE, LAST_ORDERED_AT, 'DD')",
            RUN_DATE=date(2026, 1, 10),
            LAST_ORDERED_AT=date(2026, 1, 1),
        )
        assert days == 9

    def test_recency_of_a_customer_who_never_ordered(self, duck) -> None:
        # The real CUSTOMER_360 expression, with the mapping variable inlined.
        expression = (
            "IIF(ISNULL(LAST_ORDERED_AT), 9999, "
            "DATE_DIFF(TRUNC(RUN_DATE), TRUNC(LAST_ORDERED_AT), 'DD'))"
        )
        assert (
            evaluate(
                duck,
                expression,
                types={"LAST_ORDERED_AT": "timestamp"},
                RUN_DATE=datetime(2026, 1, 31, 12, 0),
                LAST_ORDERED_AT=None,
            )
            == 9999
        )
        # And TRUNC must drop the time, or a part-day would shift the count.
        assert (
            evaluate(
                duck,
                expression,
                RUN_DATE=datetime(2026, 1, 31, 23, 59),
                LAST_ORDERED_AT=datetime(2026, 1, 1, 0, 1),
            )
            == 30
        )

    def test_trunc_drops_the_time_part(self, duck) -> None:
        assert evaluate(
            duck, "TRUNC(OPENED_AT)", OPENED_AT=datetime(2026, 3, 4, 15, 30, 45)
        ) == datetime(2026, 3, 4, 0, 0)

    def test_to_date_to_char_round_trip_truncates_to_a_day(self, duck) -> None:
        """A faithful translation of a strange expression.

        `EXP_ORDERS.ORDERED_AT_DAY` formats a timestamp to 'DD-MM-YY' and
        parses it straight back. The effect is truncation to midnight, with the
        century dropped on the way. We reproduce it rather than simplify it.
        """
        assert evaluate(
            duck,
            "TO_DATE(TO_CHAR(ORDERED_AT, 'DD-MM-YY'), 'DD-MM-YY')",
            ORDERED_AT=datetime(2026, 3, 4, 15, 30),
        ) == datetime(2026, 3, 4, 0, 0)

    @pytest.mark.parametrize(
        ("type_value", "expected"),
        [("JAFFLE", 1), ("  jaffle  ", 1), ("BEVERAGE", 0)],
    )
    def test_is_food_item_trims_and_uppercases(
        self, duck, type_value: str, expected: int
    ) -> None:
        assert (
            evaluate(
                duck,
                "IIF( UPPER(LTRIM(RTRIM(TYPE))) = 'JAFFLE', 1, 0 )",
                TYPE=type_value,
            )
            == expected
        )

    @pytest.mark.parametrize(
        ("orders", "score", "r", "f", "m", "expected"),
        [
            (0, 0, 1, 1, 1, "PROSPECT"),
            (5, 333, 3, 3, 3, "CHAMPION"),
            (5, 322, 3, 2, 2, "LOYAL"),
            (5, 123, 1, 2, 2, "AT_RISK"),
            (5, 211, 2, 1, 1, "STANDARD"),
        ],
    )
    def test_rfm_segment_picks_the_right_branch(
        self, duck, orders, score, r, f, m, expected
    ) -> None:
        """Every branch of the four-deep nested IIF, executed."""
        expression = (
            "IIF(COUNT_LIFETIME_ORDERS=0,'PROSPECT',"
            "IIF(RFM_SCORE=333,'CHAMPION',"
            "IIF(R_SCORE=3 AND F_SCORE>=2 AND M_SCORE>=2,'LOYAL',"
            "IIF(R_SCORE=1 AND F_SCORE>=2,'AT_RISK','STANDARD'))))"
        )
        assert (
            evaluate(
                duck,
                expression,
                COUNT_LIFETIME_ORDERS=orders,
                RFM_SCORE=score,
                R_SCORE=r,
                F_SCORE=f,
                M_SCORE=m,
            )
            == expected
        )

    def test_divide_by_zero_guard_is_preserved(self, duck) -> None:
        assert (
            evaluate(
                duck,
                "IIF(PRODUCT_PRICE = 0, 0, (PRODUCT_PRICE - v_SUPPLY_COST_CL) "
                "/ PRODUCT_PRICE)",
                PRODUCT_PRICE=0.0,
                v_SUPPLY_COST_CL=3.0,
            )
            == 0
        )
        assert evaluate(
            duck,
            "IIF(PRODUCT_PRICE = 0, 0, (PRODUCT_PRICE - v_SUPPLY_COST_CL) "
            "/ PRODUCT_PRICE)",
            PRODUCT_PRICE=10.0,
            v_SUPPLY_COST_CL=4.0,
        ) == pytest.approx(0.6)

    def test_filtered_aggregate_only_totals_matching_rows(self, duck) -> None:
        """Informatica's two-argument ``SUM(value, condition)``.

        Unused in this export, but a documented feature, so it is translated
        and checked rather than silently mistranslated if it ever appears.
        """
        sql = translate("SUM(AMOUNT, BAND = 'HIGH')")
        assert sql == "sum(case when band = 'HIGH' then amount end)"
        total = duck.execute(
            "with r(amount, band) as (values (10, 'HIGH'), (5, 'LOW'), (7, 'HIGH')) "
            f"select {sql} from r"
        ).fetchone()[0]
        assert total == 17

    def test_null_handling_of_isnull_matches_sql(self, duck) -> None:
        assert evaluate(duck, "ISNULL(X)", X=None) is True
        assert evaluate(duck, "ISNULL(X)", X=1) is False
        assert evaluate(duck, "NOT ISNULL(X)", X=None) is False


class TestOperatorPrecedence:
    """Brackets are added only where they change the answer."""

    @pytest.mark.parametrize(
        ("expression", "expected_sql"),
        [
            ("A - B - C", "a - b - c"),
            ("A - (B - C)", "a - (b - c)"),
            ("(A + B) * C", "(a + b) * c"),
            ("A + B * C", "a + b * c"),
            ("A / B / C", "a / b / c"),
            ("A AND B OR C", "a and b or c"),
            ("A AND (B OR C)", "a and (b or c)"),
            ("NOT (A AND B)", "not (a and b)"),
        ],
    )
    def test_brackets_are_kept_only_where_needed(
        self, expression: str, expected_sql: str
    ) -> None:
        assert translate(expression) == expected_sql

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("A - B - C", 2),
            ("A - (B - C)", 8),
            ("(A + B) * C", 45),
            ("A + B * C", 25),
        ],
    )
    def test_the_arithmetic_actually_evaluates_correctly(
        self, duck, expression: str, expected: int
    ) -> None:
        assert evaluate(duck, expression, A=10, B=5, C=3) == expected

    def test_logical_precedence_evaluates_correctly(self, duck) -> None:
        # "A AND B OR C": AND binds tighter, so false AND false OR true is true.
        assert (
            evaluate(duck, "A AND B OR C", A=False, B=False, C=True) is True
        )
        assert (
            evaluate(duck, "A AND (B OR C)", A=False, B=False, C=True) is False
        )


# ---------------------------------------------------------------------------
# 3. IIF with no false branch
# ---------------------------------------------------------------------------


class TestTwoArgumentIif:
    """``IIF(condition, value)`` has a trap in it.

    Informatica returns an empty string when the condition is false and the
    result is string-typed, but NULL for every other type. A plain
    ``CASE WHEN ... THEN ... END`` gets the string case wrong.

    No expression in this export uses the two-argument form, so this is
    defensive, but it is the kind of difference that produces data that looks
    fine and is wrong.
    """

    def test_string_result_falls_back_to_empty_string(self, duck) -> None:
        assert (
            translate("IIF(FLAG = 1, NAME)", result_datatype="string")
            == "coalesce(case when flag = 1 then name end, '')"
        )
        assert (
            evaluate(
                duck,
                "IIF(FLAG = 1, NAME)",
                result_datatype="string",
                FLAG=0,
                NAME="x",
            )
            == ""
        )

    def test_other_types_fall_back_to_null(self, duck) -> None:
        assert (
            translate("IIF(FLAG = 1, AMOUNT)", result_datatype="decimal")
            == "case when flag = 1 then amount end"
        )
        assert (
            evaluate(
                duck,
                "IIF(FLAG = 1, AMOUNT)",
                result_datatype="decimal",
                FLAG=0,
                AMOUNT=5,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 4. Local variables
# ---------------------------------------------------------------------------


def _variable(name: str, expression: str) -> Port:
    return Port(
        name=name,
        port_type="LOCAL VARIABLE",
        expression=expression,
        expression_type="GENERAL",
        datatype="decimal",
    )


class TestVariableOrdering:
    def test_variables_are_ordered_so_each_is_defined_before_use(self) -> None:
        """Declaration order is unusable: a variable appears after its users.

        `EXP_ITEM_CALCULATIONS` declares v_MARGIN_CALC, which reads
        v_SUPPLY_COST_CL, before v_SUPPLY_COST_CL itself.
        """
        ports = [
            _variable("v_MARGIN_CALC", "v_SUPPLY_COST_CL / PRODUCT_PRICE"),
            _variable("v_SUPPLY_COST_CL", "IIF(ISNULL(SUPPLY_COST), 0, SUPPLY_COST)"),
        ]
        assert [p.name for p in order_variable_ports(ports)] == [
            "v_SUPPLY_COST_CL",
            "v_MARGIN_CALC",
        ]

    def test_a_chain_three_deep_is_ordered(self) -> None:
        ports = [
            _variable("v_c", "v_b + 1"),
            _variable("v_a", "AMOUNT"),
            _variable("v_b", "v_a * 2"),
        ]
        assert [p.name for p in order_variable_ports(ports)] == ["v_a", "v_b", "v_c"]

    def test_non_variable_ports_are_left_out(self) -> None:
        ports = [
            Port(name="TOTAL", port_type="OUTPUT", expression="v_a + 1"),
            _variable("v_a", "AMOUNT"),
        ]
        assert [p.name for p in order_variable_ports(ports)] == ["v_a"]

    def test_mutually_referencing_variables_are_rejected(self) -> None:
        ports = [_variable("v_a", "v_b + 1"), _variable("v_b", "v_a + 1")]
        with pytest.raises(ExpressionError, match="cycle"):
            order_variable_ports(ports)

    def test_every_other_transformation_in_the_export_orders_cleanly(
        self, repo
    ) -> None:
        for mapping in repo.mappings:
            for transformation in mapping.transformations.values():
                if stateful_variable_ports(transformation.ports):
                    continue
                ordered = order_variable_ports(transformation.ports)
                defined: set[str] = set()
                known = {p.name for p in ordered}
                for port in ordered:
                    used = _names_in(port.expression) & known
                    assert used <= defined, (
                        f"{transformation.name}.{port.name} uses "
                        f"{used - defined} before it is defined"
                    )
                    defined.add(port.name)


class TestStatefulVariables:
    """Variables that read their own previous value.

    PowerCenter evaluates variable ports once per row and keeps their value
    between rows, so this is how a running counter is written:

        v_ORDER_SEQUENCE = IIF(v_PREV_CUSTOMER_ID = CUSTOMER_ID,
                               v_ORDER_SEQUENCE + 1, 1)

    No expression computes that. It is `row_number()` written by hand, and
    translating it needs the ordering from the upstream Sorter, so it is
    handled where joins and window functions are, not here.
    """

    def test_the_export_contains_exactly_two_of_them(self, repo) -> None:
        found = {
            f"{mapping.name}.{transformation.name}.{port.name}"
            for mapping in repo.mappings
            for transformation in mapping.transformations.values()
            for port in stateful_variable_ports(transformation.ports)
        }
        assert found == {
            "m_FL_JS_MARTS_CORE.EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE",
            "m_FL_JS_MARTS_ENRICHED.EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE",
        }

    def test_ordering_refuses_them_by_default(self, repo) -> None:
        """They must not be silently treated as ordinary expressions, which
        would emit SQL that runs and counts the wrong thing."""
        transformation = repo.mapping("m_FL_JS_MARTS_CORE").transformations[
            "EXP_CUSTOMER_ORDER_NUM"
        ]
        with pytest.raises(ExpressionError, match="running counter"):
            order_variable_ports(transformation.ports)

    def test_ordering_allows_them_when_asked_explicitly(self, repo) -> None:
        transformation = repo.mapping("m_FL_JS_MARTS_CORE").transformations[
            "EXP_CUSTOMER_ORDER_NUM"
        ]
        ordered = order_variable_ports(transformation.ports, allow_stateful=True)
        assert [p.name for p in ordered] == ["v_PREV_CUSTOMER_ID", "v_ORDER_SEQUENCE"]

    def test_a_plain_variable_chain_is_not_flagged(self) -> None:
        ports = [_variable("v_a", "AMOUNT"), _variable("v_b", "v_a * 2")]
        assert stateful_variable_ports(ports) == ()


def _names_in(expression: str | None) -> set[str]:
    from pwc2dbt.expressions import Binary, Call, Reference, Unary

    def walk(node) -> set[str]:
        if isinstance(node, Reference):
            return {node.name}
        if isinstance(node, Unary):
            return walk(node.operand)
        if isinstance(node, Binary):
            return walk(node.left) | walk(node.right)
        if isinstance(node, Call):
            names: set[str] = set()
            for argument in node.arguments:
                names |= walk(argument)
            return names
        return set()

    return walk(parse_expression(expression)) if expression else set()


# ---------------------------------------------------------------------------
# 5. Failure modes
# ---------------------------------------------------------------------------


class TestFailures:
    def test_an_unknown_function_is_rejected_not_guessed(self) -> None:
        """Passing an unrecognised function through would emit SQL that runs
        and returns the wrong answer."""
        with pytest.raises(ExpressionError, match="unsupported function DECODE"):
            translate("DECODE(STATUS, 1, 'a', 2, 'b')")

    def test_the_error_lists_what_is_supported(self) -> None:
        with pytest.raises(ExpressionError, match="IIF"):
            translate("SOUNDEX(NAME)")

    @pytest.mark.parametrize(
        "expression",
        ["IIF(A, B", "A +", "(A + B", "A B", "IIF(,)", "A ** B"],
    )
    def test_malformed_expressions_are_rejected(self, expression: str) -> None:
        with pytest.raises(ExpressionError):
            translate(expression)

    def test_wrong_argument_count_is_reported(self) -> None:
        with pytest.raises(ExpressionError, match="IIF takes 2 or 3"):
            translate("IIF(A)")
        with pytest.raises(ExpressionError, match="ISNULL takes 1"):
            translate("ISNULL(A, B)")

    def test_an_unknown_date_part_is_reported(self) -> None:
        with pytest.raises(ExpressionError, match="unsupported date part"):
            translate("DATE_DIFF(A, B, 'FORTNIGHT')")

    def test_a_non_literal_date_part_is_reported(self) -> None:
        with pytest.raises(ExpressionError, match="literal date part"):
            translate("DATE_DIFF(A, B, SOME_COLUMN)")


class TestResolver:
    def test_names_can_be_rewritten_on_the_way_out(self) -> None:
        """The hook that later lets columns be table-qualified, and local
        variables be replaced by their own SQL."""
        assert (
            translate(
                "IIF(ISNULL(TAX_RATE), 0, TAX_RATE)",
                resolver=lambda name: f"orders.{name.lower()}",
            )
            == "case when orders.tax_rate is null then 0 else orders.tax_rate end"
        )

    def test_quotes_inside_string_literals_survive(self) -> None:
        assert translate("IIF(NAME = 'O''Brien', 1, 0)") == (
            "case when name = 'O''Brien' then 1 else 0 end"
        )
