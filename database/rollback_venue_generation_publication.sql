-- Production rollback keeps all generation/audit data for diagnosis.
-- Run only after the GitHub workflow has been switched back to the legacy
-- cloud_actions_rpc_publish.py path.

begin;

revoke execute on function public.cloud_stage_venue_generation(text,date,text,text,text,jsonb,jsonb,text,jsonb) from service_role;
revoke execute on function public.cloud_publish_venue_generation(text,date,text,text,bigint,text,text) from service_role;

-- Readers continue using the last complete compatibility projection in
-- shared_snapshots.  New venue writes are disabled, but no current or previous
-- generations are deleted.  A later approved recovery can re-grant EXECUTE.

commit;
