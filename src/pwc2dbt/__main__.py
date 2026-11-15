"""Command line entry point.

Usage::

    python -m pwc2dbt inspect FLOWLINE_DEMO_JAFFLESHOP.xml

Commands are added as the converter grows; ``inspect`` reports what the parser
found, which is the quickest way to sanity-check a new export.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .model import Kind, Repository
from .parse import ParseError, parse_file


def _heading(text: str) -> str:
    return f"\n{text}\n{'-' * len(text)}"


def _render_inspect(repo: Repository) -> str:
    """Build the human-readable inventory printed by ``inspect``."""
    out: list[str] = []

    out.append(f"repository : {repo.name}")
    out.append(f"folder     : {repo.folder_name}")
    out.append(f"database   : {repo.database_type}")

    # --- sources and targets ------------------------------------------------
    out.append(_heading(f"sources ({len(repo.sources)})"))
    for name in sorted(repo.sources):
        table = repo.sources[name]
        keys = len(table.primary_keys), len(table.foreign_keys)
        out.append(
            f"  {name:<20} schema={table.schema or '-':<6} "
            f"cols={len(table.columns):<3} pk={keys[0]} fk={keys[1]}"
        )

    out.append(_heading(f"targets ({len(repo.targets)})"))
    for name in sorted(repo.targets):
        table = repo.targets[name]
        out.append(f"  {name:<28} cols={len(table.columns)}")

    # --- mappings -----------------------------------------------------------
    out.append(_heading(f"mappings ({len(repo.mappings)})"))
    total_targets = 0
    for mapping in repo.mappings:
        kinds = Counter(t.kind.value for t in mapping.transformations.values())
        kind_summary = ", ".join(
            f"{kind} {count}" for kind, count in sorted(kinds.items())
        )
        targets = mapping.target_instances
        total_targets += len(targets)
        out.append(f"\n  {mapping.name}")
        out.append(f"    transformations : {len(mapping.transformations)}")
        out.append(f"    instances       : {len(mapping.instances)}")
        out.append(f"    connectors      : {len(mapping.connectors)}")
        out.append(f"    variables       : {', '.join(v.name for v in mapping.variables) or '-'}")
        out.append(f"    kinds           : {kind_summary}")
        out.append(f"    targets ({len(targets)})     : {', '.join(t.name for t in targets)}")

    # --- totals -------------------------------------------------------------
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
    # One dbt model will be produced per target instance, so this is the
    # headline number: not the mapping count.
    out.append(f"  target instances: {total_targets}   <- one dbt model each")

    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pwc2dbt",
        description="Convert Informatica PowerCenter mapping exports into dbt models.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser(
        "inspect",
        help="report what the parser found in an export (no output written)",
    )
    inspect.add_argument("xml", type=Path, help="path to the PowerCenter export XML")

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
        return 0

    # argparse enforces a known command, so this is unreachable in practice.
    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
