-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- shared step UN_ORDER_BANDS, built once and referenced by several models
-- note: EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE was a variable holding its own previous value; translated as row_number()

with

srt_order_id as (
    -- SRT_ORDER_ID feeds several models, so it is built once
    select * from {{ ref('int_srt_order_id') }}
),

stg_orders1 as (
    select * from {{ ref('stg_orders') }}
),

agg_items_by_order as (
    select
        order_id,
        count(order_item_id) as item_lines,
        sum(item_revenue) as item_revenue,
        sum(item_cogs) as order_cogs,
        sum(item_gross_profit) as order_gross_profit,
        sum(is_food_item) as food_item_lines,
        sum(is_drink_item) as drink_item_lines
    from srt_order_id
    group by 1
),

sq_stg_orders1 as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal_cents,
        tax_paid_cents,
        order_total_cents,
        subtotal,
        tax_paid,
        order_total
    from stg_orders1
),

jnr_orders_items_agg as (
    -- PowerCenter Master Outer Join: master=sq_stg_orders1, detail=agg_items_by_order
    select
        sq_stg_orders1.order_id as order_id,
        sq_stg_orders1.customer_id as customer_id,
        sq_stg_orders1.location_id as location_id,
        sq_stg_orders1.ordered_at_day as ordered_at_day,
        sq_stg_orders1.subtotal as subtotal,
        sq_stg_orders1.tax_paid as tax_paid,
        sq_stg_orders1.order_total as order_total,
        agg_items_by_order.item_lines as item_lines,
        agg_items_by_order.item_revenue as item_revenue,
        agg_items_by_order.order_cogs as order_cogs,
        agg_items_by_order.order_gross_profit as order_gross_profit,
        agg_items_by_order.food_item_lines as food_item_lines,
        agg_items_by_order.drink_item_lines as drink_item_lines
    from agg_items_by_order
    left join sq_stg_orders1 on sq_stg_orders1.order_id = agg_items_by_order.order_id
),

exp_order_financials as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal as order_subtotal,
        tax_paid as order_tax_paid,
        order_total,
        (case when item_lines is null then 0 else item_lines end) as item_lines,
        (case when item_revenue is null then 0 else item_revenue end) as item_revenue,
        (case when order_cogs is null then 0 else order_cogs end) as order_cogs,
        (case when order_gross_profit is null then 0 else order_gross_profit end) as order_gross_profit,
        case when (case when item_revenue is null then 0 else item_revenue end) = 0 then 0 else (case when order_gross_profit is null then 0 else order_gross_profit end) / (case when item_revenue is null then 0 else item_revenue end) end as order_gross_margin_pct,
        case when subtotal = 0 then 0 else tax_paid / subtotal end as effective_tax_rate,
        (case when food_item_lines is null then 0 else food_item_lines end) as food_item_lines,
        (case when drink_item_lines is null then 0 else drink_item_lines end) as drink_item_lines,
        case when (case when item_lines is null then 0 else item_lines end) = 0 then 0 else (case when food_item_lines is null then 0 else food_item_lines end) / (case when item_lines is null then 0 else item_lines end) end as food_share_of_lines,
        case when (case when item_lines is null then 0 else item_lines end) = 0 then 0 else (case when drink_item_lines is null then 0 else drink_item_lines end) / (case when item_lines is null then 0 else item_lines end) end as drink_share_of_lines
    from jnr_orders_items_agg
),

srt_for_customer_seq as (
    -- sort order dropped
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        drink_share_of_lines
    from exp_order_financials
),

exp_customer_order_num as (
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        row_number() over (partition by customer_id order by ordered_at_day, order_id) as customer_order_number
    from srt_for_customer_seq
),

srt_before_router as (
    -- sort order dropped
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        customer_order_number
    from exp_customer_order_num
),

rtr_order_value_band__high_value as (
    -- router group HIGH_VALUE
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        customer_order_number
    from srt_before_router
    where order_total >= 50
),

rtr_order_value_band__standard as (
    -- router group STANDARD
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        customer_order_number
    from srt_before_router
    where order_total < 50
),

exp_high_band as (
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        'HIGH' as order_value_band,
        customer_order_number
    from rtr_order_value_band__high_value
),

exp_std_band as (
    select
        customer_id,
        ordered_at_day,
        order_id,
        location_id,
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
        'STANDARD' as order_value_band,
        customer_order_number
    from rtr_order_value_band__standard
),

un_order_bands as (
    -- union leg EXP_HIGH_BAND
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
    from exp_high_band

    union all

    -- union leg EXP_STD_BAND
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
    from exp_std_band
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
