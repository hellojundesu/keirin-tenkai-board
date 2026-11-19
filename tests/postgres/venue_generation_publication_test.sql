\set ON_ERROR_STOP on

create or replace function pg_temp.assert_true(condition boolean, message text)
returns void language plpgsql as $$
begin
    if not coalesce(condition, false) then
        raise exception 'assertion failed: %', message;
    end if;
end;
$$;

create or replace function pg_temp.card_payload(
    race_date text, venue_code text, race_id text, generation_id text, line_state text default 'fetched'
) returns jsonb language sql immutable as $$
    select jsonb_build_object(
        'race', jsonb_build_object(
            'race_id', race_id, 'race_date', race_date, 'venue_code', venue_code,
            'race_number', substring(race_id from 11 for 2)::integer,
            'line_state', line_state
        ),
        'entries', jsonb_build_array(
            jsonb_build_object('registration_no','10001','bike_number',1),
            jsonb_build_object('registration_no','10002','bike_number',2),
            jsonb_build_object('registration_no','10003','bike_number',3),
            jsonb_build_object('registration_no','10004','bike_number',4),
            jsonb_build_object('registration_no','10005','bike_number',5)
        ),
        'cloud_generation', jsonb_build_object(
            'generation_id', generation_id, 'dataset_family', 'racecard',
            'race_date', race_date, 'venue_code', venue_code,
            'generation_status', 'complete'
        )
    );
$$;

create or replace function pg_temp.venue_manifest(
    race_date text, venue_code text, venue_name text, generation_id text, race_ids jsonb
) returns jsonb language sql immutable as $$
    select jsonb_build_object(
        'status','complete', 'race_date',race_date, 'venue_code',venue_code,
        'venue_name',venue_name, 'expected_races',jsonb_array_length(race_ids),
        'race_ids',race_ids,
        'cloud_generation',jsonb_build_object('generation_id',generation_id)
    );
$$;

-- Venue 01: two races.  Venue 02 deliberately starts incomplete.
select public.cloud_stage_venue_generation(
    'racecard','2026-11-18','01','A場',repeat('a',64),
    '["202611180101","202611180102"]'::jsonb,
    pg_temp.venue_manifest('2026-11-18','01','A場',repeat('a',64),'["202611180101","202611180102"]'::jsonb),
    repeat('1',64),
    jsonb_build_array(
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180101','payload',pg_temp.card_payload('2026-11-18','01','202611180101',repeat('a',64)),'content_hash',repeat('2',64),'source_updated_at','2026-11-18T00:00:00Z'),
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180102','payload',pg_temp.card_payload('2026-11-18','01','202611180102',repeat('a',64)),'content_hash',repeat('3',64),'source_updated_at','2026-11-18T00:00:00Z')
    )
);

do $$
begin
    perform public.cloud_stage_venue_generation(
        'racecard','2026-11-18','02','B場',repeat('b',64),
        '["202611180201","202611180202"]'::jsonb,
        pg_temp.venue_manifest('2026-11-18','02','B場',repeat('b',64),'["202611180201","202611180202"]'::jsonb),
        repeat('4',64),
        jsonb_build_array(
            jsonb_build_object('record_type','cloud_racecard','record_key','202611180201','payload',pg_temp.card_payload('2026-11-18','02','202611180201',repeat('b',64)),'content_hash',repeat('5',64),'source_updated_at','2026-11-18T00:00:00Z')
        )
    );
    raise exception 'partial venue unexpectedly staged';
exception when sqlstate '22023' then
    null;
end;
$$;

select public.cloud_publish_venue_generation('racecard','2026-11-18','01',repeat('a',64),0,'postgres-test');
select pg_temp.assert_true(
    (select generation_id = repeat('a',64) and revision = 1
     from public.cloud_active_venue_generations
     where dataset_family='racecard' and race_date='2026-11-18' and venue_code='01'),
    'complete venue was not published'
);
select pg_temp.assert_true(
    not exists (select 1 from public.cloud_active_venue_generations where race_date='2026-11-18' and venue_code='02'),
    'incomplete venue became active'
);

-- Complete venue 02 later. Its publication must preserve venue 01.
select public.cloud_stage_venue_generation(
    'racecard','2026-11-18','02','B場',repeat('b',64),
    '["202611180201","202611180202"]'::jsonb,
    pg_temp.venue_manifest('2026-11-18','02','B場',repeat('b',64),'["202611180201","202611180202"]'::jsonb),
    repeat('4',64),
    jsonb_build_array(
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180201','payload',pg_temp.card_payload('2026-11-18','02','202611180201',repeat('b',64)),'content_hash',repeat('5',64),'source_updated_at','2026-11-18T00:00:00Z'),
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180202','payload',pg_temp.card_payload('2026-11-18','02','202611180202',repeat('b',64)),'content_hash',repeat('6',64),'source_updated_at','2026-11-18T00:00:00Z')
    )
);
select public.cloud_publish_venue_generation('racecard','2026-11-18','02',repeat('b',64),0,'postgres-test');
select pg_temp.assert_true(
    (select jsonb_array_length(payload->'venues')=2 and revision=2 from public.cloud_active_day_indexes where race_date='2026-11-18'),
    'day index does not contain both independent venue pointers'
);

-- Idempotent staging and activation.
select public.cloud_stage_venue_generation(
    'racecard','2026-11-18','01','A場',repeat('a',64),
    '["202611180101","202611180102"]'::jsonb,
    pg_temp.venue_manifest('2026-11-18','01','A場',repeat('a',64),'["202611180101","202611180102"]'::jsonb),
    repeat('1',64),
    jsonb_build_array(
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180101','payload',pg_temp.card_payload('2026-11-18','01','202611180101',repeat('a',64)),'content_hash',repeat('2',64),'source_updated_at','2026-11-18T00:00:00Z'),
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180102','payload',pg_temp.card_payload('2026-11-18','01','202611180102',repeat('a',64)),'content_hash',repeat('3',64),'source_updated_at','2026-11-18T00:00:00Z')
    )
);
select public.cloud_publish_venue_generation('racecard','2026-11-18','01',repeat('a',64),1,'postgres-test');
select pg_temp.assert_true(
    (select revision=1 from public.cloud_active_venue_generations where race_date='2026-11-18' and venue_code='01'),
    'idempotent publish changed venue revision'
);

-- Same generation identity with different bytes is rejected.
do $$
begin
    perform public.cloud_stage_venue_generation(
        'racecard','2026-11-18','01','A場',repeat('a',64),
        '["202611180101","202611180102"]'::jsonb,
        pg_temp.venue_manifest('2026-11-18','01','A場',repeat('a',64),'["202611180101","202611180102"]'::jsonb),
        repeat('1',64),
        jsonb_build_array(
            jsonb_build_object('record_type','cloud_racecard','record_key','202611180101','payload',pg_temp.card_payload('2026-11-18','01','202611180101',repeat('a',64)) || '{"tampered":true}'::jsonb,'content_hash',repeat('2',64),'source_updated_at','2026-11-18T00:00:00Z'),
            jsonb_build_object('record_type','cloud_racecard','record_key','202611180102','payload',pg_temp.card_payload('2026-11-18','01','202611180102',repeat('a',64)),'content_hash',repeat('3',64),'source_updated_at','2026-11-18T00:00:00Z')
        )
    );
    raise exception 'conflicting generation unexpectedly accepted';
exception when unique_violation then
    null;
end;
$$;

-- A newer complete generation can switch one venue without changing venue 02.
select public.cloud_stage_venue_generation(
    'racecard','2026-11-18','01','A場',repeat('c',64),
    '["202611180101","202611180102"]'::jsonb,
    pg_temp.venue_manifest('2026-11-18','01','A場',repeat('c',64),'["202611180101","202611180102"]'::jsonb),
    repeat('7',64),
    jsonb_build_array(
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180101','payload',pg_temp.card_payload('2026-11-18','01','202611180101',repeat('c',64)) || '{"new_fact":true}'::jsonb,'content_hash',repeat('8',64),'source_updated_at','2026-11-18T01:00:00Z'),
        jsonb_build_object('record_type','cloud_racecard','record_key','202611180102','payload',pg_temp.card_payload('2026-11-18','01','202611180102',repeat('c',64)) || '{"new_fact":true}'::jsonb,'content_hash',repeat('9',64),'source_updated_at','2026-11-18T01:00:00Z')
    )
);
select public.cloud_publish_venue_generation('racecard','2026-11-18','01',repeat('c',64),1,'postgres-test');
select pg_temp.assert_true(
    (select generation_id=repeat('b',64) and revision=1 from public.cloud_active_venue_generations where race_date='2026-11-18' and venue_code='02'),
    'unrelated venue pointer changed'
);
select pg_temp.assert_true(
    (select count(*)=3 from public.cloud_venue_generations where race_date='2026-11-18'),
    'old generations were not retained'
);

-- Roll back venue 01 by repointing to its retained generation.
select public.cloud_publish_venue_generation('racecard','2026-11-18','01',repeat('a',64),2,'postgres-test','rollback');
select pg_temp.assert_true(
    (select generation_id=repeat('a',64) and revision=3 from public.cloud_active_venue_generations where race_date='2026-11-18' and venue_code='01'),
    'rollback did not restore retained generation'
);
select pg_temp.assert_true(
    (select count(*)=4 from public.cloud_venue_publication_audit where race_date='2026-11-18'),
    'publication audit count mismatch'
);

select 'venue_generation_publication_test: PASS' as result;
