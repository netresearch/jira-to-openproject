# Rollback of a migration run

A migration run writes into the OpenProject target. To undo it, restore a snapshot that was taken right before the run. `scripts/openproject-snapshot.sh` takes the snapshot, checks that it restores, and compares the target with it after a restore ([ADR-015](adr/ADR-015-rollback-by-snapshot.md)).

A snapshot holds three parts:

- `openproject.dump`: the OpenProject database (`pg_dump`, custom format).
- `attachments.tar`: the attachment storage of the OpenProject container (`OPENPROJECT_ATTACHMENTS__STORAGE__PATH`).
- `counts.tsv`, `attachment-files.txt`, `attachment-path.txt` and `SHA256SUMS`: row counts of the tables a migration writes, the number of attachment files, the storage path and the checksums of all files.

Back up J2O's own data directory (`var/data`: the mappings and `written_values.sqlite` of [ADR-013](adr/ADR-013-content-rewrites-only-what-j2o-wrote.md)) at the same moment, because a restored target and a newer data directory do not fit together.

## Before each production run

Run these steps on the Docker host of the target while no migration and no import runs.

1. Set the containers:

   ```bash
   export J2O_SNAPSHOT_DB_CONTAINER=<postgres container>
   export J2O_SNAPSHOT_APP_CONTAINER=<openproject container>
   ```

2. Take the snapshot into a directory on encrypted disk storage. The script prints the snapshot directory.

   ```bash
   scripts/openproject-snapshot.sh take /srv/backup/j2o
   ```

   The snapshot holds every user's data. The script creates it with mode 600 and 700. Do not put it on a RAM disk such as `/tmp`.

3. Back up the J2O data directory next to it:

   ```bash
   tar -C var -cf <snapshot directory>/j2o-data.tar data
   ```

4. Prove that the snapshot restores:

   ```bash
   scripts/openproject-snapshot.sh drill <snapshot directory>
   ```

   The drill checks the checksums, restores the dump into a scratch database on the same PostgreSQL server, compares the row counts with `counts.tsv`, counts the files in the archive and drops the scratch database. The run may start only after `drill OK`.

## Restore the target

Restore only while no user works on the target, for example before the first access grant of the staged projects (ADR-005). The restore removes everything written since the snapshot, also by people.

1. Stop J2O. Stop every OpenProject process that uses the database: web, worker and cron.
2. Replace the database:

   ```bash
   docker exec "$J2O_SNAPSHOT_DB_CONTAINER" dropdb -U openproject --force openproject
   docker exec "$J2O_SNAPSHOT_DB_CONTAINER" createdb -U openproject -O openproject openproject
   docker exec -i "$J2O_SNAPSHOT_DB_CONTAINER" pg_restore -U openproject -d openproject --no-owner --exit-on-error \
     < <snapshot directory>/openproject.dump
   ```

   Use the database name and user of your target if they differ.

3. Replace the attachment storage. The OpenProject container is stopped, so use a helper container that mounts its volumes. `attachment-path.txt` names the storage path; the example uses the default.

   ```bash
   docker run --rm -i --volumes-from "$J2O_SNAPSHOT_APP_CONTAINER" alpine \
     sh -c 'rm -rf /var/openproject/assets/files && tar -C /var/openproject/assets -xf -' \
     < <snapshot directory>/attachments.tar
   ```

4. Restore the J2O data directory: move `var/data` aside and unpack `j2o-data.tar` into `var`.
5. Start OpenProject. Compare the target with the snapshot:

   ```bash
   scripts/openproject-snapshot.sh verify <snapshot directory>
   ```

   `verify OK` means that the table counts and the number of attachment files equal the snapshot.

## What was tested

On the OpenProject 17.9.7 pilot (55 MB database, 426 attachment files), `take`, `drill` and `verify` ran on 2026-11-22: the drill restored all 10 counted tables with equal counts and left no scratch database. A drill on a copy with one changed byte in the dump stopped at the checksum, and a copy with a changed count reported `drill FAILED`. The restore of the live target itself was not run on the pilot, because it stops the OpenProject container.
