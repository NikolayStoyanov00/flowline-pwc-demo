# pwc2dbt — Informatica PowerCenter to dbt converter

Reads a PowerCenter repository export and writes a runnable dbt project.
On the sample export: **3 mappings → 18 dbt models**, which build and test clean
on DuckDB and match **dbt's real jaffle shop project** everywhere the two
overlap, except for two deliberate quirks in the source mapping.

Nothing is hard-coded to the jaffle shop answer. Each PowerCenter step becomes a
CTE: the converter walks the connector graph back from every target and
translates each step it passes.

---

## How to run it

Python 3.12+, on macOS, Linux or Windows. No warehouse, no accounts.

```bash
python3 -m venv .venv                  # "python" on Windows
source .venv/bin/activate              # .venv\Scripts\activate on Windows
pip install -e ".[dev,dbt]"

python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted && dbt build          # PASS=56 ERROR=0
```

| Then | |
|---|---|
| `dbt docs generate && dbt docs serve` | the lineage DAG, `localhost:8080` |
| `python scripts/browse.py` | the data, `localhost:4213` |
| `python scripts/compare_reference.py` | diff against dbt's real jaffle shop |
| `pytest` | 267 tests, about a minute |

Three commands show the converter's reasoning rather than just doing the work —
the quickest way to judge it without reading Python:

```bash
python -m pwc2dbt plan        FLOWLINE_DEMO_JAFFLESHOP.xml --verbose  # the 18 models, step by step
python -m pwc2dbt expressions FLOWLINE_DEMO_JAFFLESHOP.xml           # all 205 expressions -> SQL
python -m pwc2dbt compare     FLOWLINE_DEMO_JAFFLESHOP.xml           # diff against a row-by-row re-run
```

[`docs/REVIEW.md`](docs/REVIEW.md) is a short guided path through the project.

### Checked against dbt's jaffle shop

The export mirrors dbt's jaffle shop, so that project is a real reference.
`scripts/compare_reference.py` builds it on the same seeds and diffs every
overlapping model:

Of the 11 overlapping models:

- **3 identical** — `stg_customers`, `stg_order_items`, `stg_locations`
- **4 differ only on the price columns** — `stg_orders`, `stg_products`,
  `order_items`, `products`
- **2 differ on row count** — `customers` (168 vs 935), `orders` (2967 vs 3000)

**Every difference traces to one of the two quirks below. Nothing else
differs.** The 5 enriched marts have no jaffle shop equivalent, so those rely on
`pwc2dbt compare`: a second implementation of the documented Informatica
semantics, run row by row. That shows the two implementations are *consistent*,
not that either matches a real PowerCenter run — both came from the same reading
of the docs.

---

## What I assumed, and what I chose not to handle

**Faithful over corrected.** Where the mapping does something odd, the converter
reproduces it and says so. Two cases, both confirmed by the reference project:

- **Cents and dollars are crossed in 8 columns.** `EXP_PRODUCTS` and
  `EXP_ORDERS` each compute a dollars column and a cents column, and the
  connectors swap them into the target, so `subtotal` holds 700 where the
  reference has 7.00. This is why columns are followed through connectors and
  never matched by name — a name-matching converter would quietly swap them back
  and look *more* correct.
- **`customers` drops 82% of its customers.** The join is a `Master Outer Join`
  with customers as master, so customers with no orders are discarded: 935
  seeded, 168 in the mart. The downstream `IIF(ISNULL(LIFETIME_ORDERS), 0, ...)`
  guard can never fire, which suggests `Detail Outer Join` was meant.

Other assumptions:

- Truncate-and-insert targets mean marts are tables, staging views.
- Sorters are droppable, except where one sets the order a window function needs.
- Target schema comes from the `TRUNCATE` statement — the only place it appears.
- All 164 target fields are `NOT A KEY`, so dbt tests are traced forward from
  the `RAW_*` source keys to the model column they become.
- Source columns are cast to their declared PowerCenter types, not left to the
  warehouse to infer.
- **Unknown means stop**: an unrecognised function or transformation raises.

**Not handled**, because none appears in this export: Lookup, Sequence
Generator, Update Strategy/SCD, Normalizer, Transaction Control, mapplets,
shortcuts, SQL overrides, multi-source Source Qualifiers, incremental loads,
descending and distinct sorters. The translator covers the 13 expression
functions used; anything else raises.

---

## Where the converter is most likely to produce wrong output

1. **Join direction.** Ten of sixteen joins are `Master Outer Join`, which keeps
   all *detail* rows — backwards from how it reads — and master/detail is
   recorded only as a `/MASTER` suffix on a port type. Verified against
   [Informatica's docs][mo]. Get it wrong and the output still builds and looks
   fine.
2. **Variable evaluation order.** Whether XML order is evaluation order cannot
   be settled without PowerCenter. The converter treats a remembered variable as
   holding the *previous* row (`lag()`); reading the XML literally instead
   changes 3 tables and makes `UNIQUE_CUSTOMERS` 0 everywhere. Measured, not
   guessed: `pwc2dbt compare --variable-order document`.
3. **Two interpretations rather than translations.** A self-referencing counter
   becomes `row_number()`, a remembered variable becomes `lag()`. Both matches
   are narrow and both are noted in the generated SQL; a running *total* is
   refused rather than guessed at.
4. **`TRUNC` on a number.** Informatica uses one name for date and number
   truncation. Every use here is a date, so the date reading is taken.
5. **Precision and scale** below the source cast are discarded.
6. **`emit._carried_through` fails open** — a column it cannot trace silently
   drops a dbt test instead of complaining.
7. **The 5 enriched marts** have no reference to compare against.

Next things I would fix: 5 and 6, then emit local variables as CTEs instead of
inlining them.

[mo]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html

---

## Where the coding agent got it wrong, and how I caught it

Seven, and **four got through a green test suite** — the main thing I learned.

| What | How it was caught |
|---|---|
| Union *instances* classified unknown (`TEMPLATENAME` is on the definition, not the instance), blocking 2 models | not by the 48 passing tests — they only inspected definitions. The next phase's output said `BLOCKED` |
| Asserted `A + B * C` = 13 with A=10, B=5, C=3. It is 25 | the test *executes* the SQL in DuckDB; comparing strings would have agreed with itself |
| `{{ var('run_date') }}` emitted unquoted, so DuckDB read `2026-04-01` as arithmetic = 2021 | only `dbt build`. Valid SQL, and sqlglot parsed it happily |
| Fork detector flagged a Router whose branches reconverge | reading `plan` output and seeing 3 forks where the analysis said 2 |
| An intermediate model referenced a CTE that was never emitted | `dbt build` |
| Source columns left to the warehouse to type: DuckDB inferred `PERISHABLE` as boolean, cast `'Y'` to true, inverting 4,528 cells | the second implementation disagreed. `dbt build` and 201 tests were green |
| A running *total* silently emitted as `row_number()`: the shape check read the `IIF` condition but never its branches | a review pass at the end, not by any test |

The pattern: the agent was good at structure and bad at being sure. What caught
things was *output* — running it, printing what it decided, executing the SQL
rather than comparing strings, and running the same mapping a second way.

---

## The prompts I gave the agent

[`docs/agent-transcript/prompts.md`](docs/agent-transcript/prompts.md) — every
instruction, in order, verbatim.
[`conversation.md`](docs/agent-transcript/conversation.md) is the full exchange.
Email addresses and home paths are redacted.

---

## The process I followed

1. **Research before code.** Three agents in parallel mapped the XML, the jaffle
   shop reference and dbt conventions. Most design decisions fell out of the
   resulting numbers: 98 transformations, 1,066 connectors, 16 targets, 13
   expression functions, no lookups, no SQL overrides.
2. **A written plan in ten phases**, each ending in something runnable. One
   question — how to handle shared upstream chains — was deliberately deferred
   until there was data to decide it with.
3. **A PR per phase** for the first three, reviewed while the next was built,
   then the rest in one pass once per-phase review cost more than it gave.
4. **Two corrections that changed the result**: cutting comments that restated
   the code, and *"test the behaviour, not the code"* — which moved tests from
   asserting that properties return values to executing the generated SQL. Five
   of the seven mistakes above were caught by that change.
5. **A review pass at the end** (`/code-review`, `/simplify`), which found the
   `row_number()` bug and the shared models being built as views. The cleanups
   were verified by snapshotting all 18 generated models and asserting they came
   out byte-identical.

## Layout

```
FLOWLINE_DEMO_JAFFLESHOP.xml   the input, unchanged
src/pwc2dbt/                   the converter; reference/ is the cross-check
tests/                         267 tests
scripts/                       browse.py, compare_reference.py
seeds/jaffle/                  3,000-order subset of dbt's jaffle shop seeds
docs/                          REVIEW.md, concepts.md, example-output/, agent-transcript/
```

`dbt/` is generated by `convert` and not tracked; three sample models are in
[`docs/example-output/`](docs/example-output/) to read without running anything.
