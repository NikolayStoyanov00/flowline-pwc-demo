"""Write a complete, runnable dbt project.

Produces the models, the YAML that documents and tests them, the project and
profile files, and copies in the seed data. The result runs with
``dbt build`` against DuckDB with no warehouse needed.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from .graph import ModelPlan, build_graph, plan_models
from .model import Kind, Repository, Table, Transformation
from .naming import model_name, source_name
from .sql import build_model_sql

__all__ = ["EmitResult", "write_project"]

PROJECT_NAME = "jaffle_converted"
PROFILE_NAME = "jaffle_converted"


@dataclass(frozen=True, slots=True)
class EmitResult:
    out_dir: Path
    models: tuple[str, ...]
    files: tuple[Path, ...]
    tests: int


# ---------------------------------------------------------------------------
# Column lineage, used to put tests on the right model column
# ---------------------------------------------------------------------------


def _carried_through(transformation: Transformation | None, field: str) -> list[str]:
    """Output ports that are this input port unchanged, under a new name.

    Connectors alone are not enough to follow a column: a step renames
    internally too. ``EXP_PRODUCTS`` turns ``SKU`` into ``PRODUCT_ID`` with a
    port expression, and a Router turns ``LOCATION_ID`` into ``LOCATION_ID1``.
    Only pure carry-throughs count, because a test for a key should not follow
    the column into a value computed from it.
    """
    if transformation is None:
        return [field]

    carried: list[str] = []
    for port in transformation.ports:
        if not port.is_output or port.is_variable:
            continue
        if port.name == field:
            carried.append(port.name)
        elif port.ref_field == field:
            carried.append(port.name)
        elif port.expression and port.expression.strip() == field:
            carried.append(port.name)

    for dependency in transformation.field_dependencies:
        if dependency.input_field == field:
            carried.append(dependency.output_field)

    return list(dict.fromkeys(carried))


def trace_column(
    repo: Repository, plan: ModelPlan, source_table: str, column: str
) -> str | None:
    """Follow one source column forward to the model column it becomes.

    ``RAW_CUSTOMERS.ID`` arrives in ``stg_customers`` as ``customer_id``, so a
    test on that key has to be attached by following the lineage rather than by
    matching the name.
    """
    mapping = repo.mapping(plan.mapping_name)
    if mapping is None:
        return None
    graph = build_graph(mapping)

    frontier = {
        (read.instance_name, column)
        for read in plan.reads
        if read.table.name == source_table
    }
    seen = set(frontier)

    while frontier:
        nxt: set[tuple[str, str]] = set()
        for instance_name, field in frontier:
            instance = mapping.instances.get(instance_name)
            definition = (
                mapping.transformation(instance.definition_name) if instance else None
            )
            for outgoing_field in _carried_through(definition, field):
                for connector in graph.outgoing.get(instance_name, ()):
                    if connector.from_field != outgoing_field:
                        continue
                    if connector.to_instance == plan.leaf.name:
                        return connector.to_field.lower()
                    step = (connector.to_instance, connector.to_field)
                    if step not in seen:
                        seen.add(step)
                        nxt.add(step)
        frontier = nxt
    return None


def _changes_grain(plan: ModelPlan) -> bool:
    """Whether the model aggregates, so source keys stop being unique."""
    return any(step.kind is Kind.AGGREGATOR for step in plan.steps)


# ---------------------------------------------------------------------------
# YAML writers
#
# Written by hand rather than with a YAML library: the shapes are small and
# fixed, and hand-writing keeps the output in dbt's own formatting.
# ---------------------------------------------------------------------------


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def _source_yaml(repo: Repository, raw_tables: list[Table]) -> str:
    by_schema: dict[str, list[Table]] = {}
    for table in raw_tables:
        by_schema.setdefault(table.schema, []).append(table)

    lines = [
        "version: 2",
        "",
        "# Generated from the PowerCenter SOURCE definitions.",
        "#",
        "# `schema` defaults to the target schema, which is where the seed data",
        "# lands, so the project runs self-contained. Against the real warehouse,",
        "# point it at the Oracle schema instead:",
        "#",
        "#   dbt build --vars '{\"raw_schema\": \"RAW\"}'",
        "sources:",
    ]
    for schema, tables in sorted(by_schema.items()):
        lines += [
            f"  - name: {source_name(schema)}",
            f"    description: Raw tables, from PowerCenter schema {schema}.",
            '    schema: "{{ var(\'raw_schema\', target.schema) }}"',
            "    tables:",
        ]
        for table in sorted(tables, key=lambda t: t.name):
            lines.append(f"      - name: {table.name}")
            # The seeds are named after the CSV files, which are lowercase.
            lines.append(f"        identifier: {table.name.lower()}")
            columns = []
            for column in table.columns:
                tests = []
                if column.is_primary_key:
                    tests += ["unique", "not_null"]
                elif not column.nullable:
                    tests.append("not_null")
                if column.is_foreign_key and column.referenced_table:
                    tests.append("relationships")
                if not tests:
                    continue
                columns.append((column, tests))
            if not columns:
                continue
            lines.append("        columns:")
            for column, tests in columns:
                lines.append(f"          - name: {column.name}")
                lines.append("            data_tests:")
                for test in tests:
                    if test == "relationships":
                        lines += [
                            "              - relationships:",
                            "                  arguments:",
                            f"                    to: source('{source_name(schema)}', "
                            f"'{column.referenced_table}')",
                            f"                    field: {column.referenced_field}",
                        ]
                    else:
                        lines.append(f"              - {test}")
    return "\n".join(lines) + "\n"


def _model_yaml(repo: Repository, plan: ModelPlan) -> tuple[str, int]:
    """One YAML file per model, with tests derived from the source keys."""
    tested: dict[str, list[str]] = {}
    if not _changes_grain(plan) and plan.table is not None:
        for read in plan.reads:
            if read.is_model:
                continue
            for key in read.table.primary_keys:
                traced = trace_column(repo, plan, read.table.name, key.name)
                if traced:
                    tested.setdefault(traced, [])
                    for test in ("unique", "not_null"):
                        if test not in tested[traced]:
                            tested[traced].append(test)

    if plan.is_intermediate:
        lines = [
            "version: 2",
            "",
            "models:",
            f"  - name: {plan.name}",
            "    description: >",
            f"      Shared step {plan.leaf.name} of PowerCenter mapping",
            f"      {plan.mapping_name}, built once because several targets read it.",
            "    columns:",
        ]
        lines += [f"      - name: {name}" for name in plan.exposed]
        return "\n".join(lines) + "\n", 0

    lines = [
        "version: 2",
        "",
        "models:",
        f"  - name: {plan.name}",
        "    description: >",
        f"      Converted from PowerCenter mapping {plan.mapping_name},",
        f"      target {plan.schema}.{plan.table.name}.",
        "    columns:",
    ]
    count = 0
    for column in plan.table.columns:
        name = column.name.lower()
        lines.append(f"      - name: {name}")
        lines.append(
            f"        description: \"{column.datatype}"
            f"({column.precision},{column.scale}) in PowerCenter\""
        )
        if name in tested:
            lines.append("        data_tests:")
            for test in tested[name]:
                lines.append(f"          - {test}")
                count += 1
    return "\n".join(lines) + "\n", count


def _iso_default(value: str) -> str:
    """Convert a PowerCenter default like ``12/31/2025 00:00:00`` to ISO."""
    for pattern in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return value.strip()


def _vars_block(repo: Repository) -> str:
    """dbt vars for the mapping variables, keeping their declared defaults."""
    variables = {
        variable.name.lstrip("$").lower(): variable
        for mapping in repo.mappings
        for variable in mapping.variables
    }
    if not variables:
        return ""
    lines = [
        "",
        "# PowerCenter mapping variables. Override per run, e.g.",
        "#   dbt build --vars '{\"run_date\": \"2026-01-01\"}'",
        "vars:",
    ]
    for name, variable in sorted(variables.items()):
        lines.append(f"  {name}: '{_iso_default(variable.default_value)}'")
    return "\n".join(lines) + "\n"


def _dbt_project_yml() -> str:
    return f"""\
name: '{PROJECT_NAME}'
version: '1.0.0'
config-version: 2

profile: '{PROFILE_NAME}'

model-paths: ["models"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

# Every PowerCenter target was loaded with truncate-and-insert, so a table is
# the faithful materialization. Staging stays a view, per dbt convention.
models:
  {PROJECT_NAME}:
    staging:
      +materialized: view
    marts:
      +materialized: table
"""


def _profiles_yml() -> str:
    return f"""\
# Committed deliberately so the project runs with no setup. DuckDB writes to a
# local file, so no warehouse credentials exist to leak.
{PROFILE_NAME}:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: '{PROJECT_NAME}.duckdb'
      threads: 4
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def write_project(
    repo: Repository,
    out_dir: Path | str,
    seeds_dir: Path | str | None = None,
) -> EmitResult:
    out_dir = Path(out_dir)
    plans = plan_models(repo)
    written: list[Path] = []
    test_count = 0

    (out_dir / "models").mkdir(parents=True, exist_ok=True)

    def write(relative: str, content: str) -> None:
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    write("dbt_project.yml", _dbt_project_yml() + _vars_block(repo))
    write("profiles.yml", _profiles_yml())

    model_tables = {plan.table.name for plan in plans if plan.table}
    raw_tables = [
        table for table in repo.sources.values() if table.name not in model_tables
    ]
    write("models/staging/__sources.yml", _source_yaml(repo, raw_tables))

    for plan in plans:
        write(f"models/{plan.layer}/{plan.name}.sql", build_model_sql(repo, plan))
        yaml, count = _model_yaml(repo, plan)
        write(f"models/{plan.layer}/{plan.name}.yml", yaml)
        test_count += count

    if seeds_dir is not None:
        seeds_dir = Path(seeds_dir)
        target = out_dir / "seeds"
        target.mkdir(parents=True, exist_ok=True)
        for csv_path in sorted(seeds_dir.glob("*.csv")):
            destination = target / csv_path.name
            shutil.copyfile(csv_path, destination)
            written.append(destination)

    return EmitResult(
        out_dir=out_dir,
        models=tuple(plan.name for plan in plans),
        files=tuple(written),
        tests=test_count,
    )
