"""Run the converter's output for real.

Generates the project into a temporary directory, runs ``dbt build`` against
DuckDB, and then queries the result. This is the only test that proves the
generated SQL executes and produces the right numbers; everything else checks
the SQL without running it.

Skipped when dbt is not installed, so the rest of the suite still runs:

    pip install -e ".[dbt]"
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pwc2dbt.emit import write_project

duckdb = pytest.importorskip("duckdb")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEDS = PROJECT_ROOT / "seeds" / "jaffle"


def _dbt_executable() -> str | None:
    candidate = Path(sys.executable).parent / "dbt.exe"
    if candidate.is_file():
        return str(candidate)
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("dbt")


@pytest.fixture(scope="module")
def built(repo, tmp_path_factory):
    """The generated project, built with dbt, plus a DuckDB connection."""
    dbt = _dbt_executable()
    if dbt is None:
        pytest.skip("dbt is not installed; pip install -e \".[dbt]\"")
    if not SEEDS.is_dir():
        pytest.skip(f"no seed data at {SEEDS}")

    out = tmp_path_factory.mktemp("dbt_run")
    write_project(repo, out, seeds_dir=SEEDS)

    result = subprocess.run(
        [dbt, "build", "--project-dir", str(out), "--profiles-dir", str(out)],
        capture_output=True,
        text=True,
        cwd=out,
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(
            "dbt build failed\n"
            + result.stdout[-6000:]
            + "\n--- stderr ---\n"
            + result.stderr[-2000:]
        )

    connection = duckdb.connect(str(out / "jaffle_converted.duckdb"), read_only=True)
    yield connection, result.stdout
    connection.close()


def count(connection, table: str) -> int:
    return connection.execute(f"select count(*) from {table}").fetchone()[0]


class TestItBuilds:
    def test_dbt_build_succeeds_with_no_errors(self, built) -> None:
        _, output = built
        assert "ERROR=0" in output

    def test_every_model_and_test_passed(self, built) -> None:
        _, output = built
        assert "WARN=0" in output
        assert "Completed successfully" in output

    def test_all_eighteen_models_exist(self, built) -> None:
        connection, _ = built
        tables = {
            row[0]
            for row in connection.execute(
                "select table_name from information_schema.tables"
            ).fetchall()
        }
        expected = {
            "stg_customers",
            "stg_orders",
            "stg_order_items",
            "stg_products",
            "stg_supplies",
            "stg_locations",
            "int_srt_order_id",
            "int_un_order_bands",
            "customers",
            "orders",
            "order_items",
            "products",
            "supplies",
            "customer_360",
            "order_financials",
            "order_item_enriched",
            "product_daily_performance",
            "store_daily_kpis",
        }
        assert expected <= tables


class TestStagingIsOneToOneWithItsSource:
    """dbt's rule for staging models, which this mapping happens to respect."""

    @pytest.mark.parametrize(
        ("model", "seed"),
        [
            ("stg_customers", "raw_customers"),
            ("stg_orders", "raw_orders"),
            ("stg_order_items", "raw_items"),
            ("stg_products", "raw_products"),
            ("stg_supplies", "raw_supplies"),
            ("stg_locations", "raw_stores"),
        ],
    )
    def test_no_rows_are_gained_or_lost(self, built, model, seed) -> None:
        connection, _ = built
        assert count(connection, model) == count(connection, seed)


class TestTheCrossedPriceColumns:
    def test_the_swap_survives_into_the_data(self, built) -> None:
        """EXP_PRODUCTS computes dollars into PRODUCT_PRICE and cents into
        PRODUCT_PRICE_CENTS; the connectors then cross them. So the column
        named for cents holds dollars, and vice versa."""
        connection, _ = built
        rows = connection.execute(
            "select p.product_price, p.product_price_cents, r.price "
            "from stg_products p join raw_products r on r.sku = p.product_id "
            "order by p.product_id"
        ).fetchall()
        assert rows
        for price, price_cents, raw_price in rows:
            assert price == raw_price           # named dollars, holds cents
            assert price_cents == raw_price / 100  # named cents, holds dollars


class TestMasterOuterJoinDropsRows:
    """`Master Outer Join` keeps all detail rows and discards unmatched master
    rows. The mapping makes customers the master, so customers who never
    ordered do not reach the mart. The real jaffle shop keeps them, which is
    evidence the mapping's join type is not what its author intended."""

    def test_customers_without_orders_are_dropped(self, built) -> None:
        connection, _ = built
        seeded = count(connection, "raw_customers")
        in_mart = count(connection, "customers")
        with_orders = connection.execute(
            "select count(distinct customer) from raw_orders"
        ).fetchone()[0]

        assert in_mart == with_orders
        assert in_mart < seeded

    def test_orders_without_items_are_dropped(self, built) -> None:
        connection, _ = built
        orphans = connection.execute(
            "select count(*) from raw_orders o where not exists "
            "(select 1 from raw_items i where i.order_id = o.id)"
        ).fetchone()[0]
        assert orphans > 0
        assert count(connection, "orders") == count(connection, "stg_orders") - orphans


class TestRowNumberCounter:
    def test_the_counter_restarts_for_each_customer(self, built) -> None:
        """If the partition were missing, the numbering would run across the
        whole table instead of per customer."""
        connection, _ = built
        mismatches = connection.execute(
            "select count(*) from ("
            "  select customer_id, max(customer_order_number) as highest, "
            "         count(*) as orders"
            "  from orders group by customer_id"
            ") where highest <> orders"
        ).fetchone()[0]
        assert mismatches == 0

    def test_the_numbering_has_no_gaps_or_repeats(self, built) -> None:
        connection, _ = built
        repeats = connection.execute(
            "select count(*) from ("
            "  select customer_id, customer_order_number, count(*) as n"
            "  from orders group by 1, 2"
            ") where n > 1"
        ).fetchone()[0]
        assert repeats == 0


class TestRouterAndUnionRoundTrip:
    def test_every_location_survives_the_router_and_union(self, built) -> None:
        """RTR_REGION splits locations three ways on a derived region and
        UN_REGIONS merges them back, so nothing should be lost or duplicated."""
        connection, _ = built
        assert count(connection, "stg_locations") == count(connection, "raw_stores")

    def test_each_location_lands_in_exactly_one_region(self, built) -> None:
        connection, _ = built
        duplicates = connection.execute(
            "select count(*) from ("
            "  select location_id, count(*) as n from stg_locations group by 1"
            ") where n > 1"
        ).fetchone()[0]
        assert duplicates == 0


class TestMappingVariable:
    def test_the_run_date_var_changes_the_output(self, repo, tmp_path) -> None:
        """`$$RUN_DATE` became a dbt var, so recency moves when it is
        overridden. This also proves the var is quoted correctly: unquoted,
        `2026-01-01` parses as arithmetic and the model fails to compile."""
        dbt = _dbt_executable()
        if dbt is None or not SEEDS.is_dir():
            pytest.skip("dbt or seed data unavailable")

        out = tmp_path / "vars"
        write_project(repo, out, seeds_dir=SEEDS)

        # Seed first: the models read *sources*, and dbt has no edge from a
        # source to the seed that fills it, so "+customer_360" would not
        # include the seeds.
        seeded = subprocess.run(
            [dbt, "seed", "--project-dir", str(out), "--profiles-dir", str(out)],
            capture_output=True,
            text=True,
            cwd=out,
            timeout=900,
        )
        assert seeded.returncode == 0, seeded.stdout[-4000:]

        recencies = []
        for run_date in ("2026-01-01", "2026-06-01"):
            result = subprocess.run(
                [
                    dbt,
                    "build",
                    "--project-dir",
                    str(out),
                    "--profiles-dir",
                    str(out),
                    # "+model" is the model and its ancestors; "model+" would
                    # be its children, which is not what needs rebuilding.
                    "--select",
                    "+customer_360",
                    "--vars",
                    f'{{"run_date": "{run_date}"}}',
                ],
                capture_output=True,
                text=True,
                cwd=out,
                timeout=900,
            )
            assert result.returncode == 0, result.stdout[-4000:]
            connection = duckdb.connect(
                str(out / "jaffle_converted.duckdb"), read_only=True
            )
            recencies.append(
                connection.execute(
                    "select min(recency_days) from customer_360"
                ).fetchone()[0]
            )
            connection.close()

        # Five months later, every customer is five months less recent.
        assert recencies[1] > recencies[0]
        assert recencies[1] - recencies[0] == 151
