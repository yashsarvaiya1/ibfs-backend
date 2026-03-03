#!/bin/bash
# backend/entrypoint.sh

# Wait for database (simple sleep or use a wait-for-it script)
echo "Waiting for postgres..."
sleep 5

echo "Applying migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

# Create superuser if it doesn't exist
# Uses env vars: DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_PASSWORD, DJANGO_SUPERUSER_EMAIL
if [ "$DJANGO_SUPERUSER_USERNAME" ]; then
    echo "Creating superuser..."
    python manage.py createsuperuser --noinput || echo "Superuser already exists."
fi

# Optional: Add crontab if using django-crontab (needs cron service running)
# python manage.py crontab add

echo "Starting Gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000
