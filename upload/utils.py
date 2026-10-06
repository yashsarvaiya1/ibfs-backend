# upload/utils.py
import io
import os
import uuid
from PIL import Image, ImageOps, UnidentifiedImageError
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

    Bug B fix: apply EXIF orientation before saving so camera photos
    aren't stored rotated. ImageOps.exif_transpose() reads the EXIF
    Orientation tag and physically rotates/flips the pixel data, then
    strips the tag so no downstream tool mis-rotates it again.
    """
    _validate(file)

    content_type = getattr(file, 'content_type', '')
    is_image = content_type in ALLOWED_IMAGE_TYPES

    if is_image:
        try:
            img = Image.open(file)
            if img.width * img.height > 25_000_000:
                raise ValueError('Image resolution is too large. Use an image below 25 megapixels.')
            img = ImageOps.exif_transpose(img)
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('This image could not be read. Upload a valid PNG, JPEG or WebP.') from exc
        img.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if subfolder == 'settings':
            # Lossless branding keeps small text sharp and signatures transparent.
            img = img.convert('RGBA' if 'A' in img.getbands() or 'transparency' in img.info else 'RGB')
            img.save(buffer, format='PNG', optimize=True)
            extension = '.png'
        else:
            if img.mode != 'RGB':
                rgba = img.convert('RGBA')
                background = Image.new('RGB', rgba.size, 'white')
                background.paste(rgba, mask=rgba.getchannel('A'))
                img = background
            quality = getattr(settings, 'UPLOAD_IMAGE_QUALITY', 75)
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            extension = '.jpg'
        buffer.seek(0)
        file_data = buffer.read()
        relative_path = _unique_path(subfolder, extension)
    else:
        # PDF — pass through untouched
        file_data = file.read()
        if subfolder == 'settings':
            raise ValueError('Use a PNG, JPEG or WebP image for letterhead and signature.')
        if not file_data.startswith(b'%PDF-'):
            raise ValueError('This file is not a valid PDF.')
        relative_path = _unique_path(subfolder, '.pdf')

    return file_data, relative_path
