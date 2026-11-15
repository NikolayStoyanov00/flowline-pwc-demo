-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target PRODUCTS (MART.PRODUCTS)

with

stg_products as (
    select
        cast(product_id as varchar) as product_id,
        cast(product_name as varchar) as product_name,
        cast(product_type as varchar) as product_type,
        cast(product_price_cents as double) as product_price_cents,
        cast(product_price as double) as product_price,
        cast(product_description as varchar) as product_description,
        cast(is_food_item as double) as is_food_item,
        cast(is_drink_item as double) as is_drink_item
    from {{ ref('stg_products') }}
),

sq_stg_products as (
    select
        product_id,
        product_name,
        product_type,
        product_price_cents,
        product_price,
        product_description,
        is_food_item,
        is_drink_item
    from stg_products
),

final as (
    select
        product_id,
        product_name,
        product_type,
        product_price_cents,
        product_price,
        product_description,
        is_food_item,
        is_drink_item
    from sq_stg_products
)

select * from final
