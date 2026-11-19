\set ON_ERROR_STOP on

create or replace function pg_temp.make_concurrent_batch(generation_id text)
returns void language plpgsql as $$
declare
    race_id text := '202611190301';
    payload jsonb;
    manifest jsonb;
begin
    payload := jsonb_build_object(
        'race',jsonb_build_object('race_id',race_id,'race_date','2026-11-19','venue_code','03','race_number',1,'line_state','fetched'),
        'entries',jsonb_build_array(
            jsonb_build_object('registration_no','10001','bike_number',1),
            jsonb_build_object('registration_no','10002','bike_number',2),
            jsonb_build_object('registration_no','10003','bike_number',3),
            jsonb_build_object('registration_no','10004','bike_number',4),
            jsonb_build_object('registration_no','10005','bike_number',5)
        ),
        'cloud_generation',jsonb_build_object('generation_id',generation_id)
    );
    manifest := jsonb_build_object(
        'status','complete','race_date','2026-11-19','venue_code','03','expected_races',1,
        'race_ids',jsonb_build_array(race_id),'cloud_generation',jsonb_build_object('generation_id',generation_id)
    );
    perform public.cloud_stage_venue_generation(
        'racecard','2026-11-19','03','C場',generation_id,jsonb_build_array(race_id),
        manifest,repeat('f',64),jsonb_build_array(jsonb_build_object(
            'record_type','cloud_racecard','record_key',race_id,'payload',payload,
            'content_hash',repeat('1',64),'source_updated_at','2026-11-19T00:00:00Z'
        ))
    );
end;
$$;

select pg_temp.make_concurrent_batch(repeat('d',64));
select pg_temp.make_concurrent_batch(repeat('e',64));
