"""Tests for writing out the dbt project."""

from __future__ import annotations

from pathlib import Path

import pytest

from pwc2dbt.emit import trace_column, write_project
from pwc2dbt.graph import plan_models


@pytest.fixture(scope="session")
def project(repo, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("project")
    write_project(repo, out, seeds_dir=None)
    return out


def _read(project: Path, relative: str) -> str:
    path = project / relative
    assert path.is_file(), f"{relative} was not written"
    return path.read_text(encoding="utf-8")


class TestProjectLayout:
    def test_the_files_dbt_needs_are_written(self, project) -> None:
        for relative in (
            "dbt_project.yml",
            "profiles.yml",
            "models/staging/__sources.yml",
        ):
            assert (project / relative).is_file(), relative

    def test_a_sql_and_a_yml_file_per_model(self, repo, project) -> None:
        for plan in plan_models(repo):
            assert (project / "models" / f"{plan.layer}/{plan.name}.sql").is_file()
            assert (project / "models" / f"{plan.layer}/{plan.name}.yml").is_file()

    def test_staging_is_a_view_and_marts_are_tables(self, project) -> None:
        # Every PowerCenter target was truncate-and-insert, so a table is the
        # faithful choice for a mart.
        config = _read(project, "dbt_project.yml")
        assert "staging:\n      +materialized: view" in config
        assert "marts:\n      +materialized: table" in config

    def test_the_profile_name_matches_the_project(self, project) -> None:
        name = _read(project, "dbt_project.yml")
        profile = _read(project, "profiles.yml")
        assert "profile: 'jaffle_converted'" in name
        assert profile.startswith("#") or "jaffle_converted:" in profile
        assert "jaffle_converted:" in profile
        assert "type: duckdb" in profile


class TestSources:
    def test_only_tables_nobody_writes_are_declared_as_sources(self, project) -> None:
        """The STG_* tables are produced by the staging models, so they are
        `ref()`s. Only the RAW_* tables are real sources."""
        sources = _read(project, "models/staging/__sources.yml")
        for raw in (
            "RAW_CUSTOMERS",
            "RAW_ORDERS",
            "RAW_ITEMS",
            "RAW_PRODUCTS",
            "RAW_STORES",
            "RAW_SUPPLIES",
        ):
            assert f"- name: {raw}" in sources
        for staged in ("STG_CUSTOMERS", "STG_ORDERS", "STG_PRODUCTS"):
            assert f"- name: {staged}" not in sources

    def test_source_keys_become_tests(self, project) -> None:
        sources = _read(project, "models/staging/__sources.yml")
        assert "- unique" in sources
        assert "- not_null" in sources
        assert "- relationships:" in sources
        # The foreign key on RAW_ORDERS.CUSTOMER points at RAW_CUSTOMERS.ID.
        assert "to: source('raw', 'RAW_CUSTOMERS')" in sources
        assert "field: ID" in sources

    def test_the_schema_can_be_redirected_for_a_real_warehouse(self, project) -> None:
        sources = _read(project, "models/staging/__sources.yml")
        assert "var('raw_schema', target.schema)" in sources
        # Seeds are lowercase files, so the identifier is pinned explicitly.
        assert "identifier: raw_customers" in sources


class TestMappingVariables:
    def test_the_variable_keeps_the_default_from_the_export(self, project) -> None:
        """`$$RUN_DATE` is declared in the mapping as 12/31/2025 00:00:00."""
        config = _read(project, "dbt_project.yml")
        assert "vars:" in config
        assert "run_date: '2025-12-31'" in config


class TestColumnLineage:
    """Tests have to be attached by following the column, not by name: a key
    called ID in the source is called customer_id in the model."""

    @pytest.mark.parametrize(
        ("model", "table", "key", "expected"),
        [
            ("stg_customers", "RAW_CUSTOMERS", "ID", "customer_id"),
            ("stg_orders", "RAW_ORDERS", "ID", "order_id"),
            ("stg_products", "RAW_PRODUCTS", "SKU", "product_id"),
            ("stg_locations", "RAW_STORES", "ID", "location_id"),
            ("stg_order_items", "RAW_ITEMS", "ID", "order_item_id"),
        ],
    )
    def test_a_source_key_is_traced_to_its_model_column(
        self, repo, model, table, key, expected
    ) -> None:
        plan = next(p for p in plan_models(repo) if p.name == model)
        assert trace_column(repo, plan, table, key) == expected

    def test_a_key_that_is_only_joined_in_is_not_traced(self, repo) -> None:
        """In stg_supplies, products are joined in for enrichment, so the
        product key is not the grain and must not get a uniqueness test."""
        plan = next(p for p in plan_models(repo) if p.name == "stg_supplies")
        assert trace_column(repo, plan, "RAW_PRODUCTS", "SKU") is None

    def test_traced_keys_become_model_tests(self, project) -> None:
        yaml = _read(project, "models/staging/stg_customers.yml")
        assert "- name: customer_id" in yaml
        assert "- unique" in yaml
        assert "- not_null" in yaml

    def test_aggregating_models_get_no_key_tests(self, project) -> None:
        """Grain changes, so a source key stops being unique."""
        yaml = _read(project, "models/marts/customer_360.yml")
        assert "- unique" not in yaml


class TestIntermediateModels:
    def test_they_are_documented_as_shared_steps(self, project) -> None:
        yaml = _read(project, "models/intermediate/int_srt_order_id.yml")
        assert "Shared step SRT_ORDER_ID" in yaml
        assert "- name: order_id" in yaml

    def test_they_have_no_key_tests(self, project) -> None:
        # They are not a declared table, so there is nothing to derive from.
        yaml = _read(project, "models/intermediate/int_un_order_bands.yml")
        assert "data_tests" not in yaml


class TestSeeds:
    def test_seeds_are_copied_when_given(self, repo, tmp_path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        (seeds / "raw_customers.csv").write_text("id,name\n1,Ada\n", encoding="utf-8")
        out = tmp_path / "project"
        result = write_project(repo, out, seeds_dir=seeds)
        assert (out / "seeds" / "raw_customers.csv").is_file()
        assert any(p.name == "raw_customers.csv" for p in result.files)

    def test_a_project_can_be_written_without_seeds(self, repo, tmp_path) -> None:
        result = write_project(repo, tmp_path / "noseeds", seeds_dir=None)
        assert not (tmp_path / "noseeds" / "seeds").exists()
        assert len(result.models) == 18
