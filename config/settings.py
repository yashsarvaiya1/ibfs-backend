# config/settings.py
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'fallback-dev-secret-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

CSRF_TRUSTED_ORIGINS = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:4000,http://127.0.0.1:4000'
).split(',')

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'django_extensions',
    'django_crontab',
    'shared',
    'accounting',
    'inventory',
    'upload',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

WHITENOISE_INDEX_FILE = True
ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'ibfs_db'),
        'USER': os.getenv('DB_USER', 'postgres'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

UPLOAD_IMAGE_QUALITY     = int(os.getenv('UPLOAD_IMAGE_QUALITY', 75))
UPLOAD_MAX_IMAGE_SIZE_MB = int(os.getenv('UPLOAD_MAX_IMAGE_SIZE_MB', 10))
UPLOAD_MAX_PDF_SIZE_MB   = int(os.getenv('UPLOAD_MAX_PDF_SIZE_MB', 20))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Pagination ────────────────────────────────────────────────────────────────
# Custom class lives in shared/pagination.py
# Supports ?page_size=20|50|100 query param (GD-03)
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'shared.pagination.IBFSPageNumberPagination',
    'PAGE_SIZE': int(os.getenv('PAGE_SIZE', 20)),
}

CORS_ALLOWED_ORIGINS = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:3000,http://127.0.0.1:3000'
).split(',')
CORS_ALLOW_CREDENTIALS = True

# ── Cron Jobs ─────────────────────────────────────────────────────────────────
# accounting.cron.cleanup_temp_pdfs: cleans up bulk/WhatsApp temp PDFs every 30 min (DV-04)
CRONJOBS = [
    ('0 2 * * *',   'upload.cron.cleanup_orphaned_uploads'),
    ('*/30 * * * *', 'accounting.cron.cleanup_temp_pdfs'),
]

CACHES = {
    'default': {
        'BACKEND': os.getenv(
            'CACHE_BACKEND',
            'django.core.cache.backends.locmem.LocMemCache',
        ),
        'LOCATION': os.getenv('CACHE_LOCATION', 'ibfs-cache'),
    }
}

# ── Playwright PDF ─────────────────────────────────────────────────────────────
PLAYWRIGHT_PDF_TIMEOUT = int(os.getenv('PLAYWRIGHT_PDF_TIMEOUT', '30000'))
PLAYWRIGHT_PDF_FORMAT  = os.getenv('PLAYWRIGHT_PDF_FORMAT', 'A4')

# ── Temp PDF Storage (bulk print / WhatsApp share) ────────────────────────────
# Files placed here are auto-deleted by accounting.cron.cleanup_temp_pdfs after TTL (DV-04)
TEMP_PDF_ROOT        = BASE_DIR / 'media' / 'temp'
TEMP_PDF_TTL_MINUTES = int(os.getenv('TEMP_PDF_TTL_MINUTES', 30))

MEDIA_BASE_URL = os.getenv('MEDIA_BASE_URL', 'http://localhost:8000')
