-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target SUPPLIES (MART.SUPPLIES)

with

stg_supplies as (
    select
        cast(supply_id as varchar) as supply_id,
        cast(supply_name as varchar) as supply_name,
        cast(supply_cost_cents as double) as supply_cost_cents,
        cast(supply_cost as double) as supply_cost,
        cast(is_perishable as double) as is_perishable,
        cast(product_id as varchar) as product_id
    from {{ ref('stg_supplies') }}
),

sq_stg_supplies as (
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from stg_supplies
),

final as (
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from sq_stg_supplies
)

select * from final
