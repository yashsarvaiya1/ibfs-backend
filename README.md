# IBFS backend

Django REST backend for IBFS billing, simplified accounting, inventory and authenticated PDF generation. The sibling `../frontend` repository contains the browser/PWA workspace and the VM Docker Compose configuration.

## Local development

Use Python **3.14**, PostgreSQL and the pinned requirements (Django 6.0.8, Django REST Framework 3.18.1). Create a dedicated local database/user, then:

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Edit the private `.env`: use `DEBUG=True`, a unique `SECRET_KEY`, your local database credentials, `DB_HOST=127.0.0.1`, `DB_PORT=5432`, and `ALLOWED_HOSTS=localhost,127.0.0.1`. Set `CSRF_TRUSTED_ORIGINS=http://localhost:4000` and `CORS_ALLOWED_ORIGINS=http://localhost:4000` to match the frontend browser origin. For local development set `CACHE_BACKEND=django.core.cache.backends.locmem.LocMemCache`; production defaults to a shared database cache.

```sh
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

The test database user needs permission to create a separate test database. Use isolated development data:

```sh
.venv/bin/python manage.py test --noinput
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python -m unittest discover -s scripts/tests
```

## Accounting contract

Quotation/PO/PI are preparatory stages. Bills/invoices establish obligations, actual payments settle them through allocations, and transfers have paired ledger legs. Automatic payment/stock handling remains optional. Challans own inventory when enabled. Decimal calculations and atomic mutations protect money/stock; edits retain real payments and the original automation mode. Full double-entry journals and multi-company tenancy remain deferred.

Document-wide tax remains the default. Optional item rates support mixed-tax documents without extra required stages. Explicit supply classifications and supplier invoice numbers improve review but are not guessed from historical data. Saved document versions start with new creates/edits; the first tracked edit of an older document records its known baseline. These are book revisions, not filed-return snapshots. Archived documents remain readable/printable and cannot be reposted by editing.

## VM deployment and documentation

Keep the existing frontend Compose project and private `.env`, including its PostgreSQL 15 data volume. The production image installs Chromium, runs as a non-root user, validates configuration and runs migrations/cache provisioning/static collection before starting Gunicorn. Database-major upgrades are a separate operation.

- [Deployment, backup, isolated restore and health tools](docs/DEPLOYMENT.md)
- [CA PDFs, GST/HSN exports, allocation review and read-only CSV comparisons](docs/REPORTS.md)
- [Classic/Modern PDF pagination and saved-data contract](docs/PDF_LAYOUT.md)
- [Encrypted offline files and local drafts](docs/OFFLINE.md)
- [Completed work, deferred scope and VM handoff](docs/IMPROVEMENT_CHECKLIST.md)

Actual production migration, credential rotation, backups and deployment were skipped at the user's request. No GST filing, third-party validation, IRN generation or bank-feed integration is included.
