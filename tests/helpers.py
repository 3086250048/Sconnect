from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

from app.api import ApiHandler
from app.config import Settings
from app.db import Database
from app.manager import AccountManager
from app.service import AccountService


def test_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        admin_token="test-admin-token-123456",
        bind="127.0.0.1",
        port=8080,
        public_host="turn.test",
        external_ip="",
        listen_ip="0.0.0.0",
        relay_ip="",
        max_accounts=4,
        turn_port_base=34780,
        tls_port_base=53490,
        relay_port_base=50000,
        relay_ports_per_account=64,
        metrics_port_base=39100,
        cli_port_base=39000,
        usage_poll_seconds=5,
        tls_enabled=False,
        tls_cert="",
        tls_key="",
        turn_binary="turnserver",
        supervisor_enabled=False,
        allow_private_peers=False,
        allow_loopback_peers=False,
        relay_threads=1,
        api_tls_cert="",
        api_tls_key="",
    )


class _Writer:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, data: bytes) -> int:
        return self.buffer.write(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def getvalue(self) -> bytes:
        return self.buffer.getvalue()


class _MemoryConnection:
    def __init__(self, request: bytes) -> None:
        self.reader = io.BytesIO(request)
        self.writer = _Writer()

    def makefile(self, mode: str, *_args: object) -> io.BytesIO | _Writer:
        return self.reader if "r" in mode else self.writer

    def sendall(self, data: bytes) -> None:
        self.writer.write(data)

    def settimeout(self, _timeout: float) -> None:
        return None

    def setsockopt(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        return None


class _ApiState:
    def __init__(self, settings: Settings, db: Database, manager: AccountManager, service: AccountService):
        self.settings = settings
        self.db = db
        self.manager = manager
        self.service = service


class _SilentApiHandler(ApiHandler):
    def log_message(self, _fmt: str, *_args: object) -> None:
        return None


class RunningApi:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self.temp.name))
        self.db = Database(self.settings.data_dir / "sconnect.db")
        self.manager = AccountManager(self.settings, self.db)
        self.service = AccountService(self.settings, self.db, self.manager)
        self.server = _ApiState(self.settings, self.db, self.manager, self.service)

    def close(self) -> None:
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> tuple[int, dict | str | None]:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = [
            f"{method} {path} HTTP/1.1",
            "Host: test.local",
            "Connection: close",
            f"Content-Length: {len(data)}",
        ]
        if body is not None:
            headers.append("Content-Type: application/json")
        if token:
            headers.append(f"Authorization: Bearer {token}")
        raw_request = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + data
        connection = _MemoryConnection(raw_request)
        _SilentApiHandler(connection, ("127.0.0.1", 12345), self.server)  # type: ignore[arg-type]

        raw_response = connection.writer.getvalue()
        header_block, _separator, raw_body = raw_response.partition(b"\r\n\r\n")
        status_line = header_block.split(b"\r\n", 1)[0]
        status = int(status_line.split(b" ", 2)[1])
        response_headers: dict[str, str] = {}
        for line in header_block.split(b"\r\n")[1:]:
            key, _sep, value = line.partition(b":")
            response_headers[key.decode("latin-1").lower()] = value.decode("latin-1").strip()
        length = int(response_headers.get("content-length", len(raw_body)))
        raw_body = raw_body[:length]
        if not raw_body:
            return status, None
        if response_headers.get("content-type", "").startswith("application/json"):
            return status, json.loads(raw_body)
        return status, raw_body.decode("utf-8", errors="replace")

