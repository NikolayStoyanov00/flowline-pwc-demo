-- Generated from PowerCenter mapping m_FL_JS_STAGING_FULL
-- target STG_LOCATIONS (STG.STG_LOCATIONS)

with

raw_stores as (
    select
        cast(id as varchar) as id,
        cast(name as varchar) as name,
        cast(opened_at as timestamp) as opened_at,
        cast(tax_rate as double) as tax_rate
    from {{ source('raw', 'RAW_STORES') }}
),

sq_raw_stores as (
    select
        id,
        name,
        opened_at,
        tax_rate
    from raw_stores
),

exp_locations as (
    select
        id as location_id,
        name as location_name,
        date_trunc('day', opened_at) as opened_date,
        tax_rate,
        case when tax_rate >= 0.15 then 'EMEA' else case when tax_rate >= 0.09 then 'APAC' else 'AMER' end end as region
    from sq_raw_stores
),

srt_loc_before as (
    -- sort order dropped
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from exp_locations
),

rtr_region__g_apac as (
    -- router group G_APAC
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from srt_loc_before
    where region = 'APAC'
),

rtr_region__g_amer as (
    -- router group G_AMER
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from srt_loc_before
    where region = 'AMER'
),

rtr_region__g_emea as (
    -- router group G_EMEA
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from srt_loc_before
    where region = 'EMEA'
),

srt_amer as (
    -- sort order dropped
    select
        location_id as location_id3,
        location_name as location_name3,
        opened_date as opened_date3,
        tax_rate as tax_rate3,
        region as region3
    from rtr_region__g_amer
),

srt_apac as (
    -- sort order dropped
    select
        location_id as location_id1,
        location_name as location_name1,
        opened_date as opened_date1,
        tax_rate as tax_rate1,
        region as region1
    from rtr_region__g_apac
),

srt_emea as (
    -- sort order dropped
    select
        location_id as location_id4,
        location_name as location_name4,
        opened_date as opened_date4,
        tax_rate as tax_rate4,
        region as region4
    from rtr_region__g_emea
),

exp_amer_rename_trans as (
    select
        location_id3 as location_id,
        location_name3 as location_name,
        opened_date3 as opened_date,
        tax_rate3 as tax_rate,
        region3 as region
    from srt_amer
),

exp_apac_rename_trans as (
    select
        location_id1 as location_id,
        location_name1 as location_name,
        opened_date1 as opened_date,
        tax_rate1 as tax_rate,
        region1 as region
    from srt_apac
),

exp_emea_rename_trans as (
    select
        location_id4 as location_id,
        location_name4 as location_name,
        opened_date4 as opened_date,
        tax_rate4 as tax_rate,
        region4 as region
    from srt_emea
),

un_regions as (
    -- union leg APAC
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from exp_apac_rename_trans

    union all

    -- union leg AMER
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from exp_amer_rename_trans

    union all

    -- union leg EMEA
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from exp_emea_rename_trans
),

srt_loc_after as (
    -- sort order dropped
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from un_regions
),

final as (
    select
        location_id,
        location_name,
        opened_date,
        tax_rate,
        region
    from srt_loc_after
)

select * from final
