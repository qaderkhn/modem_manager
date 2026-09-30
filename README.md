# modem_manager

A Django web app that monitors LTE service on the **Huawei HUAWEI 5G Outdoor CPE N5368X** and can request a reconnect when the modem reports an LTE outage. It also provides manual login and reconnect controls.

The client implements the N5368X's web API and authentication protocol. Other Huawei devices and firmware variants are not established as compatible. It checks modem-reported LTE service, not Internet or DNS reachability; it cannot repair an upstream outage while the modem still reports healthy service. Automatic reconnect is disabled by default.

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/), the modem's login credentials, and network access from the application host to the modem. The supplied Gunicorn and Docker deployment targets Linux.

From the checkout:

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
uv run python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Put the generated value in `DJANGO_SECRET_KEY` in `.env`. Set `MODEM_BASE_URL` to the modem's HTTP(S) origin, with no path or embedded credentials. The default `http://192.168.1.1` is an example LAN address; change it for your network. For local HTTP development, set `DJANGO_DEBUG=true`.

```bash
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>, sign in with your application account, then choose **Login to modem**. Every authenticated application user can operate the modem; only create accounts for trusted operators. Application and modem credentials are separate.

For pip installations, use a Python 3.12 virtual environment and `pip install --require-hashes -r requirements.txt`, then run the same `python manage.py` commands without `uv run`.

## Configuration

[.env.example](.env.example) lists the settings. Environment variables override `.env`. Keep `.env`, credential files, databases and logs private.

| Setting                                      | Purpose                                                                               |
| -------------------------------------------- | ------------------------------------------------------------------------------------- |
| `DJANGO_SECRET_KEY`                          | Required random application secret, at least 50 characters                            |
| `DJANGO_DEBUG`                               | Default `false`; use `true` only for local development                                |
| `DJANGO_ALLOWED_HOSTS`                       | Comma-separated application hostnames; retain `127.0.0.1` for container health checks |
| `MODEM_BASE_URL`                             | Modem HTTP(S) origin reachable from the server/container                              |
| `MODEM_TIMEOUT`                              | Per-request connect/read inactivity timeout, default 10 seconds, maximum 60           |
| `MODEM_CA_BUNDLE`                            | Optional trusted PEM CA file for modem HTTPS; TLS verification defaults on            |
| `MODEM_MONITOR_ENABLED`                      | Start background monitoring under the supplied Gunicorn configuration                 |
| `MODEM_AUTO_RECONNECT_ENABLED`               | Allow automatic recovery; requires monitoring                                         |
| `MODEM_LTE_UNAVAILABLE_STATUSES`             | Confirmed outage statuses; example uses `0`, empty disables outage detection          |
| `MODEM_USERNAME_FILE`, `MODEM_PASSWORD_FILE` | Protected UTF-8 files containing one credential each, required for autonomous login   |

SQLite is the default. `DJANGO_SQLITE_PATH` changes its location. To use PostgreSQL, set the `POSTGRES_*` variables shown in the example and run migrations. Changing databases does not transfer existing accounts or history.

## Continuous monitoring

`runserver` does not start the monitor. Create protected credential files outside the checkout, readable only by the service account, and set their paths in `.env`. Credentials are reread when authentication is needed; they are not stored in Django's database or browser sessions.

Set `MODEM_MONITOR_ENABLED=true` and initially keep `MODEM_AUTO_RECONNECT_ENABLED=false`. Then run:

```bash
uv run python manage.py collectstatic --noinput
uv run gunicorn -c gunicorn.conf.py config.wsgi:application
```

Keep local development bound to loopback. For deployment, configure HTTPS as described below. After checking healthy/outage classification on your firmware, enable `MODEM_AUTO_RECONNECT_ENABLED=true` and stop/start the application.

The default recovery sequence is:

1. Authenticate through `login_challenge`, PBKDF2/HMAC proof, server-signature verification and `login_done`. Cookies and verification tokens remain in server memory.
2. Verify the protected `POST /api/modemmng/queryModemMonitorWithName` session, then read heartbeat, `/api/modemmng/getSignal` and `/api/signalmng/getsiglevel`. The protected query uses the N5368X-required RSA-OAEP/SHA-1 format; public signal endpoints cannot prove authentication.
3. Wait 30 seconds between completed checks. `ServiceStatus=1`, `SysModeName=LTE` and successful API responses mean healthy service. Status `0` in LTE mode is the known N5368X outage mapping; verify it for your firmware. Weak signal, unknown modes, API/authentication failures and malformed responses do not trigger recovery.
4. After three consecutive confirmed outages, check again under the device lock and request `GET /api/dialupmng/rebootModem`. Wait at least 30 seconds before checking recovery. A command acknowledgement alone does not prove restored LTE.
5. Space automatic attempts at least 300 seconds apart. Stop after three unsuccessful attempts until a healthy observation clears the incident. Failed authentication backs off for 300 seconds. These values are configurable in the example.

**Run one worker and one replica.** The monitor and browser operations share an in-memory client and lock. Deploy with stop/start, never overlapping reloads or multiple application instances against the same modem. Restart loses sessions, counters and exhaustion state, and starts a new cooldown grace period; do not use repeated restarts to bypass recovery limits.

In manual mode, a modem session belongs to the browser that logged in; another login replaces it. In monitoring mode, operators share the service's session and signing out does not stop monitoring. Idle sessions expire locally after 240 seconds by default. Login is limited to five attempts per five minutes; manual reconnect has a 30-second cooldown. An unconfirmed reconnect is not immediately retried.

## Deployment

Use `DJANGO_DEBUG=false` and an HTTPS reverse proxy with explicit allowed hosts and `DJANGO_CSRF_TRUSTED_ORIGINS`. Secure cookies require HTTPS. Enable `DJANGO_TRUST_PROXY_HTTPS` only when a trusted proxy overwrites `X-Forwarded-Proto` and clients cannot bypass it. Enable SSL redirect/HSTS after HTTPS works. Restrict access to trusted operators and rate-limit application/admin login at the proxy.

Docker Compose publishes the app on `127.0.0.1:8000` and enables monitoring. Set `MODEM_USERNAME_SECRET_PATH` and `MODEM_PASSWORD_SECRET_PATH` in `.env` to protected **host** files outside the checkout. File-backed secrets are mounts, not encrypted storage; make the files readable by container UID 10001, for example owner 10001 with mode 0400. Rootless Docker may require different host IDs.

```bash
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py createsuperuser
docker compose up -d app
docker compose logs --tail=50 app
docker compose exec app python manage.py check --deploy
```

The entrypoint also migrates before starting Gunicorn. SQLite persists in the `data` volume; back it up and periodically run `python manage.py clearsessions`. WhiteNoise serves static assets. `/health/` checks application liveness only, not modem or database health.

### Nginx Proxy Manager

For Nginx Proxy Manager on the same Docker host, connect the application to an existing Docker network shared with NPM. Set `NPM_NETWORK` in `.env` to that network's name, then run:

```bash
docker compose -f compose.yaml -f compose.npm.yaml up -d app
```

In NPM, create a Proxy Host using scheme `http`, forward hostname `modem-manager-app`, and forward port `8000`. Configure its certificate and Force SSL. This connection uses Docker networking; the host's loopback port binding does not prevent it. Attach NPM to the same network in its own Compose configuration so the connection survives container recreation. Only trusted containers should share this network. See [Docker's shared-network documentation](https://docs.docker.com/compose/how-tos/networking/).

Set the application environment for your public hostname, for example:

```dotenv
DJANGO_ALLOWED_HOSTS=modem.example.com,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://modem.example.com
DJANGO_TRUST_PROXY_HTTPS=true
DJANGO_SECURE_SSL_REDIRECT=true
```

NPM must overwrite `X-Forwarded-Proto` with the client connection scheme. After verifying HTTPS, set `DJANGO_HSTS_SECONDS` to an appropriate nonzero value. Enable subdomain coverage or preload only if suitable for your domain. Configure access restrictions and login rate limiting in your HTTPS proxy; the supplied application does not throttle Django account/admin login.

### Optional HTTP proxy

`docker compose -f compose.yaml -f compose.prod.yaml up -d` adds nginx. It publishes on **all host interfaces**, allowing a separate proxy container or remote proxy to reach the host. The copied `.env.example` selects port **2500**; if `PROXY_PORT` is unset, Compose falls back to **8080**. Set `PROXY_BIND_IP` to a specific reachable host address to restrict the binding, or to `127.0.0.1` for a proxy running directly on the host. This nginx service does not provide TLS or login rate limiting. Restrict access to this HTTP port to trusted clients/proxies.

The optional nginx overwrites `X-Forwarded-Proto` with its own connection scheme (`http`). For NPM HTTPS termination, use the direct shared-network route above; chaining NPM through this HTTP nginx requires separate trusted-proxy configuration to preserve the original scheme and avoid HTTPS redirect loops. A proxy running directly on the host can forward to `127.0.0.1:8000` instead.

For updates or credential rotation, stop the app, then recreate it. Replacing a mounted credential file can leave a running container reading the old file; preserve ownership/permissions and use `docker compose up -d --force-recreate app` after stopping. Do not remove the data volume during upgrades.

## Troubleshooting

- **Authentication failed:** check credential file paths/readability and credentials; wait for authentication backoff. Never paste credentials or unredacted `docker compose config` output into an issue.
- **Modem unavailable:** check the server/container's route to the modem, its configured origin, TLS trust and timeout. Browser HTTPS does not encrypt an HTTP connection from the server to the modem.
- **Degraded or recovery exhausted:** inspect the dashboard's normalized status and mode. Unknown firmware states require investigation; signal strength alone cannot authorize recovery.
- **Read-only database:** the service needs write access to both the SQLite file and its directory. Existing Docker volumes retain old ownership; back up and repair their permissions with the app stopped.

Logs report state changes and recovery results without raw modem responses. Avoid HTTP wire logging or request/local-variable capture that could expose credentials. Hardware recovery still needs validation on your own N5368X; the automated tests use simulated responses.

## Development

```bash
uv sync --locked
uv run python manage.py test --settings=config.test_settings
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run --settings=config.test_settings
```

Tests use an in-memory database and mocked modem transport. For the optional Linux container smoke test, build `docker build -t modem_manager-app .`, install the host's `setfacl` utility, then run `uv run python scripts/docker_smoke.py`. It creates temporary containers and tests a local fake modem, never the configured physical device.

After dependency changes, regenerate the pip export:

```bash
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

## License

This project is licensed under the [MIT License](LICENSE).

Dependencies retain their own licenses. In particular, Psycopg/psycopg-binary use [LGPL-3.0](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt) and certifi uses MPL-2.0; preserve applicable notices and review redistribution obligations before distributing bundled wheels or container images.
