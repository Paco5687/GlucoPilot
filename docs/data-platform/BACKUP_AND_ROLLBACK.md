# Backup, upgrade, rollback, and recovery

This is the operator runbook for any release containing database migrations.
Backups contain health records and credentials. Store them encrypted, restrict
access, and never commit or attach them to an issue.

## What the backup guarantees

`python -m server.backup create`:

1. opens the source database read-only and runs `PRAGMA integrity_check`;
2. checks destination free space before copying;
3. uses SQLite's online backup API, which includes committed WAL data;
4. copies every regular file under `/data/records` and rejects symlinks;
5. records SHA-256 hashes, sizes, entity counts, and migration metadata in a
   value-free checksummed manifest;
6. fails if record files change during the copy or a database record references
   a missing uploaded file; and
7. restores into a temporary clean data directory and rechecks the database,
   counts, migrations, file hashes, and record references before publishing the
   backup directory.

Application startup performs this verified backup automatically before pending
migrations touch an existing database. The default destination is
`/data/backups`; set `MIGRATION_BACKUP_DIR` to a separately mounted path for
off-volume protection. Clean installs and restarts with no pending migrations
do not create a backup.

## Manual backup

Create a host directory with private permissions, then run the command from the
deployment checkout. Pulling the candidate image does not restart the service.

```bash
mkdir -p backups
chmod 700 backups
docker compose pull glucopilot
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backup" \
  glucopilot python -m server.backup create \
  --data-dir /data --backup-root /backup --reason pre-upgrade
```

The command prints the new backup directory and a metadata-only verification
summary. Verify it again at any time:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backup:ro" \
  glucopilot python -m server.backup verify /backup/BACKUP_DIRECTORY
```

Copy at least one verified backup to encrypted storage outside the application
volume and host before a destructive migration.

## Upgrade procedure

1. Record the current image tag or digest and `docker compose ps` output.
2. Create and verify the manual off-volume backup above.
3. Keep the existing data volume; do not extract archives over it.
4. Start the candidate image with `docker compose up -d`.
5. Inspect `docker compose logs glucopilot`. Migration failures prevent the app
   and scheduler from starting and include the failed version/name.
6. Confirm `/healthz`, login, Dashboard, Records, uploaded-file opening, and
   connected-source status.
7. Confirm expected versions in the logs/verification evidence and retain the
   prior image plus backup for at least two verified release cycles.

## Failure and rollback

Prefer a read/code rollback over schema reversal:

- If a typed-read feature fails, disable its read feature switch and keep dual
  writes running when safe.
- If a feature fails after an additive migration commits, first disable its
  read/write switches while keeping the migration-compatible image. The
  migration runner intentionally rejects an older image against a newer schema
  ledger. To roll the image back, restore the verified pre-migration backup to
  a new volume and attach that volume to the recorded older image; do not
  delete migration-ledger rows or manually reverse DDL.
- If preflight or backup fails, no migration runs. Correct integrity, missing
  file, permissions, or free-space errors—or redeploy the prior image.
- If a migration statement fails, its entire startup transaction rolls back.
  Preserve logs and the failed volume before retrying.
- Perform schema/data restore only for confirmed corruption, data loss, or an
  explicitly documented non-additive migration rollback.

## Restore into a new Docker volume

Never overwrite the current volume. Restore into a new empty volume, verify it,
then switch `DATA_VOLUME`; the old volume remains the fastest rollback.

```bash
docker volume create glucopilot_restore_YYYYMMDD

docker compose run --rm --no-deps \
  -v "$PWD/backups:/backup:ro" \
  -v glucopilot_restore_YYYYMMDD:/restore \
  glucopilot python -m server.backup restore \
  /backup/BACKUP_DIRECTORY /restore
```

Verify the restored volume before attaching it:

```bash
docker run --rm \
  -v glucopilot_restore_YYYYMMDD:/data:ro \
  ghcr.io/paco5687/glucopilot:IMAGE_TAG \
  python -m server.data_audit /data/app.sqlite3
```

Set `DATA_VOLUME=glucopilot_restore_YYYYMMDD`, run `docker compose up -d`, and
repeat the health/login/Dashboard/Records/file checks. Do not delete the failed
or old volume until the restored deployment has passed an observation period.

## Recovery cautions

- Never copy only `app.sqlite3` from a running WAL database.
- Never restore into a nonempty directory or volume.
- Never edit `schema_migrations` to bypass checksum, gap, or drift failures.
- Never publish a manifest or backup: the manifest is value-free, but the
  adjacent database and record files contain private data and secrets.
