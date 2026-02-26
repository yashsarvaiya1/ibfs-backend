# upload/cron.py
import logging
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

ORPHAN_AGE_DAYS = 7

# Every DB column that stores a media path — raw SQL to avoid circular imports
DB_PATH_QUERIES = [
    "SELECT image_url    FROM inventory_product  WHERE image_url IS NOT NULL",
    "SELECT header_image FROM shared_settings    WHERE header_image IS NOT NULL",
    "SELECT sign_image   FROM shared_settings    WHERE sign_image IS NOT NULL",
    # attachment_urls is a JSONField array — unnest each element
    "SELECT jsonb_array_elements_text(attachment_urls) FROM accounting_document "
    "WHERE attachment_urls IS NOT NULL AND attachment_urls != '[]'::jsonb",
]


def _get_all_referenced_paths():
    paths = set()
    with connection.cursor() as cursor:
        for query in DB_PATH_QUERIES:
            try:
                cursor.execute(query)
                for (val,) in cursor.fetchall():
                    if val:
                        paths.add(val.strip())
            except Exception as e:
                logger.warning(f"IBFS cron query failed: {e}")
    return paths


def cleanup_orphaned_uploads():
    referenced  = _get_all_referenced_paths()
    media_root  = Path(settings.MEDIA_ROOT)
    upload_root = media_root / 'uploads'

    if not upload_root.exists():
        return

    cutoff          = datetime.now() - timedelta(days=ORPHAN_AGE_DAYS)
    deleted, kept   = 0, 0

    for filepath in upload_root.rglob('*'):
        if not filepath.is_file():
            continue
        if datetime.fromtimestamp(filepath.stat().st_mtime) >= cutoff:
            kept += 1
            continue
        relative = str(filepath.relative_to(media_root))
        if relative not in referenced:
            filepath.unlink()
            deleted += 1
            logger.info(f"IBFS cron: deleted orphan → {relative}")
        else:
            kept += 1

    logger.info(f"IBFS cron done — deleted: {deleted}, kept: {kept}")
