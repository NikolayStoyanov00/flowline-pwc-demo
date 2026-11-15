-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_ORDER_ITEMS (STG.STG_ORDER_ITEMS)

with

raw_items as (
    select
        cast(id as varchar) as id,
        cast(order_id as varchar) as order_id,
        cast(sku as varchar) as sku
    from {{ source('raw', 'RAW_ITEMS') }}
),

sq_raw_items as (
    select
        id,
        order_id,
        sku
    from raw_items
),

exp_order_items as (
    select
        id as order_item_id,
        order_id,
        sku as product_id
    from sq_raw_items
),

final as (
    select
        order_item_id,
        order_id,
        product_id
    from exp_order_items
)

select * from final
