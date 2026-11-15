# PowerCenter concepts, in SQL terms

A reference for the vocabulary in `src/pwc2dbt/model.py`. Everything here is
explained as "what this is if you already know SQL".

## The one-line version

PowerCenter stores a pipeline as a **graph**: boxes wired together column by
column. dbt wants a **SQL file**: one `SELECT` per output table. Converting
means walking the graph and turning each box into a CTE.

## The vocabulary

| PowerCenter | In SQL terms | Becomes |
|---|---|---|
| **Source** | a table you read `FROM` | `{{ source() }}` or `{{ ref() }}` |
| **Target** | a table you write `INTO` | **one dbt model** |
| **Mapping** | a whole ETL job | several dbt models |
| **Transformation** | one step: a `SELECT`, `WHERE`, `JOIN`, `GROUP BY` | one CTE |
| **Port** | one item in a `SELECT` list | one column |
| **Instance** | a table alias — one *use* of a thing | a node in the graph |
| **Connector** | `x.a AS b` — one column flowing onward | an edge in the graph |

### The two that are genuinely confusing

**Transformation vs Instance.** A transformation is the *definition* of a step.
An instance is one *placement* of it in a pipeline. The relationship is exactly
that of a table to a table alias:

```sql
from orders o1
join orders o2 on ...
```

One table, two aliases, two independent nodes. PowerCenter does the same thing
and names them `STG_ORDERS` and `STG_ORDERS1`. **Connectors reference the
alias, not the definition** — so code must resolve
`instance.name → instance.definition_name → definition`.

**Source vs Target.** They are not two different kinds of thing; both are just
tables. The difference is direction. And one job's target is the next job's
source: `STG_ORDERS` is a *target* in the staging job and a *source* in the
marts job. That is precisely what `{{ ref() }}` expresses in dbt.

## A real pipeline, end to end

The simplest one in the file, producing `STG_PRODUCTS`:

```
RAW_PRODUCTS  ──►  SQ_RAW_PRODUCTS  ──►  EXP_PRODUCTS  ──►  STG_PRODUCTS
  (Source)        (Source Qualifier)     (Expression)        (Target)
   a table           FROM clause        the SELECT list      a dbt model
```

`EXP_PRODUCTS` has 5 input ports (`SKU`, `NAME`, `TYPE`, `PRICE`,
`DESCRIPTION`) and 8 output ports. Three of its real output ports:

| Port | Expression |
|---|---|
| `PRODUCT_ID` | `SKU` |
| `PRODUCT_PRICE` | `PRICE/100` |
| `IS_FOOD_ITEM` | `IIF( UPPER(LTRIM(RTRIM(TYPE))) = 'JAFFLE', 1, 0 )` |

Which is just a `SELECT` list written vertically:

```sql
select
    sku                                              as product_id,
    price / 100                                      as product_price,
    case when upper(trim(type)) = 'JAFFLE'
         then 1 else 0 end                           as is_food_item
from raw_products
```

That is the entire conversion idea. The rest is handling the other box types
and the awkward cases below.

## Why connectors cannot be skipped

It is tempting to match columns between steps by name. That breaks, and this
file proves it.

`EXP_PRODUCTS` computes two prices:

```
port PRODUCT_PRICE        = PRICE/100     (dollars)
port PRODUCT_PRICE_CENTS  = PRICE         (cents)
```

The connectors into the target then cross over:

```xml
<CONNECTOR FROMFIELD="PRODUCT_PRICE"       TOFIELD="PRODUCT_PRICE_CENTS" .../>
<CONNECTOR FROMFIELD="PRODUCT_PRICE_CENTS" TOFIELD="PRODUCT_PRICE"       .../>
```

So the target column `PRODUCT_PRICE` receives **cents**, and
`PRODUCT_PRICE_CENTS` receives **dollars**. Name-matching would quietly swap
them. A converter has to follow the wires, and should reproduce this faithfully
rather than "fixing" it — the job is to convert the mapping, not to correct it.

## The box types in this export

| Transformation | SQL | Count |
|---|---|---|
| Source Qualifier | `FROM` | 28 |
| Expression | `SELECT` list | 25 |
| Joiner | `JOIN ... ON` | 16 |
| Sorter | `ORDER BY` | 15 |
| Aggregator | `GROUP BY` + `SUM`/`COUNT`/... | 9 |
| Router | one CTE per branch, each with a `WHERE` | 2 |
| Union | `UNION ALL` | 2 |
| Filter | `WHERE` | 1 |

Two notes on these:

- A **Union** is exported as `TYPE="Custom Transformation"` and is identifiable
  only by `TEMPLATENAME="Union Transformation"`.
- A **Sorter** is usually droppable: row order does not survive into a table.
  The exception is a sorter feeding a row-numbering step, where the order is
  what the numbering means.

## Ports: the three flavours

| `PORTTYPE` | Meaning |
|---|---|
| `INPUT` | a column coming in |
| `OUTPUT` | a column going out, usually with an expression |
| `INPUT/OUTPUT` | passes straight through untouched |
| `LOCAL VARIABLE` | scratch value reused by other ports in the same step |
| `.../MASTER` | marks the **master** side of a join |

`LOCAL VARIABLE` ports exist because PowerCenter cannot reference a value
computed in the same step, so intermediate results get a named slot. In SQL
they become either a nested CTE or an inlined sub-expression. They are declared
*after* the ports that use them and can reference each other, so they need a
topological sort rather than being read in order.

The `/MASTER` suffix is the only record of which side of a join is which, and
it decides the join direction:

| `Join Type` | SQL |
|---|---|
| `Normal Join` | `inner join` |
| `Master Outer Join` | keeps all **detail** rows → `detail left join master` |
| `Detail Outer Join` | keeps all **master** rows → `master left join detail` |

`Master Outer Join` reads backwards from what the name suggests, and 10 of the
16 joins in this file use it. It is the most likely source of wrong output.

## What is deliberately ignored

`SESSION`, `WORKFLOW` and `CONFIG` describe *scheduling* and *database
connections*, not data transformation. The run order they encode
(staging → core → enriched) is reproduced automatically by the `{{ ref() }}`
dependencies between generated models, so dbt works it out for itself.
