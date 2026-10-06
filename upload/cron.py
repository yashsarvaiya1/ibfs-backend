"""Conservative cleanup: no files are deleted if reference discovery fails."""
import logging
import time
from pathlib import Path
from django.conf import settings

logger=logging.getLogger(__name__)
ORPHAN_AGE_DAYS=7


def _get_all_referenced_paths():
    from inventory.models import Product
    from shared.models import Settings
    from accounting.models import Document
    paths=set(Product.objects.exclude(image_url__isnull=True).values_list('image_url',flat=True))
    for header,signature in Settings.objects.values_list('header_image','sign_image'):
        paths.update(path for path in (header,signature) if path)
    # Include archived documents: deleting a document must not destroy its evidence.
    for attachments in Document.objects.values_list('attachment_urls',flat=True).iterator(chunk_size=500):
        if not isinstance(attachments,list) or any(not isinstance(path,str) for path in attachments):
            raise ValueError('Invalid stored attachment paths; cleanup aborted.')
        paths.update(attachments)
    return {path.strip() for path in paths if path}


def cleanup_orphaned_uploads():
    try:
        referenced=_get_all_referenced_paths()
    except Exception:
        logger.exception('Upload cleanup aborted: could not read all references.')
        return
    root=Path(settings.MEDIA_ROOT).resolve()
    cutoff=time.time()-ORPHAN_AGE_DAYS*86400
    deleted=0
    for file in (root/'uploads').rglob('*'):
        if file.is_symlink() or not file.is_file() or file.stat().st_mtime>=cutoff:
            continue
        if file.relative_to(root).as_posix() not in referenced:
            file.unlink()
            deleted+=1
    logger.info('Upload cleanup removed %s old unreferenced files.',deleted)
