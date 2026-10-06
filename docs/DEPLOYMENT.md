# Release and recovery

The development branches are not deployed automatically. Back up and rehearse the migrations on a restored database before releasing. Keep the existing PostgreSQL 15 data volume; changing the major PostgreSQL image requires a separate upgrade plan.

## Configuration and first release

1. Copy the frontend `.env.example` to `.env` on the VM. Set private database credentials, a unique Django `SECRET_KEY`, real `ALLOWED_HOSTS`, and the public HTTPS origin in `CSRF_TRUSTED_ORIGINS`. Set `DEBUG=False`.
2. Browsers use `/api` and `/media` on the frontend origin. Set only the server-side `DJANGO_ORIGIN=http://backend:8000` for the frontend. Remove the old public API URL override. Session cookies require HTTPS in production; terminate TLS at the existing reverse proxy and preserve `X-Forwarded-Proto`.
3. Run `docker compose config --quiet` and build both images. The frontend uses Node 24 and a standalone Next server. The backend uses Python 3.14 and pinned requirements. Database and application health checks gate startup.
4. Existing media/static volumes may be owned by root. Before the first non-root backend release, grant UID/GID 1000 ownership of those two volumes. Do not change ownership of the PostgreSQL volume.
5. Run the backup script from the frontend Compose directory. It stops the running app services during the database/media snapshot and restarts them in a finally block. Pause other writers and scheduled maintenance too. Retain the previous image tags and database dump.
6. Start the deployment. Startup waits for PostgreSQL, checks configuration, applies migrations, provisions the shared cache, runs optional first-admin setup and collects static files; any error stops the container. On an empty user table, set `DJANGO_SUPERUSER_USERNAME` and `DJANGO_SUPERUSER_PASSWORD` (optional `DJANGO_SUPERUSER_EMAIL`) in the private `.env` to create the first administrator automatically. Passwords use Django validation/hashing. Once any user exists, startup leaves all accounts and passwords unchanged. Leave all three fields blank for manual `docker compose exec backend python manage.py createsuperuser` instead.
7. Verify login, a document edit, a payment allocation, stock movement and PDF download using a test contact. Existing web users sign in once again because legacy browser-stored passwords are removed.

New migrations add print settings/template choice, optional document tax metadata, document stock ownership, payment allocation and paired transfer history. Migration 0010 adds default-compatible item-tax mode, supply classification and supplier invoice number; 0011 adds prospective document revisions. Existing documents remain in document-wide tax mode, and no filed-history records are fabricated. Allocation backfill preserves cash and inventory. Historical vouchers or charge/discount payments without enough stored linkage require a manual allocation review; do not infer invoice matches from amounts alone.

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

The local operations check used isolated PostgreSQL 15 Compose projects: backup, manifest verification, fresh database/media restore and accounting snapshots before/after migrations passed. A separate upgrade rehearsal restored the previous code image with a saved legacy bill and applied candidate migrations 0010/0011 without changing the accounting snapshot. Restore readiness waits for the final TCP server, avoiding the image’s temporary initialization server. Production data and volumes have not been checked.

The frontend's unpatched Next ESLint → fast-glob → micromatch → braces development chain was replaced with a private, narrow Node 24 glob adapter. The pinned Next plugin uses only `globSync` for root directories; the adapter rejects unsupported options. Clean `npm ci`, adapter/plugin integration checks, focused lint, TypeScript and the full audit passed with zero findings on 6 October 2026. The adapter is not a general replacement for fast-glob; recheck the plugin contract when upgrading Next.

## Build and push images

From the local frontend directory, replace the Docker Hub username below with your own account. Use a fresh release tag for later changes. These commands publish images; building code locally does not run them automatically.

```sh
IBFS_REGISTRY_USER=YOUR_DOCKERHUB_USERNAME
IBFS_IMAGE_TAG=2026-10-06-stock
docker login
docker buildx build --platform linux/amd64 -t "$IBFS_REGISTRY_USER/ibfs-backend:$IBFS_IMAGE_TAG" --push ../backend
docker buildx build --platform linux/amd64 -t "$IBFS_REGISTRY_USER/ibfs-frontend:$IBFS_IMAGE_TAG" --push .
```

Use `linux/arm64` for an ARM VM, or `linux/amd64,linux/arm64` to publish both architectures. Set the VM's private `.env` `BACKEND_IMAGE` and `FRONTEND_IMAGE` to those exact registry tags. After the release backup/rehearsal, run `docker compose pull backend frontend` and `docker compose up -d --no-build` on the VM. Keep the existing Compose project/volumes; never remove volumes for a code update.

## Initial administrator environment

The optional `DJANGO_SUPERUSER_*` fields belong only in the backend runtime environment, not Docker build arguments or the frontend container. Startup creates an admin only when the database has no users; it never resets passwords, promotes existing users or adds a different admin on restart. Partial/invalid first-start credentials fail with a clear error. Remove the password field after the initial successful setup if desired. Existing installations keep their current login; use `createsuperuser` to add an administrator manually or `changepassword USERNAME` for an intentional password change.

Images published before the bootstrap-admin change still require manual creation. Rebuild/push the backend image, then pull/recreate that service before expecting automatic first-start setup. No new migration or port change is needed.
