# pwc2dbt — Informatica PowerCenter to dbt converter

Reads a PowerCenter repository export and writes a runnable dbt project.
On the sample export: **3 mappings → 18 dbt models**, which build and test clean
on DuckDB and match **dbt's real jaffle shop project** everywhere the two
overlap, except where the source mapping is itself wrong — three such
quirks, each reproduced on purpose rather than quietly corrected.

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
| `pytest` | 270 tests, about a minute |

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

**Every difference traces to one of the quirks below. Nothing else
differs.** The 5 enriched marts have no jaffle shop equivalent, so those rely on
`pwc2dbt compare`: a second implementation of the documented Informatica
semantics, run row by row. That shows the two implementations are *consistent*,
not that either matches a real PowerCenter run — both came from the same reading
of the docs.

---

## What I assumed, and what I chose not to handle

### The one that matters: reproduce the mapping, don't fix it

Three things in this export look like mistakes in the *mapping*, not the
conversion. The converter emits each as it is and flags it — a tool that
silently repairs its input is the more dangerous one. These are the **quirks**
referred to throughout.

**1. Cents and dollars are crossed, in 8 columns.** `EXP_PRODUCTS` and
`EXP_ORDERS` each compute a dollars *and* a cents column; the connectors wire
each into the other's target, so `subtotal` holds `700` where the reference has
`7.00`. Hence columns are followed through connectors, never matched by name
— name-matching would swap them back and look *more* correct.

**2. `customers` loses 82% of its rows.** A `Master Outer Join` with customers
as master keeps every *detail* row and drops unmatched masters: 935 customers
seeded, 168 in the mart. The `IIF(ISNULL(LIFETIME_ORDERS), 0, ...)` guard
downstream can then never fire, which suggests `Detail Outer Join` was meant.

**3. The per-customer order counter never restarts.**
`EXP_CUSTOMER_ORDER_NUM` sets `v_PREV_CUSTOMER_ID = CUSTOMER_ID` in the port
*above* the one that tests it, so the test compares a row against itself and is
always true. Informatica evaluates variable ports in display order
([docs][po]), so `customer_order_number` counts 1…2967 straight through and
`unique_customers` is 0 everywhere. `--variable-order deferred` emits the
intended reading, changing 3 tables.

### The smaller ones

| The export doesn't say | What I assumed |
|---|---|
| Table or view? | Targets are truncate-and-insert, so marts are tables, staging views |
| Which schema a target lands in | Only its `TRUNCATE TABLE STG.X` pre-SQL names one |
| Whether a Sorter means anything | Droppable, unless a downstream window function needs that order |
| Which columns deserve dbt tests | All 164 target fields are `NOT A KEY`, so keys trace *forward* from the `RAW_*` sources |
| What type a column is | Its declared PowerCenter type, cast explicitly rather than inferred |

Throughout: **unknown means stop.** An unrecognised function or transformation
raises rather than guessing.

### Not handled

Absent from this export, so unimplemented rather than guessed at — the
converter raises on each: Lookup, Sequence Generator, Update Strategy/SCD,
Normalizer, Transaction Control, mapplets, shortcuts, SQL overrides,
multi-source Source Qualifiers, incremental loads, descending and distinct
sorters. The translator covers the 13 expression functions this export uses.

---

## Where the converter is most likely to produce wrong output

The first three would change data without anything looking broken; the rest are
narrower. All are decisions taken with incomplete information, not bugs I know
about.

**1. Join direction.** Ten of the sixteen joins are `Master Outer Join`, which
keeps all *detail* rows — the opposite of how the name reads. Which side is
master appears only as a `/MASTER` suffix on a port type, so one misread
attribute silently drops or duplicates rows and the output still builds.
Checked against [Informatica's docs][mo].

**2. Variable evaluation order.** Quirk 3 rests on the exporter writing ports
in the order Informatica evaluates them. The docs tie the two together and this
export follows the prescribed layout, but I cannot prove the exporter always
preserves it — if it does not, three tables are wrong.
`pwc2dbt compare --variable-order deferred` measures the gap.

**3. Two patterns are interpreted, not translated.** A self-referencing counter
becomes `row_number()`, a variable remembering the previous row becomes
`lag()`. Both matchers are narrow and annotate the SQL they emit, but a mapping
that *looks* like either and means something else would be mistranslated. A
running total is refused rather than guessed — because this went wrong once
(next section).

The narrower six, in the order I would fix them:

- **`sql.sort_order_for` matches by name** — the one place the "follow
  connectors, never match names" rule is broken. A connector that renamed a
  sort key would pick the wrong column, and it takes the nearest Sorter on
  *any* branch, which behind a Joiner can be the other input's. Nothing here
  renames a sort key.
- **Precision and scale** below the source cast are discarded.
- **`emit._carried_through` fails open** — a column it cannot trace drops a
  dbt test silently instead of complaining.
- **`DATE_DIFF` counts boundaries, not fractions.** Informatica returns 1.25
  days; the emitted `date_diff` counts crossings, so 23:59 → 00:01 is a whole
  day. Both uses here `TRUNC` both sides, where the two agree.
- **`TRUNC` on a number.** Informatica uses one name for date and number
  truncation; every use here is a date, so the date reading is taken.
- **The 5 enriched marts** have nothing independent to be checked against.

After those: emit local variables as CTEs instead of inlining them.

[mo]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html
[po]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/working-with-transformations/local-variables/guidelines-for-configuring-variable-ports/port-order.html

---

## Where the coding agent got it wrong, and how I caught it

Seven mistakes. **Four of them passed a green test suite**, which is the part
worth knowing: writing more tests would not have found them. They fall into
three kinds.

**1. The tests agreed with the code instead of checking it.** Early tests
asserted that a function returned something, or compared generated SQL against
a string written minutes earlier in the same session. A test like that passes
whatever the code does. One asserted that `A + B * C` is 13, with A=10, B=5,
C=3. It is 25, and the suite stayed green because nothing ever ran the SQL.
The rule changed to *test the behaviour, not the code*: generate the SQL,
execute it in DuckDB, assert the numbers that come back. That is what made the
suite able to fail.

**2. It guessed where the export was ambiguous.** Faced with something it could
not resolve, the agent picked a plausible reading and moved on without saying
so. It emitted a running *total* as `row_number()` because its pattern check
read the `IIF` condition but never looked at the branches. It left column types
for the warehouse to infer, and DuckDB decided `PERISHABLE` was boolean and
cast `'Y'` to true, inverting 4,528 cells while `dbt build` and 201 tests
stayed green. Two things fixed this: the converter now raises on anything it
does not recognise, and a second implementation re-runs the same mapping row by
row so the two can disagree.

**3. Valid SQL that means the wrong thing.** `{{ var('run_date') }}` was
emitted unquoted. That is syntactically fine, so sqlglot parsed it without
complaint, but DuckDB read `2026-04-01` as arithmetic and got 2021. Only a real
`dbt build` surfaced it.

What caught these was output: running the converter, printing what it had
decided, executing the SQL instead of comparing strings, and running the same
mapping a second way to see whether the two answers matched.

<details>
<summary><b>All seven, and what caught each</b></summary>

| What | How it was caught |
|---|---|
| Union *instances* classified unknown (`TEMPLATENAME` is on the definition, not the instance), blocking 2 models | not by the 48 passing tests — they only inspected definitions. The next phase's output said `BLOCKED` |
| Asserted `A + B * C` = 13 with A=10, B=5, C=3. It is 25 | the test *executes* the SQL in DuckDB; comparing strings would have agreed with itself |
| `{{ var('run_date') }}` emitted unquoted, so DuckDB read `2026-04-01` as arithmetic = 2021 | only `dbt build`. Valid SQL, and sqlglot parsed it happily |
| Fork detector flagged a Router whose branches reconverge | reading `plan` output and seeing 3 forks where the analysis said 2 |
| An intermediate model referenced a CTE that was never emitted | `dbt build` |
| Source columns left to the warehouse to type: DuckDB inferred `PERISHABLE` as boolean, cast `'Y'` to true, inverting 4,528 cells | the second implementation disagreed. `dbt build` and 201 tests were green |
| A running *total* silently emitted as `row_number()`: the shape check read the `IIF` condition but never its branches | a review pass at the end, not by any test |

</details>

---

## The prompts I gave the agent

[`docs/agent-transcript/prompts.md`](docs/agent-transcript/prompts.md) — all 41
instructions, in order, verbatim.
[`conversation.md`](docs/agent-transcript/conversation.md) is the full exchange,
agent replies included.

Both are generated from the Claude Code session log by
`python scripts/export_transcript.py`, so they cannot drift behind the work.
Email addresses and home directory paths are redacted, and the export fails if
either survives.

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
tests/                         270 tests
scripts/                       browse.py, compare_reference.py, export_transcript.py
seeds/jaffle/                  3,000-order subset of dbt's jaffle shop seeds
docs/                          REVIEW.md, concepts.md, example-output/, agent-transcript/
```

`dbt/` is generated by `convert` and not tracked; three sample models are in
[`docs/example-output/`](docs/example-output/) to read without running anything.
