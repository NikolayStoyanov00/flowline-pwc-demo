# How to review this

A path through the project for someone who has to understand and defend it.
About two hours end to end. Each stage stands alone, so stop whenever you have
what you need.

## First, the thing that makes this confusing

**There is no reference output to diff against.** The lineage graph is not
"wrong" or "right" compared to some known answer — nothing exists to compare it
to. PowerCenter cannot be run (licensed software, needs a domain and a
repository database), and the real jaffle shop project has different models.

So verification is never *"is this the correct answer?"*. It is always:

> **Does the output match what the mapping says?**

That question you can answer completely, by hand, from the XML. Everything
below is a way of asking it.

Three separate things can be checked, and it is worth keeping them apart:

| Question | How it is answered |
|---|---|
| Does the SQL say what the mapping says? | read the XML next to the generated SQL (stage 1) |
| Does the data follow from the input? | count the seed CSVs yourself (stage 2) |
| Does the converter actually read the mapping? | change the XML and watch the output change (stage 3) |

Stage 3 is the most convincing and the fastest. If you are short of time, do
stage 1 then stage 3.

---

## Setup, and the bit that was missing

```bash
pip install -e ".[dev,dbt]"
python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
cd dbt/jaffle_converted && dbt build && cd ../..
```

**Close any `browse.py` or `dbt docs serve` you have running first** — they hold
the DuckDB file open and `dbt build` will fail with a lock error.

Three ways to look at the result. You found the first; the second is the one you
were missing.

**The lineage graph** — structure only, no data:

```bash
cd dbt/jaffle_converted && dbt docs generate && dbt docs serve   # localhost:8080
```

**The data** — this is how you query it:

```bash
python scripts/browse.py                                          # localhost:4213
```

A SQL console over all 24 tables: the 6 seeded source tables and the 18
generated models, side by side. Everything in stages 1 and 2 is a query you
paste in here.

**The diff** against a row-by-row reference run:

```bash
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml
```

### What the lineage graph can and cannot tell you

It cannot tell you the SQL is right. It *can* be checked against the mapping,
and that is worth five minutes:

- Click `customers`. It should show exactly two parents: `stg_customers` and
  `stg_orders`. Now run `python -m pwc2dbt plan FLOWLINE_DEMO_JAFFLESHOP.xml`
  and find `marts/customers.sql` — it lists the same two. Both came from the
  mapping's source instances, so if the graph and the plan disagree, something
  is wrong.
- Count the nodes: 6 seeds, 6 staging, 2 intermediate, 10 marts.
- `int_srt_order_id` and `int_un_order_bands` should each have several children.
  Those are the shared steps; the whole fan-out design is visible there.

---

## Stage 1 — Trace one model by hand (20 min)

Do this once and the other 17 models stop being mysterious. Use
`stg_products`, which is the simplest pipeline in the export.

**1. What does the mapping say?** Open `FLOWLINE_DEMO_JAFFLESHOP.xml`, search
for `EXP_PRODUCTS`. You will find a `<TRANSFORMATION>` with ports. Three of
them:

```
PRODUCT_ID           = SKU
PRODUCT_PRICE        = PRICE/100
IS_FOOD_ITEM         = IIF( UPPER(LTRIM(RTRIM(TYPE))) = 'JAFFLE', 1, 0 )
```

**2. What did the converter produce?** Open
`dbt/jaffle_converted/models/staging/stg_products.sql`. In the `exp_products`
CTE:

```sql
sku as product_id,
price / 100 as product_price,
case when upper(ltrim(rtrim(type))) = 'JAFFLE' then 1 else 0 end as is_food_item,
```

Line for line, the same three rules. **That is the whole conversion idea**: a
port becomes an item in a `SELECT` list.

**3. What is in the data?** In the browser UI:

```sql
select product_id, product_type, product_price, is_food_item
from stg_products order by product_id;
```

Check one row against the CSV yourself: `grep '^JAF-001' seeds/jaffle/raw_products.csv`
gives `JAF-001,...,1100`. So `price / 100` should be `11`.

**It is not.** `product_price` shows `1100` and `product_price_cents` shows
`11`. That is the deliberate finding — scroll to the `final` CTE:

```sql
product_price as product_price_cents,
product_price_cents as product_price,
```

The connectors in the XML cross the two columns over. Search the XML for
`TOFIELD ="PRODUCT_PRICE_CENTS"` and you will see it. This is the single best
illustration of why a converter must follow the connectors rather than match
column names — and why the converter reproduces the mapping rather than
"fixing" it.

**Now repeat the same three steps on a hard one.** `marts/customers.sql` has a
join and an aggregate. Look at `jnr_customers_orders` and read the comment the
converter left in it.

---

## Stage 2 — Check the numbers without trusting the converter (20 min)

Every number below can be derived from the seed CSVs with `wc` and `cut`, so
the converter has no say in it. Run the shell line, then the SQL, and compare.

```bash
cd seeds/jaffle

# 935
echo $(($(wc -l < raw_customers.csv) - 1))
# 168
tail -n +2 raw_orders.csv | cut -d, -f2 | sort -u | wc -l
# 3000
echo $(($(wc -l < raw_orders.csv) - 1))
# 2967
tail -n +2 raw_items.csv | cut -d, -f2 | sort -u | wc -l
```

```sql
select 'stg_customers' t, count(*) n from stg_customers
union all select 'customers',   count(*) from customers
union all select 'stg_orders',  count(*) from stg_orders
union all select 'orders',      count(*) from orders;
```

| table | expected | why |
|---|---|---|
| `stg_customers` | **935** | staging is 1:1 with its source |
| `customers` | **168** | exactly the customers who have an order |
| `stg_orders` | **3000** | 1:1 again |
| `orders` | **2967** | exactly the orders that have an item |

**935 → 168 is not a bug in the converter.** It is what `Master Outer Join`
means: keep every *detail* row, discard unmatched *master* rows. Customers are
the master side, so a customer with no orders is dropped. Stage 3 proves this.

The giveaway that the mapping probably did not intend it: `EXP_CUSTOMER_METRICS`
computes `IIF(ISNULL(LIFETIME_ORDERS), 0, LIFETIME_ORDERS)` — a guard for
customers with no orders, which under this join can never fire. The real jaffle
shop `customers` model keeps all 935.

Two more worth checking:

```sql
-- the counter resets per customer: max should equal the count, every time
select count(*) as rows_where_it_does_not
from (select customer_id, max(customer_order_number) h, count(*) c
      from orders group by 1)
where h <> c;        -- expect 0

-- the router/union round trip loses nothing: 6 stores in, 6 locations out
select count(*) from stg_locations;   -- expect 6
```

---

## Stage 3 — Break it on purpose (20 min)

The best way to confirm the converter actually reads the mapping, rather than
having the answer baked in. Each experiment is: copy the XML, change one
attribute, convert, look.

### Does it really read the join type?

```bash
mkdir -p /tmp/rv && cp FLOWLINE_DEMO_JAFFLESHOP.xml /tmp/rv/flipped.xml
```

Open `/tmp/rv/flipped.xml`, find `JNR_CUSTOMERS_ORDERS`, and change its
`Join Type` from `Master Outer Join` to `Detail Outer Join`. Then:

```bash
python -m pwc2dbt convert /tmp/rv/flipped.xml --out /tmp/rv/flipped
cd /tmp/rv/flipped && dbt build && cd -
```

```sql
select count(*) from customers;
```

**168 becomes 935.** And in the generated SQL the `from`/`join` order flips:
`Master Outer` leads with the detail table, `Detail Outer` leads with the
master. That one experiment demonstrates three things at once: the join type is
read from the XML, the direction logic is real, and the 168 was a faithful
consequence of the mapping.

### Does it refuse what it cannot translate?

Change `EXP_PRODUCTS.IS_FOOD_ITEM` to use a function that does not exist:

```
EXPRESSION ="DECODE(TYPE, 'JAFFLE', 1, 0)"
```

Convert. It fails, with exit code 1 and the model named:

```
error: stg_products: unsupported function DECODE(); supported: ABS, AVG,
COUNT, DATE_DIFF, IIF, ISNULL, LENGTH, LOWER, LTRIM, MAX, MIN, ROUND, RTRIM,
SUM, TO_CHAR, TO_DATE, TRUNC, UPPER
```

Not something plausible. That is the design rule: guessing produces SQL that
runs and returns the wrong answer, which is worse than failing.

### Does the test suite actually catch a regression?

Break the converter on purpose. In `src/pwc2dbt/sql.py`, find `_join_shape` and
change the `master outer join` line from `return detail, master, "left join"`
to `return master, detail, "left join"` — i.e. get the direction backwards.
Then:

```bash
pytest tests/test_sql.py tests/test_end_to_end.py
```

Five failures across **three independent layers**:

```
FAILED test_sql.py::TestJoinDirection::test_master_outer_join_leads_with_the_detail_table
FAILED test_end_to_end.py::TestMasterOuterJoinDropsRows::test_customers_without_orders_are_dropped
FAILED test_end_to_end.py::TestMasterOuterJoinDropsRows::test_orders_without_items_are_dropped
FAILED test_end_to_end.py::TestAgainstTheReferenceImplementation::test_every_table_matches_cell_for_cell
FAILED test_end_to_end.py::TestAgainstTheReferenceImplementation::test_the_literal_reading_would_disagree
```

The generated SQL, the row counts in the warehouse, and the reference
implementation each catch it separately. Revert with
`git checkout src/pwc2dbt/sql.py`.

Worth trying the contrast too: in `expressions.py`, make `emit_parameter`
return a bare `{{ var(...) }}` with no quotes or cast. The unit tests stay
green and `dbt build` fails — that is the real bug that happened, and the
reason the end-to-end layer exists at all.

---

## Stage 4 — What `compare` proves, and what it does not (15 min)

```bash
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml
```

```
matched 16 of 16 tables
```

This runs the mapping a second time — row by row in Python, in
`src/pwc2dbt/simulate.py` — and diffs the result against what dbt built, cell
by cell.

**What it proves:** two independent implementations agree. One builds set-based
SQL, the other loops over rows keeping variable state. They share a parser and
nothing else. Different code paths fail differently, so this catches
implementation mistakes — it caught two that `dbt build` and 239 passing tests
both missed.

**What it does not prove:** that either matches a real PowerCenter run. Both
were written from the same reading of Informatica's documentation. A shared
misunderstanding of the spec appears identically in both and shows as a match.
Be precise about this if you are asked — it is the most likely place to be
caught overclaiming.

The unresolved question made concrete:

```bash
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml --variable-order document
```

Three tables now differ. PowerCenter evaluates variable ports in Ports-tab
order, and whether the XML preserves that order cannot be settled without
PowerCenter. Read literally, `CUSTOMER_ORDER_NUMBER` never resets and climbs
past 2,900, and `UNIQUE_CUSTOMERS` is 0 on every row. Those are dead columns,
which is the argument for the reading the converter uses — stated, with the
cost of being wrong measured rather than guessed.

---

## Stage 5 — Read the code (45 min)

In dependency order. Each module only knows about the ones above it.

| Read | Lines | What to check |
|---|---|---|
| `docs/concepts.md` | — | **Start here** if "mapping / instance / port" is still slippery |
| `model.py` | 339 | Just shapes. Note `Instance` = a table alias |
| `parse.py` | 317 | The only module touching XML |
| `graph.py` | 444 | `split_points` and `_walk_back` — the fork decision |
| `expressions.py` | 775 | The only real algorithm: tokenise → parse → emit |
| `sql.py` | 833 | **The core.** `_join_shape` first, then `inputs_of`/`register` |
| `emit.py` | 360 | The dbt project; `trace_column` attaches tests by lineage |
| `evaluate.py`, `simulate.py`, `compare.py` | 1,400 | The second implementation. Skim unless reviewing it |

### The five places to look hardest

1. **`sql.py:_join_shape`** — 15 lines, highest risk in the project. `Master
   Outer Join` keeps all *detail* rows, which reads backwards from the name.
   Ten of sixteen joins use it. Verified against
   [Informatica's docs](https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/joiner-transformation/defining-the-join-type/master-outer-join.html).
2. **`sql.py` — the `(step, port) → (CTE, column)` lookup** (`inputs_of`,
   `register`). The core idea; the crossed price columns are why it exists.
3. **`sql.py:counter_partition_keys`** — the narrowest, most interpretive code.
   Ask why it refuses a running total.
4. **`graph.py:split_points`** — why a reconverging Router is not a fork.
5. **`emit.py:_carried_through`** — a known weakness. It **fails open**: a
   column it cannot trace silently drops a dbt test instead of complaining.
   Listed as unfixed in the README.

### Questions worth being able to answer

Pointers, not answers — work them out and ask me about any that do not resolve.

- Why is it 18 models and not 16, or 3? (`graph.py`, `plan_models`)
- Why can't columns be matched by name between steps? (stage 1)
- Why does `customers` have 168 rows? (stage 2 and 3)
- What makes `v_ORDER_SEQUENCE` untranslatable as an expression, and why is
  `row_number()` an interpretation rather than a translation?
- Why are Sorters dropped, and which one isn't?
- Why does `PERISHABLE` need `cast(... as varchar)`? (README, agent mistake #6)
- Where is the converter most likely to be wrong? (README has seven, ranked)
- What does the reference implementation prove? (stage 4 — be precise)

---

## If something looks wrong

Three different ways to localise it:

```bash
python -m pwc2dbt plan FLOWLINE_DEMO_JAFFLESHOP.xml --verbose   # every step of every model
python -m pwc2dbt expressions FLOWLINE_DEMO_JAFFLESHOP.xml      # all 205 expressions -> SQL
python -m pwc2dbt compare FLOWLINE_DEMO_JAFFLESHOP.xml          # cell-level diff
```

Every generated model also carries its own provenance in the header:

```sql
-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target CUSTOMERS (MART.CUSTOMERS)
-- note: EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE was a variable holding its own
--       previous value; translated as row_number()
```

Any `-- note:` line marks a place where the converter interpreted rather than
translated. There should be few, and each should be defensible.
