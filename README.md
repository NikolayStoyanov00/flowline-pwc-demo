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

## How to run it

Needs Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev,dbt]"
```

Convert the export and build the result:

```bash
python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted
dbt build
```

`dbt build` seeds the sample data, builds all 18 models and runs 32 tests. It
needs no warehouse: DuckDB writes to a local file.

Run the test suite with `pytest` — **201 tests**, about 35 seconds.

### Looking at what it does

Four commands, each of which prints its reasoning rather than just doing the work:

| Command | What it shows |
|---|---|
| `pwc2dbt inspect <xml>` | what the parser found: mappings, steps, counts |
| `pwc2dbt plan <xml>` | the models it will produce and what feeds each one (`--verbose` for every step) |
| `pwc2dbt expressions <xml>` | all 205 Informatica expressions next to their SQL |
| `pwc2dbt convert <xml> --out <dir>` | writes the dbt project |

`plan` and `expressions` exist because the interesting part of this problem is
the decision-making, and those two make it inspectable without reading any
Python.

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

### 2. `TRUNC` on a number

Informatica uses one `TRUNC` for dates and for numbers, and the export does not
record which is meant. Every use here is a date, so the date reading is taken
and `TRUNC(x)` becomes `date_trunc('day', x)`. On an export that truncates a
*number*, this produces wrong numbers silently. A fix needs the port's datatype
threaded into the translator.

### 3. Local variables are inlined

A variable used by several output ports is substituted into each of them, so
its SQL appears more than once. Correct, but it makes wide expressions
repetitive, and a deep chain would grow quickly. Emitting one CTE per variable
level would avoid it.

### 4. Interpreted rather than translated: the running counter

`v_ORDER_SEQUENCE` reads its own previous value, which no expression can do. It
is a hand-rolled `row_number()`, and it is translated as one, with the
partition key taken from its condition and the ordering from the nearest
upstream Sorter. That is an interpretation of intent, not a mechanical
translation — the only one in the converter, and it is noted in the generated
SQL wherever it happens.

The matching is deliberately narrow: only the exact
`IIF(v_PREV_KEY = KEY, v_COUNTER + 1, 1)` shape. Anything else raises.

### 5. Precision and scale are discarded

Ports carry precision and scale; the generated SQL does not cast to them. DuckDB
infers types. Against a real Oracle warehouse, a `number(12,2)` target fed by a
computed `double` could round differently.

### 6. The five enriched marts cannot be checked against anything

`CUSTOMER_360`, `ORDER_FINANCIALS`, `ORDER_ITEM_ENRICHED`,
`PRODUCT_DAILY_PERFORMANCE` and `STORE_DAILY_KPIS` have no jaffle shop
equivalent. They build and their row counts are internally consistent, but
there is no reference output to compare against.

### 7. One mapping variable, one default

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

### What this says about working with the agent

The agent was good at structure and bad at being sure. Every one of these got
through code that looked right and, in two cases, through a green test suite.
What caught them was output: running the thing, printing what it decided, and
executing the SQL rather than comparing it to a string.

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
tests/                         201 tests
seeds/jaffle/                  sample data, a subset of dbt's jaffle shop seeds
dbt/jaffle_converted/          generated output, committed as evidence
docs/concepts.md               PowerCenter vocabulary in SQL terms
docs/agent-transcript/         prompts and full conversation
```

The sample export is from
[infinitelambda/flowline-pwc-demo](https://github.com/infinitelambda/flowline-pwc-demo).
Seed data is a subset of
[dbt-labs/jaffle-shop](https://github.com/dbt-labs/jaffle-shop), trimmed to
3,000 orders and kept referentially consistent.

[master-outer]: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html
