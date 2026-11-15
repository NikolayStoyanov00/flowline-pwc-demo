-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target ORDER_ITEMS (MART.ORDER_ITEMS)

with

stg_orders as (
    select
        cast(order_id as varchar) as order_id,
        cast(customer_id as varchar) as customer_id,
        cast(location_id as varchar) as location_id,
        cast(ordered_at_day as timestamp) as ordered_at_day,
        cast(subtotal_cents as double) as subtotal_cents,
        cast(tax_paid_cents as double) as tax_paid_cents,
        cast(order_total_cents as double) as order_total_cents,
        cast(subtotal as double) as subtotal,
        cast(tax_paid as double) as tax_paid,
        cast(order_total as double) as order_total
    from {{ ref('stg_orders') }}
),

stg_order_items as (
    select
        cast(order_item_id as varchar) as order_item_id,
        cast(order_id as varchar) as order_id,
        cast(product_id as varchar) as product_id
    from {{ ref('stg_order_items') }}
),

stg_products1 as (
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

stg_supplies1 as (
    select
        cast(supply_id as varchar) as supply_id,
        cast(supply_name as varchar) as supply_name,
        cast(supply_cost_cents as double) as supply_cost_cents,
        cast(supply_cost as double) as supply_cost,
        cast(is_perishable as double) as is_perishable,
        cast(product_id as varchar) as product_id
    from {{ ref('stg_supplies') }}
),

sq_stg_orders as (
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
    from stg_orders
),

sq_stg_order_items as (
    select
        order_item_id,
        order_id,
        product_id
    from stg_order_items
),

sq_stg_products1 as (
    select
        product_id,
        product_name,
        product_type,
        product_price_cents,
        product_price,
        product_description,
        is_food_item,
        is_drink_item
    from stg_products1
),

sq_stg_supplies1 as (
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from stg_supplies1
),

jnr_order_items_orders as (
    -- PowerCenter Normal Join: master=sq_stg_orders, detail=sq_stg_order_items
    select
        sq_stg_order_items.order_item_id as order_item_id,
        sq_stg_order_items.order_id as order_id,
        sq_stg_order_items.product_id as product_id,
        sq_stg_orders.ordered_at_day as ordered_at_day
    from sq_stg_order_items
    inner join sq_stg_orders on sq_stg_orders.order_id = sq_stg_order_items.order_id
),

srt_supplies as (
    -- sort order dropped
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from sq_stg_supplies1
),

agg_supplies_by_product as (
    select
        product_id,
        sum(supply_cost) as supply_cost_per_product,
        max(is_perishable) as has_perishable_supply
    from srt_supplies
    group by 1
),

jnr_add_products as (
    -- PowerCenter Normal Join: master=sq_stg_products1, detail=jnr_order_items_orders
    select
        jnr_order_items_orders.order_item_id as order_item_id,
        jnr_order_items_orders.order_id as order_id,
        jnr_order_items_orders.product_id as product_id,
        jnr_order_items_orders.ordered_at_day as ordered_at_day,
        sq_stg_products1.product_name as product_name,
        sq_stg_products1.product_type as product_type,
        sq_stg_products1.product_price as product_price,
        sq_stg_products1.is_food_item as is_food_item,
        sq_stg_products1.is_drink_item as is_drink_item
    from jnr_order_items_orders
    inner join sq_stg_products1 on sq_stg_products1.product_id = jnr_order_items_orders.product_id
),

jnr_add_supply_cost as (
    -- PowerCenter Master Outer Join: master=jnr_add_products, detail=agg_supplies_by_product
    select
        jnr_add_products.order_item_id as order_item_id,
        jnr_add_products.order_id as order_id,
        agg_supplies_by_product.product_id as product_id,
        jnr_add_products.ordered_at_day as ordered_at_day,
        jnr_add_products.product_name as product_name,
        jnr_add_products.product_type as product_type,
        jnr_add_products.product_price as product_price,
        jnr_add_products.is_food_item as is_food_item,
        jnr_add_products.is_drink_item as is_drink_item,
        agg_supplies_by_product.supply_cost_per_product as supply_cost_per_product,
        agg_supplies_by_product.has_perishable_supply as has_perishable_supply
    from agg_supplies_by_product
    left join jnr_add_products on jnr_add_products.product_id = agg_supplies_by_product.product_id
),

exp_order_items_calc as (
    select
        order_item_id,
        order_id,
        product_id,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        case when supply_cost_per_product is null then 0 else supply_cost_per_product end as supply_cost_per_product,
        case when has_perishable_supply is null then 0 else has_perishable_supply end as has_perishable_supply
    from jnr_add_supply_cost
),

final as (
    select
        order_item_id,
        order_id,
        product_id,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        supply_cost_per_product,
        has_perishable_supply
    from exp_order_items_calc
)

select * from final
