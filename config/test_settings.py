import os

os.environ.setdefault("DJANGO_SECRET_KEY", "tests-only-" * 8)
from .settings import *  # noqa: F403, E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SECURE_SSL_REDIRECT = False
# Tests opt into autonomous behavior explicitly, independent of the operator's .env.
MODEM_MONITOR_ENABLED = False
MODEM_AUTO_RECONNECT_ENABLED = False
MODEM_SESSION_IDLE_TIMEOUT = 240
