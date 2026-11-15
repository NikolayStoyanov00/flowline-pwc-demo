"""Tests for turning a mapping into a graph and a list of dbt models.

These assert what the converter *produces* — model names, file paths, the
`ref()`/`source()` references, which models depend on which — rather than that
internal structures have a given shape.
"""

from __future__ import annotations

import pytest

from pwc2dbt.graph import GraphError, build_graph, plan_models, split_points
from pwc2dbt.model import Kind, Repository
from pwc2dbt.parse import parse_string


@pytest.fixture(scope="session")
def plans(repo: Repository):
    return plan_models(repo)


@pytest.fixture(scope="session")
def by_name(plans):
    return {plan.name: plan for plan in plans}


# ---------------------------------------------------------------------------
# What gets produced
# ---------------------------------------------------------------------------


class TestModelsProduced:
    def test_three_mappings_produce_sixteen_models(self, plans) -> None:
        assert len(plans) == 16

    def test_model_file_paths(self, plans) -> None:
        assert sorted(plan.path for plan in plans) == [
            "marts/customer_360.sql",
            "marts/customers.sql",
            "marts/order_financials.sql",
            "marts/order_item_enriched.sql",
            "marts/order_items.sql",
            "marts/orders.sql",
            "marts/product_daily_performance.sql",
            "marts/products.sql",
            "marts/store_daily_kpis.sql",
            "marts/supplies.sql",
            "staging/stg_customers.sql",
            "staging/stg_locations.sql",
            "staging/stg_order_items.sql",
            "staging/stg_orders.sql",
            "staging/stg_products.sql",
            "staging/stg_supplies.sql",
        ]

    def test_one_mapping_produces_many_models(self, plans) -> None:
        """The headline fact: a mapping is not a model."""
        per_mapping: dict[str, int] = {}
        for plan in plans:
            per_mapping[plan.mapping_name] = per_mapping.get(plan.mapping_name, 0) + 1
        assert per_mapping == {
            "m_FL_JS_STAGING_FULL": 6,
            "m_FL_JS_MARTS_ENRICHED": 5,
            "m_FL_JS_MARTS_CORE": 5,
        }

    def test_staging_models_land_in_the_staging_layer(self, by_name) -> None:
        # Derived from "TRUNCATE TABLE STG.x" / "TRUNCATE TABLE MART.x", the
        # only record of a target's schema.
        assert by_name["stg_orders"].layer == "staging"
        assert by_name["stg_orders"].schema == "STG"
        assert by_name["orders"].layer == "marts"
        assert by_name["orders"].schema == "MART"

    def test_no_model_contains_an_unconvertible_step(self, plans) -> None:
        blocked = {
            plan.name: [s.name for s in plan.steps if s.kind is Kind.UNKNOWN]
            for plan in plans
        }
        assert {name: steps for name, steps in blocked.items() if steps} == {}

    def test_union_instances_are_recognised(self, by_name) -> None:
        """A Union instance has no TEMPLATENAME, so its kind comes from the
        definition. Without that it reads as an unknown custom transformation
        and blocks two models."""
        kinds = {
            step.name: step.kind
            for step in by_name["stg_locations"].steps
            if step.name == "UN_REGIONS"
        }
        assert kinds == {"UN_REGIONS": Kind.UNION}


# ---------------------------------------------------------------------------
# How models refer to their inputs
# ---------------------------------------------------------------------------


class TestReferences:
    def test_staging_reads_raw_tables_as_sources(self, by_name) -> None:
        reads = by_name["stg_customers"].reads
        assert [r.table.name for r in reads] == ["RAW_CUSTOMERS"]
        assert reads[0].is_model is False
        assert reads[0].reference == "{{ source('raw', 'RAW_CUSTOMERS') }}"

    def test_marts_read_staging_tables_as_refs(self, by_name) -> None:
        reads = by_name["customers"].reads
        assert [r.reference for r in reads] == [
            "{{ ref('stg_customers') }}",
            "{{ ref('stg_orders') }}",
        ]

    def test_dependencies_between_models(self, by_name) -> None:
        assert by_name["customers"].depends_on == ("stg_customers", "stg_orders")
        assert by_name["order_items"].depends_on == (
            "stg_order_items",
            "stg_orders",
            "stg_products",
            "stg_supplies",
        )
        assert by_name["stg_customers"].depends_on == ()

    def test_a_table_read_twice_is_two_reads(self, by_name) -> None:
        """`stg_supplies` reads RAW_PRODUCTS under the alias RAW_PRODUCTS1.

        Aliased reads are separate nodes feeding different parts of the
        pipeline, so they must not be collapsed into one.
        """
        reads = by_name["stg_supplies"].reads
        assert [(r.instance_name, r.table.name) for r in reads] == [
            ("RAW_PRODUCTS1", "RAW_PRODUCTS"),
            ("RAW_SUPPLIES", "RAW_SUPPLIES"),
        ]

    def test_every_mart_depends_only_on_models_that_exist(self, plans) -> None:
        produced = {plan.name for plan in plans}
        for plan in plans:
            missing = set(plan.depends_on) - produced
            assert missing == set(), f"{plan.name} refs missing model(s) {missing}"

    def test_no_model_depends_on_itself(self, plans) -> None:
        for plan in plans:
            assert plan.name not in plan.depends_on


# ---------------------------------------------------------------------------
# Step ordering
# ---------------------------------------------------------------------------


class TestStepOrdering:
    def test_producers_always_come_before_consumers(self, repo, plans) -> None:
        """The ordering guarantee the SQL builder will rely on: by the time a
        step is reached, everything feeding it has already been emitted."""
        graphs = {m.name: build_graph(m) for m in repo.mappings}
        for plan in plans:
            graph = graphs[plan.mapping_name]
            seen: set[str] = set()
            for step in plan.steps:
                feeders_in_model = graph.feeders(step.name) & {
                    s.name for s in plan.steps
                }
                assert feeders_in_model <= seen, (
                    f"{plan.name}: {step.name} emitted before "
                    f"{feeders_in_model - seen}"
                )
                seen.add(step.name)

    def test_a_model_starts_from_its_source_tables(self, by_name) -> None:
        first = by_name["stg_products"].steps[0]
        assert first.name == "RAW_PRODUCTS"
        assert first.is_source is True

    def test_ordering_is_stable_across_runs(self, repo) -> None:
        # Generated SQL must not churn between runs.
        first = [[s.name for s in p.steps] for p in plan_models(repo)]
        second = [[s.name for s in p.steps] for p in plan_models(repo)]
        assert first == second


# ---------------------------------------------------------------------------
# Where pipelines fork
# ---------------------------------------------------------------------------


class TestForks:
    def test_enriched_mapping_forks_at_two_steps(self, repo) -> None:
        forks = split_points(repo.mapping("m_FL_JS_MARTS_ENRICHED"))
        assert sorted(forks) == ["SRT_ORDER_ID", "UN_ORDER_BANDS"]
        assert len(forks["SRT_ORDER_ID"]) == 5
        assert len(forks["UN_ORDER_BANDS"]) == 3

    def test_a_router_that_reconverges_is_not_a_fork(self, repo) -> None:
        """RTR_REGION splits into three branches that a Union merges back.

        It feeds three consumers, but they all reach the same single target,
        so nothing would be duplicated by leaving it inline.
        """
        forks = split_points(repo.mapping("m_FL_JS_STAGING_FULL"))
        assert forks == {}

    def test_core_mapping_pipelines_are_independent(self, repo) -> None:
        assert split_points(repo.mapping("m_FL_JS_MARTS_CORE")) == {}

    def test_shared_steps_sit_upstream_of_a_fork(self, repo, by_name) -> None:
        """Everything shared is upstream of a fork, which is why cutting at the
        forks is enough to stop any duplication."""
        mapping = repo.mapping("m_FL_JS_MARTS_ENRICHED")
        graph = build_graph(mapping)
        forks = set(split_points(mapping))

        fork_ancestry = set(forks)
        for fork in forks:
            fork_ancestry |= graph.ancestors(fork)

        shared = set(by_name["customer_360"].shared_steps)
        assert shared and shared <= fork_ancestry


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestBadGraphs:
    def test_a_cycle_is_reported(self, wrap) -> None:
        repo = parse_string(
            wrap(
                '<TARGET NAME="T"/>'
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="A" TRANSFORMATION_NAME="A"'
                ' TRANSFORMATION_TYPE="Expression" TYPE="TRANSFORMATION"/>'
                '<INSTANCE NAME="B" TRANSFORMATION_NAME="B"'
                ' TRANSFORMATION_TYPE="Expression" TYPE="TRANSFORMATION"/>'
                '<INSTANCE NAME="T" TRANSFORMATION_NAME="T"'
                ' TRANSFORMATION_TYPE="Target Definition" TYPE="TARGET"/>'
                '<CONNECTOR FROMFIELD="x" FROMINSTANCE="A" TOFIELD="x" TOINSTANCE="B"/>'
                '<CONNECTOR FROMFIELD="x" FROMINSTANCE="B" TOFIELD="x" TOINSTANCE="A"/>'
                '<CONNECTOR FROMFIELD="x" FROMINSTANCE="B" TOFIELD="x" TOINSTANCE="T"/>'
                "</MAPPING>"
            )
        )
        with pytest.raises(GraphError, match="cycle"):
            plan_models(repo)

    def test_a_connector_to_an_unknown_instance_is_reported(self, wrap) -> None:
        repo = parse_string(
            wrap(
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="A" TRANSFORMATION_NAME="A"'
                ' TRANSFORMATION_TYPE="Expression" TYPE="TRANSFORMATION"/>'
                '<CONNECTOR FROMFIELD="x" FROMINSTANCE="A" TOFIELD="x"'
                ' TOINSTANCE="GHOST"/>'
                "</MAPPING>"
            )
        )
        with pytest.raises(GraphError, match="unknown instance 'GHOST'"):
            build_graph(repo.mappings[0])

    def test_a_target_with_nothing_feeding_it_is_reported(self, wrap) -> None:
        repo = parse_string(
            wrap(
                '<TARGET NAME="T"/>'
                '<MAPPING NAME="m">'
                '<INSTANCE NAME="T" TRANSFORMATION_NAME="T"'
                ' TRANSFORMATION_TYPE="Target Definition" TYPE="TARGET"/>'
                "</MAPPING>"
            )
        )
        with pytest.raises(GraphError, match="nothing feeding it"):
            plan_models(repo)
