# How to review this

## The thing that makes it confusing

**There is no "correct output" to diff against.** PowerCenter can't be run, so
the question is never *"is this right?"* — it is:

> **Does the output match what the mapping says?**

That you can answer completely, from the XML. Three ways, in order of
confidence-per-minute:

| | |
|---|---|
| **Change the XML, watch the output change** | proves the converter reads the mapping |
| **Diff against dbt's jaffle shop** | real working code, not mine |
| **Trace one model by hand** | XML port → generated SQL → rows |

## Setup

```bash
python3 -m venv .venv             # "python" on Windows
source .venv/bin/activate         # .venv\Scripts\activate on Windows
pip install -e ".[dev,dbt]"

python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted && dbt build && cd ../..
python scripts/browse.py          # the data, localhost:4213
```

`browse.py` is the bit that's easy to miss — a SQL console over all 24 tables
(6 seeds, 18 models). `dbt docs serve` only shows structure.

---

## 1. Change the XML (10 min, do this first)

```bash
cp FLOWLINE_DEMO_JAFFLESHOP.xml /tmp/flipped.xml
```

Find `JNR_CUSTOMERS_ORDERS` in the copy, change `Join Type` from
`Master Outer Join` to `Detail Outer Join`, then:

```bash
python -m pwc2dbt convert /tmp/flipped.xml --out /tmp/flipped
cd /tmp/flipped && dbt build && cd -
```

`select count(*) from customers` goes from **168 to 935**, and the `from`/`join`
order reverses in the generated SQL. One experiment, three conclusions: the join
type is read from the XML, the direction logic is real, and the 168 was faithful
rather than a bug.

Then try breaking it in a way it should refuse — change
`EXP_PRODUCTS.IS_FOOD_ITEM` to `DECODE(TYPE, 'JAFFLE', 1, 0)`:

```
error: stg_products: unsupported function DECODE(); supported: ABS, AVG, ...
```

Exit code 1, not plausible-looking SQL. Guessing would produce output that runs
and is wrong.

## 2. Diff against dbt's real jaffle shop (5 min)

```bash
python scripts/compare_reference.py
```

Clones dbt's project, runs it on the same seeds, diffs every overlapping model.
3 identical, 4 differing only on the crossed cents/dollars columns, 2 on row
count from the join. **Every difference is one of the quirks in the
README.** This is the strongest check available, because the reference is not
mine.

## 3. Trace one model by hand (15 min)

Do this once and the other 17 stop being mysterious.

1. Search the XML for `EXP_PRODUCTS`. Its ports include
   `PRODUCT_ID = SKU`, `PRODUCT_PRICE = PRICE/100`,
   `IS_FOOD_ITEM = IIF( UPPER(LTRIM(RTRIM(TYPE))) = 'JAFFLE', 1, 0 )`.
2. Open `dbt/jaffle_converted/models/staging/stg_products.sql`. The
   `exp_products` CTE has the same three rules, line for line. **That is the
   whole idea**: a port becomes an item in a `select` list.
3. In `browse.py`: `select product_id, product_price, product_price_cents from
   stg_products`. `JAF-001` shows 1100 and 11 — swapped, because the connectors
   cross them. Search the XML for `TOFIELD ="PRODUCT_PRICE_CENTS"` to see it.

Then repeat on `marts/customers.sql`, which has a join and an aggregate.

## 4. Check the numbers without trusting the converter (5 min)

These come from the CSVs, so the converter has no say:

```bash
cd seeds/jaffle
echo $(($(wc -l < raw_customers.csv) - 1))                 # 935 -> stg_customers
tail -n +2 raw_orders.csv | cut -d, -f2 | sort -u | wc -l   # 168 -> customers
tail -n +2 raw_items.csv  | cut -d, -f2 | sort -u | wc -l   # 2967 -> orders
```

## 5. Confirm the tests are real (5 min)

In `src/pwc2dbt/sql.py`, find `_join_shape` and reverse the `master outer join`
line to `return master, detail, "left join"`. Then
`pytest tests/test_sql.py tests/test_end_to_end.py`:

```
FAILED ...test_master_outer_join_leads_with_the_detail_table
FAILED ...test_customers_without_orders_are_dropped
FAILED ...test_orders_without_items_are_dropped
FAILED ...test_every_table_matches_cell_for_cell
FAILED ...test_correcting_the_mapping_would_change_three_tables
```

Five failures across three independent layers — the SQL text, the row counts,
and the second implementation. `git checkout src/pwc2dbt/sql.py` to revert.

---

## Reading the code

~5,000 lines, strictly layered: each module only knows the ones above it, and
`src/pwc2dbt/__init__.py` lists the stages. `reference/` is a separate concern —
the converter never imports it — so the converter itself is ~3,600 lines.
Read `docs/concepts.md` first if "mapping / instance / port" is still unclear.

| Read | Lines | Look for |
|---|---|---|
| `model.py` | 339 | just shapes. `Instance` is a table alias |
| `parse.py` | 317 | the only module touching XML |
| `graph.py` | 444 | `split_points`, `_walk_back` — the fork decision |
| `expressions.py` | 775 | the only real algorithm: tokenise → parse → emit |
| **`sql.py`** | 833 | **the core.** `_join_shape`, then `inputs_of`/`register` |
| `emit.py` | 360 | the dbt project; `trace_column` attaches tests by lineage |
| `reference/` | 1,400 | the second implementation; skim |

### Walking it through in a debugger

Worth doing once, but not on the real export — 142 instances and 1,066
connectors is too much to follow. Use the tiny fixture instead: one source, one
target, four steps, ten connectors, one model, and it still exercises every
part of the pipeline.

```bash
python -m pwc2dbt convert tests/fixtures/minimal.xml --out /tmp/min
cat /tmp/min/models/staging/stg_items.sql
```

Then set these breakpoints and debug it.

| Breakpoint | What to look at |
|---|---|
| `parse.py:230` `_parse_mapping` | `transformations`, `instances`, `connectors` — the XML as objects |
| `graph.py:306` `_plan` | `step_names`, and the order `topological_order` puts them in |
| `sql.py:700` `_ModelBuilder.build` | step into each `emit_*` in turn |
| `sql.py:98` `register` | watch `self.columns` fill up — this is the core |

Both IDEs have committed configurations, so open the **repo itself** as the
project — not a parent folder, or the interpreter will be the system Python and
`pwc2dbt` will not import.

**VS Code:** Run and Debug (`Ctrl+Shift+D`) → **"convert: minimal.xml (start
here)"** → F5. If the interpreter is wrong, `Ctrl+Shift+P` → *Python: Select
Interpreter* → the one in `.venv`.

**PyCharm:** the same four configurations are in the run dropdown. They use the
project interpreter, which PyCharm detects from `.venv` on open; set it under
Settings → Project → Python Interpreter if it has not.

To build one by hand: Run → Edit Configurations → `+` → Python, switch *Script
path* to **Module name** (the dropdown is easy to miss), module `pwc2dbt`,
parameters `convert tests/fixtures/minimal.xml --out .debug-out`, working
directory the repo root.

That third one is the core of the project. `self.columns` is the
`(step, port) → (CTE, column)` lookup; watching it grow as each CTE is emitted
is the clearest way to see why renames work without matching names.

The fixture also shows a documented limitation in miniature: `v_QTY` is read by
two output ports, so its SQL appears twice in the generated model. That is the
cost of inlining local variables.

For the real export, the CLI prints the same intermediate state faster than
stepping:

```bash
python -m pwc2dbt plan FLOWLINE_DEMO_JAFFLESHOP.xml --verbose   # every step of every model
python -m pwc2dbt expressions FLOWLINE_DEMO_JAFFLESHOP.xml      # all 205 expressions -> SQL
```

### Five places to look hardest

1. **`sql.py:_join_shape`** — 15 lines, highest risk in the project.
2. **`sql.py:inputs_of` / `register`** — the `(step, port) → (CTE, column)`
   lookup. The core idea; the crossed columns are why it exists.
3. **`sql.py:counter_partition_keys`** — the most interpretive code. Ask why it
   refuses a running total.
4. **`graph.py:split_points`** — why a reconverging Router is not a fork.
5. **`emit.py:_carried_through`** — a known weakness: it fails open, silently
   dropping a dbt test. Listed as unfixed in the README.

### Questions worth being able to answer

- Why 18 models, not 16 or 3?
- Why can't columns be matched by name between steps?
- Why does `customers` have 168 rows?
- What makes `v_ORDER_SEQUENCE` untranslatable as an expression?
- Why does `customer_order_number` reach 2967 rather than 37?
- Why are Sorters dropped, and which one isn't?
- What does `pwc2dbt compare` prove, and what does it not?

---

## If something looks wrong

Every generated model carries its own provenance:

```sql
-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target CUSTOMERS (MART.CUSTOMERS)
-- note: EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE ... translated as row_number()
```

A `-- note:` line marks where the converter **interpreted** rather than
translated. There should be few, and each should be defensible.
