-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target SUPPLIES (MART.SUPPLIES)

with

stg_supplies as (
    select * from {{ ref('stg_supplies') }}
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
