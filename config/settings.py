# config/settings.py
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'fallback-dev-secret-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

CSRF_TRUSTED_ORIGINS = os.getenv(
    'CSRF_TRUSTED_ORIGINS',
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
    'shared.http.PrivateResponseMiddleware',
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
        'OPTIONS': {'connect_timeout':5},
        'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE','60')),
        'CONN_HEALTH_CHECKS': True,
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
        'rest_framework.authentication.SessionAuthentication',
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
    'DEFAULT_THROTTLE_RATES': {'login':'10/min'},
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
            'django.core.cache.backends.locmem.LocMemCache' if DEBUG else 'django.core.cache.backends.db.DatabaseCache',
        ),
        'LOCATION': os.getenv('CACHE_LOCATION', 'ibfs-cache' if DEBUG else 'ibfs_cache'),
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

# Browser sessions never expose passwords to JavaScript storage.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

if not DEBUG:
    from django.core.exceptions import ImproperlyConfigured
    if len(SECRET_KEY) < 50 or SECRET_KEY.startswith(('fallback-', 'replace-')):
        raise ImproperlyConfigured('Set SECRET_KEY before starting production.')
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_REDIRECT_EXEMPT = [r'^health/?$']
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS','31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
