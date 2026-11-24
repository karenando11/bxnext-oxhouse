"""Django settings for OxHouse Research.

All deployment-specific values come from environment variables (see
``config/envconfig.py`` and ``.env.example``). Nothing secret is committed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.envconfig import ConfigError, read_config

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    # A `manage.py <command>` run (migrations, batch jobs) may use the direct database address.
    _command = sys.argv[1] if Path(sys.argv[0]).name == "manage.py" and len(sys.argv) > 1 else None
    _config = read_config(os.environ, management_command=_command)
except ConfigError as exc:
    raise ImproperlyConfigured(str(exc)) from exc

SECRET_KEY = _config.secret_key
DEBUG = _config.debug
ALLOWED_HOSTS = _config.allowed_hosts

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps. See the architecture document for each one's responsibility.
    "apps.platform",
    "apps.companies",
    "apps.marketdata",
    "apps.sec",
    "apps.trials",
    "apps.scoring",
    "apps.screener",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": _config.database}

# Alert email (Step 17). With no recipients the alerts go to the error log only. Any SMTP mail
# service works; in development the default backend prints the message to the console.
ALERT_EMAIL_TO = [a.strip() for a in os.environ.get("ALERT_EMAIL_TO", "").split(",") if a.strip()]
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "alerts@localhost")
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND") or (
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").strip().lower() in ("1", "true", "yes")
EMAIL_TIMEOUT = 20

# Raw downloads: kept unchanged on local disk in development, never kept in production (only the
# parsed rows and each download's hash). RAW_STORAGE_BACKEND is "local" or "none"; the default
# follows DEBUG. Local disk is refused in production.
RAW_STORAGE_DIR = Path(os.environ.get("RAW_STORAGE_DIR") or BASE_DIR / "var" / "raw")
RAW_STORAGE_BACKEND = os.environ.get("RAW_STORAGE_BACKEND", "").strip().lower() or (
    "local" if DEBUG else "none"
)
if RAW_STORAGE_BACKEND not in ("local", "none"):
    raise ImproperlyConfigured(
        f"RAW_STORAGE_BACKEND must be 'local' or 'none', not {RAW_STORAGE_BACKEND!r}."
    )
if RAW_STORAGE_BACKEND == "local" and not DEBUG:
    raise ImproperlyConfigured(
        "Raw files must not be kept on local disk in production. Unset RAW_STORAGE_BACKEND "
        "(production keeps no raw files) or set it to 'none'."
    )
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# All timestamps are stored in UTC. Market dates use the US Eastern trading
# date and are handled explicitly where they are computed.
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

if not DEBUG:
    # Production runs behind a TLS-terminating reverse proxy (Caddy).
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
