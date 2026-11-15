"""Command line entry point.

::

    python -m pwc2dbt inspect FLOWLINE_DEMO_JAFFLESHOP.xml
    python -m pwc2dbt plan    FLOWLINE_DEMO_JAFFLESHOP.xml
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .graph import ModelPlan, plan_models, split_points, unsupported_steps
from .model import Kind, Repository
from .parse import ParseError, parse_file


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
            out.append(f"    target   : {plan.target.name} ({plan.schema})")
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
