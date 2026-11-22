# ADR-015: A migration run is undone by restoring a snapshot

## Status

Accepted (2026-11-22)

## Context

`create_backup` and `restore_backup` in `src/migration.py` copy only local mapping files. Nothing restored the OpenProject database or its attachments, so a faulty production run could not be undone. The official OpenProject Jira importer records every entity it creates per run and offers a revert until the run is approved (`app/workers/import/jira_revert_import_job.rb` at v17.9.8).

J2O writes far more than it creates: it updates descriptions, fields, journals, memberships, versions and settings of existing records. A ledger of created IDs would not undo those updates, and a revert per kind of write would have to know the previous value of every attribute.

## Decision

A run is undone by restoring a snapshot taken right before it: a `pg_dump` of the database, an archive of the attachment storage, row counts and checksums (`scripts/openproject-snapshot.sh take`). Each snapshot is proven restorable before the run (`drill`: restore into a scratch database, compare counts). After a restore, `verify` compares the live target with the snapshot. The J2O data directory is backed up with it. `docs/operator-rollback.md` is the runbook.

A per-run ledger with a revert command is not built.

## Consequences

- A restore removes everything written after the snapshot, also by people. It fits the time before the first access grant (ADR-005), when only J2O writes.
- Each production run starts with `take` and `drill`; the drill needs space for one more copy of the database on the PostgreSQL server.
- The restore of the live target stops OpenProject; the drill does not.
