# accounting/cron.py
import logging
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings

logger = logging.getLogger(__name__)


def cleanup_temp_pdfs():
    """
    Deletes temp PDFs (bulk print / WhatsApp share) older than TEMP_PDF_TTL_MINUTES.
    Runs every 30 minutes via django-crontab (configured in settings.CRONJOBS).
    """
    temp_root = Path(getattr(settings, 'TEMP_PDF_ROOT', settings.MEDIA_ROOT / 'temp'))

    if not temp_root.exists():
        return

    ttl_minutes = getattr(settings, 'TEMP_PDF_TTL_MINUTES', 30)
    cutoff      = datetime.now() - timedelta(minutes=ttl_minutes)
    deleted     = 0

    for filepath in temp_root.rglob('*.pdf'):
        if not filepath.is_file():
            continue
        if datetime.fromtimestamp(filepath.stat().st_mtime) < cutoff:
            filepath.unlink()
            deleted += 1
            logger.info(f"IBFS temp cron: deleted → {filepath.name}")

    if deleted:
        logger.info(f"IBFS temp cron done — deleted {deleted} temp PDF(s)")
