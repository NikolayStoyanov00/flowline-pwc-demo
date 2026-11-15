"""Open the built warehouse in DuckDB's browser UI.

    python scripts/browse.py [path/to/file.duckdb]

Starts a local server and tries to open a browser at http://localhost:4213,
where the seeded source tables and the generated models can be browsed and
queried side by side. Ctrl-C to stop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

DEFAULT = Path("dbt") / "jaffle_converted" / "jaffle_converted.duckdb"
URL = "http://localhost:4213"


def main(argv: list[str]) -> int:
    try:
        import duckdb
    except ImportError:
        print("duckdb is not installed; pip install -e \".[dev]\"", file=sys.stderr)
        return 1

    path = Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not path.is_file():
        print(
            f"no database at {path}\n"
            "run the converter and `dbt build` first:\n"
            "  python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml "
            "--out dbt/jaffle_converted\n"
            "  cd dbt/jaffle_converted && dbt build",
            file=sys.stderr,
        )
        return 1

    connection = duckdb.connect(str(path))
    connection.execute("install ui")
    connection.execute("load ui")

    # start_ui also opens a browser, which fails where there is none.
    try:
        connection.execute("CALL start_ui()")
    except Exception:
        connection.execute("CALL start_ui_server()")

    tables = connection.execute(
        "select count(*) from information_schema.tables"
    ).fetchone()[0]
    print(f"{path} — {tables} tables")
    print(f"open {URL}   (ctrl-c to stop)")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
