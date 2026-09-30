import json
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from modem.exceptions import (
    ModemAPIError,
    ModemAuthenticationError,
    ModemConnectionError,
    ModemSessionExpired,
)
from modem.modem_client import ModemClient


def response(data=None, status=200, raw=None):
    result = requests.Response()
    result.status_code = status
    result._content = raw if raw is not None else json.dumps(data).encode()
    result.url = "http://modem/api/test"
    return result


class ClientTests(TestCase):
    def setUp(self):
        self.client = ModemClient("http://modem", timeout=7)
        self.http = Mock()
        self.client.session.request = self.http
        self.addCleanup(self.client.close)

    def test_token_queue_and_refresh(self):
        self.http.side_effect = [
            response({"tokens": "one,two,"}),
            response({"ErrorCode": 0}),
            response({"ErrorCode": 0}),
            response({"tokens": "three"}),
            response({"ErrorCode": 0}),
        ]
        for _ in range(3):
            self.client.reconnect_network()
        tokens = [c.kwargs["headers"]["requestVerificationToken"] for c in self.http.call_args_list]
        self.assertEqual(tokens, ["crsf_token", "one", "two", "crsf_token", "three"])
        for c in self.http.call_args_list:
            self.assertEqual(c.kwargs["timeout"], 7)
            self.assertFalse(c.kwargs["allow_redirects"])
        self.assertEqual(
            self.http.call_args.args, ("GET", "http://modem/api/dialupmng/rebootModem")
        )

    def test_invalid_token_responses(self):
        for data in [None, [], {}, {"tokens": None}, {"tokens": ""}, {"tokens": ",,"}]:
            with self.subTest(data=data):
                self.http.return_value = response(data)
                with self.assertRaises((ModemAuthenticationError, ModemAPIError)):
                    self.client._refresh_tokens()

    def login_responses(self, signature):
        return [
            response(raw=b"<html/>"),
            response({"tokens": "a,b,unused"}),
            response(
                {
                    "salt": "00112233445566778899aabbccddeeff",
                    "iterations": 4096,
                    "servernonce": "server-nonce",
                }
            ),
            response({"serversignature": signature}),
            response({"tokens": "fresh"}),
            response(raw=b""),
        ]

    @patch("modem.modem_client.secrets.token_hex", return_value="first-nonce")
    def test_login_known_protocol_vector(self, nonce):
        # Fixed vector for the N5368X protocol's unusual HMAC key/message order.
        self.http.side_effect = self.login_responses(SERVER_SIGNATURE)
        self.client.login("admin", "test-password")
        self.assertEqual(self.http.call_count, 6)
        calls = self.http.call_args_list
        self.assertEqual(
            json.loads(calls[2].kwargs["data"]), {"username": "admin", "firstnonce": "first-nonce"}
        )
        self.assertEqual(
            json.loads(calls[3].kwargs["data"]),
            {"clientproof": CLIENT_PROOF, "finalnonce": "server-nonce"},
        )
        self.assertEqual(calls[5].kwargs["headers"]["requestVerificationToken"], "fresh")
        self.assertEqual(json.loads(calls[5].kwargs["data"]), {"status": 0})
        nonce.assert_called_once_with(32)

    @patch("modem.modem_client.secrets.token_hex", return_value="first-nonce")
    def test_bad_server_signatures(self, nonce):
        for value in ["00" * 32, "not-hex", None]:
            with self.subTest(value=value):
                self.client.tokens.clear()
                self.http.side_effect = self.login_responses(value)
                with self.assertRaises(ModemAuthenticationError):
                    self.client.login("admin", "test-password")

    def test_invalid_challenges(self):
        for data in [
            {},
            [],
            {"salt": "zz", "iterations": 1, "servernonce": "n"},
            {"salt": "00", "iterations": 0, "servernonce": "n"},
            {"salt": "00", "iterations": 1000001, "servernonce": "n"},
            {"salt": "00", "iterations": "bad", "servernonce": "n"},
            {"salt": "00", "iterations": 1, "servernonce": 5},
        ]:
            with self.subTest(data=data):
                self.http.side_effect = [
                    response(raw=b"page"),
                    response({"tokens": "a"}),
                    response(data),
                ]
                with self.assertRaises((ModemAuthenticationError, ModemAPIError)):
                    self.client.login("user", "password")

    def test_blank_credentials_do_not_send_http(self):
        for username, password in [(" ", "pass"), ("admin", " ")]:
            with self.assertRaises(ModemAuthenticationError):
                self.client.login(username, password)
        self.http.assert_not_called()

    def test_malformed_json(self):
        self.http.return_value = response(raw=b"<html>private-data</html>")
        with self.assertRaises(ModemAPIError) as exc:
            self.client._request("GET", "web/heartbeat", "token")
        self.assertNotIn("private-data", str(exc.exception))

    def test_api_errors_are_sanitized(self):
        for data in [{"error": "sensitive"}, {"ErrorCode": "sensitive"}]:
            self.http.return_value = response(data)
            with self.assertRaises(ModemAPIError) as exc:
                self.client._request("GET", "web/heartbeat", "token")
            self.assertNotIn("sensitive", str(exc.exception))

    def test_http_errors(self):
        for status in [400, 404, 500, 503]:
            self.http.return_value = response({}, status=status)
            with self.assertRaises(ModemConnectionError):
                self.client._request("GET", "web/heartbeat", "token")

    def test_redirects_and_unauthorized(self):
        for status in [301, 302, 307, 401, 403]:
            self.http.return_value = response({}, status=status)
            with self.assertRaises(ModemSessionExpired):
                self.client.login("user", "password")

    def test_transport_errors(self):
        for error in [
            requests.Timeout("secret"),
            requests.ConnectionError("secret"),
            requests.exceptions.SSLError("secret"),
        ]:
            self.http.side_effect = error
            with self.assertRaises(ModemConnectionError) as exc:
                self.client._request("GET", "web/heartbeat", "token")
            self.assertNotIn("secret", str(exc.exception))

    def test_heartbeat_requires_explicit_success(self):
        for data in [None, {}, [], {"ErrorCode": 1}]:
            self.client.tokens.append("t")
            self.http.return_value = response(data)
            with self.assertRaises(ModemAPIError):
                self.client.heartbeat()
        for code in [0, "0"]:
            self.client.tokens.append("t")
            self.http.return_value = response({"ErrorCode": code})
            self.client.heartbeat()

    def test_reconnect_requires_explicit_acknowledgement_without_retry(self):
        for data in (None, {}, {"ErrorCode": False}, {"ErrorCode": 1}):
            with self.subTest(data=data):
                self.client.tokens.append("token")
                self.http.reset_mock()
                self.http.return_value = response(data)
                with self.assertRaises(ModemAPIError):
                    self.client.reconnect_network()
                self.http.assert_called_once()
        self.client.tokens.append("token")
        self.http.return_value = response(raw=b"")
        with self.assertRaises(ModemAPIError):
            self.client.reconnect_network()

    def test_transport_diagnostics_never_log_exception_text(self):
        self.http.side_effect = requests.ConnectionError("private-password-cookie-token")
        with self.assertLogs("modem", "WARNING") as logs:
            with self.assertRaises(ModemConnectionError):
                self.client.heartbeat()
        self.assertIn("category=ConnectionError", "".join(logs.output))
        self.assertNotIn("private-password-cookie-token", "".join(logs.output))

    def test_tls_and_close(self):
        self.assertEqual(self.client.session.headers["Connection"], "close")
        self.assertTrue(self.client.session.verify)
        self.assertFalse(self.client.session.trust_env)
        other = ModemClient("https://modem", verify="/ca.pem")
        self.assertEqual(other.session.verify, "/ca.pem")
        other.close()
        self.client.tokens.append("secret")
        self.client.session.cookies.set("session", "secret")
        self.client.close()
        self.assertFalse(self.client.tokens)
        self.assertFalse(self.client.session.cookies)


CLIENT_PROOF = "de660be3ede1483fcdaab7fc0e318c6155ff0e8bc8e4666555802e79fd4dcd32"
SERVER_SIGNATURE = "30d531625ba9864a43933ec4ab5e69aeba455b2af2253bd601cd3705cc2c7c33"


class SignalClientTests(TestCase):
    setUp = ClientTests.setUp

    def test_signal_paths_use_existing_tokens(self):
        self.client.tokens.extend(["one", "two"])
        self.http.return_value = response({"ErrorCode": 0})
        self.client.signal_status()
        self.client.signal_level()
        self.assertEqual(
            [call.args[1] for call in self.http.call_args_list],
            ["http://modem/api/modemmng/getSignal", "http://modem/api/signalmng/getsiglevel"],
        )
        self.assertEqual(
            [
                call.kwargs["headers"]["requestVerificationToken"]
                for call in self.http.call_args_list
            ],
            ["one", "two"],
        )


class ProtectedStatusTests(TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        self.client = ModemClient("http://modem")
        self.addCleanup(self.client.close)
        self.result = {
            "ErrorCode": 0,
            "result": {
                "totalTxFlow": "100",
                "totalRxFlow": "200",
                "totalDsTime": "30",
            },
        }

    def test_encrypted_body_matches_firmware_and_uses_tokens(self):
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        self.client.tokens.extend(["key-token", "post-token"])
        self.client.session.request = Mock(
            side_effect=[response({"pubkey": self.pem}), response(self.result)]
        )
        self.client.verify_session()
        calls = self.client.session.request.call_args_list
        self.assertEqual(calls[0].args, ("GET", "http://modem/api/web/pubkey"))
        self.assertEqual(
            calls[1].args, ("POST", "http://modem/api/modemmng/queryModemMonitorWithName")
        )
        body = calls[1].kwargs["data"]
        self.assertEqual(len(body), 344)
        plaintext = self.key.decrypt(
            base64.b64decode(body, validate=True),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
        )
        self.assertEqual(plaintext, b'{"monitorName":"dataFlow","argJson":{}}')
        self.assertEqual(calls[1].kwargs["headers"]["requestVerificationToken"], "post-token")
        self.assertEqual(
            calls[1].kwargs["headers"]["Content-Type"],
            "application/x-www-form-urlencoded; charset=UTF-8",
        )

    def test_malformed_key_never_dispatches_protected_post(self):
        for key in (None, "bad-key", "x" * 8193, "non-ascii-\u1234"):
            self.client._api = Mock(return_value={"pubkey": key})
            with self.assertRaises(ModemAPIError):
                self.client.verify_session()
            self.client._api.assert_called_once_with("GET", "web/pubkey")

    def test_unknown_or_rejected_protected_response_cannot_authenticate(self):
        for result in (
            None,
            {},
            {"ErrorCode": 0},
            {"ErrorCode": 0, "result": {}},
            {"ErrorCode": 0, "result": {"totalTxFlow": True, "totalRxFlow": 1, "totalDsTime": 1}},
            ModemAPIError("private response"),
        ):
            self.client._api = Mock(side_effect=[{"pubkey": self.pem}, result])
            with self.assertRaises(ModemSessionExpired) as error:
                self.client.verify_session()
            self.assertNotIn("private response", str(error.exception))
