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
import sysconfig
import sys
from pathlib import Path

import pytest

from pwc2dbt.emit import write_project

duckdb = pytest.importorskip("duckdb")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEDS = PROJECT_ROOT / "seeds" / "jaffle"


def _dbt_executable() -> str | None:
    """The dbt executable for the interpreter running the tests.

    A virtualenv puts it next to python; a system Python puts it in a sibling
    scripts directory. Getting this wrong skips every end-to-end test silently.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    for directory in (scripts, Path(sys.executable).parent):
        for name in ("dbt.exe", "dbt"):
            candidate = directory / name
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
    emitted = write_project(repo, out, seeds_dir=SEEDS)

    built = subprocess.run(
        [dbt, "build", "--project-dir", str(out), "--profiles-dir", str(out)],
        capture_output=True,
        text=True,
        cwd=out,
        timeout=900,
    )
    if built.returncode != 0:
        pytest.fail(
            "dbt build failed\n"
            + built.stdout[-6000:]
            + "\n--- stderr ---\n"
            + built.stderr[-2000:]
        )

    # The project, and so the database, is named after the output directory.
    database = out / f"{emitted.project_name}.duckdb"
    connection = duckdb.connect(str(database), read_only=True)
    yield connection, built.stdout, database
    connection.close()


def count(connection, table: str) -> int:
    return connection.execute(f"select count(*) from {table}").fetchone()[0]


class TestItBuilds:
    def test_dbt_build_succeeds_with_no_errors(self, built) -> None:
        _, output, _ = built
        assert "ERROR=0" in output

    def test_every_model_and_test_passed(self, built) -> None:
        _, output, _ = built
        assert "WARN=0" in output
        assert "Completed successfully" in output

    def test_all_eighteen_models_exist(self, built) -> None:
        connection, _, _ = built
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
        connection, _, _ = built
        assert count(connection, model) == count(connection, seed)


class TestTheCrossedPriceColumns:
    def test_the_swap_survives_into_the_data(self, built) -> None:
        """EXP_PRODUCTS computes dollars into PRODUCT_PRICE and cents into
        PRODUCT_PRICE_CENTS; the connectors then cross them. So the column
        named for cents holds dollars, and vice versa."""
        connection, _, _ = built
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
        connection, _, _ = built
        seeded = count(connection, "raw_customers")
        in_mart = count(connection, "customers")
        with_orders = connection.execute(
            "select count(distinct customer) from raw_orders"
        ).fetchone()[0]

        assert in_mart == with_orders
        assert in_mart < seeded

    def test_orders_without_items_are_dropped(self, built) -> None:
        connection, _, _ = built
        orphans = connection.execute(
            "select count(*) from raw_orders o where not exists "
            "(select 1 from raw_items i where i.order_id = o.id)"
        ).fetchone()[0]
        assert orphans > 0
        assert count(connection, "orders") == count(connection, "stg_orders") - orphans


class TestRowNumberCounter:
    """CUSTOMER_ORDER_NUMBER reproduces the mapping's bug, and the numbers
    prove which of the two readings was emitted. See README quirk 3."""

    def test_the_counter_never_restarts(self, built) -> None:
        """The mapping sets v_PREV_CUSTOMER_ID before the test that reads it,
        so the test is always true and the counter runs across the whole
        table. The highest value is therefore the row count, not the busiest
        customer's order count."""
        connection, _, _ = built
        highest, rows = connection.execute(
            "select max(customer_order_number), count(*) from orders"
        ).fetchone()
        assert highest == rows

        busiest = connection.execute(
            "select max(n) from (select count(*) as n from orders group by customer_id)"
        ).fetchone()[0]
        # The corrected reading would top out here instead.
        assert busiest < rows

    def test_every_order_gets_a_distinct_number(self, built) -> None:
        """A global sequence, so the numbers are unique table-wide rather
        than unique per customer."""
        connection, _, _ = built
        distinct, rows = connection.execute(
            "select count(distinct customer_order_number), count(*) from orders"
        ).fetchone()
        assert distinct == rows

    def test_the_first_customer_flag_is_always_zero(self, built) -> None:
        """EXP_MARK_FIRST_CUSTOMER compares three columns against variables
        that already hold this row's values, so the test cannot fail and the
        flag never fires."""
        connection, _, _ = built
        flagged = connection.execute(
            "select count(*) from store_daily_kpis where unique_customers <> 0"
        ).fetchone()[0]
        assert flagged == 0


class TestRouterAndUnionRoundTrip:
    def test_every_location_survives_the_router_and_union(self, built) -> None:
        """RTR_REGION splits locations three ways on a derived region and
        UN_REGIONS merges them back, so nothing should be lost or duplicated."""
        connection, _, _ = built
        assert count(connection, "stg_locations") == count(connection, "raw_stores")

    def test_each_location_lands_in_exactly_one_region(self, built) -> None:
        connection, _, _ = built
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
        emitted = write_project(repo, out, seeds_dir=SEEDS)

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
                str(out / f"{emitted.project_name}.duckdb"), read_only=True
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


class TestAgainstTheReferenceImplementation:
    """Diff dbt's output against the row-by-row reference implementation.

    This shares the converter's reading of the Informatica documentation but
    none of its code, so a match shows the two implementations agree — not
    that either matches a real PowerCenter run. See the README.
    """

    def test_every_table_matches_cell_for_cell(self, repo, built) -> None:
        # No variable_order: both sides take their default, so this breaks if
        # the converter and the reference ever drift apart on the reading.
        from pwc2dbt.reference.compare import compare

        _, _, database = built
        comparison = compare(repo, SEEDS, database)
        differing = {
            diff.name: dict(diff.mismatched) or diff.note
            for diff in comparison.differing
        }
        assert differing == {}
        assert len(comparison.matched) == 16

    def test_correcting_the_mapping_would_change_three_tables(
        self, repo, built
    ) -> None:
        """What the quirk costs, measured.

        The converter reproduces the mapping's variable order. Correcting it
        (``--variable-order deferred``) changes three tables, so reproducing
        it is a real choice rather than a cosmetic one.
        """
        from pwc2dbt.reference.compare import compare
        from pwc2dbt.reference.simulate import VariableOrder

        _, _, database = built
        comparison = compare(
            repo, SEEDS, database, variable_order=VariableOrder.DEFERRED
        )
        assert {diff.name for diff in comparison.differing} == {
            "ORDERS",
            "ORDER_FINANCIALS",
            "STORE_DAILY_KPIS",
        }
