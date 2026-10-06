# Release and recovery

The development branches are not deployed automatically. Back up and rehearse the migrations on a restored database before releasing. Keep the existing PostgreSQL 15 data volume; changing the major PostgreSQL image requires a separate upgrade plan.

## Configuration and first release

1. Copy the frontend `.env.example` to `.env` on the VM. Set private database credentials, a unique Django `SECRET_KEY`, real `ALLOWED_HOSTS`, and the public HTTPS origin in `CSRF_TRUSTED_ORIGINS`. Set `DEBUG=False`.
2. Browsers use `/api` and `/media` on the frontend origin. Set only the server-side `DJANGO_ORIGIN=http://backend:8000` for the frontend. Remove the old public API URL override. Session cookies require HTTPS in production; terminate TLS at the existing reverse proxy and preserve `X-Forwarded-Proto`.
3. Run `docker compose config --quiet` and build both images. The frontend uses Node 24 and a standalone Next server. The backend uses Python 3.14 and pinned requirements. Database and application health checks gate startup.
4. Existing media/static volumes may be owned by root. Before the first non-root backend release, grant UID/GID 1000 ownership of those two volumes. Do not change ownership of the PostgreSQL volume.
5. Run the backup script from the frontend Compose directory. It stops the running app services during the database/media snapshot and restarts them in a finally block. Pause other writers and scheduled maintenance too. Retain the previous image tags and database dump.
6. Start the deployment. Startup waits for PostgreSQL, checks configuration, applies migrations and collects static files; any error stops the container. Create the initial administrator separately with `docker compose exec backend python manage.py createsuperuser`.
7. Verify login, a document edit, a payment allocation, stock movement and PDF download using a test contact. Existing web users sign in once again because legacy browser-stored passwords are removed.

New migrations add print settings/template choice, optional document tax metadata, document stock ownership, payment allocation and paired transfer history. Allocation backfill preserves cash and inventory. Historical vouchers or charge/discount payments without enough stored linkage require a manual allocation review; do not infer invoice matches from amounts alone.

## Shared cache

Production defaults to Django `DatabaseCache` (`ibfs_cache`). Startup creates the table after migrations, so login throttling shares data across Gunicorn workers without adding Redis. Existing `.env` files continue to work; the examples make these optional settings explicit. A custom cache backend is still supported. Local development defaults to process memory.

## Scheduled maintenance, backups and monitoring

The host needs Python 3 and Docker Compose. Continue running from the existing frontend Compose directory and keep its project name/volume names unchanged. `../backend/scripts/backup.sh` delegates to the standard-library operations tool. A backup contains a custom-format PostgreSQL dump, media archive, read-only accounting snapshot, image IDs and SHA-256 manifest. Only a completed, verified bundle is renamed to a timestamp directory; `.incomplete-*` directories never count as backups.

```sh
cd /path/to/ibfs/frontend
BACKUP_DIR=/encrypted-backups/ibfs ../backend/scripts/backup.sh
python3 ../backend/scripts/ops.py verify /encrypted-backups/ibfs/TIMESTAMP
python3 ../backend/scripts/ops.py rehearse /encrypted-backups/ibfs/TIMESTAMP --backend-image ibfs-backend:CANDIDATE
python3 ../backend/scripts/ops.py health --directory /encrypted-backups/ibfs
```

Rehearsal starts an isolated PostgreSQL container with temporary database storage and a uniquely named media volume. It restores using the recorded backend image, compares the saved accounting snapshot, applies candidate migrations, and compares again. Cleanup removes only resources created by that rehearsal. It does not mount production volumes or alter the live database. Snapshot differences fail the check and require review; intentional migrations may change allocations without changing cash. The old image must remain locally available, and rehearse the candidate image before release. Check saved PDFs and actual data manually in addition to the snapshot.

Use the host scheduler; no cron daemon is assumed inside the web container. Example (replace paths, arrange failure notifications, and do not overlap backup/maintenance):

```cron
*/30 * * * * cd /path/to/ibfs/frontend && docker compose exec -T backend python manage.py maintenance >> /var/log/ibfs-maintenance.log 2>&1
5 1 * * * cd /path/to/ibfs/frontend && BACKUP_DIR=/encrypted-backups/ibfs ../backend/scripts/backup.sh >> /var/log/ibfs-backup.log 2>&1
*/15 * * * * cd /path/to/ibfs/frontend && python3 ../backend/scripts/ops.py health --directory /encrypted-backups/ibfs >> /var/log/ibfs-health.log 2>&1
```

Health checks fail on unhealthy containers, backup age above 30 hours, invalid checksums, or backup disk usage at/above 85%. Thresholds are configurable. Send scheduler failures to your existing monitoring system; this code does not create a third-party monitoring account. For large backups, use a less frequent checksum verification schedule.

Backups are sensitive plaintext files with restrictive permissions, intended for encrypted host storage. Copy completed bundles to encrypted off-host storage using your existing method, keep configuration separately encrypted, retain multiple generations, and rehearse restores regularly. Retention/deletion and off-host credentials remain under your control; the tool does not silently delete old backups. Upload cleanup retains archived documents' files and stops if reference discovery fails.

For extra before/after release comparison (pause writes first):

```sh
docker compose exec -T backend python manage.py release_snapshot > before.json
# After migrations, take another snapshot and review the comparison.
docker compose exec -T backend python manage.py release_snapshot > after.json
diff before.json after.json
```

Database rollback requires a verified database/media backup. Switching a code image alone does not reverse data migrations. Actual production restore and migrations are deliberately outside this session at the user's request.

## Secrets and scope

The previously tracked frontend `.vne` is excluded from tracking and build contexts. This does not remove Git history or rotate credentials. Rotate exposed deployment credentials when you next maintain the actual VM, then replace its environment. No credential rotation, production migration, schedule installation or deployment was performed locally.

The local operations check used an isolated PostgreSQL 15 Compose project: backup, manifest verification, fresh database/media restore and accounting snapshots before/after migrations passed. Production data and volumes have not been checked.

The frontend's unpatched Next ESLint → fast-glob → micromatch → braces development chain was replaced with a private, narrow Node 24 glob adapter. The pinned Next plugin uses only `globSync` for root directories; the adapter rejects unsupported options. Clean `npm ci`, adapter/plugin integration checks, lint, TypeScript and the full audit passed with zero findings on 6 October 2026. The adapter is not a general replacement for fast-glob; recheck the plugin contract when upgrading Next.
