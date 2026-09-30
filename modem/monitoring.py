"""One background thread in the sole Gunicorn worker; all state uses the device lock."""

import logging
import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from modem.credentials import FileCredentialProvider
from modem.exceptions import (
    ModemAuthenticationError,
    ModemConnectionError,
    ModemError,
    ModemRateLimited,
    ModemSessionExpired,
)
from modem.health import HealthState as State
from modem.health import ModemHealth, classify

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, service, provider=None):
        self.service = service
        self.provider = provider or FileCredentialProvider(
            settings.MODEM_USERNAME_FILE, settings.MODEM_PASSWORD_FILE
        )
        self.health = ModemHealth()
        self.state = self.health.health_state
        self.failures = self.lte_failures = self.recovery_attempts = 0
        self.last_check = self.last_recovery = None
        self.next_auth = self.verify_after = 0
        # Restart cannot immediately trigger recovery. Counters are otherwise in memory.
        self.next_recovery = service.clock() + settings.MODEM_AUTO_RECONNECT_COOLDOWN
        self.pending = self.exhausted = False

    def snapshot(self):
        with self.service.lock:
            return dict(
                health=self.health,
                state=self.state,
                failures=self.failures,
                attempts=self.recovery_attempts,
                last_check=self.last_check,
                last_recovery=self.last_recovery,
                enabled=settings.MODEM_MONITOR_ENABLED,
                automatic=settings.MODEM_AUTO_RECONNECT_ENABLED,
                max_attempts=settings.MODEM_AUTO_RECONNECT_MAX_ATTEMPTS,
                outage_configured=bool(settings.MODEM_LTE_UNAVAILABLE_STATUSES),
            )

    @sensitive_variables()
    def _authenticate(self):
        service = self.service
        if service._owned(None):
            return
        if service.clock() < self.next_auth:
            raise ModemAuthenticationError("Authentication backoff active")
        self.next_auth = service.clock() + settings.MODEM_AUTH_BACKOFF
        log.info("event=modem_authentication_started")
        username, password = self.provider.load()
        service.login(None, username, password)
        log.info("event=modem_session_restored")

    def _check(self):
        try:
            self._authenticate()
            self.service.client.verify_session()
            self.service.activity_confirmed()
            self.service.client.heartbeat()
            health = classify(
                self.service.client.signal_status(),
                self.service.client.signal_level(),
                settings.MODEM_LTE_UNAVAILABLE_STATUSES,
                authenticated=True,
            )
            return health
        except ModemSessionExpired:
            self.service._clear()
            return ModemHealth(State.AUTHENTICATION_REQUIRED, "Modem session expired", True, False)
        except (ModemAuthenticationError, ModemRateLimited):
            self.service._clear()
            log.debug("event=modem_authentication_failed")
            return ModemHealth(
                State.AUTHENTICATION_FAILED, "Authentication required or backoff active"
            )
        except ModemConnectionError:
            self.service._clear()
            return ModemHealth(State.MODEM_UNAVAILABLE, "Modem API unavailable")
        except ModemError:
            self.service._clear()
            return ModemHealth(State.DEGRADED, "Modem response could not be verified")

    def _observe(self, health):
        changed = self.last_check is None or self.health.health_state != health.health_state
        self.health, self.state = health, health.health_state
        self.last_check = timezone.now()
        if self.state == State.HEALTHY:
            if self.failures:
                log.info("event=lte_health_restored")
            if self.pending:
                log.info("event=lte_recovery_succeeded")
            self.failures = self.lte_failures = self.recovery_attempts = 0
            self.pending = self.exhausted = False
        else:
            self.failures += 1
            self.lte_failures = self.lte_failures + 1 if self.state == State.LTE_UNAVAILABLE else 0
        log.log(
            logging.INFO if changed else logging.DEBUG,
            "event=lte_health_check health_state=%s failure_count=%d recovery_attempt=%d",
            self.state,
            self.failures,
            self.recovery_attempts,
        )

    def manual_recovery(self):
        # Command acknowledgement is not proof of restored LTE; verify on a later tick.
        # Preserve exhaustion and the budget until LTE is verified healthy.
        self.pending = True
        self.state = State.RECOVERING
        self.last_recovery = timezone.now()
        self.verify_after = self.service.clock() + settings.MODEM_RECOVERY_VERIFY_DELAY
        self.next_recovery = self.service.clock() + settings.MODEM_AUTO_RECONNECT_COOLDOWN

    def tick(self):
        with self.service.lock:
            now = self.service.clock()
            if now < self.verify_after:
                return
            self._observe(self._check())
            if self.state == State.HEALTHY:
                return
            if self.pending:
                self.pending = False
                log.warning("event=lte_recovery_failed")
                if self.recovery_attempts >= settings.MODEM_AUTO_RECONNECT_MAX_ATTEMPTS:
                    self.exhausted = True
                    log.warning("event=lte_recovery_exhausted")
            if self.exhausted:
                self.state = State.RECOVERY_EXHAUSTED
                return
            if self.lte_failures < settings.MODEM_HEALTH_FAILURE_THRESHOLD:
                return
            self.state = State.RECOVERY_PENDING
            log.debug(
                "event=lte_recovery_candidate automatic=%s", settings.MODEM_AUTO_RECONNECT_ENABLED
            )
            if not settings.MODEM_AUTO_RECONNECT_ENABLED or now < self.next_recovery:
                return
            if now - self.service.last_reconnect < settings.MODEM_RECONNECT_COOLDOWN:
                return
            # Final recheck and canonical reconnect remain inside the same RLock.
            self._observe(self._check())
            if self.state != State.LTE_UNAVAILABLE:
                return
            self.recovery_attempts += 1
            self.last_recovery = timezone.now()
            self.next_recovery = self.service.clock() + settings.MODEM_AUTO_RECONNECT_COOLDOWN
            self.pending = True
            self.state = State.RECOVERING
            log.info("event=lte_recovery_started recovery_attempt=%d", self.recovery_attempts)
            try:
                self.service.reconnect_network(None, automatic=True)
            except ModemError:
                log.warning("event=lte_recovery_dispatch_unconfirmed")
            finally:
                self.next_recovery = self.service.clock() + settings.MODEM_AUTO_RECONNECT_COOLDOWN
                self.verify_after = self.service.clock() + settings.MODEM_RECOVERY_VERIFY_DELAY


class MonitorRunner:
    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="lte-monitor", daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        log.info("event=lte_monitor_started")
        while not self.stop_event.is_set():
            try:
                close_old_connections()
                self.service.monitor.tick()
            except Exception as exc:
                # No traceback/local variables or exception text containing secrets.
                log.error("event=lte_monitor_error exception_class=%s", type(exc).__name__)
            finally:
                try:
                    close_old_connections()
                except Exception as exc:
                    log.error(
                        "event=monitor_db_cleanup_failed exception_class=%s", type(exc).__name__
                    )
            self.stop_event.wait(settings.MODEM_MONITOR_INTERVAL)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            with self.service.lock:
                self.service._clear()
