"""Tests for generating a model's SQL.

These assert properties of the SQL that is produced, and in the join cases the
assertion is about which table leads, because that is the thing most likely to
be wrong and it is invisible unless you look.
"""

from __future__ import annotations

import re

import pytest
import sqlglot

from pwc2dbt.graph import plan_models
from pwc2dbt.sql import build_model_sql


@pytest.fixture(scope="session")
def models(repo) -> dict[str, str]:
    return {plan.name: build_model_sql(repo, plan) for plan in plan_models(repo)}


def _jinja_free(sql: str) -> str:
    """Replace dbt's Jinja with plain SQL so a parser can read it."""
    sql = re.sub(r"'\{\{ var\([^)]*\) \}\}'", "current_date", sql)
    return re.sub(r"\{\{ (?:ref|source)\([^)]*\) \}\}", "some_table", sql)


def _cte_body(sql: str, name: str) -> str:
    """The text of one named CTE."""
    match = re.search(rf"^{re.escape(name)} as \((.*?)^\)", sql, re.S | re.M)
    if match is None:
        raise AssertionError(f"no CTE called {name!r} in:\n{sql}")
    return match.group(1)


class TestAllModelsGenerate:
    def test_every_model_produces_sql(self, models) -> None:
        assert len(models) == 18
        assert all(sql.strip() for sql in models.values())

    def test_every_model_is_valid_sql(self, models) -> None:
        failures = []
        for name, sql in models.items():
            try:
                sqlglot.parse_one(_jinja_free(sql), dialect="duckdb")
            except sqlglot.ParseError as exc:
                failures.append(f"{name}: {exc}")
        assert failures == []

    def test_every_model_ends_by_selecting_from_final(self, models) -> None:
        # The dbt convention: swap the name in the last select to audit any
        # intermediate step while developing.
        for name, sql in models.items():
            assert sql.rstrip().endswith("select * from final"), name

    def test_every_ref_points_at_a_model_we_generate(self, models) -> None:
        produced = set(models)
        for name, sql in models.items():
            for referenced in re.findall(r"\{\{ ref\('([^']+)'\) \}\}", sql):
                assert referenced in produced, f"{name} refs missing {referenced}"

    def test_generation_is_deterministic(self, repo) -> None:
        first = {p.name: build_model_sql(repo, p) for p in plan_models(repo)}
        second = {p.name: build_model_sql(repo, p) for p in plan_models(repo)}
        assert first == second


class TestJoinDirection:
    """PowerCenter names outer joins after the side that may be dropped, which
    reads backwards from SQL. Ten of the sixteen joins in this export are
    `Master Outer Join`, so this is the highest-risk translation."""

    def test_master_outer_join_leads_with_the_detail_table(self, models) -> None:
        """`Master Outer Join` keeps every detail row, so in SQL the detail
        table has to come first and the master is joined onto it."""
        body = _cte_body(models["customers"], "jnr_customers_orders")
        # Master is the customers side; detail is the order aggregate.
        assert "from agg_orders_by_customer" in body
        assert "left join sq_stg_customers" in body
        assert "from sq_stg_customers" not in body

    def test_normal_join_is_an_inner_join(self, models) -> None:
        body = _cte_body(models["stg_supplies"], "jnr_supplies_products")
        assert "inner join" in body
        assert "left join" not in body

    def test_join_condition_sides_are_resolved_by_port_not_position(
        self, models
    ) -> None:
        """Conditions appear in both orders in the export, so each side has to
        be resolved through its own port rather than assumed."""
        body = _cte_body(models["customers"], "jnr_customers_orders")
        condition = body.split(" on ", 1)[1]
        assert "sq_stg_customers.customer_id" in condition
        assert "agg_orders_by_customer.customer_id" in condition

    def test_every_join_in_every_model_is_explicit_about_its_type(
        self, models
    ) -> None:
        for name, sql in models.items():
            for line in sql.splitlines():
                stripped = line.strip()
                if stripped.startswith("join "):
                    raise AssertionError(f"{name}: bare join: {stripped}")


class TestRowNumber:
    def test_the_self_referencing_counter_becomes_row_number(self, models) -> None:
        sql = models["orders"]
        assert (
            "row_number() over (partition by customer_id "
            "order by ordered_at_day, order_id) as customer_order_number" in sql
        )

    def test_the_ordering_comes_from_the_upstream_sorter(self, repo, models) -> None:
        """SRT_BY_CUSTOMER sorts on CUSTOMER_ID, ORDERED_AT_DAY, ORDER_ID. The
        partition key is dropped from the ordering, leaving the other two."""
        sorter = repo.mapping("m_FL_JS_MARTS_CORE").transformations["SRT_BY_CUSTOMER"]
        assert [p.name for p in sorter.sort_keys] == [
            "CUSTOMER_ID",
            "ORDERED_AT_DAY",
            "ORDER_ID",
        ]
        assert "order by ordered_at_day, order_id" in models["orders"]

    def test_the_translation_is_recorded_in_the_model(self, models) -> None:
        # Anything interpreted rather than translated literally should be
        # visible to whoever reads the generated SQL.
        assert "row_number()" in models["orders"]
        assert "-- note:" in models["orders"]
        assert "previous value" in models["orders"]


class TestRouterAndUnion:
    def test_each_router_group_becomes_its_own_cte(self, models) -> None:
        sql = models["stg_locations"]
        for group, region in (
            ("rtr_region__g_apac", "APAC"),
            ("rtr_region__g_amer", "AMER"),
            ("rtr_region__g_emea", "EMEA"),
        ):
            body = _cte_body(sql, group)
            assert f"where region = '{region}'" in body

    def test_the_unconnected_default_group_is_not_emitted(self, models) -> None:
        # Rows matching no branch are discarded by the mapping, so a CTE for
        # them would be dead SQL.
        assert "default1" not in models["stg_locations"].lower()

    def test_a_union_becomes_union_all_of_its_legs(self, models) -> None:
        body = _cte_body(models["stg_locations"], "un_regions")
        assert body.count("union all") == 2
        assert body.count("select") == 3

    def test_union_columns_line_up_across_legs(self, models) -> None:
        """Alignment comes from FIELDDEPENDENCY, not from column order."""
        body = _cte_body(models["stg_locations"], "un_regions")
        legs = body.split("union all")
        aliases = [
            re.findall(r"^\s{8}(\w+)(?: as (\w+))?,?$", leg, re.M) for leg in legs
        ]
        outputs = [[(b or a) for a, b in leg] for leg in aliases]
        assert outputs[0] == outputs[1] == outputs[2]
        assert outputs[0]


class TestSorters:
    def test_no_model_sorts_its_own_rows(self, models) -> None:
        """Row order does not survive into a table, so the sort is dropped.

        Where the order matters it is read off the Sorter by a window
        function, so `order by` is allowed inside `over (...)` and nowhere
        else. Checked by removing every `over (...)` first.
        """
        window = re.compile(r"over \([^)]*\)")
        for name, sql in models.items():
            assert "order by" not in window.sub("over ()", sql), name

    def test_dropping_a_sort_is_recorded(self, models) -> None:
        assert "sort order dropped" in models["stg_orders"]


class TestSharedSteps:
    def test_a_fork_is_read_as_a_model_not_rebuilt(self, models) -> None:
        sql = models["customer_360"]
        assert "{{ ref('int_un_order_bands') }}" in sql
        # The chain above the fork belongs to the intermediate model now.
        assert "jnr_items_orders as (" not in sql

    def test_the_intermediate_model_contains_the_shared_chain(self, models) -> None:
        sql = models["int_srt_order_id"]
        for step in ("jnr_items_orders", "jnr_add_products", "exp_item_calculations"):
            assert f"{step} as (" in sql

    def test_forks_chain_onto_each_other(self, models) -> None:
        # UN_ORDER_BANDS sits downstream of SRT_ORDER_ID, so it reads it
        # rather than rebuilding it too.
        assert "{{ ref('int_srt_order_id') }}" in models["int_un_order_bands"]

    def test_extracting_shared_steps_removes_the_duplication(self, repo) -> None:
        """Without this, 21 CTEs were copied into all five enriched models."""
        with_forks = {p.name: build_model_sql(repo, p) for p in plan_models(repo)}
        without = {
            p.name: build_model_sql(repo, p)
            for p in plan_models(repo, extract_forks=False)
        }
        enriched = [
            "customer_360",
            "order_financials",
            "order_item_enriched",
            "product_daily_performance",
            "store_daily_kpis",
        ]
        cte = re.compile(r"^(\w+) as \(", re.M)
        shared_before = set.intersection(
            *(set(cte.findall(without[n])) for n in enriched)
        )
        shared_after = set.intersection(
            *(set(cte.findall(with_forks[n])) for n in enriched)
        )
        assert len(shared_before) > 20
        # Only the final select is still common to all five.
        assert shared_after == {"final"}

        total_before = sum(len(without[n].splitlines()) for n in enriched)
        total_after = sum(len(with_forks[n].splitlines()) for n in enriched)
        assert total_after < total_before / 2


class TestTargetColumns:
    def test_the_crossed_price_connectors_are_reproduced(self, models) -> None:
        """EXP_PRODUCTS computes dollars into PRODUCT_PRICE and cents into
        PRODUCT_PRICE_CENTS, and the connectors then swap them. Converting the
        mapping faithfully means keeping the swap."""
        body = _cte_body(models["stg_products"], "final")
        assert "product_price as product_price_cents" in body
        assert "product_price_cents as product_price" in body

    def test_target_columns_come_out_in_the_targets_own_order(
        self, repo, models
    ) -> None:
        plan = next(p for p in plan_models(repo) if p.name == "stg_products")
        body = _cte_body(models["stg_products"], "final")
        emitted = [
            (b or a)
            for a, b in re.findall(r"^\s{8}(\w+)(?: as (\w+))?,?$", body, re.M)
        ]
        assert emitted == [c.name.lower() for c in plan.table.columns]


class TestFailureIsLoud:
    def test_an_unconvertible_step_is_reported_with_its_name(self, repo) -> None:
        from dataclasses import replace

        from pwc2dbt.model import Kind
        from pwc2dbt.sql import SqlError

        plan = next(p for p in plan_models(repo) if p.name == "stg_customers")
        broken = replace(
            plan,
            steps=tuple(
                replace(step, kind=Kind.UNKNOWN) if step.kind is Kind.SOURCE_QUALIFIER
                else step
                for step in plan.steps
            ),
        )
        with pytest.raises(SqlError, match="SQ_RAW_CUSTOMERS"):
            build_model_sql(repo, broken)


class TestStatefulVariableShapes:
    """A variable reading its own previous value is only translatable when it
    is a running *count*. Anything else must fail loudly rather than be
    emitted as a row ordinal."""

    @staticmethod
    def _transformation(counter_expression: str):
        from pwc2dbt.model import Kind, Port, Transformation

        return Transformation(
            name="EXP_T",
            kind=Kind.EXPRESSION,
            ports=(
                Port(name="CUSTOMER_ID", port_type="INPUT"),
                Port(name="AMOUNT", port_type="INPUT", datatype="decimal"),
                Port(
                    name="v_PREV_C",
                    port_type="LOCAL VARIABLE",
                    expression="CUSTOMER_ID",
                ),
                Port(
                    name="v_C",
                    port_type="LOCAL VARIABLE",
                    datatype="integer",
                    expression=counter_expression,
                ),
            ),
        )

    def _keys(self, expression: str):
        """Run just the shape check, with a builder over a throwaway mapping."""
        from pwc2dbt.sql import _ModelBuilder

        transformation = self._transformation(expression)
        builder = _ModelBuilder.__new__(_ModelBuilder)
        builder.mapping = type("M", (), {"name": "m"})()
        return builder.counter_partition_keys(
            transformation, transformation.ports[-1]
        )

    def test_a_running_count_is_recognised(self) -> None:
        assert self._keys("IIF(v_PREV_C = CUSTOMER_ID, v_C + 1, 1)") == (
            "CUSTOMER_ID",
        )

    def test_the_increment_may_be_written_either_way_round(self) -> None:
        assert self._keys("IIF(v_PREV_C = CUSTOMER_ID, 1 + v_C, 1)") == (
            "CUSTOMER_ID",
        )

    @pytest.mark.parametrize(
        ("expression", "why"),
        [
            (
                "IIF(v_PREV_C = CUSTOMER_ID, v_C + AMOUNT, AMOUNT)",
                "a running total, not a count",
            ),
            (
                "IIF(v_PREV_C = CUSTOMER_ID, v_C + 2, 1)",
                "steps by two",
            ),
            (
                "IIF(v_PREV_C = CUSTOMER_ID, v_C + 1, 0)",
                "resets to zero, so it is off by one",
            ),
            (
                "IIF(v_PREV_C = CUSTOMER_ID, v_C - 1, 1)",
                "counts down",
            ),
        ],
    )
    def test_anything_other_than_a_count_is_refused(
        self, expression: str, why: str
    ) -> None:
        """The condition is identical in all of these, so checking only the
        condition would emit row_number() and silently replace every value."""
        from pwc2dbt.sql import SqlError

        with pytest.raises(SqlError, match="only a running count"):
            self._keys(expression)


class TestSortDirection:
    def test_a_descending_sorter_orders_the_window_descending(self, repo) -> None:
        """Sort direction is load-bearing for a window function: descending
        makes a counter number rows from the other end."""
        from dataclasses import replace

        from pwc2dbt.graph import plan_models


        mapping = repo.mapping("m_FL_JS_MARTS_CORE")
        sorter = mapping.transformations["SRT_BY_CUSTOMER"]
        flipped = replace(
            sorter,
            ports=tuple(
                replace(p, sort_direction="DESCENDING") if p.is_sort_key else p
                for p in sorter.ports
            ),
        )
        patched_transformations = dict(mapping.transformations)
        patched_transformations["SRT_BY_CUSTOMER"] = flipped
        patched = replace(mapping, transformations=patched_transformations)
        patched_repo = replace(
            repo,
            mappings=tuple(
                patched if m.name == mapping.name else m for m in repo.mappings
            ),
        )

        plan = next(p for p in plan_models(patched_repo) if p.name == "orders")
        sql = build_model_sql(patched_repo, plan)
        assert "order by ordered_at_day desc, order_id desc" in sql

    def test_the_real_export_is_all_ascending(self, models) -> None:
        # All 140 sort keys in this export are ASCENDING, so no ordering in
        # the generated project should be descending. The word boundary keeps
        # this from matching "description".
        for name, sql in models.items():
            assert not re.search(r"\bdesc\b", sql), name
