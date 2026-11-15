-- Generated from PowerCenter mapping m_FL_JS_MARTS_ENRICHED
-- shared step SRT_ORDER_ID, built once and referenced by several models

with

stg_customers as (
    select
        cast(customer_id as varchar) as customer_id,
        cast(customer_name as varchar) as customer_name
    from {{ ref('stg_customers') }}
),

stg_locations as (
    select
        cast(location_id as varchar) as location_id,
        cast(location_name as varchar) as location_name,
        cast(opened_date as timestamp) as opened_date,
        cast(tax_rate as double) as tax_rate,
        cast(region as varchar) as region
    from {{ ref('stg_locations') }}
),

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

sq_stg_customers as (
    select
        customer_id,
        customer_name
    from stg_customers
),

sq_stg_locations as (
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from stg_locations
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

jnr_items_orders as (
    -- PowerCenter Normal Join: master=sq_stg_order_items, detail=sq_stg_orders
    select
        sq_stg_order_items.order_item_id as order_item_id,
        sq_stg_order_items.order_id as order_id,
        sq_stg_order_items.product_id as product_id,
        sq_stg_orders.customer_id as customer_id,
        sq_stg_orders.location_id as location_id,
        sq_stg_orders.ordered_at_day as ordered_at_day
    from sq_stg_orders
    inner join sq_stg_order_items on sq_stg_order_items.order_id = sq_stg_orders.order_id
),

srt_supplies as (
    -- sort order dropped
    select
        product_id,
        supply_cost,
        is_perishable
    from sq_stg_supplies
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
    -- PowerCenter Normal Join: master=jnr_items_orders, detail=sq_stg_products
    select
        jnr_items_orders.order_item_id as order_item_id,
        jnr_items_orders.order_id as order_id,
        jnr_items_orders.product_id as product_id,
        jnr_items_orders.customer_id as customer_id,
        jnr_items_orders.location_id as location_id,
        jnr_items_orders.ordered_at_day as ordered_at_day,
        sq_stg_products.product_name as product_name,
        sq_stg_products.product_type as product_type,
        sq_stg_products.product_price as product_price,
        sq_stg_products.is_food_item as is_food_item,
        sq_stg_products.is_drink_item as is_drink_item
    from sq_stg_products
    inner join jnr_items_orders on jnr_items_orders.product_id = sq_stg_products.product_id
),

jnr_add_customers as (
    -- PowerCenter Master Outer Join: master=jnr_add_products, detail=sq_stg_customers
    select
        jnr_add_products.order_item_id as order_item_id,
        jnr_add_products.order_id as order_id,
        jnr_add_products.product_id as product_id,
        jnr_add_products.customer_id as customer_id,
        jnr_add_products.location_id as location_id,
        jnr_add_products.ordered_at_day as ordered_at_day,
        jnr_add_products.product_name as product_name,
        jnr_add_products.product_type as product_type,
        jnr_add_products.product_price as product_price,
        jnr_add_products.is_food_item as is_food_item,
        jnr_add_products.is_drink_item as is_drink_item,
        sq_stg_customers.customer_name as customer_name
    from sq_stg_customers
    left join jnr_add_products on jnr_add_products.customer_id = sq_stg_customers.customer_id
),

jnr_add_locations as (
    -- PowerCenter Master Outer Join: master=jnr_add_customers, detail=sq_stg_locations
    select
        jnr_add_customers.order_item_id as order_item_id,
        jnr_add_customers.order_id as order_id,
        jnr_add_customers.product_id as product_id,
        jnr_add_customers.customer_id as customer_id,
        jnr_add_customers.location_id as location_id,
        jnr_add_customers.ordered_at_day as ordered_at_day,
        jnr_add_customers.product_name as product_name,
        jnr_add_customers.product_type as product_type,
        jnr_add_customers.product_price as product_price,
        jnr_add_customers.is_food_item as is_food_item,
        jnr_add_customers.is_drink_item as is_drink_item,
        jnr_add_customers.customer_name as customer_name,
        sq_stg_locations.location_name as location_name,
        sq_stg_locations.tax_rate as tax_rate,
        sq_stg_locations.region as region
    from sq_stg_locations
    left join jnr_add_customers on jnr_add_customers.location_id = sq_stg_locations.location_id
),

jnr_add_supplies as (
    -- PowerCenter Master Outer Join: master=jnr_add_locations, detail=agg_supplies_by_product
    select
        jnr_add_locations.order_item_id as order_item_id,
        jnr_add_locations.order_id as order_id,
        jnr_add_locations.product_id as product_id,
        jnr_add_locations.customer_id as customer_id,
        jnr_add_locations.location_id as location_id,
        jnr_add_locations.ordered_at_day as ordered_at_day,
        jnr_add_locations.product_name as product_name,
        jnr_add_locations.product_type as product_type,
        jnr_add_locations.product_price as product_price,
        jnr_add_locations.is_food_item as is_food_item,
        jnr_add_locations.is_drink_item as is_drink_item,
        jnr_add_locations.customer_name as customer_name,
        jnr_add_locations.location_name as location_name,
        jnr_add_locations.tax_rate as tax_rate,
        jnr_add_locations.region as region,
        agg_supplies_by_product.supply_cost_per_product as supply_cost_per_product,
        agg_supplies_by_product.has_perishable_supply as has_perishable_supply
    from agg_supplies_by_product
    left join jnr_add_locations on jnr_add_locations.product_id = agg_supplies_by_product.product_id
),

exp_item_calculations as (
    select
        order_item_id,
        order_id,
        product_id,
        customer_id,
        location_id,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        case when customer_name is null then 'Unknown' else customer_name end as customer_name,
        case when location_name is null then 'Unknown' else location_name end as location_name,
        case when region is null then 'Unknown' else region end as region,
        product_price as item_revenue,
        (case when supply_cost_per_product is null then 0 else supply_cost_per_product end) as item_cogs,
        product_price - (case when supply_cost_per_product is null then 0 else supply_cost_per_product end) as item_gross_profit,
        (case when product_price = 0 then 0 else (product_price - (case when supply_cost_per_product is null then 0 else supply_cost_per_product end)) / product_price end) as item_gross_margin_pct,
        product_price * (case when tax_rate is null then 0 else tax_rate end) as item_tax_estimate,
        product_price + product_price * (case when tax_rate is null then 0 else tax_rate end) as item_total_estimate,
        (case when has_perishable_supply is null then 0 else has_perishable_supply end) as has_perishable_supply
    from jnr_add_supplies
),

srt_order_id as (
    -- sort order dropped
    select
        order_id,
        order_item_id,
        product_id,
        customer_id,
        customer_name,
        location_id,
        location_name,
        region,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        item_revenue,
        item_cogs,
        item_gross_profit,
        item_gross_margin_pct,
        item_tax_estimate,
        item_total_estimate,
        has_perishable_supply
    from exp_item_calculations
),

final as (
    select
        order_id,
        order_item_id,
        product_id,
        customer_id,
        customer_name,
        location_id,
        location_name,
        region,
        ordered_at_day,
        product_name,
        product_type,
        product_price,
        is_food_item,
        is_drink_item,
        item_revenue,
        item_cogs,
        item_gross_profit,
        item_gross_margin_pct,
        item_tax_estimate,
        item_total_estimate,
        has_perishable_supply
    from srt_order_id
)

select * from final
