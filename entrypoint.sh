#!/bin/sh
set -eu
python manage.py wait_for_db --timeout "${DB_STARTUP_TIMEOUT:-60}"
python manage.py check --deploy --fail-level ERROR
python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Provision the initial admin deliberately through manage.py createsuperuser.
# Never swallow database errors or keep an admin password in the runtime image.
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
  --workers "${WEB_CONCURRENCY:-2}" --threads "${WEB_THREADS:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-90}" --access-logfile - --error-logfile -
