-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- target PRODUCT_DAILY_PERFORMANCE (MART.PRODUCT_DAILY_PERFORMANCE)

with

srt_order_id as (
    -- SRT_ORDER_ID feeds several models, so it is built once
    select * from {{ ref('int_srt_order_id') }}
),

srt_before_agg_pdp as (
    -- sort order dropped
    select
        product_id,
        product_name,
        product_type,
        ordered_at_day,
        order_item_id,
        item_revenue,
        item_cogs,
        item_gross_profit,
        has_perishable_supply
    from srt_order_id
),

agg_by_product_day as (
    select
        product_id,
        product_name,
        product_type,
        ordered_at_day,
        count(order_item_id) as units_sold,
        sum(item_revenue) as revenue,
        sum(item_cogs) as cogs,
        sum(item_gross_profit) as gross_profit,
        max(has_perishable_supply) as perishable_sales_flag
    from srt_before_agg_pdp
    group by 1, 2, 3, 4
),

exp_product_metrics as (
    select
        product_id,
        product_name,
        product_type,
        ordered_at_day,
        units_sold,
        revenue,
        cogs,
        gross_profit,
        perishable_sales_flag,
        case when revenue = 0 then 0 else gross_profit / revenue end as margin_pct
    from agg_by_product_day
),

final as (
    select
        product_id,
        product_name,
        product_type,
        ordered_at_day,
        units_sold,
        revenue,
        cogs,
        gross_profit,
        perishable_sales_flag as margin_pct,
        margin_pct as perishable_sales_flag
    from exp_product_metrics
)

select * from final
