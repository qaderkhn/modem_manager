"""Single-process device registry. All device traffic is serialized by a lock.
The server owns monitored sessions; manual mode keeps browser isolation.
Deploy with exactly one worker/replica. See README before scaling.
"""

import hashlib
import logging
import threading
import time
from collections import deque

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from modem.exceptions import ModemError, ModemRateLimited, ModemSessionExpired
from modem.models import ModemConnection
from modem.modem_client import ModemClient
from modem.monitoring import Monitor

log = logging.getLogger(__name__)


class ModemService:
    def __init__(self, name, base_url, client_factory=ModemClient, clock=time.monotonic):
        self.name, self.base_url = name, base_url
        self.client_factory, self.clock = client_factory, clock
        self.lock = threading.RLock()
        self.client = self.owner = None
        self.expires = 0
        self.expiry_timer = None
        self.attempts = deque()
        self.last_reconnect = float("-inf")
        self.autonomous = settings.MODEM_MONITOR_ENABLED
        self.monitor = Monitor(self)

    def _clear(self):
        if self.expiry_timer is not None:
            self.expiry_timer.cancel()
            self.expiry_timer = None
        if self.client:
            self.client.close()
        self.client = self.owner = None
        self.expires = 0

    def _expire_client(self, client):
        with self.lock:
            if self.client is client:
                remaining = self.expires - self.clock()
                if remaining > 0:
                    self._schedule_expiry(remaining)
                else:
                    self._clear()
                    log.info("event=modem_session_expired")

    def _schedule_expiry(self, delay):
        if self.expiry_timer is not None:
            self.expiry_timer.cancel()
        self.expiry_timer = threading.Timer(delay, self._expire_client, args=(self.client,))
        self.expiry_timer.daemon = True
        self.expiry_timer.start()

    def activity_confirmed(self):
        # Caller holds the device lock. Successful authenticated traffic refreshes
        # the inactivity deadline, not a fixed lifetime measured from login.
        self.expires = self.clock() + settings.MODEM_SESSION_IDLE_TIMEOUT

    def _owned(self, owner):
        if self.client and self.clock() >= self.expires:
            self._clear()
            log.info("event=modem_session_expired")
        return self.client is not None and (self.autonomous or self.owner == owner)

    @sensitive_variables()
    def login(self, owner, username, password):
        with self.lock:
            now = self.clock()
            while self.attempts and now - self.attempts[0] >= settings.MODEM_LOGIN_WINDOW:
                self.attempts.popleft()
            if len(self.attempts) >= settings.MODEM_LOGIN_LIMIT:
                raise ModemRateLimited("Too many login attempts. Try again in five minutes.")
            self.attempts.append(now)
            log.info("event=modem_login_attempted")
            self._clear()
            client = self.client_factory(
                self.base_url,
                verify=settings.MODEM_VERIFY_TLS,
                timeout=settings.MODEM_TIMEOUT,
            )
            try:
                client.login(username, password)
                try:
                    client.verify_session()
                except ModemError:
                    log.warning("event=modem_session_check_failed")
                    raise
            except Exception as exc:
                client.close()
                log.warning("event=modem_login_failed category=%s", type(exc).__name__)
                raise
            self.client, self.owner = client, owner
            self.activity_confirmed()
            self._schedule_expiry(settings.MODEM_SESSION_IDLE_TIMEOUT)
            try:
                ModemConnection.objects.update_or_create(
                    name=self.name,
                    defaults={
                        "base_url": self.base_url,
                        "last_login_at": timezone.now(),
                    },
                )
            except DatabaseError:
                # Authentication succeeded. Historical metadata must not destroy it.
                log.error("event=modem_login_audit_failed")
            log.info("event=modem_login_succeeded")

    def is_authenticated(self, owner):
        with self.lock:
            if not self._owned(owner):
                return False
            try:
                self.client.verify_session()
            except ModemError as exc:
                log.warning("event=modem_session_check_failed category=%s", type(exc).__name__)
                self._clear()
                return False
            self.activity_confirmed()
            return True

    def reconnect_network(self, owner, *, automatic=False):
        with self.lock:
            if not self._owned(owner):
                raise ModemSessionExpired("Log in to the modem again.")
            if self.clock() - self.last_reconnect < settings.MODEM_RECONNECT_COOLDOWN:
                raise ModemRateLimited("Please wait 30 seconds before reconnecting again.")
            log.info("event=modem_reconnect_requested")
            try:
                self.client.verify_session()
            except ModemError:
                self._clear()
                log.warning("event=modem_session_expired")
                raise ModemSessionExpired(
                    "Modem authentication could not be verified. Log in again."
                ) from None
            # Reserve the cooldown before dispatch: a timeout may mean the modem acted.
            self.last_reconnect = self.clock()
            try:
                self.client.reconnect_network()
                self.activity_confirmed()
            except ModemError as exc:
                self._clear()
                log.warning("event=modem_reconnect_failed category=%s", type(exc).__name__)
                raise
            finally:
                if self.autonomous and not automatic:
                    self.monitor.manual_recovery()
            try:
                ModemConnection.objects.filter(name=self.name).update(
                    last_reconnect_at=timezone.now()
                )
            except DatabaseError:
                # The device acted; metadata failure must not invite a retry.
                log.error("event=modem_reconnect_audit_failed")
            log.info("event=modem_reconnect_succeeded")

    def logout(self, owner):
        with self.lock:
            if self.autonomous:
                # Browser logout must not terminate the autonomous service.
                return
            if self._owned(owner):
                self._clear()
            log.info("event=modem_logout")


_registry = {}
_registry_lock = threading.Lock()


def get_modem_service():
    # Only operator-configured devices can enter this registry.
    with _registry_lock:
        key = ("default", settings.MODEM_BASE_URL)
        if key not in _registry:
            _registry[key] = ModemService(*key)
        return _registry[key]


def browser_owner(request):
    if not request.session.session_key:
        request.session.create()
    return hashlib.sha256(f"{request.user.pk}:{request.session.session_key}".encode()).hexdigest()
