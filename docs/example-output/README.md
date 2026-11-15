# Example output

**These files are generated.** They are not source, and nothing reads them.

The full dbt project is produced by the converter and is not tracked, because
it is entirely derived from the XML:

```bash
python -m pwc2dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml --out dbt/jaffle_converted
```

Three of the eighteen models are copied here so the output can be read without
running anything. They were chosen to cover the interesting cases:

| File | Why this one |
|---|---|
| `stg_products.sql` | The simplest shape: source → qualifier → expression → target. Also where the [crossed price columns](../../README.md#the-price-columns-are-crossed) show up: look at the `final` CTE. |
| `customers.sql` | A join and an aggregate. The `jnr_customers_orders` CTE is the `Master Outer Join` translation — note the detail table leads. |
| `stg_locations.sql` | A Router fanning into three branches that a Union merges back: one CTE per branch, then `union all`. |

If these drift from what the converter currently emits, the generated project
is the truth and these are stale. Regenerate with the command above.
