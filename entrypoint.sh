#!/bin/sh
set -eu
python manage.py wait_for_db --timeout "${DB_STARTUP_TIMEOUT:-60}"
python manage.py check --deploy --fail-level ERROR
python manage.py migrate --noinput
python manage.py provision_cache
python manage.py bootstrap_admin
python manage.py collectstatic --noinput
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
  --workers "${WEB_CONCURRENCY:-2}" --threads "${WEB_THREADS:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-90}" --access-logfile - --error-logfile -
