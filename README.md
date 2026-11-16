# pwc2dbt — Informatica PowerCenter to dbt converter

Reads a PowerCenter repository export and writes a runnable dbt project.

On the sample export: **3 mappings → 18 dbt models**, which build and test
clean on DuckDB, and which match **dbt's real jaffle shop project** everywhere
the two overlap except for two deliberate quirks in the source mapping.

Nothing is hard-coded to the jaffle shop answer — every model, column, join and
test is derived from the XML.

---

## Quickstart

Python 3.12+. No warehouse, no accounts.

```bash
pip install -e ".[dev,dbt]"
python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted && dbt build          # PASS=56 ERROR=0
```

| Then | |
|---|---|
| `dbt docs generate && dbt docs serve` | the lineage DAG, `localhost:8080` |
| `python scripts/browse.py` | **the data**, `localhost:4213` |
| `python scripts/compare_reference.py` | diff against dbt's real jaffle shop |
| `python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml` | diff against a row-by-row reference run |
| `pytest` | 251 tests, ~35s |

Close `browse.py` before re-running `dbt build` — it holds the DuckDB file.

Four commands show the converter's reasoning rather than just doing the work:

```bash
python -m pwc2dbt inspect     FLOWLINE_DEMO_JAFFLESHOP.xml   # what was parsed
python -m pwc2dbt plan        FLOWLINE_DEMO_JAFFLESHOP.xml   # the 18 models and their steps
python -m pwc2dbt expressions FLOWLINE_DEMO_JAFFLESHOP.xml   # all 205 expressions -> SQL
python -m pwc2dbt convert     FLOWLINE_DEMO_JAFFLESHOP.xml --out <dir>
```

**Reviewing this?** [`docs/REVIEW.md`](docs/REVIEW.md) is a short path through
it. [`docs/concepts.md`](docs/concepts.md) explains PowerCenter in SQL terms.

---

## How it works

PowerCenter stores a pipeline as a **graph** (boxes wired column by column).
dbt wants **SQL files** (one `select` per table). So: parse the graph, walk back
from each target, turn each step into a CTE.

| PowerCenter | SQL |
|---|---|
| Source Qualifier | `from {{ source() }}` / `{{ ref() }}` |
| Expression | the `select` list |
| Filter / Joiner / Aggregator | `where` / `join ... on` / `group by` |
| Router | one CTE per branch, each with a `where` |
| Union | `union all` |
| Sorter | dropped — except where a window function needs its order |

| Module | |
|---|---|
| `model.py` | PowerCenter concepts as dataclasses |
| `parse.py` | the only module that touches XML |
| `graph.py` | the DAG, and which models to produce |
| `expressions.py` | Informatica expression language → SQL |
| `sql.py` | steps → CTEs → one model |
| `emit.py` | the dbt project: models, YAML, tests, seeds |
| `reference/` | a second implementation, for cross-checking |

**Three decisions that shape everything:**

- **One model per target, not per mapping.** 3 mappings, 16 targets. Splitting
  on connected components would be wrong: the enriched mapping is one connected
  graph writing five targets.
- **Columns are followed, never matched by name.** A `(step, port) → (CTE,
  column)` lookup answers where each input comes from. Load-bearing — see the
  crossed columns below.
- **Shared chains are built once.** Five enriched models shared 33 upstream
  steps above two fork points; promoting those two to `int_` models cut the
  generated SQL from 3,592 to 2,001 lines with identical row counts.

---

## Checked against dbt's jaffle shop

The export mirrors dbt's jaffle shop, so that project is a real reference:
working code someone else maintains. `scripts/compare_reference.py` builds it on
the same seeds and diffs every overlapping model.

```
stg_customers     935    935   all identical
stg_order_items  4528   4528   all identical
stg_locations       6      6   all identical
stg_orders       3000   3000   differing: subtotal, tax_paid, order_total (+ _cents)
stg_products       10     10   differing: product_price
order_items      4528   4528   differing: product_price
products           10     10   differing: product_price
customers         168    935   ROW COUNT DIFFERS
orders           2967   3000   ROW COUNT DIFFERS
```

**Every difference traces to one of the two quirks below. Nothing else
differs.** That is the strongest evidence here, because the reference is not
mine.

It covers only the 11 overlapping models. The 5 enriched marts
(`CUSTOMER_360`, `ORDER_FINANCIALS`, `ORDER_ITEM_ENRICHED`,
`PRODUCT_DAILY_PERFORMANCE`, `STORE_DAILY_KPIS`) have no jaffle shop
equivalent, so they rely on `pwc2dbt compare` — a second implementation of the
documented Informatica semantics, run row by row. That proves the two
implementations are *consistent*, not that either matches a real PowerCenter
run: both came from the same reading of the docs.

---

## Two quirks in the source mapping

Both reproduced faithfully and documented rather than corrected — the job is to
convert the mapping, not to fix it.

### Cents and dollars are crossed, in 8 columns

`EXP_PRODUCTS` and `EXP_ORDERS` each compute a dollars column and a cents
column, and the connectors swap them on the way into the target:

```xml
<CONNECTOR FROMFIELD="SUBTOTAL"       TOFIELD="SUBTOTAL_CENTS" .../>
<CONNECTOR FROMFIELD="SUBTOTAL_CENTS" TOFIELD="SUBTOTAL"       .../>
```

So `subtotal` holds 700 where the reference has 7.00. This is the proof that a
converter cannot match columns by name: a name-matching converter would quietly
swap them back and look *more* correct while doing it.

### `customers` drops 82% of its customers

The join attaching order history to customers is a `Master Outer Join` with
customers as master, so customers with no orders are discarded: **935 seeded,
168 in the mart.** The downstream `IIF(ISNULL(LIFETIME_ORDERS), 0, ...)` guard
can therefore never fire, which suggests `Detail Outer Join` was intended —
what the reference project does. The same pattern drops 33 item-less orders.

Flip that one attribute in the XML and the mart goes to 935, which also shows
the join type is read rather than assumed.

---

## Assumptions, and what is not handled

- **Faithful over corrected.** Where the mapping looks wrong, reproduce and say so.
- **Truncate-and-insert means a table.** Every target's `Pre SQL` is `TRUNCATE
  TABLE`, so marts are tables, staging views, intermediates tables.
- **Sorters are droppable**, except where one establishes the order a window
  function depends on. All 140 sort keys here are ascending.
- **Target schema comes from the `TRUNCATE` statement** — the only place it appears.
- **Tests come from the sources.** All 164 target fields are `NOT A KEY`, so keys
  are traced forward from the `RAW_*` definitions to the model column they become.
- **Unknown means stop.** An unrecognised function or transformation raises.
- **Source columns are cast to their declared PowerCenter types**, not left to
  the warehouse to guess.

Not implemented, because none appears in this export: Lookup, Sequence
Generator, Update Strategy/SCD, Normalizer, Transaction Control, mapplets,
shortcuts, SQL overrides, multi-source Source Qualifiers, incremental loads,
descending and distinct sorters. The expression translator covers the 13
functions used; anything else raises.

---

## Where this is most likely wrong

1. **Join direction.** Ten of sixteen joins are `Master Outer Join`, which keeps
   all *detail* rows — backwards from how it reads. Master/detail is recorded
   only as a `/MASTER` suffix on a port type. Verified against
   [Informatica's docs][mo]. Wrong here and the output still builds and looks fine.
2. **Variable evaluation order.** Whether XML order is evaluation order cannot be
   settled without PowerCenter. The converter treats a remembered variable as
   holding the *previous* row (`lag()`); taking the XML literally instead changes
   3 tables and makes `UNIQUE_CUSTOMERS` 0 everywhere. Measured, not guessed:
   `pwc2dbt compare --variable-order document`.
3. **`TRUNC` on a number.** Informatica uses one name for date and number
   truncation; every use here is a date, so the date reading is taken.
4. **Two interpretations, not translations.** A self-referencing counter becomes
   `row_number()`; a remembered variable becomes `lag()`. Both narrow, both noted
   in the generated SQL. A running *total* is refused rather than guessed at.
5. **Precision and scale** below the source cast are discarded.
6. **Local variables are inlined**, so one used by several outputs appears twice.
7. **`emit._carried_through` fails open** — a column it cannot trace silently
   drops a dbt test instead of complaining.

[mo]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html

---

## Where the coding agent got it wrong

Seven, and **four got through a green test suite** — the main thing I learned.

| What | How it was caught |
|---|---|
| Union *instances* classified unknown (`TEMPLATENAME` is on the definition, not the instance), blocking 2 models | not by the 48 passing tests — they only inspected definitions. The next phase's output said `BLOCKED` |
| Asserted `A + B * C` = 13 with A=10, B=5, C=3. It is 25 | the test *executes* the SQL in DuckDB; string comparison would have agreed with itself |
| `{{ var('run_date') }}` emitted unquoted, so DuckDB read `2026-04-01` as arithmetic = 2021 | only `dbt build`. Valid SQL, and sqlglot parsed it happily |
| Fork detector flagged a Router whose branches reconverge | reading `plan` output and seeing 3 forks where the analysis said 2 |
| An intermediate model referenced a CTE that was never emitted | `dbt build` |
| Source columns left to the warehouse to type: DuckDB inferred `PERISHABLE` as boolean, cast `'Y'` to true, inverting 4,528 cells | the second implementation disagreed. `dbt build` and 201 tests were green |
| Two readings of variable order mixed, so `UNIQUE_CUSTOMERS` was 0 on every row | the same comparison, once the above was fixed |

The pattern: the agent was good at structure and bad at being sure. What caught
things was *output* — running it, printing what it decided, executing the SQL
rather than comparing strings, and running the same mapping a second way.

---

## Process

1. **Research before code.** Three parallel agents mapped the XML, the jaffle
   shop reference and dbt conventions. Most design decisions fell out of the
   resulting numbers: 98 transformations, 1,066 connectors, 16 targets, 13
   expression functions, no lookups, no SQL overrides.
2. **A written plan in ten phases**, each ending in something runnable. One
   question — how to handle shared chains — deliberately deferred until there
   was data to decide it with.
3. **A PR per phase** for the first three, reviewed while the next was built,
   then the rest in one pass once per-phase review became more context than it
   was worth.
4. **Two corrections that changed the result**: cutting comments that restated
   the code, and *"test the behaviour, not the code"* — which moved tests from
   asserting that properties return values to executing the generated SQL. Five
   of the seven mistakes above were caught by that change.
5. **A review pass at the end** (`/code-review`, `/simplify`) which found a
   silent wrong answer: a running total emitted as `row_number()`.

Full record in [`docs/agent-transcript/`](docs/agent-transcript/) — every
prompt, and the whole conversation, with emails and home paths redacted.

## What I would do next

Thread port precision into the generated SQL; emit local variables as CTEs
instead of inlining them; name intermediate models after what they contain; make
`emit._carried_through` fail loudly rather than silently dropping a test.

## Layout

```
FLOWLINE_DEMO_JAFFLESHOP.xml   the input, unchanged
src/pwc2dbt/                   the converter
tests/                         251 tests
scripts/                       browse.py, compare_reference.py
seeds/jaffle/                  3,000-order subset of dbt's jaffle shop seeds
docs/                          REVIEW.md, concepts.md, example-output/, agent-transcript/
```

`dbt/` is not tracked — `convert` generates it. Three sample models live in
[`docs/example-output/`](docs/example-output/) to read without running anything.
