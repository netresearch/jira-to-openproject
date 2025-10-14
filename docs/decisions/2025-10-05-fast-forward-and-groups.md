# 2025-10-05 — Fast-Forward Checkpoints & Group Sync Enhancements

## Context

- Work package fast-forward runs now rely on `.migration_checkpoints.db`.
- Group synchronization recently gained persisted mappings and project-role hydration.
- CI/dev feedback highlighted libffi dependency gaps on host environments.

## Decisions

1. **Container-first test flow**  
   Added `make container-test` plus documentation guidance so developers execute pytest inside the Docker test profile. This guarantees native dependencies (`libffi`, OpenSSL tooling, etc.) without polluting the host.

2. **Checkpoint hygiene tooling**  
   Introduced a CLI flag (`--reset-wp-checkpoints`) which deletes/rotates the checkpoint store before a run. The migration now detects and resets corrupted SQLite files automatically, preventing hard failures after rehearsal restores.

3. **Group mapping persistence**  
   Group migration saves/reloads `group_mapping.json` under the component data directory, enabling follow-on components to reuse the mapping in subsequent executions.

4. **Avatar/cache observability**  
   Avatar backfills now cache digests on disk; helper tests guarantee digest mismatches trigger uploads while matches are skipped.

## Follow-up

- Ensure container test target is wired into CI once GitHub Actions (or alternative) is provisioned.
- Add broader integration rehearsals that exercise the new checkpoint lifecycle end-to-end.
