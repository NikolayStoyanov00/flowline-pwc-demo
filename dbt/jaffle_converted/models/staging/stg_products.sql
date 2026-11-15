-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_PRODUCTS (STG.STG_PRODUCTS)

with

raw_products as (
    select * from {{ source('raw', 'RAW_PRODUCTS') }}
),

sq_raw_products as (
    select
        sku,
        name,
        type,
        price,
        description
    from raw_products
),

exp_products as (
    select
        sku as product_id,
        name as product_name,
        type as product_type,
        price / 100 as product_price,
        price as product_price_cents,
        description as product_description,
        case when upper(ltrim(rtrim(type))) = 'JAFFLE' then 1 else 0 end as is_food_item,
        case when upper(ltrim(rtrim(type))) = 'BEVERAGE' then 1 else 0 end as is_drink_item
    from sq_raw_products
),

final as (
    select
        product_id,
        product_name,
        product_type,
        product_price as product_price_cents,
        product_price_cents as product_price,
        product_description,
        is_food_item,
        is_drink_item
    from exp_products
)

select * from final
