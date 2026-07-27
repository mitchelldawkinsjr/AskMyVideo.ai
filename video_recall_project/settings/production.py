import os
from pathlib import Path

from .base import *

# Security Settings - Production defaults
DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes", "on")
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable must be set in production!")

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",")
# Filter out empty strings
ALLOWED_HOSTS = [h.strip() for h in ALLOWED_HOSTS if h.strip()]

# Allow private IPs for ALB health checks (10.x.x.x, 172.16-31.x.x, 192.168.x.x)
# This is safe because ALB validates requests before forwarding
if not ALLOWED_HOSTS or ALLOWED_HOSTS == [""]:
    raise ValueError("ALLOWED_HOSTS environment variable must be set in production!")

# Note: Private IPs for ALB health checks are handled by AllowPrivateIPsMiddleware
# This is safe because ALB validates requests before forwarding them

# Warn if DEBUG is enabled in production
if DEBUG:
    import logging

    logger = logging.getLogger(__name__)
    logger.warning(
        "DEBUG=True is enabled in production settings. This is a security risk!"
    )

# Database - Fallback to SQLite if DATABASE_URL not provided
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    try:
        import dj_database_url

        DATABASES = {"default": dj_database_url.parse(DATABASE_URL)}
    except Exception as e:
        # Fallback to SQLite if dj_database_url parsing fails
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }
else:
    # Default to SQLite if no DATABASE_URL provided
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Cache — dummy unless REDIS_URL is set and django-redis is installed
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    }
}
CELERY_BROKER_URL = None
CELERY_RESULT_BACKEND = None

# AWS S3 Storage (Optional)
if os.environ.get("USE_S3", "False").lower() == "true":
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "us-east-1")
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"
    AWS_DEFAULT_ACL = None
    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": "max-age=86400",
    }

    # Static files
    STATICFILES_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    STATIC_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/static/"

    # Media files
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/media/"
else:
    # Local file storage with WhiteNoise compression
    STATIC_URL = "/static/"
    STATIC_ROOT = BASE_DIR / "staticfiles"
    MEDIA_URL = "/media/"
    MEDIA_ROOT = BASE_DIR / "media"
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }

# Security Headers - Only enforce if behind HTTPS proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "False").lower() == "true"
# Only set secure cookies if using HTTPS
USE_HTTPS = os.environ.get("USE_HTTPS", "False").lower() == "true"
SESSION_COOKIE_SECURE = USE_HTTPS
CSRF_COOKIE_SECURE = USE_HTTPS
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Session Cookie Settings - Critical for ALB/proxy setups
# When behind ALB, cookies need special handling to work properly
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"  # Allow cookies to work behind ALB
SESSION_COOKIE_AGE = 86400  # 24 hours
# Don't set SESSION_COOKIE_DOMAIN - let it default to None for ALB compatibility

# CSRF Cookie Settings
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
# CSRF trusted origins for ALB
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Login redirect - should go to library, not home
LOGIN_REDIRECT_URL = "/library/"

# CORS Settings - Production should be restrictive
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if not CORS_ALLOWED_ORIGINS:
    import logging

    logger = logging.getLogger(__name__)
    logger.warning("CORS_ALLOWED_ORIGINS is empty. CORS will be very restrictive.")

# Enhanced Logging Configuration
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S UTC",
        },
        # JSON formatter (only if pythonjsonlogger is installed)
        # 'json': {
        #     '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
        #     'format': '%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d',
        # },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "stream": "ext://sys.stdout",
        },
        "error_console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "stream": "ext://sys.stderr",
            "level": "ERROR",
        },
    },
    "root": {
        "handlers": ["console", "error_console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console", "error_console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "error_console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console", "error_console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "video_processor": {
            "handlers": ["console", "error_console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "gunicorn": {
            "handlers": ["console", "error_console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}

# Use JSON logging if requested (for CloudWatch Logs Insights)
if os.environ.get("USE_JSON_LOGGING", "False").lower() == "true":
    try:
        import json_logging

        LOGGING["formatters"]["json"] = {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
        }
        for handler in LOGGING["handlers"].values():
            handler["formatter"] = "json"
    except ImportError:
        pass  # JSON logging not available, use default

# Performance
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024 * 1024  # 1GB
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024 * 1024  # 1GB

# Email (for notifications)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = os.environ.get("EMAIL_PORT", 587)
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@yourdomain.com")

# Monitoring (optional)
if os.environ.get("SENTRY_DSN"):
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=os.environ.get("SENTRY_DSN"),
        integrations=[
            DjangoIntegration(),
        ],
        traces_sample_rate=0.1,
        send_default_pii=True,
    )
