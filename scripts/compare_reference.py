"""Diff the converted models against dbt's real jaffle shop project.

The PowerCenter export mirrors dbt's jaffle shop, so that project is a genuine
reference: working code someone else maintains, run on the same seed data.
Where the two disagree, the difference has to be explained by the mapping.

    python scripts/compare_reference.py

Clones dbt-labs/jaffle-shop (the `jaffle-shop-old` branch, which runs on
released dbt), points it at this repo's seeds, builds it, and compares.
Needs network access the first time.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "seeds" / "jaffle"
WORK = ROOT / ".reference"
OURS = ROOT / "dbt" / "jaffle_converted" / "jaffle_converted.duckdb"

REPO = "https://github.com/dbt-labs/jaffle-shop.git"
# `main` requires dbt >= 2.0.0, which is not released; this branch runs on 1.x.
BRANCH = "jaffle-shop-old"

PROFILE = """\
default:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: 'jaffle_reference.duckdb'
      threads: 4
"""

#: Model name -> the column identifying a row in both projects.
KEYS = {
    "stg_customers": "customer_id",
    "stg_orders": "order_id",
    "stg_order_items": "order_item_id",
    "stg_products": "product_id",
    "stg_locations": "location_id",
    "customers": "customer_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "products": "product_id",
}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def dbt() -> str | None:
    """Find the dbt executable for the interpreter running this script.

    Not simply "next to python": that holds in a virtualenv, but a system
    Python keeps its executables in a sibling Scripts/bin directory.
    sysconfig knows which it is.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    for directory in (scripts, Path(sys.executable).parent):
        for name in ("dbt.exe", "dbt"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("dbt")


def build_reference() -> Path:
    """Clone, configure and build the reference project. Returns its database."""
    executable = dbt()
    if executable is None:
        sys.exit(
            "could not find a dbt executable for this interpreter\n"
            f"  interpreter : {sys.executable}\n"
            f"  looked in   : {sysconfig.get_path('scripts')}\n"
            '  install it  : pip install -e ".[dbt]"'
        )

    if not WORK.is_dir():
        print(f"cloning {REPO} ({BRANCH})")
        cloned = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--branch", BRANCH, REPO, str(WORK)],
            capture_output=True,
            text=True,
        )
        if cloned.returncode != 0:
            sys.exit(f"clone failed (network?):\n{cloned.stderr[-800:]}")

    # Same input as the converted project, or the comparison means nothing.
    (WORK / "profiles.yml").write_text(PROFILE, encoding="utf-8")
    for csv in sorted(SEEDS.glob("*.csv")):
        (WORK / "seeds" / "jaffle-data" / csv.name).write_bytes(csv.read_bytes())

    print("dbt deps")
    run([executable, "deps"], WORK)

    print("dbt build (seeds first, so the sources exist)")
    run([executable, "seed", "--vars", '{"load_source_data": true}'], WORK)
    built = run([executable, "build", "--vars", '{"load_source_data": true}'], WORK)
    if "ERROR=0" not in built.stdout:
        sys.exit(f"reference build failed:\n{built.stdout[-2500:]}")

    return WORK / "jaffle_reference.duckdb"


def compare(ours: Path, reference: Path) -> int:
    import duckdb

    connection = duckdb.connect()
    connection.execute(f"attach '{ours}' as ours (read_only)")
    connection.execute(f"attach '{reference}' as ref (read_only)")

    def columns(database: str, table: str) -> list[str] | None:
        try:
            return [
                row[0]
                for row in connection.execute(
                    f"describe {database}.main.{table}"
                ).fetchall()
            ]
        except Exception:
            return None

    print()
    print(f"{'model':18} {'ours':>6} {'ref':>6}  shared columns")
    print("-" * 64)

    differences: list[str] = []
    for table, key in KEYS.items():
        ours_columns = columns("ours", table)
        ref_columns = columns("ref", table)
        if ours_columns is None or ref_columns is None:
            print(f"{table:18} {'missing from one side':>30}")
            continue

        ours_rows = connection.execute(
            f"select count(*) from ours.main.{table}"
        ).fetchone()[0]
        ref_rows = connection.execute(
            f"select count(*) from ref.main.{table}"
        ).fetchone()[0]

        shared = [c for c in ours_columns if c in ref_columns and c != key]
        mismatched = []
        if key in ours_columns and key in ref_columns:
            for column in shared:
                count = connection.execute(
                    f"select count(*) from ours.main.{table} o "
                    f"join ref.main.{table} r using ({key}) "
                    f"where o.{column} is distinct from r.{column}"
                ).fetchone()[0]
                if count:
                    mismatched.append(f"{column} ({count})")

        rows = f"{ours_rows:>6} {ref_rows:>6}"
        if ours_rows != ref_rows:
            print(f"{table:18} {rows}  ROW COUNT DIFFERS")
            differences.append(f"{table}: {ours_rows} rows vs {ref_rows}")
        elif mismatched:
            print(f"{table:18} {rows}  {len(shared)} shared, differing: "
                  + ", ".join(mismatched))
            differences.append(f"{table}: {', '.join(mismatched)}")
        else:
            print(f"{table:18} {rows}  {len(shared)} shared, all identical")

    print()
    if not differences:
        print("every compared model is identical to the reference project")
        return 0

    print(f"{len(differences)} model(s) differ. Each should be explained by the")
    print("mapping — see 'Checked against dbt's jaffle shop' in the README.")
    return 0


def main() -> int:
    if not OURS.is_file():
        sys.exit(
            f"no converted database at {OURS}\n"
            "run the converter and dbt build first:\n"
            "  python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml "
            "--out dbt/jaffle_converted\n"
            "  cd dbt/jaffle_converted && dbt build"
        )
    if not SEEDS.is_dir():
        sys.exit(f"no seed data at {SEEDS}")
    return compare(OURS, build_reference())


if __name__ == "__main__":
    raise SystemExit(main())
