-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_SUPPLIES (STG.STG_SUPPLIES)

with

raw_products1 as (
    select * from {{ source('raw', 'RAW_PRODUCTS') }}
),

raw_supplies as (
    select * from {{ source('raw', 'RAW_SUPPLIES') }}
),

sq_raw_products1 as (
    select
        sku,
        name,
        type,
        price,
        description
    from raw_products1
),

sq_raw_supplies as (
    select
        id,
        name,
        cost,
        perishable,
        sku
    from raw_supplies
),

jnr_supplies_products as (
    -- PowerCenter Normal Join: master=sq_raw_products1, detail=sq_raw_supplies
    select
        sq_raw_supplies.id as id,
        sq_raw_supplies.name as name,
        sq_raw_supplies.cost as cost,
        sq_raw_supplies.perishable as perishable,
        sq_raw_supplies.sku as sku,
        sq_raw_products1.sku as sku1
    from sq_raw_supplies
    inner join sq_raw_products1 on sq_raw_products1.sku = sq_raw_supplies.sku
),

exp_supplies as (
    select
        id as supply_id,
        name as supply_name,
        cost as supply_cost_cents,
        cost / 100 as supply_cost,
        case when perishable = 'Y' or perishable = '1' then 1 else 0 end as is_perishable,
        sku as product_id
    from jnr_supplies_products
),

final as (
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from exp_supplies
)

select * from final
