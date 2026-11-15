-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target PRODUCTS (MART.PRODUCTS)

with

stg_products as (
    select * from {{ ref('stg_products') }}
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
