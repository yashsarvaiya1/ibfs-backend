# Release and recovery

The development branches are not deployed automatically. Back up and rehearse the migrations on a restored database before releasing. Keep the existing PostgreSQL 15 data volume; changing the major PostgreSQL image requires a separate upgrade plan.

## Configuration and first release

1. Copy the frontend `.env.example` to `.env` on the VM. Set private database credentials, a unique Django `SECRET_KEY`, real `ALLOWED_HOSTS`, and the public HTTPS origin in `CSRF_TRUSTED_ORIGINS`. Set `DEBUG=False`.
2. Browsers use `/api` and `/media` on the frontend origin. Set only the server-side `DJANGO_ORIGIN=http://backend:8000` for the frontend. Remove the old public API URL override. Session cookies require HTTPS in production; terminate TLS at the existing reverse proxy and preserve `X-Forwarded-Proto`.
3. Run `docker compose config --quiet` and build both images. The frontend uses Node 24 and a standalone Next server. The backend uses Python 3.14 and pinned requirements. Database and application health checks gate startup.
4. Existing media/static volumes may be owned by root. Before the first non-root backend release, grant UID/GID 1000 ownership of those two volumes. Do not change ownership of the PostgreSQL volume.
5. Run the backup script from the frontend Compose directory. Stop writes while taking the release snapshot, and retain the previous image tags and database dump.
6. Start the deployment. Startup waits for PostgreSQL, checks configuration, applies migrations and collects static files; any error stops the container. Create the initial administrator separately with `docker compose exec backend python manage.py createsuperuser`.
7. Verify login, a document edit, a payment allocation, stock movement and PDF download using a test contact. Existing web users sign in once again because legacy browser-stored passwords are removed.

New migrations add print settings, document stock ownership and payment allocation. Allocation backfill preserves cash and inventory. Historical vouchers or charge/discount payments without enough stored linkage require a manual allocation review; do not infer invoice matches from amounts alone.

## Scheduled maintenance and backups

Use the host scheduler, rather than assuming a cron daemon runs in the web container:

```cron
*/30 * * * * cd /path/to/frontend && docker compose exec -T backend python manage.py maintenance >> /var/log/ibfs-maintenance.log 2>&1
0 1 * * * cd /path/to/frontend && BACKUP_DIR=/encrypted-backups/ibfs ../backend/scripts/backup.sh >> /var/log/ibfs-backup.log 2>&1
```

Copy backups off the VM, protect them as financial data, and set a retention policy. Keep a separate encrypted copy of production configuration. Upload cleanup retains archived documents' files and stops if reference discovery fails.

## Restore rehearsal

Restore into a fresh isolated database and media volume. Never test restore against production:

```sh
docker compose exec -T db sh -c 'exec pg_restore --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /encrypted-backups/ibfs/db-TIMESTAMP.dump
docker compose exec -T backend tar -C /app -xzf - < /encrypted-backups/ibfs/media-TIMESTAMP.tgz
```

Use matching configuration, start the application, check counts, contact balances, payments, stock and saved document PDFs. A database rollback requires restoring the release backup; switching the code image alone does not reverse data migrations.

## Monitoring and secrets

Monitor `/health/` on Django and `/health` on Next, container restarts, disk usage and failed backup/maintenance jobs. Gunicorn logs go to container stdout/stderr. The login throttle uses the configured cache; use a shared cache for multiple backend replicas.

The previously tracked frontend `.vne` is excluded from tracking and build contexts. This does not remove Git history or rotate credentials. Rotate any exposed deployment credentials through the actual hosting provider, then replace the VM environment. No credential rotation, production migration, backup schedule installation or deployment is claimed as performed locally.

The installed production npm dependency graph passes `npm audit --omit=dev`. A development-only glob dependency in the Next ESLint configuration may still be reported without an upstream fix; avoid untrusted lint input, and review updates rather than forcing a Next.js downgrade.
