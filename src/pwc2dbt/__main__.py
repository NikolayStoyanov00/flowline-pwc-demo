"""Command line entry point.

::

    python -m pwc2dbt inspect     FLOWLINE_DEMO_JAFFLESHOP.xml
    python -m pwc2dbt plan        FLOWLINE_DEMO_JAFFLESHOP.xml
    python -m pwc2dbt expressions FLOWLINE_DEMO_JAFFLESHOP.xml
    python -m pwc2dbt convert     FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
    python -m pwc2dbt compare     FLOWLINE_DEMO_JAFFLESHOP.xml
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .compare import CompareError, compare as compare_outputs
from .emit import write_project
from .expressions import ExpressionError, translate
from .graph import ModelPlan, plan_models, split_points, unsupported_steps
from .model import Kind, Repository, Transformation
from .parse import ParseError, parse_file
from .simulate import SimulationError, VariableOrder
from .sql import SqlError


def _heading(text: str) -> str:
    return f"\n{text}\n{'-' * len(text)}"


def _render_inspect(repo: Repository) -> str:
    out: list[str] = []

    out.append(f"repository : {repo.name}")
    out.append(f"folder     : {repo.folder_name}")
    out.append(f"database   : {repo.database_type}")

    out.append(_heading(f"sources ({len(repo.sources)})"))
    for name in sorted(repo.sources):
        table = repo.sources[name]
        out.append(
            f"  {name:<20} schema={table.schema or '-':<6} "
            f"cols={len(table.columns):<3} "
            f"pk={len(table.primary_keys)} fk={len(table.foreign_keys)}"
        )

    out.append(_heading(f"targets ({len(repo.targets)})"))
    for name in sorted(repo.targets):
        out.append(f"  {name:<28} cols={len(repo.targets[name].columns)}")

    out.append(_heading(f"mappings ({len(repo.mappings)})"))
    total_targets = 0
    for mapping in repo.mappings:
        kinds = Counter(t.kind.value for t in mapping.transformations.values())
        targets = mapping.target_instances
        total_targets += len(targets)
        out.append(f"\n  {mapping.name}")
        out.append(f"    transformations : {len(mapping.transformations)}")
        out.append(f"    instances       : {len(mapping.instances)}")
        out.append(f"    connectors      : {len(mapping.connectors)}")
        out.append(
            f"    variables       : "
            f"{', '.join(v.name for v in mapping.variables) or '-'}"
        )
        out.append(
            f"    kinds           : "
            f"{', '.join(f'{k} {c}' for k, c in sorted(kinds.items()))}"
        )
        out.append(
            f"    targets ({len(targets)})     : "
            f"{', '.join(t.name for t in targets)}"
        )

    all_kinds: Counter[str] = Counter()
    for mapping in repo.mappings:
        all_kinds.update(t.kind.value for t in mapping.transformations.values())

    out.append(_heading("transformation kinds, all mappings"))
    for kind, count in all_kinds.most_common():
        out.append(f"  {kind:<20} {count}")

    unknown = all_kinds.get(Kind.UNKNOWN.value, 0)
    if unknown:
        out.append(f"\n  WARNING: {unknown} transformation(s) of an unrecognised type")

    out.append(_heading("totals"))
    out.append(f"  transformations : {sum(all_kinds.values())}")
    out.append(f"  instances       : {sum(len(m.instances) for m in repo.mappings)}")
    out.append(f"  connectors      : {sum(len(m.connectors) for m in repo.mappings)}")
    out.append(f"  target instances: {total_targets}   <- one dbt model each")

    return "\n".join(out)


def _render_chain(plan: ModelPlan, verbose: bool) -> list[str]:
    """One line per step, or a single summary line when not verbose."""
    if not verbose:
        kinds = Counter(step.kind.value for step in plan.steps)
        return [
            "    steps    : "
            + ", ".join(f"{k} x{c}" if c > 1 else k for k, c in sorted(kinds.items()))
        ]

    lines = ["    steps    :"]
    shared = set(plan.shared_steps)
    for position, step in enumerate(plan.steps, start=1):
        marker = "  <- shared" if step.name in shared else ""
        lines.append(
            f"      {position:>2}. {step.name:<32} [{step.kind.value}]{marker}"
        )
    return lines


def _render_plan(repo: Repository, verbose: bool) -> str:
    plans = plan_models(repo)
    out: list[str] = []

    by_mapping: dict[str, list[ModelPlan]] = {}
    for plan in plans:
        by_mapping.setdefault(plan.mapping_name, []).append(plan)

    for mapping_name, mapping_plans in by_mapping.items():
        out.append(
            _heading(f"{mapping_name}  ->  {len(mapping_plans)} model(s)")
        )
        for plan in mapping_plans:
            out.append(f"\n  {plan.path}")
            label = "shared" if plan.is_intermediate else "target"
            out.append(f"    {label:<9}: {plan.leaf.name} ({plan.schema or '-'})")
            out.append(f"    reads    : {len(plan.reads)} table read(s)")
            for read in plan.reads:
                # The instance name is shown because one table is often read
                # more than once in a mapping, as separate graph nodes.
                alias = (
                    "" if read.instance_name == read.table.name
                    else f" (as {read.instance_name})"
                )
                kind = "model " if read.is_model else "source"
                out.append(f"      {kind} {read.table.name}{alias}")
            if plan.depends_on:
                out.append(f"    ref()    : {', '.join(plan.depends_on)}")
            if plan.boundaries:
                out.append(
                    f"    reuses   : "
                    + ", ".join(f"{k} -> {v}" for k, v in plan.boundaries.items())
                )
            out.extend(_render_chain(plan, verbose))

            blocked = unsupported_steps(plan)
            if blocked:
                out.append(f"    BLOCKED  : unsupported steps {', '.join(blocked)}")

    for mapping in repo.mappings:
        forks = split_points(mapping)
        if not forks:
            continue
        shared = len(
            {
                name
                for plan in plans
                if plan.mapping_name == mapping.name
                for name in plan.shared_steps
            }
        )
        out.append(_heading(f"{mapping.name}: where pipelines fork"))
        out.append(
            f"  {shared} step(s) feed more than one model, but they all sit "
            f"upstream of {len(forks)} fork(s):"
        )
        for name, targets in sorted(forks.items()):
            out.append(f"      {name:<24} -> {len(targets)} targets: {', '.join(targets)}")

    out.append(_heading("totals"))
    out.append(f"  models : {len(plans)}")
    layers = Counter(p.layer for p in plans)
    for layer, count in sorted(layers.items()):
        out.append(f"  {layer:<8}: {count}")

    return "\n".join(out)


def _expression_corpus(
    repo: Repository,
) -> list[tuple[str, str, str, str, str]]:
    """Every Informatica expression in the export, with where it came from.

    Expressions hide in four places, not just on ports.
    """
    corpus: list[tuple[str, str, str, str, str]] = []

    def add(
        mapping: str,
        transformation: Transformation,
        origin: str,
        label: str,
        text: str,
        datatype: str = "",
    ) -> None:
        corpus.append((mapping, f"{transformation.name}.{label}", origin, text, datatype))

    for mapping in repo.mappings:
        for transformation in mapping.transformations.values():
            for port in transformation.ports:
                if port.expression and not port.is_passthrough:
                    origin = "variable" if port.is_variable else "port"
                    add(
                        mapping.name,
                        transformation,
                        origin,
                        port.name,
                        port.expression,
                        port.datatype,
                    )
            for group in transformation.groups:
                if group.expression:
                    add(mapping.name, transformation, "router group", group.name, group.expression)
            for key, origin in (
                ("Filter Condition", "filter"),
                ("Join Condition", "join"),
            ):
                condition = transformation.attribute(key)
                if condition:
                    add(mapping.name, transformation, origin, key, condition)

    return corpus


def _render_expressions(repo: Repository, origin_filter: str | None) -> str:
    out: list[str] = []
    translated = 0
    failed: list[tuple[str, str, str]] = []
    by_origin: Counter[str] = Counter()

    current_mapping = None
    for mapping, label, origin, text, datatype in _expression_corpus(repo):
        if origin_filter and origin != origin_filter:
            continue
        by_origin[origin] += 1

        if mapping != current_mapping:
            out.append(_heading(mapping))
            current_mapping = mapping

        out.append(f"\n  {label}   [{origin}{', ' + datatype if datatype else ''}]")
        out.append(f"    pwc : {text}")
        try:
            sql = translate(text, result_datatype=datatype)
        except ExpressionError as exc:
            out.append(f"    FAIL: {exc}")
            failed.append((mapping, label, str(exc)))
        else:
            out.append(f"    sql : {sql}")
            translated += 1

    out.append(_heading("totals"))
    for origin, count in sorted(by_origin.items()):
        out.append(f"  {origin:<14}: {count}")
    out.append(f"  {'translated':<14}: {translated}")
    out.append(f"  {'failed':<14}: {len(failed)}")
    for mapping, label, message in failed:
        out.append(f"      {mapping}.{label}: {message}")

    return "\n".join(out)


def _render_comparison(comparison) -> str:
    out: list[str] = []
    out.append(
        f"matched {len(comparison.matched)} of {len(comparison.diffs)} tables"
    )

    for diff in comparison.diffs:
        flag = "OK  " if diff.matches else "DIFF"
        out.append(
            f"  {flag} {diff.name:<28} reference={diff.simulated_rows:>6} "
            f"dbt={diff.dbt_rows:>6}"
            + (f"  key={diff.key}" if diff.key else "")
        )
        if diff.matches:
            continue
        if diff.note:
            out.append(f"         {diff.note}")
        if diff.missing_columns:
            out.append(f"         columns missing from dbt: {', '.join(diff.missing_columns)}")
        for column, count in sorted(diff.mismatched.items()):
            out.append(f"         {column}: {count} cell(s) differ")
        for key, column, left, right in diff.examples:
            out.append(f"           {column} where key={key}: reference={left!r} dbt={right!r}")
        if diff.only_simulated:
            out.append(f"         only in reference: {diff.only_simulated}")
        if diff.only_dbt:
            out.append(f"         only in dbt: {diff.only_dbt}")

    if comparison.notes:
        out.append(_heading("notes from the reference run"))
        for note in dict.fromkeys(comparison.notes):
            out.append(f"  {note}")

    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pwc2dbt",
        description="Convert Informatica PowerCenter mapping exports into dbt models.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser(
        "inspect", help="report what the parser found in an export"
    )
    inspect.add_argument("xml", type=Path, help="path to the PowerCenter export XML")

    plan = subparsers.add_parser(
        "plan", help="list the dbt models an export produces, and what feeds them"
    )
    plan.add_argument("xml", type=Path, help="path to the PowerCenter export XML")
    plan.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list every step of every model rather than a summary",
    )

    expressions = subparsers.add_parser(
        "expressions",
        help="translate every Informatica expression in an export into SQL",
    )
    expressions.add_argument("xml", type=Path, help="path to the PowerCenter export XML")
    expressions.add_argument(
        "--only",
        choices=["port", "variable", "router group", "filter", "join"],
        help="show only expressions from one place",
    )

    convert = subparsers.add_parser(
        "convert", help="write a runnable dbt project from an export"
    )
    convert.add_argument("xml", type=Path, help="path to the PowerCenter export XML")
    convert.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory to write the dbt project into",
    )
    convert.add_argument(
        "--seeds",
        type=Path,
        default=Path("seeds/jaffle"),
        help="directory of seed CSVs to copy in (default: seeds/jaffle)",
    )

    compare_cmd = subparsers.add_parser(
        "compare",
        help="run the mapping row by row and diff the result against dbt's",
    )
    compare_cmd.add_argument("xml", type=Path, help="path to the PowerCenter export XML")
    compare_cmd.add_argument(
        "--seeds",
        type=Path,
        default=Path("seeds/jaffle"),
        help="directory of seed CSVs to run against (default: seeds/jaffle)",
    )
    compare_cmd.add_argument(
        "--duckdb",
        type=Path,
        default=Path("dbt/jaffle_converted/jaffle_converted.duckdb"),
        help="the database dbt built",
    )
    compare_cmd.add_argument(
        "--variable-order",
        choices=[VariableOrder.DEFERRED, VariableOrder.DOCUMENT],
        default=VariableOrder.DEFERRED,
        help=(
            "how to read local variable evaluation order: 'deferred' treats a "
            "remember-the-previous-row variable as holding the previous row "
            "(the reading the converter uses); 'document' takes the XML order "
            "literally"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        repo = parse_file(args.xml)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.command == "inspect":
        print(_render_inspect(repo))
    elif args.command == "plan":
        print(_render_plan(repo, verbose=args.verbose))
    elif args.command == "expressions":
        print(_render_expressions(repo, args.only))
    elif args.command == "convert":
        seeds = args.seeds if args.seeds and args.seeds.is_dir() else None
        try:
            result = write_project(repo, args.out, seeds)
        except SqlError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {len(result.files)} file(s) to {result.out_dir}")
        print(f"  models : {len(result.models)}")
        print(f"  tests  : {result.tests}")
        if seeds is None:
            print(f"  seeds  : none copied ({args.seeds} not found)")
        print()
        print("next:")
        print(f"  cd {result.out_dir}")
        print("  dbt build")
    elif args.command == "compare":
        try:
            comparison = compare_outputs(
                repo,
                args.seeds,
                args.duckdb,
                variable_order=args.variable_order,
            )
        except (CompareError, SimulationError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(_render_comparison(comparison))
        return 0 if not comparison.differing else 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
