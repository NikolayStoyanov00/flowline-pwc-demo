-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- target STORE_DAILY_KPIS (MART.STORE_DAILY_KPIS)

with

stg_locations1 as (
    select * from {{ ref('stg_locations') }}
),

un_order_bands as (
    -- UN_ORDER_BANDS feeds several models, so it is built once
    select * from {{ ref('int_un_order_bands') }}
),

sq_stg_locations1 as (
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from stg_locations1
),

srt_before_agg as (
    -- sort order dropped
    select
        location_id,
        order_id,
        customer_id,
        ordered_at_day,
        order_total,
        order_subtotal,
        order_tax_paid,
        order_cogs,
        order_gross_profit,
        order_value_band
    from un_order_bands
),

srt_for_distinct_customer as (
    -- sort order dropped
    select
        location_id,
        ordered_at_day,
        customer_id,
        order_id,
        order_total,
        order_subtotal,
        order_tax_paid,
        order_cogs,
        order_gross_profit,
        order_value_band
    from srt_before_agg
),

exp_mark_first_customer as (
    select
        location_id,
        ordered_at_day,
        customer_id,
        order_id,
        order_total,
        order_subtotal,
        order_tax_paid,
        order_cogs,
        order_gross_profit,
        order_value_band,
        (case when (location_id) = location_id and (ordered_at_day) = ordered_at_day and (customer_id) = customer_id then 0 else 1 end) as is_first_customer
    from srt_for_distinct_customer
),

agg_by_location_day as (
    select
        location_id,
        ordered_at_day,
        count(order_id) as orders_count,
        sum(is_first_customer) as unique_customers,
        sum(order_total) as revenue_total,
        sum(order_subtotal) as subtotal_total,
        sum(order_tax_paid) as tax_total,
        sum(order_cogs) as cogs_total,
        sum(order_gross_profit) as gross_profit_total,
        sum(case when order_value_band = 'HIGH' then 1 else 0 end) as high_value_orders
    from exp_mark_first_customer
    group by 1, 2
),

exp_store_kpis as (
    select
        location_id,
        ordered_at_day,
        orders_count,
        unique_customers,
        revenue_total,
        subtotal_total,
        tax_total,
        cogs_total,
        gross_profit_total,
        high_value_orders,
        case when subtotal_total = 0 then 0 else gross_profit_total / subtotal_total end as gross_margin_pct,
        case when orders_count = 0 then 0 else revenue_total / orders_count end as avg_order_value,
        case when orders_count = 0 then 0 else high_value_orders / orders_count end as high_value_orders_pct
    from agg_by_location_day
),

jnr_add_location_info as (
    -- PowerCenter Master Outer Join: master=exp_store_kpis, detail=sq_stg_locations1
    select
        exp_store_kpis.location_id as location_id,
        sq_stg_locations1.location_name as location_name,
        sq_stg_locations1.region as region,
        exp_store_kpis.ordered_at_day as ordered_at_day,
        exp_store_kpis.orders_count as orders_count,
        exp_store_kpis.unique_customers as unique_customers,
        exp_store_kpis.revenue_total as revenue_total,
        exp_store_kpis.subtotal_total as subtotal_total,
        exp_store_kpis.tax_total as tax_total,
        exp_store_kpis.cogs_total as cogs_total,
        exp_store_kpis.gross_profit_total as gross_profit_total,
        exp_store_kpis.gross_margin_pct as gross_margin_pct,
        exp_store_kpis.avg_order_value as avg_order_value,
        exp_store_kpis.high_value_orders as high_value_orders,
        exp_store_kpis.high_value_orders_pct as high_value_orders_pct
    from sq_stg_locations1
    left join exp_store_kpis on exp_store_kpis.location_id = sq_stg_locations1.location_id
),

fil_locs as (
    select
        location_id,
        location_name,
        region,
        ordered_at_day,
        orders_count,
        unique_customers,
        revenue_total,
        subtotal_total,
        tax_total,
        cogs_total,
        gross_profit_total,
        gross_margin_pct,
        avg_order_value,
        high_value_orders,
        high_value_orders_pct
    from jnr_add_location_info
    where location_id is not null
),

final as (
    select
        location_id,
        location_name,
        region,
        ordered_at_day,
        orders_count,
        unique_customers,
        revenue_total,
        subtotal_total,
        tax_total,
        cogs_total,
        gross_profit_total,
        gross_margin_pct,
        avg_order_value,
        high_value_orders,
        high_value_orders_pct
    from fil_locs
)

select * from final
