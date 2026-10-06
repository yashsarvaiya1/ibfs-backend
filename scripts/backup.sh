#!/bin/sh
set -eu
# Run from the frontend directory containing docker-compose.yml.
# Store BACKUP_DIR on a separate encrypted disk, and copy off the VM.
backup_root=${BACKUP_DIR:-./backups}
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
umask 077
mkdir -p "$backup_root"
docker compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_root/db-$backup_stamp.dump"
docker compose exec -T backend tar -C /app -czf - media > "$backup_root/media-$backup_stamp.tgz"
printf 'Database and media backup completed: %s\n' "$backup_stamp"
