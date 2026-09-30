#!/bin/sh
set -eu
umask 077
# One application instance: schema preparation completes before monitoring starts.
# Management commands remain usable without implicitly starting the web service.
if [ "${1:-}" = "gunicorn" ]; then
    python manage.py migrate --noinput
fi
exec "$@"
