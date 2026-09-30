"""Linux Docker smoke test against a local fake modem; never touches a real modem.

Run after docker compose build with Python 3.12.
Set MODEM_SMOKE_IMAGE to override the default modem_manager-app image.
Only synthetic credentials are used. Temporary containers/files are removed.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class FakeModem(BaseHTTPRequestHandler):
    nonce = ""
    paths = []
    authenticated = False
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply()

    def do_POST(self):
        self.reply()

    def reply(self):
        type(self).paths.append(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        result = {"ErrorCode": 0}
        if self.path.endswith("/crsf_token"):
            result = {"tokens": "synthetic-one,synthetic-two,synthetic-three"}
        elif self.path.endswith("/login_challenge"):
            type(self).nonce = json.loads(body)["firstnonce"]
            result = {"salt": "00112233", "iterations": 10, "servernonce": "synthetic-server"}
        elif self.path.endswith("/login_auth"):
            salted = hashlib.pbkdf2_hmac(
                "sha256", b"synthetic-password", bytes.fromhex("00112233"), 10
            )
            key = hmac.new(b"Server Key", salted, hashlib.sha256).digest()
            message = f"{self.nonce},synthetic-server,synthetic-server".encode()
            result = {"serversignature": hmac.new(message, key, hashlib.sha256).hexdigest()}
        elif self.path.endswith("/login_done"):
            type(self).authenticated = True
        elif self.path.endswith("/pubkey"):
            result["pubkey"] = (
                self.private_key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode()
            )
        elif self.path.endswith("/queryModemMonitorWithName"):
            plaintext = self.private_key.decrypt(
                base64.b64decode(body, validate=True),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
            )
            assert plaintext == b'{"monitorName":"dataFlow","argJson":{}}'
            assert self.authenticated
            result["result"] = {"totalTxFlow": "100", "totalRxFlow": "200", "totalDsTime": "30"}
        elif self.path.endswith("/getSignal"):
            result.update(
                ServiceStatus=1,
                SysModeName="LTE",
                LteRssi=-52,
                LteRsrp=-82,
                LteRsrq=-10,
                LteSinr=10,
                NrRsrp=-999,
            )
        elif self.path.endswith("/getsiglevel"):
            result["LteLevel"] = "LTE_SIGNAL_LEVEL_FOUR"
        payload = json.dumps(result).encode()
        authenticated_cookie = "synthetic-session=present" in self.headers.get("Cookie", "")
        self.send_response(
            401 if self.path.startswith("/api/") and not authenticated_cookie else 200
        )
        if self.path.endswith("/login.html"):
            self.send_header("Set-Cookie", "synthetic-session=present; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModem)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = "modem-smoke-" + secrets.token_hex(4)
    image = os.getenv("MODEM_SMOKE_IMAGE", "modem_manager-app:latest")
    volume = name + "-data"
    with tempfile.TemporaryDirectory(prefix="modem-smoke-") as directory:
        root = Path(directory)
        (root / "username").write_text("synthetic-user")
        (root / "password").write_text("synthetic-password")
        for path in (root / "username", root / "password"):
            path.chmod(0o600)
            subprocess.run(["setfacl", "-m", "u:10001:r--,g::---,o::---", str(path)], check=True)
        env = {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DJANGO_SQLITE_PATH": "/data/db.sqlite3",
            "MODEM_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "MODEM_MONITOR_ENABLED": "true",
            "MODEM_AUTO_RECONNECT_ENABLED": "false",
            "MODEM_USERNAME_FILE": "/run/secrets/username",
            "MODEM_PASSWORD_FILE": "/run/secrets/password",
        }
        args = [
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            "host",
            "--mount",
            f"type=volume,src={volume},dst=/data",
            "--mount",
            f"type=bind,src={root / 'username'},dst=/run/secrets/username,readonly",
            "--mount",
            f"type=bind,src={root / 'password'},dst=/run/secrets/password,readonly",
        ]
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        # Pick an unused local port; the short reservation ends before Gunicorn starts.
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        try:
            docker(
                "run",
                "-d",
                "--name",
                name,
                *args,
                image,
                "gunicorn",
                "-c",
                "gunicorn.conf.py",
                "--bind",
                f"127.0.0.1:{port}",
                "config.wsgi:application",
            )
            for _ in range(60):
                logs = docker("logs", name)  # Docker writes stderr separately; inspect both below.
                output = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
                logs += output.stderr
                if "health_state=HEALTHY" in logs:
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("Monitor did not reach HEALTHY against mock modem")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/", timeout=5) as response:
                assert json.load(response) == {"status": "ok"}
            assert "/api/modemmng/queryModemMonitorWithName" in FakeModem.paths
            assert "/api/modemmng/getSignal" in FakeModem.paths
            assert "/api/signalmng/getsiglevel" in FakeModem.paths
            assert "/api/dialupmng/rebootModem" not in FakeModem.paths
            assert "synthetic-password" not in logs and "synthetic-user" not in logs
            assert docker("exec", name, "id", "-u").strip() == "10001"
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; assert not Path('/app/.env').exists(); assert not Path('/app/db.sqlite3').exists()",
            )
            docker(
                "exec",
                name,
                "uv",
                "run",
                "--no-sync",
                "python",
                "-c",
                "import os; assert os.access('/home/app', os.W_OK); assert not os.access('/app', os.W_OK)",
            )
            status = docker("exec", name, "cat", "/proc/1/status")
            assert "CapEff:\t0000000000000000" in status
            assert "NoNewPrivs:\t1" in status
            print(
                "PASS: non-root container, migrations, mounted credentials, challenge login, monitor HEALTHY, liveness, no reconnect or credential logs"
            )
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
            server.shutdown()


if __name__ == "__main__":
    main()
