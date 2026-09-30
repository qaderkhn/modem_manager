from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from modem.exceptions import (
    ModemAuthenticationError,
    ModemConnectionError,
    ModemRateLimited,
    ModemSessionExpired,
)


class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="app-password")
        self.client.force_login(self.user)
        self.service = Mock()
        self.service.is_authenticated.return_value = False
        self.patcher = patch("modem.views.get_modem_service", return_value=self.service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_modem_login_page(self):
        r = self.client.get(reverse("modem:login"))
        self.assertContains(r, "Login to modem")
        self.assertContains(r, 'type="password"')
        self.assertIn("no-store", r.headers["Cache-Control"])

    def test_invalid_form(self):
        r = self.client.post(reverse("modem:login"), {"username": "", "password": "secret"})
        self.assertEqual(r.status_code, 200)
        self.service.login.assert_not_called()
        self.assertNotContains(r, 'value="secret"')

    def test_password_whitespace_is_preserved(self):
        self.client.post(reverse("modem:login"), {"username": "admin", "password": " pass "})
        self.assertEqual(self.service.login.call_args.kwargs["password"], " pass ")

    def test_login_success(self):
        r = self.client.post(reverse("modem:login"), {"username": "admin", "password": "secret"})
        self.assertRedirects(r, reverse("modem:dashboard"))
        self.service.login.assert_called_once()
        self.assertNotIn("secret", str(dict(self.client.session)))

    def test_login_failure_is_generic(self):
        self.service.login.side_effect = ModemAuthenticationError("sensitive-response")
        r = self.client.post(reverse("modem:login"), {"username": "admin", "password": "secret"})
        self.assertContains(r, "Unable to authenticate", status_code=400)
        self.assertNotContains(r, "sensitive-response", status_code=400)
        self.assertNotContains(r, 'value="secret"', status_code=400)

    def test_login_rate_limit(self):
        self.service.login.side_effect = ModemRateLimited()
        r = self.client.post(reverse("modem:login"), {"username": "admin", "password": "secret"})
        self.assertEqual(r.status_code, 429)

    def test_dashboard_disabled(self):
        r = self.client.get(reverse("modem:dashboard"))
        self.assertContains(r, "disabled>Reconnect Network")
        self.assertContains(r, "Not authenticated")

    def test_dashboard_enabled(self):
        self.service.is_authenticated.return_value = True
        r = self.client.get(reverse("modem:dashboard"))
        self.assertNotContains(r, "disabled>Reconnect Network")
        self.assertContains(r, "Authenticated")

    def test_reconnect_success(self):
        r = self.client.post(reverse("modem:reconnect"), follow=True)
        self.assertContains(r, "Network reconnect requested successfully.")
        self.service.reconnect_network.assert_called_once()

    def test_backend_rejects_unauthenticated_modem(self):
        self.service.reconnect_network.side_effect = ModemSessionExpired()
        r = self.client.post(reverse("modem:reconnect"), follow=True)
        self.assertContains(r, "Log in again.")
        self.assertEqual(r.request["PATH_INFO"], reverse("modem:login"))

    def test_reconnect_failure_and_rate_limit(self):
        for exception, phrase in [
            (ModemConnectionError("sensitive"), "Unable to confirm reconnect"),
            (ModemRateLimited(), "Please wait 30 seconds"),
        ]:
            self.service.reconnect_network.side_effect = exception
            r = self.client.post(reverse("modem:reconnect"), follow=True)
            self.assertContains(r, phrase)
            self.assertNotContains(r, "sensitive")

    def test_logout(self):
        r = self.client.post(reverse("modem:logout"))
        self.assertRedirects(r, reverse("modem:login"))
        self.service.logout.assert_called_once()

    def test_app_logout_clears_modem(self):
        r = self.client.post(reverse("app_logout"))
        self.assertRedirects(r, reverse("app_login"))
        self.service.logout.assert_called_once()
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_state_changes_reject_get(self):
        for name in ["modem:reconnect", "modem:logout", "app_logout"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 405)
        self.service.reconnect_network.assert_not_called()
        self.service.logout.assert_not_called()

    def test_csrf_required_for_all_posts(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name in ["modem:login", "modem:reconnect", "modem:logout", "app_logout"]:
            self.assertEqual(client.post(reverse(name)).status_code, 403)
        self.service.login.assert_not_called()
        self.service.reconnect_network.assert_not_called()
        self.service.logout.assert_not_called()

    def test_valid_csrf_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get(reverse("modem:login"))
        token = client.cookies["csrftoken"].value
        r = client.post(
            reverse("modem:login"),
            {"username": "admin", "password": "secret", "csrfmiddlewaretoken": token},
        )
        self.assertEqual(r.status_code, 302)
        self.service.login.assert_called_once()

    def test_django_auth_required(self):
        client = Client()
        for name in ["modem:dashboard", "modem:login", "modem:reconnect", "modem:logout"]:
            r = client.post(reverse(name))
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.url.startswith(reverse("app_login")))
        self.service.login.assert_not_called()
        self.service.reconnect_network.assert_not_called()

    def test_application_login(self):
        client = Client()
        r = client.post(reverse("app_login"), {"username": "operator", "password": "app-password"})
        self.assertRedirects(r, reverse("modem:dashboard"))

    def test_health_has_no_modem_interaction(self):
        self.assertEqual(Client().get(reverse("health")).json(), {"status": "ok"})
        self.service.assert_not_called()
        self.assertEqual(self.service.method_calls, [])

    @override_settings(MODEM_MONITOR_ENABLED=True)
    def test_monitor_snapshot_contains_no_credentials(self):
        from modem.health import ModemHealth

        self.service.monitor.snapshot.return_value = {
            "enabled": True,
            "state": "HEALTHY",
            "health": ModemHealth(),
            "failures": 0,
        }
        response = self.client.get(reverse("modem:dashboard"))
        self.assertContains(response, "LTE Monitor")
        self.assertNotContains(response, "test-password")
        self.assertNotIn("password", dict(self.client.session))

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_health_remains_local_liveness_with_https_redirect(self):
        with self.assertNumQueries(0):
            response = Client().get(reverse("health"))
        self.assertEqual(response.status_code, 200)
