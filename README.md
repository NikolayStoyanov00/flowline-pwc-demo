# pwc2dbt — Informatica PowerCenter to dbt converter

Reads an Informatica PowerCenter repository export and writes a runnable dbt
project.

Run against the sample export in this repo, it turns **3 mappings into 18 dbt
models** that build and test cleanly on DuckDB:

```
Done. PASS=56 WARN=0 ERROR=0 SKIP=0 TOTAL=56
```

Nothing is hard-coded to the jaffle shop answer. Every model, column, join,
expression and test is derived from the XML.

---

## Quickstart

Needs Python 3.12+ and about two minutes. No warehouse, no accounts, no Docker —
DuckDB writes to a local file.

### 1. Install

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev,dbt]"
```

### 2. Convert the export and build the result

```bash
python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted
dbt build
```

Seeds the sample data, builds all 18 models, runs 32 tests. Expect
`PASS=56 ERROR=0`.

### 3. See the lineage, in a browser

```bash
dbt docs generate
dbt docs serve                         # http://localhost:8080
```

A clickable DAG of all 18 models with every `ref()` edge, plus the column
descriptions carried over from the PowerCenter port definitions and the tests
attached to each one. This is the closest thing to the PowerCenter Designer
canvas for the converted version: click `products` and walk back through
`stg_products` to `raw_products`.

### 4. Browse the data, in a browser

```bash
cd ../..
python scripts/browse.py                # http://localhost:4213
```

Spreadsheet-style browsing and a SQL console over all 24 tables: the 6 seeded
source tables and the 18 generated models, side by side. Ctrl-C to stop.

Useful first queries, once it is open:

```sql
-- the crossed price columns: PRODUCT_PRICE holds cents, and vice versa
select product_id, product_price, product_price_cents from stg_products;

-- 935 customers seeded, 168 in the mart: the master outer join drops the rest
select (select count(*) from stg_customers) as staged,
       (select count(*) from customers)     as in_mart;

-- the hand-rolled counter, now a window function, restarting per customer
select customer_id, customer_order_number, ordered_at_day
from orders order by customer_id, customer_order_number limit 20;
```

Any SQL client works too — the file is
`dbt/jaffle_converted/jaffle_converted.duckdb`.

### 5. Diff the output against a row-by-row reference run

```bash
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml
```

```
matched 16 of 16 tables
```

PowerCenter itself cannot be run, so this compares against a second
implementation of the documented semantics rather than against PowerCenter.
[What that does and does not prove](#seeing-the-before-and-the-after) is worth
reading before relying on it.

### 6. Run the tests

```bash
pytest                                 # 239 tests, about 50 seconds
```

---

## Looking at what it decided

Five commands. Three of them print the converter's reasoning rather than
just doing the work, which is the quickest way to judge it:

| Command | What it shows |
|---|---|
| `pwc2dbt inspect <xml>` | what the parser found: mappings, steps, counts |
| `pwc2dbt plan <xml>` | the models it will produce and what feeds each one (`--verbose` for every step) |
| `pwc2dbt expressions <xml>` | all 205 Informatica expressions next to their SQL |
| `pwc2dbt convert <xml> --out <dir>` | writes the dbt project |
| `pwc2dbt compare <xml>` | runs the mapping row by row and diffs the result against dbt's |

`plan` and `expressions` exist because the interesting part of this problem is
the decision-making, and those two make it inspectable without reading any
Python.

## Seeing the before and the after

There is no way to run the PowerCenter side: it is licensed software needing a
domain, a repository database and running services, and there is no free
edition. So the "before" is a **reference implementation** of the documented
Informatica semantics — Python that walks the same graph row by row, keeping
variable state between rows, and writes the same 16 tables. `compare` diffs it
against what dbt built:

```
matched 16 of 16 tables
  OK   CUSTOMERS                    reference=   168 dbt=   168  key=customer_id
  OK   ORDERS                       reference=  2967 dbt=  2967  key=order_id
  OK   STORE_DAILY_KPIS             reference=    58 dbt=    58  key=ordered_at_day
  ...
```

**What that does and does not prove.** The reference implementation was written
from the same reading of Informatica's documentation as the converter, so
agreement shows the two implementations are **consistent**, not that either
matches a real PowerCenter run. Nothing here has been tested against
PowerCenter. It still earns its place: it shares no code with the converter and
fails differently, the way an interpreter and a compiler do, and it found two
real bugs (below).

Three things in this export cannot be checked this way at all, and are listed
under [where the converter is most likely wrong](#where-the-converter-is-most-likely-to-produce-wrong-output).

---

## How it works

PowerCenter stores a pipeline as a **graph**: boxes wired together column by
column. dbt wants **SQL files**: one `select` per output table. So:

```
XML ─► parse ─► build graph ─► walk back from each target ─► one CTE per step ─► .sql
```

| PowerCenter | SQL |
|---|---|
| Source Qualifier | `from {{ source() }}` / `{{ ref() }}` |
| Expression | the `select` list |
| Filter | `where` |
| Joiner | `join ... on` |
| Aggregator | `group by` + aggregates |
| Sorter | dropped (see below) |
| Router | one CTE per branch, each with a `where` |
| Union | `union all` |

One module per step of that pipeline:

| File | Role |
|---|---|
| `model.py` | PowerCenter concepts as dataclasses. No XML, no SQL. |
| `parse.py` | the only module that touches XML |
| `validate.py` | input validation, kept off the parsing path |
| `graph.py` | the DAG, the reverse walk per target, fork detection |
| `expressions.py` | the Informatica expression language → SQL |
| `naming.py` | PowerCenter names → dbt names |
| `sql.py` | steps → CTEs → one model |
| `emit.py` | the dbt project: models, YAML, tests, seeds |
| `evaluate.py` | the same expressions, *computed* rather than translated |
| `simulate.py` | a reference implementation: the graph, run row by row |
| `compare.py` | diffs the reference output against dbt's |

`docs/concepts.md` explains the PowerCenter vocabulary in SQL terms. Worth
reading first if "mapping", "instance" and "port" are unfamiliar.

### Three decisions that shape everything

**One model per target, not per mapping.** A mapping holds several independent
pipelines. There are 3 mappings and 16 targets, so there are 16 models.
Splitting on *connected components* instead would be wrong: the enriched
mapping is one connected graph writing five different targets.

**Columns are followed, never matched by name.** Every step can rename, and
PowerCenter records renames in the connectors between steps. The converter
keeps a `(step, port) → (CTE, column)` lookup and asks it. This matters — see
the crossed price columns below.

**Shared chains are built once.** Five enriched models shared 33 upstream
steps, all sitting above just two points where the pipeline genuinely forks.
Those two are promoted to `int_` models. Inlining instead duplicated 21 CTEs
across five files and cost 1,600 extra lines of SQL, with identical results.

---

## What I assumed

- **Faithful conversion, not improvement.** Where the mapping does something
  that looks wrong, the converter reproduces it and says so in a comment. The
  job is to convert the mapping; deciding whether the mapping is right is the
  reader's call, and silently "fixing" it would hide a real finding.
- **Truncate-and-insert means a table.** Every target's `Pre SQL` is
  `TRUNCATE TABLE ...`, so marts are `materialized='table'`. Staging stays a
  view, per dbt convention.
- **Sorters are droppable.** Row order does not survive into a table. All 140
  sort keys in this export are ascending with `Distinct=NO`, so the sort is a
  no-op — *except* where a Sorter establishes the order a row-numbering step
  depends on, which is read straight off the Sorter instead.
- **Target schema comes from the `TRUNCATE` statement.** Target definitions
  carry no schema; `TRUNCATE TABLE STG.X` is the only place it appears, and it
  decides whether a model is `staging/` or `marts/`.
- **dbt tests come from the sources.** All 164 target fields are `NOT A KEY`
  and nullable, so nothing can be derived from the target side. Primary and
  foreign keys exist only on the `RAW_*` sources, and are traced forward
  through the graph to the model column they become (`RAW_CUSTOMERS.ID` →
  `stg_customers.customer_id`). Models that aggregate get no key tests, because
  the grain changes.
- **Unknown means stop.** An unrecognised function or transformation raises
  instead of being passed through. SQL that runs and returns the wrong answer
  is worse than a failure.
- **The export is trusted input.** It is parsed with `xml.etree`, which does
  not resolve external entities, but no other hardening was done.

## What I chose not to handle

None of these appear in this export, so they are unimplemented rather than
untested guesses:

Lookup, Sequence Generator, Update Strategy / SCD, Normalizer, Transaction
Control, Stored Procedure, mapplets, shortcuts, reusable folder-level
transformations, Source Qualifier SQL overrides, multi-source Source
Qualifiers, non-ascending or `Distinct=YES` sorters, incremental loads, and
`Full Outer Join` (implemented but unexercised).

Also out of scope: translating session- or workflow-level scheduling. The run
order the workflow encodes (staging → core → enriched) is reproduced
automatically by the `ref()` dependencies, so dbt works it out itself.

The expression translator covers the 13 functions this export uses plus a few
obvious neighbours. A different export will hit `ExpressionError` on the first
function it does not know, which is the intended behaviour.

---

## Where the converter is most likely to produce wrong output

In rough order of risk.

### 1. Join direction

Ten of the sixteen joins are `Master Outer Join`, which keeps every **detail**
row and discards unmatched **master** rows — the opposite of how the name
reads. In SQL that is `detail left join master`.

Master and detail are recorded *only* as a `/MASTER` suffix on a port type, and
join conditions appear in both operand orders in this export
(`PRODUCT_ID = PRODUCT_ID1` and `PRODUCT_ID1 = PRODUCT_ID`), so each side is
resolved through its own port rather than by position.

If this is wrong anywhere, the output still builds and still looks plausible —
it just quietly has the wrong rows. It is verified by
[Informatica's own documentation][master-outer], asserted in `test_sql.py`, and
checked against real data in `test_end_to_end.py`.

### 2. The variable evaluation order, which is now measured

The "remember the previous row" idiom only works if the comparison is evaluated
*before* the assignment that updates the remembered value. In this export the
assignment is declared **first**, and I cannot verify from the XML alone
whether document order is evaluation order.

So the converter commits to one reading — a remembered variable holds the
**previous** row, translated as `lag(...) over (order by <sorter keys>)` — and
the reference implementation can run **both**, which turns the open question
into a number:

| reading | effect |
|---|---|
| **deferred** (what the converter does) | matches dbt on all 16 tables |
| **document** (the literal reading) | 3 tables differ: `ORDERS`, `ORDER_FINANCIALS`, `STORE_DAILY_KPIS` |

Read literally, `CUSTOMER_ORDER_NUMBER` never restarts and climbs past 2,900,
and `UNIQUE_CUSTOMERS` is **0 for every row**, because the first-customer test
compares a row against itself. Those are dead columns, which is the evidence
for choosing the other reading. Run it either way:

```bash
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml --variable-order document
```

If a real PowerCenter run disagrees, this is the thing it will disagree about.

### 3. `TRUNC` on a number

Informatica uses one `TRUNC` for dates and for numbers, and the export does not
record which is meant. Every use here is a date, so the date reading is taken
and `TRUNC(x)` becomes `date_trunc('day', x)`. On an export that truncates a
*number*, this produces wrong numbers silently. A fix needs the port's datatype
threaded into the translator.

### 4. Local variables are inlined

A variable used by several output ports is substituted into each of them, so
its SQL appears more than once. Correct, but it makes wide expressions
repetitive, and a deep chain would grow quickly. Emitting one CTE per variable
level would avoid it.

### 5. Interpreted rather than translated: the row-by-row variables

Two constructs are interpreted rather than translated mechanically, because
PowerCenter's variable ports keep state between rows and no SQL expression can:

| PowerCenter | becomes | why |
|---|---|---|
| `v_SEQ = IIF(v_PREV_KEY = KEY, v_SEQ + 1, 1)` | `row_number() over (partition by KEY order by …)` | a counter reading its own previous value |
| `v_PREV_KEY = KEY`, read by another variable | `lag(KEY) over (order by …)` | a slot holding the previous row |

Both take their ordering from the nearest upstream Sorter, which is the one
case where a Sorter is not droppable. Both are noted in the generated SQL
wherever they happen, so a reader can see where interpretation occurred.

The counter match is deliberately narrow: only the exact
`IIF(v_PREV_KEY = KEY, v_COUNTER + 1, 1)` shape, and anything else raises. The
`lag()` rule is narrow too — a variable that is only one port name *and* is
read by another variable. A bare variable nobody else reads is just an alias
and is left alone.

These are the places most likely to diverge from a real PowerCenter run. See
risk 2 for how much is at stake.

### 6. Precision and scale

Source columns *are* cast to their declared types now, which the reference
implementation forced (see below). But port-level precision and scale further
down the chain are still discarded: a `number(12,2)` target fed by a computed
double is not rounded to two places. The session has
`Enable high precision = NO`, so double is the faithful arithmetic, but the
final rounding to the target's scale is missing.

### 7. The five enriched marts cannot be checked against anything

`CUSTOMER_360`, `ORDER_FINANCIALS`, `ORDER_ITEM_ENRICHED`,
`PRODUCT_DAILY_PERFORMANCE` and `STORE_DAILY_KPIS` have no jaffle shop
equivalent. They build and their row counts are internally consistent, but
there is no reference output to compare against.

### 8. One mapping variable, one default

`$$RUN_DATE` becomes `{{ var('run_date') }}` with the default taken from the
export. It has to be emitted quoted and cast, because an unquoted
`{{ var('run_date') }}` renders as `2026-04-01` and SQL reads that as
arithmetic. That bug is described below.

---

## Curious things in the source mapping

Three findings about the demo mapping itself. All three are reproduced
faithfully rather than corrected, and all three are worth raising with whoever
wrote it.

### The price columns are crossed

`EXP_PRODUCTS` computes two prices:

```
port PRODUCT_PRICE        = PRICE/100      -> dollars
port PRODUCT_PRICE_CENTS  = PRICE          -> cents
```

and the connectors into the target then swap them:

```xml
<CONNECTOR FROMFIELD="PRODUCT_PRICE"       TOFIELD="PRODUCT_PRICE_CENTS" .../>
<CONNECTOR FROMFIELD="PRODUCT_PRICE_CENTS" TOFIELD="PRODUCT_PRICE"       .../>
```

So `STG_PRODUCTS.PRODUCT_PRICE` holds **cents** and `PRODUCT_PRICE_CENTS` holds
**dollars**. Confirmed in the built data: `product_price = 600` where
`product_price_cents = 6.0`.

This is the proof that a converter cannot match columns by name. A name-matching
converter would quietly swap them back — and look *more* correct while doing it.

### `customers` drops 82% of its customers

The join that attaches order history to customers is a `Master Outer Join` with
customers as the master, so customers with no orders are discarded. With the
sample data: **935 customers seeded, 168 in the mart** — exactly the number that
have placed an order.

The downstream expression is
`IIF(ISNULL(LIFETIME_ORDERS), 0, LIFETIME_ORDERS)`, a guard for customers with
no orders. It can never fire. That strongly suggests the mapping means
`Detail Outer Join`, which is what the real jaffle shop `customers` model does.
The same pattern drops 33 orders that have no items.

### The seed data does not match the flag convention the mapping expects

`IS_PERISHABLE` is `IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)`, which
expects an Oracle-style flag. dbt's jaffle shop seed data holds `True`/`False`,
so with this data the flag is 0 for every supply and
`HAS_PERISHABLE_SUPPLY` is 0 throughout. That is a mismatch between the sample
data and the mapping, not a conversion error — against the Oracle source the
mapping was written for, the column presumably holds `Y`/`N`.

### The previous-row variables are declared in the wrong order

The running-counter idiom only works if the comparison is evaluated **before**
the remember-the-previous-value assignment:

```
v_PREV_CUSTOMER_ID = CUSTOMER_ID                                    <- declared first
v_ORDER_SEQUENCE   = IIF(v_PREV_CUSTOMER_ID = CUSTOMER_ID, ...)     <- declared second
```

Here the assignment comes first. If XML document order is the evaluation order —
which PowerCenter's port ordering implies, though I cannot verify it without
PowerCenter itself — the comparison sees the current row, is always true, and
the counter never resets. `EXP_MARK_FIRST_CUSTOMER` has the same shape, which
would make `IS_FIRST_CUSTOMER` constant.

Flagged rather than asserted. Either way both constructs need window functions,
so the conversion is unchanged.

---

## Where the coding agent got something wrong

Five, in the order they happened. Three were caught by tests, two by looking at
output.

### 1. Union transformations were classified as unknown

A Union is exported as `TYPE="Custom Transformation"` and is identifiable only
by `TEMPLATENAME="Union Transformation"`. The agent handled that for
transformation *definitions* — but an `<INSTANCE>` records
`TRANSFORMATION_TYPE` and **not** `TEMPLATENAME`, so Union instances came out
`UNKNOWN` and blocked two models.

**How it was caught:** not by the 48 tests that existed, all of which passed.
They only ever inspected definitions. The bug appeared the moment the next phase
actually *used* the instances and `plan` printed
`BLOCKED: unsupported steps UN_REGIONS`.

**The lesson**, and the reason the test strategy changed afterwards: those tests
asserted that code worked, not that the tool produced the right answer.

### 2. `A + B * C` is 25, not 13

While writing tests for operator precedence, the agent asserted that
`A + B * C` with `A=10, B=5, C=3` equals 13. It is 25. The translator was right;
the test was wrong.

**How it was caught:** the test *executes* the generated SQL in DuckDB rather
than comparing strings. Comparing text would have agreed with itself and found
nothing. This is the strongest argument in the project for running the output.

### 3. Mapping variables rendered as arithmetic

`{{ var('run_date') }}` was emitted unquoted, so it rendered as `2026-04-01`
and DuckDB read it as `2026 - 4 - 1 = 2021`, an integer. `customer_360` failed
with `date_trunc(STRING_LITERAL, INTEGER)`.

**How it was caught:** `dbt build`. Nothing else would have — the SQL is
syntactically valid and sqlglot parsed it happily. A date variable is now
emitted as `cast('{{ var("run_date") }}' as timestamp)`, with the type taken
from the variable's declaration.

### 4. The fork detector flagged a branch that reconverges

The first version promoted any step with several consumers to its own model.
That flagged `RTR_ORDER_VALUE_BAND`, whose two branches are immediately merged
again by `UN_ORDER_BANDS` — both reach identical targets, so inlining
duplicates nothing. The rule is now "consumers that reach **different**
targets".

**How it was caught:** reading the `plan` output and noticing a third fork where
the analysis predicted two.

### 5. An intermediate model referenced a CTE it never emitted

When a fork was promoted to its own model, the walk collected the steps
*feeding* the fork but not the fork itself — so `int_srt_order_id` selected from
a `srt_order_id` CTE that did not exist.

**How it was caught:** `dbt build` — `Catalog Error: Table with name
srt_order_id does not exist`.

### 6. Source columns were left to the warehouse to type

`RAW_SUPPLIES.PERISHABLE` is a `varchar2` tested with
`IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)`. The seed CSV holds
`True`/`False`, DuckDB inferred the column as **boolean**, and then happily
cast `'Y'` to `true` — so the comparison silently meant something else
entirely, and `HAS_PERISHABLE_SUPPLY` came out 1 everywhere instead of 0.

**How it was caught:** the reference implementation, which compares strings as
the SOURCE definition says, disagreed on 4,528 cells. Both `dbt build` and the
whole test suite were green.

Generated models now cast every source column to the type the PowerCenter
SOURCE declares, which is the general fix.

### 7. The converter mixed two readings of variable order

It used the intended reading for the self-referencing counter (translating it
to `row_number()`) but the literal reading everywhere else — so
`IS_FIRST_CUSTOMER` compiled to `location_id = location_id`, always true, and
`UNIQUE_CUSTOMERS` was 0 on every row of `STORE_DAILY_KPIS`.

**How it was caught:** the same comparison. Once the counter agreed, this was
the only table left disagreeing. Remembered variables are now `lag()`, so one
reading is used throughout.

### What this says about working with the agent

The agent was good at structure and bad at being sure. Every one of these got
through code that looked right, and four of the seven got through a green test
suite. What caught them was output: running the thing, printing what it
decided, executing the SQL rather than comparing it to a string, and finally
running the same mapping a second way and diffing.

The last two are the most telling. `dbt build` passed, 201 tests passed, and
two columns were still wrong — one silently inverted by a type the warehouse
guessed, one constant at zero. Only a second independent implementation found
them.

---

## The process

1. **Research before code.** Three parallel agents mapped the XML structure, the
   jaffle shop reference project and dbt conventions. The output of that step is
   the fact table in the plan: 98 transformations, 1,066 connectors, 16 targets,
   13 expression functions, no lookups, no SQL overrides. Most design decisions
   fell out of those numbers.
2. **A written plan first, in ten phases**, each ending in something runnable.
   One open question (how to handle shared chains) was deliberately deferred
   until there was data to decide it with.
3. **A PR per phase for the first three**, reviewed while the next was built.
   Then the strategy changed: reading new code every phase was more context than
   was useful, so the remaining phases were built in one pass.
4. **Two corrections to how the agent worked**, both of which improved the
   result:
   - *"most of the comments give no value"* — the parser's comments were cut from
     ~70 lines to ~15, keeping only what the code cannot say.
   - *"ensure tests are testing the behavior and not just code"* — the decisive
     one. Tests moved from asserting that properties return values to asserting
     what the tool produces, then to executing the generated SQL. Findings 2, 3
     and 5 above were all caught by that change.
5. **Faithful over clever, throughout.** Every time the mapping looked wrong,
   the choice was to reproduce it and document it.
6. **A second implementation, at the end.** Asked how to see the before and the
   after, the honest answer was that PowerCenter cannot be run — so the
   semantics were implemented a second time, row by row, and the two outputs
   diffed. It found two bugs that a green `dbt build` and 201 passing tests had
   both missed, and it turned the one unresolved semantic question into a
   measurement.

The full record is in `docs/agent-transcript/`:
[`prompts.md`](docs/agent-transcript/prompts.md) is every instruction given, in
order; [`conversation.md`](docs/agent-transcript/conversation.md) is the whole
exchange. Email addresses and home paths are redacted.

---

## What I would do next

- Thread port precision and scale into the generated SQL so numeric types match
  the target definitions.
- Emit local variables as one CTE per level instead of inlining them.
- Name intermediate models after what they contain rather than after the
  PowerCenter step (`int_srt_order_id` is traceable but not descriptive).
- Compare against the real jaffle shop project model by model, rather than only
  checking the overlapping names and internal consistency.
- Give `ExpressionError` and `SqlError` a mapping/step/port breadcrumb in every
  case; some messages have it, not all.

## Repo layout

```
FLOWLINE_DEMO_JAFFLESHOP.xml   the input, unchanged
src/pwc2dbt/                   the converter
tests/                         239 tests
scripts/browse.py              opens the built warehouse in a browser
seeds/jaffle/                  sample data, a subset of dbt's jaffle shop seeds
docs/concepts.md               PowerCenter vocabulary in SQL terms
docs/example-output/           three generated models, to read without running
docs/agent-transcript/         prompts and full conversation
```

**`dbt/` is not tracked.** The whole dbt project is output of
`pwc2dbt convert`, so committing it would put 36 generated files in the repo
and make them go stale the moment the converter changed. Run step 2 of the
quickstart to produce it. Three representative models live in
[`docs/example-output/`](docs/example-output/) so the output can still be read
without running anything.

The sample export is from
[infinitelambda/flowline-pwc-demo](https://github.com/infinitelambda/flowline-pwc-demo).
Seed data is a subset of
[dbt-labs/jaffle-shop](https://github.com/dbt-labs/jaffle-shop), trimmed to
3,000 orders and kept referentially consistent.

[master-outer]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html
