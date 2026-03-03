# upload/utils.py
import io
import os
import uuid
from PIL import Image
from django.conf import settings

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
ALLOWED_PDF_TYPES   = {'application/pdf'}
ALLOWED_TYPES       = ALLOWED_IMAGE_TYPES | ALLOWED_PDF_TYPES

MAX_IMAGE_MB = int(os.getenv('UPLOAD_MAX_IMAGE_SIZE_MB', 10))
MAX_PDF_MB   = int(os.getenv('UPLOAD_MAX_PDF_SIZE_MB', 20))


def _validate(file):
    content_type = getattr(file, 'content_type', '')
    if content_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type: {content_type}. Allowed: JPEG, PNG, WEBP, PDF.")
    max_mb = MAX_IMAGE_MB if content_type in ALLOWED_IMAGE_TYPES else MAX_PDF_MB
    if file.size > max_mb * 1024 * 1024:
        raise ValueError(f"File too large. Max allowed: {max_mb}MB.")


def _unique_path(subfolder, ext):
    filename = uuid.uuid4().hex + ext
    return f"uploads/{subfolder}/{filename}"


def process_upload(file, subfolder='documents'):
    """
    Validates the file, compresses if image, returns (file_data: bytes, relative_path: str).
    relative_path is what gets stored in the DB (attachment_urls, image_url, etc.)
    """
    _validate(file)

    content_type = getattr(file, 'content_type', '')
    is_image = content_type in ALLOWED_IMAGE_TYPES

    if is_image:
        img = Image.open(file)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        buffer = io.BytesIO()
        quality = getattr(settings, 'UPLOAD_IMAGE_QUALITY', 75)
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        buffer.seek(0)
        file_data = buffer.read()
        relative_path = _unique_path(subfolder, '.jpg')
    else:
        # PDF — pass through untouched
        file_data = file.read()
        relative_path = _unique_path(subfolder, '.pdf')

    return file_data, relative_path
