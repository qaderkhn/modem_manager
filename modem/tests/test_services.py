from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from modem.exceptions import (
    ModemAuthenticationError,
    ModemConnectionError,
    ModemRateLimited,
    ModemSessionExpired,
)
from modem.models import ModemConnection
from modem.services import ModemService


@override_settings(MODEM_MONITOR_ENABLED=False, MODEM_AUTO_RECONNECT_ENABLED=False)
class ServiceTests(TestCase):
    def setUp(self):
        self.device = Mock()
        self.factory = Mock(return_value=self.device)
        self.clock = Mock(return_value=100)
        self.service = ModemService("test", "http://modem", self.factory, self.clock)
        self.addCleanup(self.service._clear)

    def login(self):
        self.service.login("browser-a", "admin", "secret-password")

    def test_login_and_protected_verification(self):
        self.login()
        self.device.login.assert_called_once_with("admin", "secret-password")
        self.device.verify_session.assert_called_once()
        self.assertTrue(self.service.is_authenticated("browser-a"))
        self.assertIsNotNone(ModemConnection.objects.get().last_login_at)

    def test_failed_login_clears_client(self):
        self.device.login.side_effect = ModemAuthenticationError("failure")
        with self.assertRaises(ModemAuthenticationError):
            self.login()
        self.device.close.assert_called_once()
        self.assertFalse(self.service.is_authenticated("browser-a"))
        self.assertFalse(ModemConnection.objects.exists())

    def test_failed_initial_protected_verification(self):
        self.device.verify_session.side_effect = ModemSessionExpired("expired")
        with self.assertRaises(ModemSessionExpired):
            self.login()
        self.assertIsNone(self.service.client)
        self.assertFalse(ModemConnection.objects.exists())

    def test_unauthenticated_reconnect_rejected(self):
        with self.assertRaises(ModemSessionExpired):
            self.service.reconnect_network("browser-a")
        self.device.reconnect_network.assert_not_called()

    def test_other_browser_cannot_use_or_logout_session(self):
        self.login()
        self.assertFalse(self.service.is_authenticated("browser-b"))
        with self.assertRaises(ModemSessionExpired):
            self.service.reconnect_network("browser-b")
        self.service.logout("browser-b")
        self.assertTrue(self.service.is_authenticated("browser-a"))

    def test_reconnect_and_cooldown(self):
        self.login()
        self.service.reconnect_network("browser-a")
        with self.assertRaises(ModemRateLimited):
            self.service.reconnect_network("browser-a")
        self.device.reconnect_network.assert_called_once()
        self.assertIsNotNone(ModemConnection.objects.get().last_reconnect_at)
        self.clock.return_value = 131
        self.service.reconnect_network("browser-a")
        self.assertEqual(self.device.reconnect_network.call_count, 2)

    def test_preflight_expiry_prevents_operation(self):
        self.login()
        self.device.verify_session.side_effect = ModemSessionExpired("expired")
        with self.assertRaises(ModemSessionExpired):
            self.service.reconnect_network("browser-a")
        self.device.reconnect_network.assert_not_called()
        self.assertIsNone(self.service.client)

    def test_ambiguous_timeout_is_never_retried(self):
        self.login()
        self.device.reconnect_network.side_effect = ModemConnectionError("timeout")
        with self.assertRaises(ModemConnectionError):
            self.service.reconnect_network("browser-a")
        self.device.reconnect_network.assert_called_once()
        self.assertIsNone(self.service.client)
        self.login()
        with self.assertRaises(ModemRateLimited):
            self.service.reconnect_network("browser-a")

    def test_dashboard_protected_check_failure_invalidates(self):
        self.login()
        self.device.verify_session.side_effect = ModemConnectionError("offline")
        self.assertFalse(self.service.is_authenticated("browser-a"))
        self.device.close.assert_called_once()

    def test_inactivity_expiry(self):
        self.login()
        self.clock.return_value = 1001
        self.assertFalse(self.service.is_authenticated("browser-a"))
        self.device.close.assert_called_once()

    def test_logout(self):
        self.login()
        self.service.logout("browser-a")
        self.assertIsNone(self.service.client)
        self.assertIsNone(self.service.owner)

    def test_login_rate_limit_survives_logout(self):
        for _ in range(5):
            self.login()
            self.service.logout("browser-a")
        with self.assertRaises(ModemRateLimited):
            self.login()
        self.assertEqual(self.device.login.call_count, 5)
        self.clock.return_value = 401
        self.login()

    def test_logs_never_include_credentials(self):
        with self.assertLogs("modem", "INFO") as logs:
            self.login()
        self.assertNotIn("secret-password", "".join(logs.output))
        self.assertNotIn("admin", "".join(logs.output))

    def test_simultaneous_reconnects_send_once(self):
        self.login()

        def call():
            try:
                self.service.reconnect_network("browser-a")
                return "sent"
            except ModemRateLimited:
                return "limited"

        with patch("modem.services.ModemConnection.objects.filter"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: call(), range(2)))
        self.assertCountEqual(results, ["sent", "limited"])
        self.device.reconnect_network.assert_called_once()

    def test_timer_clears_idle_session(self):
        self.login()
        self.clock.return_value = 341
        self.service._expire_client(self.device)
        self.assertIsNone(self.service.client)
        self.assertIsNone(self.service.expiry_timer)

    def test_stale_timer_cannot_clear_new_client(self):
        self.login()
        self.service._expire_client(Mock())
        self.assertTrue(self.service.is_authenticated("browser-a"))

    def test_reconnect_metadata_failure_does_not_invite_retry(self):
        from django.db import DatabaseError

        self.login()
        with patch("modem.services.ModemConnection.objects.filter", side_effect=DatabaseError):
            self.service.reconnect_network("browser-a")
        self.device.reconnect_network.assert_called_once()
        with self.assertRaises(ModemRateLimited):
            self.service.reconnect_network("browser-a")

    def test_activity_refreshes_idle_deadline_beyond_original_lifetime(self):
        self.login()
        for timestamp in range(200, 2001, 100):
            self.clock.return_value = timestamp
            self.assertTrue(self.service.is_authenticated("browser-a"))
        self.device.login.assert_called_once()
        self.clock.return_value = 2241
        self.assertFalse(self.service.is_authenticated("browser-a"))

    def test_old_deadline_timer_reschedules_after_activity(self):
        self.login()
        self.clock.return_value = 300
        self.assertTrue(self.service.is_authenticated("browser-a"))
        self.clock.return_value = 340
        self.service._expire_client(self.device)
        self.assertIs(self.service.client, self.device)
        self.assertEqual(self.service.expires, 540)

    def test_database_audit_failure_preserves_authenticated_session(self):
        from django.db import OperationalError

        with patch(
            "modem.services.ModemConnection.objects.update_or_create",
            side_effect=OperationalError("no such table: modem_modemconnection"),
        ):
            with self.assertLogs("modem", "INFO") as logs:
                self.login()
        self.assertTrue(self.service.is_authenticated("browser-a"))
        self.device.close.assert_not_called()
        self.assertIn("modem_login_audit_failed", "".join(logs.output))
        self.assertNotIn("modem_login_failed", "".join(logs.output))
