-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target CUSTOMERS (MART.CUSTOMERS)

with

stg_customers as (
    select * from {{ ref('stg_customers') }}
),

stg_orders2 as (
    select * from {{ ref('stg_orders') }}
),

sq_stg_customers as (
    select
        customer_id,
        customer_name
    from stg_customers
),

sq_stg_orders2 as (
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
    from stg_orders2
),

agg_orders_by_customer as (
    select
        customer_id,
        count(order_id) as lifetime_orders,
        min(ordered_at_day) as first_order_date,
        max(ordered_at_day) as last_order_date,
        sum(order_total) as lifetime_spend,
        sum(subtotal) as lifetime_spend_pretax,
        sum(tax_paid) as lifetime_tax_paid
    from sq_stg_orders2
    group by 1
),

jnr_customers_orders as (
    -- PowerCenter Master Outer Join: master=sq_stg_customers, detail=agg_orders_by_customer
    select
        sq_stg_customers.customer_id as customer_id,
        sq_stg_customers.customer_name as customer_name,
        agg_orders_by_customer.lifetime_orders as lifetime_orders,
        agg_orders_by_customer.first_order_date as first_order_date,
        agg_orders_by_customer.last_order_date as last_order_date,
        agg_orders_by_customer.lifetime_spend as lifetime_spend,
        agg_orders_by_customer.lifetime_spend_pretax as lifetime_spend_pretax,
        agg_orders_by_customer.lifetime_tax_paid as lifetime_tax_paid
    from agg_orders_by_customer
    left join sq_stg_customers on sq_stg_customers.customer_id = agg_orders_by_customer.customer_id
),

exp_customer_metrics as (
    select
        customer_id,
        customer_name,
        case when lifetime_orders is null then 0 else lifetime_orders end as lifetime_orders_cl,
        first_order_date,
        last_order_date,
        case when lifetime_spend is null then 0 else lifetime_spend end as lifetime_spend_cl,
        case when lifetime_spend_pretax is null then 0 else lifetime_spend_pretax end as lifetime_spend_pretax_cl,
        case when lifetime_tax_paid is null then 0 else lifetime_tax_paid end as lifetime_tax_paid_cl,
        case when case when lifetime_orders is null then 0 else lifetime_orders end > 1 then 'returning' else 'new' end as customer_type
    from jnr_customers_orders
),

final as (
    select
        customer_id,
        customer_name,
        lifetime_orders_cl as lifetime_orders,
        first_order_date,
        last_order_date,
        lifetime_spend_cl as lifetime_spend,
        lifetime_spend_pretax_cl as lifetime_spend_pretax,
        lifetime_tax_paid_cl as lifetime_tax_paid,
        customer_type
    from exp_customer_metrics
)

select * from final
