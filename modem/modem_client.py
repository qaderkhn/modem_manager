import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections import deque

import requests
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.views.decorators.debug import sensitive_variables

from modem.exceptions import (
    ModemAPIError,
    ModemAuthenticationError,
    ModemConnectionError,
    ModemSessionExpired,
)

log = logging.getLogger(__name__)


class ModemClient:
    """Huawei N5368X web API; wire-format quirks are firmware requirements."""

    def __init__(self, base_url, verify=True, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        self.session.trust_env = False
        self.session.headers.update(
            {
                # Firmware can drop idle HTTP keep-alive connections between polls.
                # Cookie/token authentication remains in this Session, independently
                # of the TCP connection. Never retry a possibly acted-on request.
                "Connection": "close",
                "_ResponseSource": "Broswer",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.base_url + "/html/login.html",
            }
        )
        self.tokens = deque()

    @sensitive_variables()
    def _request(self, method, path, token, payload=None):
        headers = {"requestVerificationToken": token}
        kwargs = {}

        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            kwargs["data"] = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )

        response = self._http(method, self.base_url + "/api/" + path, headers=headers, **kwargs)

        # login_done's browser callback ignores its response body.
        if not response.content:
            return None

        try:
            result = response.json()
        except ValueError:
            raise ModemAPIError("Expected a JSON response") from None

        if not isinstance(result, dict):
            raise ModemAPIError("Expected a JSON object")

        if "ErrorCode" in result and str(result["ErrorCode"]) != "0":
            raise ModemAPIError("Modem rejected the operation")
        if result.get("error"):
            raise ModemAPIError("Modem rejected the operation")

        return result

    def _refresh_tokens(self):
        result = self._request("GET", "web/crsf_token", "crsf_token")
        if not isinstance(result, dict):
            raise ModemAuthenticationError("Token endpoint returned no object")

        value = result.get("tokens")
        if not isinstance(value, str):
            raise ModemAuthenticationError("Token endpoint returned no token string")

        self.tokens = deque(t for t in value.split(",") if t)
        if not self.tokens:
            raise ModemAuthenticationError("Token endpoint returned an empty token list")

    def _api(self, method, path, payload=None):
        if not self.tokens:
            self._refresh_tokens()
        token = self.tokens.popleft()

        return self._request(method, path, token, payload)

    @sensitive_variables()
    def login(self, username, password):
        if not username.strip() or not password.strip():
            raise ModemAuthenticationError("Username and password must not be blank")

        # Establish any cookies that the login page sets.
        self._http("GET", self.base_url + "/html/login.html")

        self._refresh_tokens()
        first_nonce = secrets.token_hex(32)

        challenge = self._api(
            "POST",
            "login/login_challenge",
            {
                "username": username,
                "firstnonce": first_nonce,
            },
        )

        required = ("salt", "iterations", "servernonce")
        if not isinstance(challenge, dict) or any(field not in challenge for field in required):
            raise ModemAuthenticationError("Incomplete login challenge")

        server_nonce = challenge["servernonce"]
        if not isinstance(server_nonce, str):
            raise ModemAuthenticationError("Invalid server nonce")

        try:
            iterations = int(challenge["iterations"])
            salt = bytes.fromhex(challenge["salt"])
        except (ValueError, TypeError, OverflowError):
            raise ModemAuthenticationError("Invalid login challenge") from None
        if (
            not 0 < iterations <= 1_000_000
            or not salt
            or len(salt) > 1024
            or not server_nonce
            or len(server_nonce) > 4096
        ):
            raise ModemAuthenticationError("Invalid login challenge")

        salted_password = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=32,
        )
        auth_message = (first_nonce + "," + server_nonce + "," + server_nonce).encode("utf-8")

        # Preserve the N5368X browser protocol's unusual HMAC key/message order.
        client_key = hmac.new(b"Client Key", salted_password, hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        client_signature = hmac.new(auth_message, stored_key, hashlib.sha256).digest()
        client_proof = bytes(a ^ b for a, b in zip(client_key, client_signature))

        result = self._api(
            "POST",
            "login/login_auth",
            {
                "clientproof": client_proof.hex(),
                "finalnonce": server_nonce,
            },
        )

        if not isinstance(result, dict) or not isinstance(result.get("serversignature"), str):
            raise ModemAuthenticationError("Authentication returned no server signature")

        server_key = hmac.new(b"Server Key", salted_password, hashlib.sha256).digest()
        expected_signature = hmac.new(auth_message, server_key, hashlib.sha256).digest()

        try:
            actual_signature = bytes.fromhex(result["serversignature"])
        except ValueError:
            raise ModemAuthenticationError("Malformed server signature") from None

        if not hmac.compare_digest(expected_signature, actual_signature):
            raise ModemAuthenticationError("Server signature verification failed")

        # Match loginDoneSubmit(): discard pre-finalization tokens.
        self.tokens.clear()
        self._api("POST", "login/login_done", {"status": 0})

    @sensitive_variables()
    def _http(self, method, url, **kwargs):
        try:
            response = self.session.request(
                method, url, timeout=self.timeout, allow_redirects=False, **kwargs
            )
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise ModemSessionExpired("Modem session is invalid")
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            # Diagnostics distinguish device HTTP errors from local transport errors.
            # Never include exception text, URLs, headers, payloads or credentials.
            status = exc.response.status_code if exc.response is not None else None
            cause = exc.__cause__ or exc.__context__
            log.warning(
                "event=modem_http_failed category=%s cause=%s status=%s",
                type(exc).__name__,
                type(cause).__name__ if cause else "unknown",
                status,
            )
            raise ModemConnectionError("Modem HTTP request failed") from None

    def heartbeat(self):
        """Public API liveness only; not evidence of modem authentication or LTE service."""
        result = self._api("GET", "web/heartbeat")
        if not isinstance(result, dict) or str(result.get("ErrorCode")) != "0":
            raise ModemAPIError("Heartbeat did not confirm API availability")

    def verify_session(self):
        """Read protected dataFlow status, also refreshing the modem's idle timer.

        index.js uses saveAjaxData with this exact JSON; util.js encrypts it using
        web/pubkey. The firmware rsa.min.js uses NodeRSA's OAEP/SHA-1 defaults.
        Public heartbeat/signal reads are not authentication evidence.
        """
        key_response = self._api("GET", "web/pubkey")
        pem = key_response.get("pubkey") if isinstance(key_response, dict) else None
        if not isinstance(pem, str) or len(pem) > 8192:
            raise ModemAPIError("Invalid modem public key")
        try:
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 4096:
                raise ValueError
            ciphertext = key.encrypt(
                b'{"monitorName":"dataFlow","argJson":{}}',
                padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
            )
        except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
            raise ModemAPIError("Invalid modem public key") from None
        try:
            result = self._api(
                "POST", "modemmng/queryModemMonitorWithName", base64.b64encode(ciphertext)
            )
        except ModemAPIError:
            raise ModemSessionExpired("Protected status did not confirm authentication") from None
        counters = result.get("result") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or str(result.get("ErrorCode")) != "0"
            or not isinstance(counters, dict)
            or not all(
                isinstance(counters.get(name), (str, int))
                and not isinstance(counters[name], bool)
                and str(counters[name]).isascii()
                and str(counters[name]).isdigit()
                and len(str(counters[name])) <= 30
                for name in ("totalTxFlow", "totalRxFlow", "totalDsTime")
            )
        ):
            raise ModemSessionExpired("Protected status did not confirm authentication")

    def signal_status(self):
        return self._api("GET", "modemmng/getSignal")

    def signal_level(self):
        return self._api("GET", "signalmng/getsiglevel")

    def reconnect_network(self):
        result = self._api("GET", "dialupmng/rebootModem")
        if not isinstance(result, dict) or str(result.get("ErrorCode")) != "0":
            raise ModemAPIError("Reconnect acknowledgement could not be verified")
        return result

    def close(self):
        self.tokens.clear()
        self.session.cookies.clear()
        self.session.close()
