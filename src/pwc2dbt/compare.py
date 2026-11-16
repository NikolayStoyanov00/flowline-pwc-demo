"""Compare the reference implementation's output against the dbt models'.

Runs the mapping row by row (:mod:`pwc2dbt.simulate`), reads the tables dbt
built, and reports where they disagree — row counts first, then cell by cell.

Values are normalised before comparing, because the two paths legitimately
produce different Python types for the same number: a DECIMAL column arrives
from DuckDB as ``Decimal``, and the interpreter computes a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from math import isclose
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import Repository
from .simulate import Result, VariableOrder, load_seeds, run_repository

__all__ = ["CompareError", "TableDiff", "Comparison", "compare_tables", "compare", "same"]

#: Numbers are compared to this many decimal places. The session in this
#: export has high precision off, so both sides are doing float arithmetic and
#: the last place or two can differ.
TOLERANCE = 6


class CompareError(Exception):
    """The comparison cannot be made."""


def normalise(value: Any) -> Any:
    """Reduce a value to something comparable across both paths."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    # "600" and 600 are the same cell loaded two ways.
    try:
        return float(text)
    except ValueError:
        return text


def same(left: Any, right: Any) -> bool:
    """Whether two normalised values should count as equal.

    Floats get a tolerance rather than being rounded and compared: rounding
    puts 0.9978075 and 0.9978075000000001 on opposite sides of a boundary,
    which is noise, not a disagreement.
    """
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, float) and isinstance(right, float):
        return isclose(left, right, rel_tol=10**-TOLERANCE, abs_tol=10**-TOLERANCE)
    return left == right


def _sortable(row: tuple) -> tuple:
    return tuple((v is None, type(v).__name__, "" if v is None else v) for v in row)


@dataclass
class TableDiff:
    name: str
    simulated_rows: int
    dbt_rows: int
    columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    key: str | None = None
    #: Keys present on one side only.
    only_simulated: tuple[Any, ...] = ()
    only_dbt: tuple[Any, ...] = ()
    #: Column name -> how many cells differ.
    mismatched: dict[str, int] = field(default_factory=dict)
    #: A few concrete examples: (key, column, simulated, dbt).
    examples: list[tuple[Any, str, Any, Any]] = field(default_factory=list)
    #: How the comparison was done, when that is worth saying.
    note: str = ""
    #: False when the table is absent from the dbt database, i.e. there was
    #: nothing to compare. Without this a missing table whose reference side
    #: is also empty would report OK and exit 0.
    present: bool = True

    @property
    def matches(self) -> bool:
        return (
            self.present
            and self.simulated_rows == self.dbt_rows
            and not self.missing_columns
            and not self.only_simulated
            and not self.only_dbt
            and not self.mismatched
        )


@dataclass
class Comparison:
    diffs: list[TableDiff] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def matched(self) -> list[TableDiff]:
        return [d for d in self.diffs if d.matches]

    @property
    def differing(self) -> list[TableDiff]:
        return [d for d in self.diffs if not d.matches]


def _candidate_key(
    columns: Sequence[str],
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> str | None:
    """A column that identifies a row on both sides, if there is one.

    With a key, differences can be reported per column, which is far more
    useful than "these two row sets are not equal".
    """
    for column in columns:
        left_keys = [normalise(row.get(column)) for row in left]
        right_keys = [normalise(row.get(column)) for row in right]
        if None in left_keys or None in right_keys:
            continue
        if len(set(left_keys)) == len(left_keys) and len(set(right_keys)) == len(
            right_keys
        ):
            return column
    return None


def compare_tables(
    name: str,
    columns: Sequence[str],
    simulated: Sequence[Mapping[str, Any]],
    from_dbt: Sequence[Mapping[str, Any]],
    *,
    max_examples: int = 5,
) -> TableDiff:
    diff = TableDiff(
        name=name,
        simulated_rows=len(simulated),
        dbt_rows=len(from_dbt),
        columns=tuple(columns),
    )

    if from_dbt:
        present = set(from_dbt[0])
        diff.missing_columns = tuple(
            c for c in columns if c.lower() not in {p.lower() for p in present}
        )

    shared = [c for c in columns if c not in diff.missing_columns]
    lowered = [
        {k.lower(): v for k, v in row.items()} for row in from_dbt
    ]
    sim_lowered = [{k.lower(): v for k, v in row.items()} for row in simulated]
    keys = [c.lower() for c in shared]

    key = _candidate_key(keys, sim_lowered, lowered)
    diff.key = key

    if key is None:
        # No key: compare the row sets as multisets instead.
        diff.note = "no unique column found; compared as unordered row sets"
        def bucket(row):
            out = []
            for column in keys:
                value = normalise(row.get(column))
                out.append(round(value, TOLERANCE) if isinstance(value, float) else value)
            return tuple(out)

        left = sorted((bucket(row) for row in sim_lowered), key=_sortable)
        right = sorted((bucket(row) for row in lowered), key=_sortable)
        if left != right:
            left_only = [row for row in left if row not in right]
            right_only = [row for row in right if row not in left]
            diff.only_simulated = tuple(left_only[:max_examples])
            diff.only_dbt = tuple(right_only[:max_examples])
        return diff

    sim_by_key = {normalise(row[key]): row for row in sim_lowered}
    dbt_by_key = {normalise(row[key]): row for row in lowered}

    diff.only_simulated = tuple(sorted(set(sim_by_key) - set(dbt_by_key))[:max_examples])
    diff.only_dbt = tuple(sorted(set(dbt_by_key) - set(sim_by_key))[:max_examples])

    for shared_key in sorted(set(sim_by_key) & set(dbt_by_key)):
        left_row, right_row = sim_by_key[shared_key], dbt_by_key[shared_key]
        for column in keys:
            left_value = normalise(left_row.get(column))
            right_value = normalise(right_row.get(column))
            if not same(left_value, right_value):
                diff.mismatched[column] = diff.mismatched.get(column, 0) + 1
                if len(diff.examples) < max_examples:
                    diff.examples.append(
                        (shared_key, column, left_row.get(column), right_row.get(column))
                    )

    return diff


def _read_dbt_tables(
    duckdb_path: Path, names: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise CompareError("duckdb is needed to read the dbt output") from exc

    if not duckdb_path.is_file():
        raise CompareError(
            f"no dbt database at {duckdb_path}; run `dbt build` in the "
            "generated project first"
        )

    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        existing = {
            row[0].lower()
            for row in connection.execute(
                "select table_name from information_schema.tables"
            ).fetchall()
        }
        tables: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            if name.lower() not in existing:
                continue
            cursor = connection.execute(f'select * from "{name.lower()}"')
            headers = [d[0] for d in cursor.description]
            tables[name] = [dict(zip(headers, row, strict=True)) for row in cursor.fetchall()]
        return tables
    finally:
        connection.close()


def compare(
    repo: Repository,
    seeds_dir: Path | str,
    duckdb_path: Path | str,
    *,
    variable_order: str = VariableOrder.DOCUMENT,
) -> Comparison:
    """Run the mapping, read dbt's tables, and diff them."""
    seeds_dir = Path(seeds_dir)
    if not seeds_dir.is_dir():
        raise CompareError(f"no seed data at {seeds_dir}")

    simulated: Result = run_repository(
        repo, load_seeds(repo, seeds_dir), variable_order=variable_order
    )
    from_dbt = _read_dbt_tables(Path(duckdb_path), sorted(simulated.tables))

    comparison = Comparison(notes=list(simulated.notes))
    for name in sorted(simulated.tables):
        if name not in from_dbt:
            comparison.diffs.append(
                TableDiff(
                    name=name,
                    simulated_rows=len(simulated.tables[name]),
                    dbt_rows=0,
                    note="not found in the dbt database",
                    present=False,
                )
            )
            continue
        comparison.diffs.append(
            compare_tables(
                name,
                simulated.columns.get(name, ()),
                simulated.tables[name],
                from_dbt[name],
            )
        )
    return comparison
