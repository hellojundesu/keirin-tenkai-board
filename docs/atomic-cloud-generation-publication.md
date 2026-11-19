# Atomic cloud generation publication — implementation contract

Status: DESIGN / NOT DEPLOYED. No production database or workflow changes.

The original contract below describes the first day-level design.  The active
validation design now publishes independently per venue; see
`venue-generation-migration-runbook.md` and
`venue-generation-validation-result.md`.  The day-level pointer described
below is retained as incident history and must not be deployed as-is.

## Confirmed incident
On 2026-11-18, 79 racecards were present in generation A. A later run tried to move them to generation B. The existing `cloud_cas_write` RPC rejected 70 racecards as `stale_rejected` because `p_source_updated_at <= v_current.source_updated_at`. Nine Kochi cards moved to B; index/manifest remained on A. The in-place record update is not an atomic generation publication.

## Required data model
- Keep `shared_snapshots` as a legacy compatibility source during migration; do not mutate its 2026-11-18 records to repair the incident.
- Create a versioned table keyed by `(record_type, record_key, generation_id)`, with payload, canonical hash, source timestamp, staged timestamp and integrity constraints.
- Create a single active-generation pointer keyed by `(race_date, dataset_family)`, including generation ID and an incrementing revision. Never infer active generation from the most recently updated racecard.
- Stage complete, immutable generations. A retry with the same generation and same hashes is idempotent; conflicting bytes for an existing generation are rejected.
- Publish via a single PostgreSQL transaction/RPC: acquire per-day advisory lock; verify all expected race IDs, distinct registration numbers/bike numbers, hashes, complete statuses, matching generation IDs, index and manifest dependencies; compare expected active revision; update exactly one pointer. If verification fails, leave the pointer unchanged.
- Read clients must resolve the active pointer first and retrieve only records of that generation. Never combine generations. Support an explicit no-active-generation result.
- Treat `source_updated_at` as the timestamp of the source fact, not as a proxy for publication generation. Same fact with a new generation may be staged without falsely claiming a newer source fact. For actual fact differences, preserve existing source-freshness and line-regression guards.

## Migration and backup
1. Export a read-only, verifiable backup of existing `shared_snapshots`, `cloud_ingest_audit`, relevant schema/functions, and the active index/manifest, with checksums and restoration instructions. Never export credentials or tokens.
2. Inventory all readers and writers of `shared_snapshots`, including GitHub Pages, cloud racecards, comments, results, payout and external consumers.
3. Add versioned schema and staging RPC without changing the active read path.
4. Backfill a generation only when all records match a complete, consistent manifest/index. For the mixed 2026-11-18 incident, reconstruct the intended complete generation from a verified backup or acquisition artifact, not by labeling mixed records complete.
5. Dual-read comparison in a nonproduction environment. Cut over readers only after full coverage; preserve rollback to the old read path.
6. Enable production cutover only after explicit approval and a tested rollback.

## Test gates
- 79/79 success publishes once and reads 79 matching-generation cards.
- 0/79, 9/79, 70/79 and 78/79 staging never move the active pointer.
- Missing/duplicate race, inconsistent manifest/index, malformed identity, wrong date, mismatched canonical hash, stale source fact and line regression all reject safely.
- Network interruption, HTTP 400/403/429/500, timeouts, repeated requests, concurrent jobs, CAS revision conflicts and retries preserve the old active generation.
- Repeated identical generation is idempotent; differing payload for same generation ID is rejected.
- Readers during staging see the old complete generation; readers after commit see only the new complete generation.
- Simulate the 2026-11-18 mixed-generation incident and demonstrate safe recovery without in-place edits.
- Verify audit and structured error reporting; never discard gateway HTTP error response bodies without sanitization.

## Stop conditions
No live SQL DDL/DML, Edge Function deployment, workflow changes on default branch, job re-runs, production data changes or publication until backup verification, tests and user approval are complete.
