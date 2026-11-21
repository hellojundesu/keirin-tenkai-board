-- Implemented schema for per-venue atomic race-card publication.
-- VALIDATION ONLY: this file is not wired to a production migration or workflow.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.cloud_venue_generation_records (
    dataset_family text not null,
    race_date date not null,
    venue_code text not null check (venue_code ~ '^[0-9]{2}$'),
    generation_id text not null check (generation_id ~ '^[0-9a-f]{64}$'),
    record_type text not null check (record_type = 'cloud_racecard'),
    record_key text not null check (record_key ~ '^[0-9]{12}$'),
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
    storage_hash text not null check (storage_hash ~ '^[0-9a-f]{64}$'),
    source_updated_at timestamptz,
    staged_at timestamptz not null default clock_timestamp(),
    primary key (dataset_family, race_date, venue_code, generation_id, record_type, record_key),
    unique (dataset_family, race_date, venue_code, generation_id, record_key),
    check (substring(record_key from 1 for 8) = to_char(race_date, 'YYYYMMDD')),
    check (substring(record_key from 9 for 2) = venue_code)
);

create table if not exists public.cloud_venue_generations (
    dataset_family text not null,
    race_date date not null,
    venue_code text not null check (venue_code ~ '^[0-9]{2}$'),
    venue_name text not null check (btrim(venue_name) <> ''),
    generation_id text not null check (generation_id ~ '^[0-9a-f]{64}$'),
    expected_race_ids jsonb not null check (jsonb_typeof(expected_race_ids) = 'array'),
    expected_race_count integer not null check (expected_race_count between 1 and 12),
    manifest jsonb not null check (jsonb_typeof(manifest) = 'object'),
    manifest_hash text not null check (manifest_hash ~ '^[0-9a-f]{64}$'),
    status text not null default 'staged' check (status in ('staged', 'published')),
    staged_at timestamptz not null default clock_timestamp(),
    published_at timestamptz,
    primary key (dataset_family, race_date, venue_code, generation_id),
    check (jsonb_array_length(expected_race_ids) = expected_race_count)
);

create table if not exists public.cloud_active_venue_generations (
    dataset_family text not null,
    race_date date not null,
    venue_code text not null check (venue_code ~ '^[0-9]{2}$'),
    venue_name text not null,
    generation_id text not null,
    revision bigint not null check (revision > 0),
    activated_at timestamptz not null default clock_timestamp(),
    primary key (dataset_family, race_date, venue_code),
    foreign key (dataset_family, race_date, venue_code, generation_id)
        references public.cloud_venue_generations (dataset_family, race_date, venue_code, generation_id)
);

create table if not exists public.cloud_active_day_indexes (
    dataset_family text not null,
    race_date date not null,
    revision bigint not null check (revision > 0),
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz not null default clock_timestamp(),
    primary key (dataset_family, race_date)
);

create table if not exists public.cloud_venue_publication_audit (
    audit_id bigint generated always as identity primary key,
    dataset_family text not null,
    race_date date not null,
    venue_code text not null,
    generation_id text not null,
    previous_generation_id text,
    venue_revision bigint not null,
    day_revision bigint not null,
    action text not null check (action in ('published', 'rollback')),
    actor text not null,
    occurred_at timestamptz not null default clock_timestamp()
);

create index if not exists cloud_venue_records_active_read_idx
    on public.cloud_venue_generation_records
    (dataset_family, race_date, venue_code, generation_id, record_key);
create index if not exists cloud_venue_generation_status_idx
    on public.cloud_venue_generations (dataset_family, race_date, status, venue_code);
create index if not exists cloud_venue_audit_lookup_idx
    on public.cloud_venue_publication_audit (dataset_family, race_date, venue_code, occurred_at desc);

alter table public.cloud_venue_generation_records enable row level security;
alter table public.cloud_venue_generations enable row level security;
alter table public.cloud_active_venue_generations enable row level security;
alter table public.cloud_active_day_indexes enable row level security;
alter table public.cloud_venue_publication_audit enable row level security;

revoke all on table public.cloud_venue_generation_records from public;
revoke all on table public.cloud_venue_generations from public;
revoke all on table public.cloud_active_venue_generations from public;
revoke all on table public.cloud_active_day_indexes from public;
revoke all on table public.cloud_venue_publication_audit from public;

create or replace function public.cloud_stage_venue_generation(
    p_dataset_family text,
    p_race_date date,
    p_venue_code text,
    p_venue_name text,
    p_generation_id text,
    p_expected_race_ids jsonb,
    p_manifest jsonb,
    p_manifest_hash text,
    p_records jsonb
) returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_expected_count integer;
    v_record_count integer;
    v_conflicts integer;
    v_existing public.cloud_venue_generations%rowtype;
begin
    if p_dataset_family <> 'racecard'
       or p_venue_code !~ '^[0-9]{2}$'
       or p_generation_id !~ '^[0-9a-f]{64}$'
       or p_manifest_hash !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(p_expected_race_ids) <> 'array'
       or jsonb_typeof(p_records) <> 'array'
       or jsonb_typeof(p_manifest) <> 'object'
       or btrim(p_venue_name) = '' then
        raise exception using errcode = '22023', message = 'invalid venue generation envelope';
    end if;
    v_expected_count := jsonb_array_length(p_expected_race_ids);
    v_record_count := jsonb_array_length(p_records);
    if v_expected_count < 1 or v_expected_count > 12 or v_record_count <> v_expected_count then
        raise exception using errcode = '22023', message = 'venue race count mismatch';
    end if;
    if p_manifest ->> 'status' <> 'complete'
       or (p_manifest ->> 'race_date')::date <> p_race_date
       or p_manifest ->> 'venue_code' <> p_venue_code
       or (p_manifest ->> 'expected_races')::integer <> v_expected_count
       or p_manifest -> 'race_ids' <> p_expected_race_ids
       or p_manifest #>> '{cloud_generation,generation_id}' <> p_generation_id then
        raise exception using errcode = '22023', message = 'venue manifest mismatch';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_dataset_family || '|' || p_race_date::text || '|' || p_venue_code || '|' || p_generation_id, 0)
    );

    create temporary table if not exists pg_temp.incoming_venue_records (
        record_type text,
        record_key text,
        payload jsonb,
        content_hash text,
        source_updated_at timestamptz,
        storage_hash text
    ) on commit drop;
    truncate pg_temp.incoming_venue_records;
    insert into pg_temp.incoming_venue_records
        (record_type, record_key, payload, content_hash, source_updated_at, storage_hash)
    select
        row.record_type,
        row.record_key,
        row.payload,
        row.content_hash,
        row.source_updated_at,
        pg_catalog.encode(extensions.digest(pg_catalog.convert_to(row.payload::text, 'UTF8'), 'sha256'), 'hex')
    from jsonb_to_recordset(p_records) as row(
        record_type text,
        record_key text,
        payload jsonb,
        content_hash text,
        source_updated_at timestamptz
    );

    if (select count(*) from pg_temp.incoming_venue_records) <> v_expected_count
       or (select count(distinct record_key) from pg_temp.incoming_venue_records) <> v_expected_count
       or exists (
           select 1 from pg_temp.incoming_venue_records r
           where r.record_type <> 'cloud_racecard'
              or r.record_key !~ '^[0-9]{12}$'
              or substring(r.record_key from 1 for 8) <> to_char(p_race_date, 'YYYYMMDD')
              or substring(r.record_key from 9 for 2) <> p_venue_code
              or r.content_hash !~ '^[0-9a-f]{64}$'
              or r.payload #>> '{race,race_id}' <> r.record_key
              or (r.payload #>> '{race,race_date}')::date <> p_race_date
              or r.payload #>> '{race,venue_code}' <> p_venue_code
              or r.payload #>> '{cloud_generation,generation_id}' <> p_generation_id
       )
       or exists (
           (select value #>> '{}' from jsonb_array_elements(p_expected_race_ids))
           except
           (select record_key from pg_temp.incoming_venue_records)
       )
       or exists (
           (select record_key from pg_temp.incoming_venue_records)
           except
           (select value #>> '{}' from jsonb_array_elements(p_expected_race_ids))
       ) then
        raise exception using errcode = '22023', message = 'invalid venue race set';
    end if;

    insert into public.cloud_venue_generation_records (
        dataset_family, race_date, venue_code, generation_id, record_type,
        record_key, payload, content_hash, storage_hash, source_updated_at
    )
    select p_dataset_family, p_race_date, p_venue_code, p_generation_id,
           record_type, record_key, payload, content_hash, storage_hash, source_updated_at
    from pg_temp.incoming_venue_records
    on conflict do nothing;

    select count(*) into v_conflicts
    from pg_temp.incoming_venue_records incoming
    join public.cloud_venue_generation_records stored
      on stored.dataset_family = p_dataset_family
     and stored.race_date = p_race_date
     and stored.venue_code = p_venue_code
     and stored.generation_id = p_generation_id
     and stored.record_key = incoming.record_key
    where stored.record_type <> incoming.record_type
       or stored.payload <> incoming.payload
       or stored.content_hash <> incoming.content_hash
       or stored.storage_hash <> incoming.storage_hash
       or stored.source_updated_at is distinct from incoming.source_updated_at;
    if v_conflicts > 0 then
        raise exception using errcode = '23505', message = 'generation_conflict';
    end if;

    insert into public.cloud_venue_generations (
        dataset_family, race_date, venue_code, venue_name, generation_id,
        expected_race_ids, expected_race_count, manifest, manifest_hash
    ) values (
        p_dataset_family, p_race_date, p_venue_code, p_venue_name, p_generation_id,
        p_expected_race_ids, v_expected_count, p_manifest, p_manifest_hash
    ) on conflict do nothing;

    select * into v_existing
    from public.cloud_venue_generations
    where dataset_family = p_dataset_family and race_date = p_race_date
      and venue_code = p_venue_code and generation_id = p_generation_id;
    if v_existing.expected_race_ids <> p_expected_race_ids
       or v_existing.expected_race_count <> v_expected_count
       or v_existing.manifest <> p_manifest
       or v_existing.manifest_hash <> p_manifest_hash
       or v_existing.venue_name <> p_venue_name then
        raise exception using errcode = '23505', message = 'generation_manifest_conflict';
    end if;
    return jsonb_build_object(
        'action', case when v_existing.staged_at < clock_timestamp() then 'staged_or_unchanged' else 'staged' end,
        'generation_id', p_generation_id,
        'race_count', v_expected_count
    );
end;
$$;

create or replace function public.cloud_publish_venue_generation(
    p_dataset_family text,
    p_race_date date,
    p_venue_code text,
    p_generation_id text,
    p_expected_revision bigint,
    p_actor text,
    p_action text default 'published'
) returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_generation public.cloud_venue_generations%rowtype;
    v_current public.cloud_active_venue_generations%rowtype;
    v_current_revision bigint := 0;
    v_previous_generation text;
    v_venue_revision bigint;
    v_day_revision bigint;
    v_day_payload jsonb;
    v_day_generation_id text;
    v_compat_index jsonb;
    v_compat_manifest jsonb;
    v_race_count integer;
    v_source_updated_at timestamptz;
begin
    if p_action not in ('published', 'rollback') or btrim(coalesce(p_actor, '')) = '' then
        raise exception using errcode = '22023', message = 'invalid publication audit identity';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_dataset_family || '|' || p_race_date::text, 0)
    );
    select * into v_generation
    from public.cloud_venue_generations
    where dataset_family = p_dataset_family and race_date = p_race_date
      and venue_code = p_venue_code and generation_id = p_generation_id
    for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'generation_not_staged';
    end if;
    if v_generation.manifest ->> 'status' <> 'complete' then
        raise exception using errcode = '23514', message = 'generation_not_complete';
    end if;
    if (
        select count(*) <> v_generation.expected_race_count
            or count(distinct record_key) <> v_generation.expected_race_count
        from public.cloud_venue_generation_records
        where dataset_family = p_dataset_family and race_date = p_race_date
          and venue_code = p_venue_code and generation_id = p_generation_id
    ) or exists (
        (select value #>> '{}' from jsonb_array_elements(v_generation.expected_race_ids))
        except
        (select record_key from public.cloud_venue_generation_records
         where dataset_family = p_dataset_family and race_date = p_race_date
           and venue_code = p_venue_code and generation_id = p_generation_id)
    ) or exists (
        select 1
        from public.cloud_venue_generation_records r
        where r.dataset_family = p_dataset_family and r.race_date = p_race_date
          and r.venue_code = p_venue_code and r.generation_id = p_generation_id
          and (
              r.storage_hash <> pg_catalog.encode(extensions.digest(pg_catalog.convert_to(r.payload::text, 'UTF8'), 'sha256'), 'hex')
              or jsonb_array_length(r.payload -> 'entries') < 5
              or jsonb_array_length(r.payload -> 'entries') > 9
              or (select count(*) from jsonb_array_elements(r.payload -> 'entries'))
                 <> (select count(distinct e ->> 'registration_no') from jsonb_array_elements(r.payload -> 'entries') e)
              or (select count(*) from jsonb_array_elements(r.payload -> 'entries'))
                 <> (select count(distinct e ->> 'bike_number') from jsonb_array_elements(r.payload -> 'entries') e)
          )
    ) then
        raise exception using errcode = '23514', message = 'generation_verification_failed';
    end if;

    select * into v_current
    from public.cloud_active_venue_generations
    where dataset_family = p_dataset_family and race_date = p_race_date and venue_code = p_venue_code
    for update;
    if found then
        v_current_revision := v_current.revision;
        v_previous_generation := v_current.generation_id;
        if v_current.generation_id = p_generation_id then
            return jsonb_build_object(
                'action', 'already_active', 'generation_id', p_generation_id,
                'venue_revision', v_current.revision,
                'day_revision', (select revision from public.cloud_active_day_indexes where dataset_family = p_dataset_family and race_date = p_race_date)
            );
        end if;
    end if;
    if p_expected_revision is null or p_expected_revision <> v_current_revision then
        raise exception using errcode = '40001', message = 'revision_conflict';
    end if;

    if v_previous_generation is not null and p_action <> 'rollback' and exists (
        select 1
        from public.cloud_venue_generation_records incoming
        join public.cloud_venue_generation_records current
          on current.dataset_family = incoming.dataset_family
         and current.race_date = incoming.race_date
         and current.venue_code = incoming.venue_code
         and current.record_key = incoming.record_key
         and current.generation_id = v_previous_generation
        where incoming.dataset_family = p_dataset_family and incoming.race_date = p_race_date
          and incoming.venue_code = p_venue_code and incoming.generation_id = p_generation_id
          and incoming.payload - 'cloud_generation' <> current.payload - 'cloud_generation'
          and (
              incoming.source_updated_at is null
              or current.source_updated_at is not null and incoming.source_updated_at <= current.source_updated_at
              or current.payload #>> '{race,line_state}' = 'fetched'
                 and incoming.payload #>> '{race,line_state}' <> 'fetched'
          )
    ) then
        raise exception using errcode = '23514', message = 'source_freshness_or_line_regression';
    end if;

    v_venue_revision := v_current_revision + 1;
    insert into public.cloud_active_venue_generations (
        dataset_family, race_date, venue_code, venue_name, generation_id, revision, activated_at
    ) values (
        p_dataset_family, p_race_date, p_venue_code, v_generation.venue_name,
        p_generation_id, v_venue_revision, clock_timestamp()
    ) on conflict (dataset_family, race_date, venue_code) do update set
        venue_name = excluded.venue_name,
        generation_id = excluded.generation_id,
        revision = excluded.revision,
        activated_at = excluded.activated_at;

    select jsonb_build_object(
        'schema_version', 2,
        'dataset_family', p_dataset_family,
        'race_date', p_race_date,
        'venues', coalesce(jsonb_agg(jsonb_build_object(
            'venue_code', pointer.venue_code,
            'venue_name', pointer.venue_name,
            'generation_id', pointer.generation_id,
            'venue_revision', pointer.revision,
            'race_ids', generation.expected_race_ids
        ) order by pointer.venue_code), '[]'::jsonb)
    ) into v_day_payload
    from public.cloud_active_venue_generations pointer
    join public.cloud_venue_generations generation
      on generation.dataset_family = pointer.dataset_family
     and generation.race_date = pointer.race_date
     and generation.venue_code = pointer.venue_code
     and generation.generation_id = pointer.generation_id
    where pointer.dataset_family = p_dataset_family and pointer.race_date = p_race_date;

    insert into public.cloud_active_day_indexes (dataset_family, race_date, revision, payload, updated_at)
    values (p_dataset_family, p_race_date, 1, v_day_payload, clock_timestamp())
    on conflict (dataset_family, race_date) do update set
        revision = public.cloud_active_day_indexes.revision + 1,
        payload = excluded.payload,
        updated_at = excluded.updated_at
    returning revision into v_day_revision;

    -- Compatibility projection for existing readers.  Every currently active
    -- venue is rewritten with one composite day generation in this same
    -- transaction, so readers of shared_snapshots never observe a mixed day.
    select pg_catalog.encode(extensions.digest(pg_catalog.convert_to(
        string_agg(pointer.venue_code || ':' || pointer.generation_id, '|' order by pointer.venue_code),
        'UTF8'
    ), 'sha256'), 'hex')
    into v_day_generation_id
    from public.cloud_active_venue_generations pointer
    where pointer.dataset_family = p_dataset_family and pointer.race_date = p_race_date;

    with active_records as (
        select record.record_key, record.payload, record.source_updated_at,
               pointer.generation_id as venue_generation_id
        from public.cloud_active_venue_generations pointer
        join public.cloud_venue_generation_records record
          on record.dataset_family = pointer.dataset_family
         and record.race_date = pointer.race_date
         and record.venue_code = pointer.venue_code
         and record.generation_id = pointer.generation_id
        where pointer.dataset_family = p_dataset_family and pointer.race_date = p_race_date
    ), compatibility as (
        select record_key,
               jsonb_set(payload, '{cloud_generation}', jsonb_build_object(
                   'generation_id', v_day_generation_id,
                   'race_date', p_race_date,
                   'generation_status', 'complete',
                   'publication_unit', 'venue',
                   'venue_generation_id', venue_generation_id
               ), true) as payload,
               source_updated_at
        from active_records
    ), hashed as (
        select record_key, payload, source_updated_at,
               pg_catalog.encode(extensions.digest(pg_catalog.convert_to(payload::text, 'UTF8'), 'sha256'), 'hex') as content_hash
        from compatibility
    )
    insert into public.shared_snapshots(record_type, record_key, payload, content_hash, source_updated_at, updated_at)
    select 'cloud_racecard', record_key, payload, content_hash, source_updated_at, clock_timestamp()
    from hashed
    on conflict (record_type, record_key) do update set
        payload = excluded.payload,
        content_hash = excluded.content_hash,
        source_updated_at = excluded.source_updated_at,
        updated_at = excluded.updated_at;

    select count(*), max(record.source_updated_at), jsonb_build_object(
        'schema_version', 2,
        'race_date', p_race_date,
        'items', coalesce(jsonb_agg(jsonb_build_object(
            'race_id', record.record_key,
            'race_date', record.payload #>> '{race,race_date}',
            'venue_code', record.payload #>> '{race,venue_code}',
            'venue_name', record.payload #>> '{race,venue_name}',
            'race_number', (record.payload #>> '{race,race_number}')::integer,
            'starters', (record.payload #>> '{race,starters}')::integer,
            'line_state', record.payload #>> '{race,line_state}',
            'source', 'cloud_racecard'
        ) order by record.record_key), '[]'::jsonb),
        'cloud_generation', jsonb_build_object(
            'generation_id', v_day_generation_id,
            'race_date', p_race_date,
            'generation_status', 'complete',
            'publication_unit', 'venue'
        )
    )
    into v_race_count, v_source_updated_at, v_compat_index
    from public.cloud_active_venue_generations pointer
    join public.cloud_venue_generation_records record
      on record.dataset_family = pointer.dataset_family
     and record.race_date = pointer.race_date
     and record.venue_code = pointer.venue_code
     and record.generation_id = pointer.generation_id
    where pointer.dataset_family = p_dataset_family and pointer.race_date = p_race_date;

    v_source_updated_at := coalesce(v_source_updated_at, clock_timestamp());
    v_compat_manifest := jsonb_build_object(
        'schema_version', 2,
        'dataset', 'cloud_acquisition_manifest',
        'race_date', p_race_date,
        'upstream_status', 'venue_atomic',
        'status', 'complete',
        'declared_races', v_race_count,
        'valid_races', v_race_count,
        'errors', '[]'::jsonb,
        'generation_hash', v_day_generation_id,
        'cloud_generation', jsonb_build_object(
            'generation_id', v_day_generation_id,
            'race_date', p_race_date,
            'generation_status', 'complete',
            'publication_unit', 'venue'
        )
    );

    insert into public.shared_snapshots(record_type, record_key, payload, content_hash, source_updated_at, updated_at)
    values (
        'cloud_races_index', p_race_date::text, v_compat_index,
        pg_catalog.encode(extensions.digest(pg_catalog.convert_to(v_compat_index::text, 'UTF8'), 'sha256'), 'hex'),
        v_source_updated_at, clock_timestamp()
    ), (
        'cloud_acquisition_manifest', p_race_date::text, v_compat_manifest,
        pg_catalog.encode(extensions.digest(pg_catalog.convert_to(v_compat_manifest::text, 'UTF8'), 'sha256'), 'hex'),
        v_source_updated_at, clock_timestamp()
    )
    on conflict (record_type, record_key) do update set
        payload = excluded.payload,
        content_hash = excluded.content_hash,
        source_updated_at = excluded.source_updated_at,
        updated_at = excluded.updated_at;

    update public.cloud_venue_generations
    set status = 'published', published_at = coalesce(published_at, clock_timestamp())
    where dataset_family = p_dataset_family and race_date = p_race_date
      and venue_code = p_venue_code and generation_id = p_generation_id;
    insert into public.cloud_venue_publication_audit (
        dataset_family, race_date, venue_code, generation_id, previous_generation_id,
        venue_revision, day_revision, action, actor
    ) values (
        p_dataset_family, p_race_date, p_venue_code, p_generation_id, v_previous_generation,
        v_venue_revision, v_day_revision, p_action, p_actor
    );
    return jsonb_build_object(
        'action', p_action, 'generation_id', p_generation_id,
        'previous_generation_id', v_previous_generation,
        'venue_revision', v_venue_revision, 'day_revision', v_day_revision
    );
end;
$$;

create or replace function public.cloud_read_active_day_index(p_dataset_family text, p_race_date date)
returns jsonb
language sql
stable
security invoker
set search_path = ''
as $$
    select jsonb_build_object('revision', revision, 'payload', payload, 'updated_at', updated_at)
    from public.cloud_active_day_indexes
    where dataset_family = p_dataset_family and race_date = p_race_date;
$$;

create or replace function public.cloud_read_active_venue_records(
    p_dataset_family text, p_race_date date, p_venue_code text
) returns setof public.cloud_venue_generation_records
language sql
stable
security invoker
set search_path = ''
as $$
    select record.*
    from public.cloud_active_venue_generations pointer
    join public.cloud_venue_generation_records record
      on record.dataset_family = pointer.dataset_family
     and record.race_date = pointer.race_date
     and record.venue_code = pointer.venue_code
     and record.generation_id = pointer.generation_id
    where pointer.dataset_family = p_dataset_family
      and pointer.race_date = p_race_date
      and pointer.venue_code = p_venue_code
    order by record.record_key;
$$;

revoke all on function public.cloud_stage_venue_generation(text,date,text,text,text,jsonb,jsonb,text,jsonb) from public;
revoke all on function public.cloud_publish_venue_generation(text,date,text,text,bigint,text,text) from public;
revoke all on function public.cloud_read_active_day_index(text,date) from public;
revoke all on function public.cloud_read_active_venue_records(text,date,text) from public;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant select, insert, update on public.cloud_venue_generation_records to service_role;
        grant select, insert, update on public.cloud_venue_generations to service_role;
        grant select, insert, update on public.cloud_active_venue_generations to service_role;
        grant select, insert, update on public.cloud_active_day_indexes to service_role;
        grant select, insert on public.cloud_venue_publication_audit to service_role;
        grant usage, select on sequence public.cloud_venue_publication_audit_audit_id_seq to service_role;
        grant execute on function public.cloud_stage_venue_generation(text,date,text,text,text,jsonb,jsonb,text,jsonb) to service_role;
        grant execute on function public.cloud_publish_venue_generation(text,date,text,text,bigint,text,text) to service_role;
        grant execute on function public.cloud_read_active_day_index(text,date) to service_role;
        grant execute on function public.cloud_read_active_venue_records(text,date,text) to service_role;
    end if;
end;
$$;

commit;
