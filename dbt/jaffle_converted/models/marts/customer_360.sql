-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- target CUSTOMER_360 (MART.CUSTOMER_360)

with

stg_customers1 as (
    select
        cast(customer_id as varchar) as customer_id,
        cast(customer_name as varchar) as customer_name
    from {{ ref('stg_customers') }}
),

un_order_bands as (
    -- UN_ORDER_BANDS feeds several models, so it is built once
    select * from {{ ref('int_un_order_bands') }}
),

aggtrans as (
    select
        customer_id,
        count(order_id) as count_lifetime_orders,
        min(ordered_at_day) as first_ordered_at,
        max(ordered_at_day) as last_ordered_at,
        sum(order_total) as lifetime_spend,
        sum(order_subtotal) as lifetime_spend_pretax,
        sum(order_tax_paid) as lifetime_tax_paid,
        sum(order_cogs) as lifetime_cogs,
        sum(order_gross_profit) as lifetime_gross_profit,
        sum(case when order_value_band = 'HIGH' then 1 else 0 end) as high_value_orders
    from un_order_bands
    group by 1
),

sq_stg_customers1 as (
    select
        customer_id,
        customer_name
    from stg_customers1
),

jnr_customers as (
    -- PowerCenter Master Outer Join: master=sq_stg_customers1, detail=aggtrans
    select
        aggtrans.customer_id as customer_id,
        aggtrans.count_lifetime_orders as count_lifetime_orders,
        aggtrans.first_ordered_at as first_ordered_at,
        aggtrans.last_ordered_at as last_ordered_at,
        aggtrans.lifetime_spend as lifetime_spend,
        aggtrans.lifetime_spend_pretax as lifetime_spend_pretax,
        aggtrans.lifetime_tax_paid as lifetime_tax_paid,
        aggtrans.lifetime_cogs as lifetime_cogs,
        aggtrans.lifetime_gross_profit as lifetime_gross_profit,
        aggtrans.high_value_orders as high_value_orders,
        sq_stg_customers1.customer_name as customer_name
    from aggtrans
    left join sq_stg_customers1 on sq_stg_customers1.customer_id = aggtrans.customer_id
),

exp_customer_metrics as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        (case when lifetime_spend is null then 0 else lifetime_spend end) as lifetime_spend,
        (case when lifetime_spend_pretax is null then 0 else lifetime_spend_pretax end) as lifetime_spend_pretax,
        (case when lifetime_tax_paid is null then 0 else lifetime_tax_paid end) as lifetime_tax_paid,
        (case when lifetime_cogs is null then 0 else lifetime_cogs end) as lifetime_cogs,
        (case when lifetime_gross_profit is null then 0 else lifetime_gross_profit end) as lifetime_gross_profit,
        case when (case when count_lifetime_orders is null then 0 else count_lifetime_orders end) = 0 then 0 else (case when lifetime_spend is null then 0 else lifetime_spend end) / (case when count_lifetime_orders is null then 0 else count_lifetime_orders end) end as avg_order_value,
        (case when high_value_orders is null then 0 else high_value_orders end) as high_value_orders,
        case when (case when count_lifetime_orders is null then 0 else count_lifetime_orders end) > 1 then 'returning' else 'new' end as customer_type
    from jnr_customers
),

exp_recency1 as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        lifetime_spend,
        lifetime_spend_pretax,
        lifetime_tax_paid,
        lifetime_cogs,
        lifetime_gross_profit,
        avg_order_value,
        high_value_orders,
        customer_type,
        (case when last_ordered_at is null then 9999 else date_diff('day', date_trunc('day', last_ordered_at), date_trunc('day', cast('{{ var("run_date") }}' as timestamp))) end) as recency_days_temp
    from exp_customer_metrics
),

exp_recency2 as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        lifetime_spend,
        lifetime_spend_pretax,
        lifetime_tax_paid,
        lifetime_cogs,
        lifetime_gross_profit,
        avg_order_value,
        high_value_orders,
        customer_type,
        (case when last_ordered_at is null then 9999 else date_diff('day', date_trunc('day', last_ordered_at), date_trunc('day', cast('{{ var("run_date") }}' as timestamp))) end) as recency_days
    from exp_recency1
),

exp_rfm_scores as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        lifetime_spend,
        lifetime_spend_pretax,
        lifetime_tax_paid,
        lifetime_cogs,
        lifetime_gross_profit,
        avg_order_value,
        high_value_orders,
        customer_type,
        recency_days,
        (case when recency_days <= 30 then 3 else case when recency_days <= 90 then 2 else 1 end end) as r_score,
        (case when count_lifetime_orders >= 5 then 3 else case when count_lifetime_orders >= 2 then 2 else 1 end end) as f_score,
        (case when lifetime_spend >= 200 then 3 else case when lifetime_spend >= 50 then 2 else 1 end end) as m_score,
        (case when recency_days <= 30 then 3 else case when recency_days <= 90 then 2 else 1 end end) * 100 + (case when count_lifetime_orders >= 5 then 3 else case when count_lifetime_orders >= 2 then 2 else 1 end end) * 10 + (case when lifetime_spend >= 200 then 3 else case when lifetime_spend >= 50 then 2 else 1 end end) as rfm_score
    from exp_recency2
),

exp_rfm_segment as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        lifetime_spend,
        lifetime_spend_pretax,
        lifetime_tax_paid,
        lifetime_cogs,
        lifetime_gross_profit,
        avg_order_value,
        high_value_orders,
        customer_type,
        recency_days,
        r_score,
        f_score,
        m_score,
        rfm_score,
        (case when count_lifetime_orders = 0 then 'PROSPECT' else case when rfm_score = 333 then 'CHAMPION' else case when r_score = 3 and f_score >= 2 and m_score >= 2 then 'LOYAL' else case when r_score = 1 and f_score >= 2 then 'AT_RISK' else 'STANDARD' end end end end) as rfm_segment
    from exp_rfm_scores
),

final as (
    select
        customer_id,
        customer_name,
        count_lifetime_orders,
        first_ordered_at,
        last_ordered_at,
        lifetime_spend as lifetime_spend_pretax,
        lifetime_spend_pretax as lifetime_tax_paid,
        lifetime_tax_paid as lifetime_spend,
        lifetime_cogs,
        lifetime_gross_profit,
        avg_order_value,
        high_value_orders,
        customer_type,
        recency_days,
        r_score,
        f_score,
        m_score,
        rfm_score,
        rfm_segment
    from exp_rfm_segment
)

select * from final
