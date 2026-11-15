-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_CUSTOMERS (STG.STG_CUSTOMERS)

with

raw_customers as (
    select
        cast(id as varchar) as id,
        cast(name as varchar) as name
    from {{ source('raw', 'RAW_CUSTOMERS') }}
),

sq_raw_customers as (
    select
        id,
        name
    from raw_customers
),

final as (
    select
        id as customer_id,
        name as customer_name
    from sq_raw_customers
)

select * from final
