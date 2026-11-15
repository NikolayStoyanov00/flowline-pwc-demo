-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- target ORDER_FINANCIALS (MART.ORDER_FINANCIALS)

with

un_order_bands as (
    -- UN_ORDER_BANDS feeds several models, so it is built once
    select * from {{ ref('int_un_order_bands') }}
),

final as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        order_subtotal,
        order_tax_paid,
        order_total,
        item_lines,
        item_revenue,
        order_cogs,
        order_gross_profit,
        order_gross_margin_pct,
        effective_tax_rate,
        food_item_lines,
        drink_item_lines,
        food_share_of_lines,
        drink_share_of_lines,
        customer_order_number,
        order_value_band
    from un_order_bands
)

select * from final
