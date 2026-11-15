-- Generated from PowerCenter mapping m_FL_JS_MARTS_CORE
-- target ORDERS (MART.ORDERS)
-- note: EXP_CUSTOMER_ORDER_NUM.v_ORDER_SEQUENCE was a variable holding its own previous value; translated as row_number()

with

stg_orders1 as (
    select * from {{ ref('stg_orders') }}
),

stg_order_items1 as (
    select * from {{ ref('stg_order_items') }}
),

stg_products2 as (
    select * from {{ ref('stg_products') }}
),

stg_supplies2 as (
    select * from {{ ref('stg_supplies') }}
),

sq_stg_orders1 as (
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
    from stg_orders1
),

sq_stg_order_items1 as (
    select
        order_item_id,
        order_id,
        product_id
    from stg_order_items1
),

sq_stg_products2 as (
    select
        product_id,
        product_name,
        product_type,
        product_price_cents,
        product_price,
        product_description,
        is_food_item,
        is_drink_item
    from stg_products2
),

sq_stg_supplies2 as (
    select
        supply_id,
        supply_name,
        supply_cost_cents,
        supply_cost,
        is_perishable,
        product_id
    from stg_supplies2
),

agg_supplies_by_product2 as (
    select
        product_id,
        sum(supply_cost) as supply_cost_per_product
    from sq_stg_supplies2
    group by 1
),

jnr_items_products as (
    -- PowerCenter Normal Join: master=sq_stg_order_items1, detail=sq_stg_products2
    select
        sq_stg_order_items1.order_id as order_id,
        sq_stg_order_items1.order_item_id as order_item_id,
        sq_stg_products2.product_id as product_id,
        sq_stg_products2.product_price as product_price,
        sq_stg_products2.is_food_item as is_food_item,
        sq_stg_products2.is_drink_item as is_drink_item
    from sq_stg_products2
    inner join sq_stg_order_items1 on sq_stg_order_items1.product_id = sq_stg_products2.product_id
),

jnr_items_supplies as (
    -- PowerCenter Master Outer Join: master=jnr_items_products, detail=agg_supplies_by_product2
    select
        jnr_items_products.order_id as order_id,
        jnr_items_products.order_item_id as order_item_id,
        jnr_items_products.product_id as product_id,
        jnr_items_products.product_price as product_price,
        jnr_items_products.is_food_item as is_food_item,
        jnr_items_products.is_drink_item as is_drink_item,
        agg_supplies_by_product2.supply_cost_per_product as supply_cost_per_product
    from agg_supplies_by_product2
    left join jnr_items_products on jnr_items_products.product_id = agg_supplies_by_product2.product_id
),

agg_items_by_order as (
    select
        order_id,
        count(*) as items_count,
        sum(product_price) as order_items_subtotal,
        sum(case when supply_cost_per_product is null then 0 else supply_cost_per_product end) as order_cost,
        max(is_food_item) as has_food_items,
        max(is_drink_item) as has_drink_items
    from jnr_items_supplies
    group by 1
),

jnr_orders_items_agg as (
    -- PowerCenter Master Outer Join: master=sq_stg_orders1, detail=agg_items_by_order
    select
        agg_items_by_order.order_id as order_id,
        sq_stg_orders1.customer_id as customer_id,
        sq_stg_orders1.location_id as location_id,
        sq_stg_orders1.ordered_at_day as ordered_at_day,
        sq_stg_orders1.subtotal_cents as subtotal_cents,
        sq_stg_orders1.tax_paid_cents as tax_paid_cents,
        sq_stg_orders1.order_total_cents as order_total_cents,
        sq_stg_orders1.subtotal as subtotal,
        sq_stg_orders1.tax_paid as tax_paid,
        sq_stg_orders1.order_total as order_total,
        agg_items_by_order.items_count as items_count,
        agg_items_by_order.order_items_subtotal as order_items_subtotal,
        agg_items_by_order.order_cost as order_cost,
        agg_items_by_order.has_food_items as has_food_items,
        agg_items_by_order.has_drink_items as has_drink_items
    from agg_items_by_order
    left join sq_stg_orders1 on sq_stg_orders1.order_id = agg_items_by_order.order_id
),

exp_orders_calc as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal_cents / 100 as subtotal_recal,
        tax_paid_cents / 100 as tax_paid_recal,
        order_total_cents / 100 as order_total_recal,
        case when order_items_subtotal is null then 0 else order_items_subtotal end as order_items_subtotal_cl,
        case when order_cost is null then 0 else order_cost end as order_cost_cl,
        case when has_food_items is null then 0 else has_food_items end as has_food_items_cl,
        case when has_drink_items is null then 0 else has_drink_items end as has_drink_items_cl
    from jnr_orders_items_agg
),

srt_by_customer as (
    -- sort order dropped
    select
        customer_id,
        location_id,
        ordered_at_day,
        order_id,
        subtotal_recal,
        tax_paid_recal,
        order_total_recal,
        order_items_subtotal_cl,
        order_cost_cl,
        has_food_items_cl,
        has_drink_items_cl
    from exp_orders_calc
),

exp_customer_order_num as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal_recal as subtotal,
        tax_paid_recal as tax_paid,
        order_total_recal as order_total,
        order_items_subtotal_cl as order_items_subtotal,
        order_cost_cl as order_cost,
        has_food_items_cl as has_food_items,
        has_drink_items_cl as has_drink_items,
        row_number() over (partition by customer_id order by ordered_at_day, order_id) as customer_order_number
    from srt_by_customer
),

final as (
    select
        order_id,
        customer_id,
        location_id,
        ordered_at_day,
        subtotal,
        tax_paid,
        order_total,
        order_items_subtotal,
        order_cost,
        has_food_items,
        has_drink_items,
        customer_order_number
    from exp_customer_order_num
)

select * from final
