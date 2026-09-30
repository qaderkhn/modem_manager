from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from modem.credentials import FileCredentialProvider
from modem.exceptions import ModemAuthenticationError, ModemConnectionError, ModemRateLimited
from modem.health import HealthState as State
from modem.health import classify
from modem.services import ModemService

HEALTHY = {
    "ServiceStatus": 1,
    "SysModeName": "LTE",
    "ErrorCode": 0,
    "LteRssi": -52,
    "LteSinr": 10,
    "LteRsrp": -82,
    "LteRsrq": -10,
    "NrRsrp": -999,
    "NrSinr": -999,
    "NrRsrq": -999,
}
LEVEL = {"ErrorCode": 0, "LteLevel": "LTE_SIGNAL_LEVEL_FOUR"}
DOWN = {**HEALTHY, "ServiceStatus": 0}  # Synthetic operator-confirmed mapping, not firmware fact.


class CredentialTests(SimpleTestCase):
    def test_load_rotation_and_safe_errors(self):
        with TemporaryDirectory() as directory:
            user, password = Path(directory) / "user", Path(directory) / "password"
            user.write_text("test-user\n")
            password.write_text(" test-password \n")
            provider = FileCredentialProvider(user, password)
            self.assertEqual(provider.load(), ("test-user", " test-password "))
            password.write_text("rotated")
            self.assertEqual(provider.load()[1], "rotated")
            for value in ("", "x" * 4097, "line\nline"):
                password.write_text(value)
                with self.assertRaisesMessage(
                    ModemAuthenticationError, "Deployment credentials are unavailable"
                ):
                    provider.load()
            password.unlink()
            with self.assertRaises(ModemAuthenticationError):
                provider.load()


class HealthTests(SimpleTestCase):
    def test_reference_weak_signal_and_sentinels(self):
        for signal in (HEALTHY, {**HEALTHY, "LteRsrp": -125, "LteSinr": -10}):
            health = classify(signal, LEVEL)
            self.assertEqual(health.health_state, State.HEALTHY)
            self.assertIsNone(health.nr_rsrp)
            self.assertIsNone(health.nr_sinr)
        self.assertIsNone(classify({**HEALTHY, "LteRssi": -999}, LEVEL).lte_rssi)

    def test_public_signal_responses_do_not_prove_authentication(self):
        self.assertFalse(classify(HEALTHY, LEVEL).authenticated)
        self.assertTrue(classify(HEALTHY, LEVEL, authenticated=True).authenticated)

    def test_operator_captured_outage_and_level_five(self):
        outage = {
            "ServiceStatus": 0,
            "Roaming": 0,
            "SysModeName": "LTE",
            "LteRssi": -60,
            "LteSinr": 1,
            "LteRsrp": -89,
            "LteRsrq": -12,
            "NrRsrp": -999,
            "NrSinr": -999,
            "NrRsrq": -999,
            "ErrorCode": 0,
        }
        loss = {
            "ErrorCode": 0,
            "LteLevel": "SYS_STAT_LTE_SIGNAL_LOSS",
            "NrLevel": "SYS_STAT_NR_SIGNAL_LOSS",
        }
        health = classify(outage, loss, (0,))
        self.assertEqual(health.health_state, State.LTE_UNAVAILABLE)
        self.assertIsNone(health.nr_rsrp)
        healthy = {
            **outage,
            "ServiceStatus": 1,
            "LteRssi": -55,
            "LteSinr": 17,
            "LteRsrp": -84,
            "LteRsrq": -8,
        }
        level = {**loss, "LteLevel": "LTE_SIGNAL_LEVEL_FIVE"}
        health = classify(healthy, level, (0,))
        self.assertEqual(health.health_state, State.HEALTHY)
        self.assertEqual(health.lte_level, "LTE_SIGNAL_LEVEL_FIVE")
        # The heartbeat's identical ErrorCode=0 cannot distinguish these states.
        self.assertEqual(healthy["ErrorCode"], outage["ErrorCode"])

    def test_unknown_and_malformed_never_authorize_reconnect(self):
        for signal in (
            None,
            [],
            {},
            DOWN,
            {**HEALTHY, "SysModeName": "UNKNOWN"},
            {**HEALTHY, "ServiceStatus": True},
            {**HEALTHY, "ErrorCode": 9},
        ):
            self.assertEqual(classify(signal, LEVEL).health_state, State.DEGRADED)
        self.assertEqual(classify(DOWN, LEVEL, (0,)).health_state, State.LTE_UNAVAILABLE)

    def test_malformed_metrics_and_signal_level_do_not_crash(self):
        for value in ([], {}, True, None, float("nan"), float("inf"), 10**400):
            with self.subTest(value=value):
                health = classify(
                    {**HEALTHY, "LteRsrp": value},
                    {**LEVEL, "LteLevel": value},
                )
                self.assertIsNone(health.lte_rsrp)
                self.assertIsNone(health.lte_level)
                self.assertEqual(health.health_state, State.HEALTHY)


@override_settings(
    MODEM_MONITOR_ENABLED=True,
    MODEM_AUTO_RECONNECT_ENABLED=True,
    MODEM_LTE_UNAVAILABLE_STATUSES=(0,),
)
class MonitorTests(TestCase):
    def setUp(self):
        self.device = Mock()
        self.device.signal_status.return_value = DOWN
        self.device.signal_level.return_value = LEVEL
        self.clock = Mock(return_value=0)
        self.service = ModemService(
            "test", "http://modem", Mock(return_value=self.device), self.clock
        )
        self.monitor = self.service.monitor
        self.monitor.provider = Mock()
        self.monitor.provider.load.return_value = ("test-user", "test-password")
        self.addCleanup(self.service._clear)

    def tick(self, time):
        self.clock.return_value = time
        self.monitor.tick()

    def recover(self):
        for time in (300, 330, 360):
            self.tick(time)

    def test_threshold_final_check_and_success(self):
        self.tick(300)
        self.tick(330)
        self.device.reconnect_network.assert_not_called()
        self.tick(360)
        self.device.reconnect_network.assert_called_once()
        self.assertEqual(self.device.signal_status.call_count, 4)
        self.assertEqual(self.monitor.state, State.RECOVERING)
        self.device.signal_status.return_value = HEALTHY
        self.tick(389)
        self.assertEqual(self.monitor.state, State.RECOVERING)
        self.tick(390)
        self.assertEqual(self.monitor.state, State.HEALTHY)
        self.assertEqual(self.monitor.recovery_attempts, 0)

    def test_stable_health_logs_only_once_at_info(self):
        self.device.signal_status.return_value = HEALTHY
        with self.assertLogs("modem", "INFO") as logs:
            self.tick(0)
            self.tick(30)
            self.device.signal_status.return_value = DOWN
            self.tick(60)
            self.tick(90)
        checks = [line for line in logs.output if "event=lte_health_check" in line]
        self.assertEqual(len(checks), 2)
        self.assertIn("health_state=HEALTHY", checks[0])
        self.assertIn("health_state=LTE_UNAVAILABLE", checks[1])

    def test_success_resets_consecutive_failures(self):
        self.tick(300)
        self.device.signal_status.return_value = HEALTHY
        self.tick(330)
        self.assertEqual(self.monitor.failures, 0)
        self.device.signal_status.return_value = DOWN
        self.tick(360)
        self.device.reconnect_network.assert_not_called()

    @override_settings(MODEM_AUTO_RECONNECT_ENABLED=False)
    def test_monitoring_only(self):
        self.recover()
        self.assertEqual(self.monitor.state, State.RECOVERY_PENDING)
        self.device.reconnect_network.assert_not_called()

    def test_final_check_aborts(self):
        self.device.signal_status.side_effect = [DOWN, DOWN, DOWN, HEALTHY]
        self.recover()
        self.device.reconnect_network.assert_not_called()

    def test_exhaustion_cooldown_and_manual_reset(self):
        self.recover()
        self.tick(390)
        self.tick(659)
        self.assertEqual(self.device.reconnect_network.call_count, 1)
        self.tick(660)
        self.tick(690)
        self.tick(960)
        self.tick(990)
        self.tick(2000)
        self.assertEqual(self.monitor.state, State.RECOVERY_EXHAUSTED)
        self.assertEqual(self.device.reconnect_network.call_count, 3)
        self.service.reconnect_network("any-operator")
        self.device.signal_status.return_value = HEALTHY
        self.tick(2030)
        self.assertEqual(self.monitor.state, State.HEALTHY)
        self.assertFalse(self.monitor.exhausted)

    def test_timeout_reserves_cooldown_and_verifies_later(self):
        self.device.reconnect_network.side_effect = ModemConnectionError("timeout")
        self.recover()
        self.assertIsNone(self.service.client)
        self.tick(390)
        self.tick(400)
        self.device.reconnect_network.assert_called_once()
        self.assertEqual(self.service.last_reconnect, 360)

    def test_authentication_backoff_secrets_and_expiry(self):
        self.device.login.side_effect = ModemAuthenticationError("test-password")
        with self.assertLogs("modem", "INFO") as logs:
            self.tick(0)
            self.tick(30)
        self.device.login.assert_called_once()
        self.assertNotIn("test-password", "".join(logs.output))
        self.assertNotIn("test-user", "".join(logs.output))
        self.device.login.side_effect = None
        self.tick(300)
        self.assertEqual(self.device.login.call_count, 2)
        self.tick(1201)
        self.assertEqual(self.device.login.call_count, 3)

    def test_api_unavailable_does_not_recover(self):
        self.device.signal_status.side_effect = ModemConnectionError("timeout")
        self.recover()
        self.device.reconnect_network.assert_not_called()
        self.assertEqual(self.monitor.lte_failures, 0)

    def test_browser_logout_preserves_service_session(self):
        self.tick(0)
        self.service.logout("browser")
        self.assertTrue(self.service.is_authenticated("another-browser"))

    def test_restart_grace_period(self):
        for time in (0, 30, 60, 90):
            self.tick(time)
        self.device.reconnect_network.assert_not_called()

    def test_concurrent_manual_and_automatic_share_lock_and_cooldown(self):
        self.tick(300)
        self.tick(330)
        self.clock.return_value = 360

        def manual():
            try:
                self.service.reconnect_network("operator")
            except ModemRateLimited:
                pass

        with patch("modem.services.ModemConnection.objects.filter"):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(self.monitor.tick),
                    pool.submit(manual),
                    pool.submit(self.monitor.tick),
                ]
                for future in futures:
                    future.result()
        self.device.reconnect_network.assert_called_once()

    def test_explicit_session_expiry_reauthenticates_on_later_tick(self):
        from modem.exceptions import ModemSessionExpired

        self.tick(0)
        self.clock.return_value = 300
        self.device.verify_session.side_effect = ModemSessionExpired("expired")
        self.monitor.tick()
        self.assertIsNone(self.service.client)
        self.device.verify_session.side_effect = None
        self.tick(330)
        self.assertEqual(self.device.login.call_count, 2)

    def test_missing_credentials_are_safe_and_backed_off(self):
        self.monitor.provider.load.side_effect = ModemAuthenticationError("unavailable")
        self.tick(0)
        self.tick(30)
        self.assertEqual(self.monitor.state, State.AUTHENTICATION_FAILED)
        self.monitor.provider.load.assert_called_once()
        self.device.login.assert_not_called()

    def test_automatic_login_respects_manual_throttle(self):
        for _ in range(5):
            self.service.login("operator", "test-user", "test-password")
        self.service._clear()
        self.tick(30)
        self.assertEqual(self.device.login.call_count, 5)
        self.tick(330)
        self.assertEqual(self.device.login.call_count, 6)

    def test_new_service_authenticates_without_browser(self):
        self.tick(0)
        self.service._clear()
        restarted = ModemService("test", "http://modem", Mock(return_value=self.device), self.clock)
        self.addCleanup(restarted._clear)
        restarted.monitor.provider = self.monitor.provider
        restarted.monitor.tick()
        self.assertEqual(self.device.login.call_count, 2)

    def test_unexpected_api_error_breaks_lte_streak(self):
        from modem.exceptions import ModemAPIError

        self.tick(300)
        self.tick(330)
        self.device.signal_status.side_effect = ModemAPIError("private response")
        self.tick(360)
        self.assertEqual(self.monitor.lte_failures, 0)
        self.assertEqual(self.monitor.state, State.DEGRADED)
        self.device.reconnect_network.assert_not_called()

    def test_manual_timeout_also_delays_automatic_recovery(self):
        self.tick(300)
        self.tick(330)
        self.clock.return_value = 360
        self.device.reconnect_network.side_effect = ModemConnectionError("timeout")
        with self.assertRaises(ModemConnectionError):
            self.service.reconnect_network("operator")
        self.tick(390)
        self.tick(420)
        self.device.reconnect_network.assert_called_once()
        self.assertEqual(self.monitor.next_recovery, 660)

    def test_failed_manual_verification_does_not_clear_exhaustion(self):
        self.tick(300)
        self.monitor.exhausted = True
        self.monitor.recovery_attempts = 3
        self.clock.return_value = 360
        self.service.reconnect_network("operator")
        self.tick(390)
        self.assertEqual(self.monitor.state, State.RECOVERY_EXHAUSTED)
        self.assertEqual(self.monitor.recovery_attempts, 3)

    def test_regular_monitor_activity_keeps_session_past_fifteen_minutes(self):
        self.device.signal_status.return_value = HEALTHY
        for timestamp in range(0, 1801, 30):
            self.tick(timestamp)
        self.device.login.assert_called_once()
        self.assertEqual(self.monitor.state, State.HEALTHY)
        self.tick(2041)
        self.assertEqual(self.device.login.call_count, 2)


class RunnerTests(SimpleTestCase):
    def test_unexpected_exception_is_sanitized_and_loop_stops(self):
        from modem.monitoring import MonitorRunner

        service = Mock()
        import threading

        service.lock = threading.RLock()
        runner = MonitorRunner(service)

        def fail():
            runner.stop_event.set()
            raise RuntimeError("private-password-and-response")

        service.monitor.tick.side_effect = fail
        with self.assertLogs("modem", "INFO") as logs:
            runner.start()
            runner.thread.join(timeout=2)
            runner.stop()
        self.assertFalse(runner.thread.is_alive())
        self.assertNotIn("private-password-and-response", "".join(logs.output))
        service.monitor.tick.assert_called_once()

    def test_database_cleanup_failure_does_not_kill_monitor(self):
        from modem.monitoring import MonitorRunner

        service = Mock()
        runner = MonitorRunner(service)
        runner.stop_event = Mock()
        runner.stop_event.is_set.side_effect = [False, False, True]
        with patch(
            "modem.monitoring.close_old_connections",
            side_effect=[None, RuntimeError("private-db-details"), None, None],
        ):
            with self.assertLogs("modem", "INFO") as logs:
                runner.run()
        self.assertEqual(service.monitor.tick.call_count, 2)
        self.assertEqual(runner.stop_event.wait.call_count, 2)
        self.assertNotIn("private-db-details", "".join(logs.output))
        self.assertIn("monitor_db_cleanup_failed", "".join(logs.output))

    def test_gunicorn_rejects_extra_or_overlapping_workers(self):
        import runpy

        from django.conf import settings

        hooks = runpy.run_path(str(settings.BASE_DIR / "gunicorn.conf.py"))
        server = Mock()
        server.cfg.workers = 2
        server.cfg.preload_app = False
        with self.assertRaises(RuntimeError):
            hooks["on_starting"](server)
        server.cfg.workers = 1
        hooks["on_starting"](server)
        server.WORKERS = {1: Mock()}
        with self.assertRaises(RuntimeError):
            hooks["pre_fork"](server, Mock())
