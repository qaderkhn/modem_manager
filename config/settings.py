import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(f"{name} must be between {minimum} and {maximum}")
    return value


def boolean(name, default=False):
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ImproperlyConfigured(f"{name} must be true or false")
    return value == "true"


def csv(name, default=""):
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


DEBUG = boolean("DJANGO_DEBUG")
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if len(SECRET_KEY) < 50 or SECRET_KEY == "change-me":
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a random value of at least 50 characters")

ALLOWED_HOSTS = csv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "modem",
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
if os.getenv("POSTGRES_DB"):
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "require")},
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation." + name}
    for name in [
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    ]
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "app_login"
LOGIN_REDIRECT_URL = "modem:dashboard"
LOGOUT_REDIRECT_URL = "app_login"

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True

CSRF_COOKIE_HTTPONLY = False
CSRF_TRUSTED_ORIGINS = csv("DJANGO_CSRF_TRUSTED_ORIGINS")

SESSION_COOKIE_SAMESITE = CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 3600
SECURE_SSL_REDIRECT = boolean("DJANGO_SECURE_SSL_REDIRECT")
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = boolean("DJANGO_HSTS_INCLUDE_SUBDOMAINS")
SECURE_HSTS_PRELOAD = boolean("DJANGO_HSTS_PRELOAD")
if boolean("DJANGO_TRUST_PROXY_HTTPS"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

MODEM_BASE_URL = os.getenv("MODEM_BASE_URL", "http://192.168.1.1").rstrip("/")
u = urlsplit(MODEM_BASE_URL)
if (
    u.scheme not in {"http", "https"}
    or not u.hostname
    or u.username
    or u.password
    or u.query
    or u.fragment
    or u.path
):
    raise ImproperlyConfigured(
        "MODEM_BASE_URL must be an HTTP(S) origin without credentials or paths"
    )
MODEM_VERIFY_TLS = os.getenv("MODEM_CA_BUNDLE") or boolean("MODEM_VERIFY_TLS", True)
MODEM_TIMEOUT = float(os.getenv("MODEM_TIMEOUT", "10"))
if not 0 < MODEM_TIMEOUT <= 60:
    raise ImproperlyConfigured("MODEM_TIMEOUT must be greater than zero and at most 60 seconds")
MODEM_LOGIN_LIMIT = 5
MODEM_LOGIN_WINDOW = 300
MODEM_RECONNECT_COOLDOWN = 30

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"events": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "events"}},
    "loggers": {"modem": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}

MODEM_SESSION_IDLE_TIMEOUT = bounded_int("MODEM_SESSION_IDLE_TIMEOUT", 240, 60, 240)

MODEM_MONITOR_ENABLED = boolean("MODEM_MONITOR_ENABLED")
MODEM_MONITOR_INTERVAL = bounded_int("MODEM_MONITOR_INTERVAL", 30, 10, 3600)
MODEM_HEALTH_FAILURE_THRESHOLD = bounded_int("MODEM_HEALTH_FAILURE_THRESHOLD", 3, 2, 100)
MODEM_AUTO_RECONNECT_ENABLED = boolean("MODEM_AUTO_RECONNECT_ENABLED")
MODEM_AUTO_RECONNECT_COOLDOWN = bounded_int("MODEM_AUTO_RECONNECT_COOLDOWN", 300, 60, 86400)
MODEM_AUTO_RECONNECT_MAX_ATTEMPTS = bounded_int("MODEM_AUTO_RECONNECT_MAX_ATTEMPTS", 3, 1, 10)
MODEM_RECOVERY_VERIFY_DELAY = bounded_int("MODEM_RECOVERY_VERIFY_DELAY", 30, 30, 3600)
MODEM_AUTH_BACKOFF = bounded_int("MODEM_AUTH_BACKOFF", 300, 60, 3600)
MODEM_USERNAME_FILE = os.getenv("MODEM_USERNAME_FILE", "")
MODEM_PASSWORD_FILE = os.getenv("MODEM_PASSWORD_FILE", "")
try:
    MODEM_LTE_UNAVAILABLE_STATUSES = tuple(
        int(value) for value in csv("MODEM_LTE_UNAVAILABLE_STATUSES")
    )
except ValueError:
    raise ImproperlyConfigured("LTE unavailable statuses must be integers") from None
if any(value == 1 or not 0 <= value <= 65535 for value in MODEM_LTE_UNAVAILABLE_STATUSES):
    raise ImproperlyConfigured("LTE unavailable statuses must exclude healthy status 1")
if MODEM_AUTO_RECONNECT_ENABLED and not MODEM_MONITOR_ENABLED:
    raise ImproperlyConfigured("Automatic recovery requires monitoring")
if (
    os.getenv("DJANGO_SQLITE_PATH")
    and DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
):
    DATABASES["default"]["NAME"] = os.environ["DJANGO_SQLITE_PATH"]
