-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- target ORDER_ITEM_ENRICHED (MART.ORDER_ITEM_ENRICHED)

with

srt_order_id as (
    -- SRT_ORDER_ID feeds several models, so it is built once
    select * from {{ ref('int_srt_order_id') }}
),

final as (
    select
        order_item_id,
        order_id,
        product_id,
        customer_id,
        customer_name,
        location_id,
        location_name,
        region,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        item_revenue,
        item_cogs,
        item_gross_profit,
        item_gross_margin_pct,
        item_tax_estimate,
        item_total_estimate,
        has_perishable_supply
    from srt_order_id
)

select * from final
