"""Tests for the reference implementation and the comparison.

The reference implementation exists to disagree with the converter when one of
them is wrong, so most of these tests are about it computing the documented
Informatica semantics correctly on its own terms. The comparison against dbt
itself lives in `test_end_to_end.py`, since it needs a built warehouse.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pwc2dbt.compare import compare_tables, normalise, same
from pwc2dbt.evaluate import EvalError, aggregate, coerce, evaluate
from pwc2dbt.simulate import (
    VariableOrder,
    load_seeds,
    run_repository,
)

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "jaffle"


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


class TestNullSemantics:
    """NULL propagating through arithmetic is why `IIF(ISNULL(x), 0, x)` is
    everywhere in this export."""

    def test_arithmetic_with_null_is_null(self) -> None:
        assert evaluate("A + B", {"A": 1, "B": None}) is None
        assert evaluate("A * B", {"A": None, "B": 2}) is None

    def test_a_comparison_with_null_is_neither_true_nor_false(self) -> None:
        assert evaluate("A = B", {"A": None, "B": 1}) is None

    def test_a_null_condition_takes_the_false_branch(self) -> None:
        # Not the same as the condition being false, but IIF treats it so.
        assert evaluate("IIF(A = B, 'yes', 'no')", {"A": None, "B": 1}) == "no"

    def test_three_valued_and_or(self) -> None:
        assert evaluate("A AND B", {"A": False, "B": None}) is False
        assert evaluate("A AND B", {"A": True, "B": None}) is None
        assert evaluate("A OR B", {"A": True, "B": None}) is True
        assert evaluate("A OR B", {"A": False, "B": None}) is None

    def test_dividing_by_zero_gives_null_not_an_error(self) -> None:
        # Informatica logs a row error and emits NULL rather than failing.
        assert evaluate("A / B", {"A": 1, "B": 0}) is None


class TestFunctions:
    def test_iif_without_a_false_branch_depends_on_the_result_type(self) -> None:
        assert (
            evaluate("IIF(A = 1, 'x')", {"A": 0}, result_datatype="string") == ""
        )
        assert evaluate("IIF(A = 1, 5)", {"A": 0}, result_datatype="decimal") is None

    def test_date_diff_is_first_minus_second(self) -> None:
        days = evaluate(
            "DATE_DIFF(A, B, 'DD')",
            {"A": datetime(2026, 1, 10), "B": datetime(2026, 1, 1)},
        )
        assert days == 9

    def test_trunc_drops_the_time(self) -> None:
        assert evaluate("TRUNC(A)", {"A": datetime(2026, 3, 4, 15, 30)}) == datetime(
            2026, 3, 4
        )

    def test_to_char_and_back_loses_the_time(self) -> None:
        assert evaluate(
            "TO_DATE(TO_CHAR(A, 'DD-MM-YY'), 'DD-MM-YY')",
            {"A": datetime(2026, 3, 4, 15, 30)},
        ) == datetime(2026, 3, 4)

    def test_the_trim_and_upper_chain(self) -> None:
        assert (
            evaluate("UPPER(LTRIM(RTRIM(A)))", {"A": "  jaffle  "}) == "JAFFLE"
        )

    def test_an_unknown_function_is_refused(self) -> None:
        with pytest.raises(EvalError, match="unsupported function"):
            evaluate("DECODE(A, 1, 2)", {"A": 1})

    def test_aggregates_are_not_row_level(self) -> None:
        with pytest.raises(EvalError, match="aggregate"):
            evaluate("SUM(A)", {"A": 1})


class TestAggregates:
    def test_nulls_are_ignored(self) -> None:
        assert aggregate("SUM", [1, None, 2]) == 3
        assert aggregate("COUNT", [1, None, 2]) == 2
        assert aggregate("MIN", [None, 5, 3]) == 3
        assert aggregate("MAX", [None, 5, 3]) == 5

    def test_an_all_null_group_aggregates_to_null_except_count(self) -> None:
        assert aggregate("SUM", [None, None]) is None
        assert aggregate("COUNT", [None, None]) == 0


class TestCoercion:
    @pytest.mark.parametrize(
        ("value", "datatype", "expected"),
        [
            ("600", "number", 600.0),
            ("6.5", "number(p,s)", 6.5),
            ("7", "integer", 7),
            ("", "number", None),
            ("2024-09-01T15:01:00", "timestamp", datetime(2024, 9, 1, 15, 1)),
            ("False", "varchar2", "False"),
        ],
    )
    def test_csv_text_becomes_the_declared_type(
        self, value, datatype, expected
    ) -> None:
        assert coerce(value, datatype) == expected

    def test_a_varchar_flag_stays_text(self) -> None:
        """The seed data holds True/False in a column the mapping tests
        against 'Y'. Read as text, that comparison is false — read as a
        boolean, a warehouse may coerce 'Y' to true and change the meaning."""
        assert coerce("False", "varchar2") == "False"
        assert evaluate(
            "IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)",
            {"PERISHABLE": coerce("False", "varchar2")},
        ) == 0


# ---------------------------------------------------------------------------
# Running a whole mapping
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def simulated(repo):
    if not SEEDS.is_dir():
        pytest.skip(f"no seed data at {SEEDS}")
    return run_repository(
        repo, load_seeds(repo, SEEDS), variable_order=VariableOrder.DEFERRED
    )


class TestRunningTheMapping:
    def test_all_sixteen_targets_are_produced(self, simulated) -> None:
        assert len(simulated.tables) == 16

    def test_mappings_run_in_dependency_order(self, simulated) -> None:
        # The marts read the staging tables, so staging has to run first; if
        # it did not, the marts would be empty.
        assert simulated.tables["STG_ORDERS"]
        assert simulated.tables["ORDERS"]

    def test_staging_is_one_to_one_with_its_source(self, repo, simulated) -> None:
        sources = load_seeds(repo, SEEDS)
        assert len(simulated.tables["STG_CUSTOMERS"]) == len(sources["RAW_CUSTOMERS"])
        assert len(simulated.tables["STG_ORDERS"]) == len(sources["RAW_ORDERS"])

    def test_the_master_outer_join_drops_unmatched_master_rows(
        self, repo, simulated
    ) -> None:
        """Independently of the converter: customers with no orders should not
        reach CUSTOMERS, because they are the master side of a master outer
        join."""
        sources = load_seeds(repo, SEEDS)
        with_orders = {row["CUSTOMER"] for row in sources["RAW_ORDERS"]}
        assert len(simulated.tables["CUSTOMERS"]) == len(with_orders)
        assert len(simulated.tables["CUSTOMERS"]) < len(sources["RAW_CUSTOMERS"])

    def test_the_router_and_union_round_trip_keeps_every_row(
        self, repo, simulated
    ) -> None:
        sources = load_seeds(repo, SEEDS)
        assert len(simulated.tables["STG_LOCATIONS"]) == len(sources["RAW_STORES"])

    def test_the_crossed_price_columns(self, repo, simulated) -> None:
        by_sku = {row["SKU"]: row["PRICE"] for row in load_seeds(repo, SEEDS)["RAW_PRODUCTS"]}
        for row in simulated.tables["STG_PRODUCTS"]:
            raw = by_sku[row["PRODUCT_ID"]]
            assert row["PRODUCT_PRICE"] == raw
            assert row["PRODUCT_PRICE_CENTS"] == raw / 100


class TestVariableEvaluationOrder:
    """The one semantic question in this export that cannot be settled without
    PowerCenter. Both readings are implemented so the cost of being wrong can
    be measured rather than guessed at."""

    def test_the_two_readings_disagree_about_the_counter(self, repo) -> None:
        sources = load_seeds(repo, SEEDS)
        document = run_repository(
            repo, sources, variable_order=VariableOrder.DOCUMENT
        )
        deferred = run_repository(
            repo, sources, variable_order=VariableOrder.DEFERRED
        )

        literal = max(row["CUSTOMER_ORDER_NUMBER"] for row in document.tables["ORDERS"])
        intended = max(row["CUSTOMER_ORDER_NUMBER"] for row in deferred.tables["ORDERS"])

        # Read literally, the counter never restarts, so it climbs to roughly
        # the row count. Read as intended, it restarts for each customer.
        assert literal > 1000
        assert intended < 100

    def test_the_deferred_reading_makes_the_counter_restart(self, repo) -> None:
        deferred = run_repository(
            repo, load_seeds(repo, SEEDS), variable_order=VariableOrder.DEFERRED
        )
        per_customer: dict[str, list[int]] = {}
        for row in deferred.tables["ORDERS"]:
            per_customer.setdefault(row["CUSTOMER_ID"], []).append(
                row["CUSTOMER_ORDER_NUMBER"]
            )
        for customer, numbers in per_customer.items():
            assert sorted(numbers) == list(range(1, len(numbers) + 1)), customer

    def test_the_literal_reading_makes_a_column_constant(self, repo) -> None:
        """UNIQUE_CUSTOMERS is SUM(IS_FIRST_CUSTOMER). Read literally, the
        first-customer test compares a row against itself, so the flag is
        always 0 and the sum is always 0 — a dead column."""
        document = run_repository(
            repo, load_seeds(repo, SEEDS), variable_order=VariableOrder.DOCUMENT
        )
        values = {row["UNIQUE_CUSTOMERS"] for row in document.tables["STORE_DAILY_KPIS"]}
        assert values == {0}

        deferred = run_repository(
            repo, load_seeds(repo, SEEDS), variable_order=VariableOrder.DEFERRED
        )
        values = {row["UNIQUE_CUSTOMERS"] for row in deferred.tables["STORE_DAILY_KPIS"]}
        assert len(values) > 1
        assert 0 not in values


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


class TestComparison:
    def test_identical_tables_match(self) -> None:
        rows = [{"ID": "a", "N": 1}, {"ID": "b", "N": 2}]
        diff = compare_tables("T", ["ID", "N"], rows, [dict(r) for r in rows])
        assert diff.matches
        assert diff.key == "id"

    def test_a_differing_cell_is_reported_against_its_key(self) -> None:
        diff = compare_tables(
            "T",
            ["ID", "N"],
            [{"ID": "a", "N": 1}],
            [{"id": "a", "n": 2}],
        )
        assert not diff.matches
        assert diff.mismatched == {"n": 1}
        assert diff.examples[0][:2] == ("a", "n")

    def test_a_missing_row_is_reported(self) -> None:
        diff = compare_tables(
            "T", ["ID"], [{"ID": "a"}, {"ID": "b"}], [{"id": "a"}]
        )
        assert diff.only_simulated == ("b",)

    def test_types_that_differ_only_by_loading_are_equal(self) -> None:
        """DuckDB hands back Decimal where the interpreter computed a float,
        and a CSV column may arrive as text."""
        from decimal import Decimal

        assert same(normalise(Decimal("6.00")), normalise(6.0))
        assert same(normalise("600"), normalise(600))
        assert same(normalise(True), normalise(1))

    def test_floats_get_a_tolerance_not_a_rounding_boundary(self) -> None:
        # These differ in the last bit only; rounding puts them on opposite
        # sides of a boundary, which would be a false alarm.
        assert same(normalise(0.9978075), normalise(0.9978075000000001))
        assert not same(normalise(0.1), normalise(0.2))

    def test_tables_without_a_unique_column_compare_as_row_sets(self) -> None:
        diff = compare_tables(
            "T",
            ["A"],
            [{"A": "x"}, {"A": "x"}],
            [{"a": "x"}, {"a": "y"}],
        )
        assert diff.key is None
        assert "row sets" in diff.note
        assert not diff.matches
