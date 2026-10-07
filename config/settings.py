"""
Django settings for LinkedClone.

Everything that differs between your laptop and the real server (secret key,
domain, database, email) is read from environment variables, normally from a
`.env` file next to manage.py. See .env.example for every setting. To change
any of it on a server: edit shared/.env, then `sudo systemctl restart
linkedclone`. No code change or redeploy is needed.
"""
import os
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# ENV_FILE lets tests (and odd setups) point at a different file, or at none.
load_dotenv(os.environ.get("ENV_FILE") or BASE_DIR / ".env")

TESTING = "test" in sys.argv


def env_bool(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Core / secrets
# ---------------------------------------------------------------------------
DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError(
            "DJANGO_SECRET_KEY is not set. Copy .env.example to .env and set it "
            "(the key generator command is in that file), or set DEBUG=True for local development."
        )

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")  # e.g. https://example.com

# ---------------------------------------------------------------------------
# Apps / middleware / templates
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "security",
    "accounts",
    "feed",
    "network",
    "messaging",
    "jobs",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    # Visitor counting/blocking runs before anything else touches the database.
    "security.middleware.MustChangePasswordMiddleware",
    "security.middleware.LastSeenMiddleware",
    "security.middleware.PageViewTrackingMiddleware",
]
MIDDLEWARE.insert(MIDDLEWARE.index("django.contrib.sessions.middleware.SessionMiddleware"),
                  "security.middleware.VisitorLoggingMiddleware")

if not (DEBUG or TESTING):  # runserver already serves static files in development
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "notifications.context_processors.unread",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database: PostgreSQL when DATABASE_URL is set, SQLite otherwise.
#   DATABASE_URL=postgres://USER:PASSWORD@localhost:5432/linkedclone
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    parsed = urllib.parse.urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": urllib.parse.unquote(parsed.username or ""),
            "PASSWORD": urllib.parse.unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH") or BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }

# ---------------------------------------------------------------------------
# Passwords / sessions
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "security.events.PasswordReuseValidator"},
]
# Argon2 is the strongest hasher Django supports; the others stay listed so
# existing passwords still verify (and are upgraded at the next sign-in).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
if TESTING:  # fast hashing so the suite runs in seconds
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14  # two weeks

# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static + media. Static files are served by WhiteNoise (and nginx in front);
# media (profile photos) is served by nginx straight from shared/media.
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").is_dir() else []
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if (DEBUG or TESTING)
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
# nginx runs as a different OS user and must be able to read uploads; a
# restrictive server umask would otherwise leave new folders untraversable.
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755
# Optional offline IP-location database (see `manage.py update_ip_location_db`).
IP_LOCATION_DB = os.environ.get("IP_LOCATION_DB") or str(BASE_DIR / "var" / "dbip-city-lite.mmdb")
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Rate limiting (login, signup, password reset). Counts live in the database
# so every gunicorn worker shares them; nginx passes the real client address.
# ---------------------------------------------------------------------------
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "ratelimit": {"BACKEND": "core.ratecache.DatabaseCounterCache"},
}
RATELIMIT_USE_CACHE = "ratelimit"
RATELIMIT_IP_META_KEY = "core.ip.client_ip"
SILENCED_SYSTEM_CHECKS = ["django_ratelimit.E003", "django_ratelimit.W001"]

# ---------------------------------------------------------------------------
# Security hardening (only enforced when DEBUG is off)
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "core.views.csrf_failure"
X_FRAME_OPTIONS = "DENY"

# HTTPS_ONLY=False is only for the short window on a new server between
# "site is up over http" and "certificate issued". Leave it True otherwise.
HTTPS_ONLY = env_bool("HTTPS_ONLY", True)

if not DEBUG and not TESTING:
    SECURE_SSL_REDIRECT = HTTPS_ONLY
    SESSION_COOKIE_SECURE = HTTPS_ONLY
    CSRF_COOKIE_SECURE = HTTPS_ONLY
    if HTTPS_ONLY:
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True
    # nginx terminates TLS and proxies over plain HTTP on localhost.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # The health check and local `curl` hit gunicorn over plain HTTP.
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]

# ---------------------------------------------------------------------------
# Email (password reset). With no EMAIL_HOST_USER set, messages are written
# to the service log instead of being sent, so nothing crashes before you
# have configured an email provider.
# ---------------------------------------------------------------------------
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "LinkedClone <no-reply@localhost>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST and EMAIL_HOST_USER
    else "django.core.mail.backends.console.EmailBackend"
)

# ---------------------------------------------------------------------------
# Logging: warnings and errors (including the traceback of any 500) go to
# stderr, which systemd keeps: `sudo journalctl -u linkedclone`.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "WARNING").upper()},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
