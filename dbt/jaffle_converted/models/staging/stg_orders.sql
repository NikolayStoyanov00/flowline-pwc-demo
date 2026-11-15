-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_ORDERS (STG.STG_ORDERS)

with

raw_orders as (
    select
        cast(id as varchar) as id,
        cast(customer as varchar) as customer,
        cast(ordered_at as timestamp) as ordered_at,
        cast(store_id as varchar) as store_id,
        cast(subtotal as double) as subtotal,
        cast(tax_paid as double) as tax_paid,
        cast(order_total as double) as order_total
    from {{ source('raw', 'RAW_ORDERS') }}
),

sq_raw_orders as (
    select
        id,
        customer,
        ordered_at,
        store_id,
        subtotal,
        tax_paid,
        order_total
    from raw_orders
),

exp_orders as (
    select
        id as order_id,
        customer as customer_id,
        store_id as location_id,
        strptime(strftime(ordered_at, '%d-%m-%y'), '%d-%m-%y') as ordered_at_day,
        subtotal / 100 as subtotal,
        tax_paid / 100 as tax_paid,
        order_total / 100 as order_total,
        subtotal as subtotal_cents,
        tax_paid as tax_paid_cents,
        order_total as order_total_cents
    from sq_raw_orders
),

srt_orders as (
    -- sort order dropped
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal,
        tax_paid,
        order_total,
        subtotal_cents,
        tax_paid_cents,
        order_total_cents
    from exp_orders
),

final as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal as subtotal_cents,
        tax_paid as tax_paid_cents,
        order_total as order_total_cents,
        subtotal_cents as subtotal,
        tax_paid_cents as tax_paid,
        order_total_cents as order_total
    from srt_orders
)

select * from final
