from __future__ import annotations

import hmac
import json
import logging
import re
import shutil
import ssl
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .credentials import hash_api_key
from .db import Database
from .manager import AccountManager
from .service import AccountService, ConflictError, ValidationError


LOG = logging.getLogger("sconnect.api")
ACCOUNT_PATH = re.compile(r"^/v1/accounts/([A-Za-z0-9_-]{1,64})(?:/([a-z0-9-]+))?$")


class AuthenticationError(PermissionError):
    pass


class ApiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        settings: Settings,
        service: AccountService,
        manager: AccountManager,
    ):
        self.settings = settings
        self.service = service
        self.manager = manager
        self.db: Database = service.db
        super().__init__(server_address, ApiHandler)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "Sconnect/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ApiServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz" and method == "GET":
                return self._json(200, {"status": "ok"})
            if parsed.path == "/readyz" and method == "GET":
                return self._ready()
            if parsed.path == "/metrics" and method == "GET":
                self._require_admin()
                return self._metrics()

            role, key_account = self._authenticate()
            if parsed.path == "/v1/accounts":
                if method == "GET":
                    self._require_admin(role)
                    accounts = [self.app.service.serialize_account(row) for row in self.app.db.list_accounts()]
                    return self._json(200, {"accounts": accounts})
                if method == "POST":
                    self._require_admin(role)
                    account, api_key = self.app.service.create_account(self._read_json())
                    body = self.app.service.serialize_account(account)
                    body["api_key"] = api_key
                    return self._json(201, body)

            match = ACCOUNT_PATH.match(parsed.path)
            if not match:
                return self._json(404, {"error": "not found"})
            account_id, action = match.groups()
            if role == "account" and key_account and key_account["id"] != account_id:
                return self._json(403, {"error": "forbidden"})

            if action is None:
                if method == "GET":
                    account = self._account(account_id)
                    return self._json(200, self.app.service.serialize_account(account))
                if method == "PATCH":
                    self._require_admin(role)
                    account = self.app.service.update_account(account_id, self._read_json())
                    return self._json(200, self.app.service.serialize_account(account))
                if method == "DELETE":
                    self._require_admin(role)
                    self.app.service.delete_account(account_id)
                    return self._json(204, None)

            if action == "usage" and method == "GET":
                account = self._account(account_id)
                usage = self.app.manager.account_usage(account)
                usage["runtime"] = self.app.manager.runtime_info(account)
                return self._json(200, usage)

            if action == "credentials" and method == "POST":
                account = self._account(account_id)
                body = self._read_json(optional=True)
                ttl = body.get("ttl_seconds")
                if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int)):
                    raise ValidationError("ttl_seconds must be an integer")
                return self._json(200, self.app.service.issue_credentials(account, ttl))

            if action == "ice-servers" and method == "GET":
                account = self._account(account_id)
                query = parse_qs(parsed.query)
                ttl = None
                if "ttl_seconds" in query:
                    try:
                        ttl = int(query["ttl_seconds"][0])
                    except ValueError as exc:
                        raise ValidationError("ttl_seconds must be an integer") from exc
                payload = self.app.service.issue_credentials(account, ttl)
                return self._json(
                    200,
                    {
                        "iceServers": payload["ice_servers"],
                        "expires_at": payload["expires_at"],
                        "ttl_seconds": payload["ttl_seconds"],
                    },
                )

            if action == "rotate-api-key" and method == "POST":
                self._require_admin(role)
                account, api_key = self.app.service.rotate_api_key(account_id)
                body = self.app.service.serialize_account(account)
                body["api_key"] = api_key
                return self._json(200, body)

            if action == "reset-usage" and method == "POST":
                self._require_admin(role)
                account = self.app.service.reset_usage(account_id)
                return self._json(200, self.app.service.serialize_account(account))

            return self._json(404, {"error": "not found"})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except ConflictError as exc:
            self._json(409, {"error": str(exc)})
        except AuthenticationError as exc:
            self._json(401, {"error": str(exc)})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "account not found"})
        except Exception:
            LOG.error("request failed\n%s", traceback.format_exc())
            self._json(500, {"error": "internal server error"})

    def _account(self, account_id: str) -> dict[str, Any]:
        account = self.app.db.get_account(account_id)
        if not account:
            raise KeyError(account_id)
        return account

    def _authenticate(self) -> tuple[str, dict[str, Any] | None]:
        header = self.headers.get("Authorization", "")
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            self._raise_auth()
        token = parts[1].strip()
        if hmac.compare_digest(token.encode("utf-8"), self.app.settings.admin_token.encode("utf-8")):
            return "admin", None
        account = self.app.db.get_account_by_key_hash(hash_api_key(token))
        if account:
            return "account", account
        self._raise_auth()
        return "unknown", None

    def _require_admin(self, role: str | None = None) -> None:
        if role is None:
            role, _account = self._authenticate()
        if role != "admin":
            raise PermissionError("admin token required")

    @staticmethod
    def _raise_auth() -> None:
        raise AuthenticationError("invalid bearer token")

    def _read_json(self, optional: bool = False) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0 or length > 65536:
            raise ValidationError("request body is too large")
        if length == 0:
            if optional:
                return {}
            raise ValidationError("JSON body is required")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("JSON body must be an object")
        return value

    def _json(self, status: int, payload: Any) -> None:
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _ready(self) -> None:
        binary = self.app.settings.turn_binary
        available = bool(shutil.which(binary)) if not Path(binary).is_absolute() else Path(binary).exists()
        try:
            self.app.db.list_accounts()
            db_ok = True
        except Exception:
            db_ok = False
        status = 200 if available and db_ok else 503
        self._json(status, {"ready": status == 200, "turnserver": available, "database": db_ok})

    def _metrics(self) -> None:
        lines = [
            "# HELP sconnect_build_info Sconnect control-plane build information",
            "# TYPE sconnect_build_info gauge",
            'sconnect_build_info{version="0.1.0"} 1',
        ]
        for account in self.app.db.list_accounts():
            account_id = account["id"]
            serialized = self.app.service.serialize_account(account)
            usage = serialized["usage"]
            runtime = serialized["runtime"]
            labels = f'account="{account_id}"'
            lines.extend(
                [
                    f'sconnect_account_enabled{{{labels}}} {1 if serialized["enabled"] else 0}',
                    f'sconnect_account_running{{{labels}}} {1 if runtime["running"] else 0}',
                    f'sconnect_account_active_sessions{{{labels}}} {usage["active_sessions"]}',
                    f'sconnect_account_max_bps{{{labels}}} {serialized["max_bps"]}',
                    f'sconnect_account_quota_bytes{{{labels}}} {serialized["traffic_quota_bytes"]}',
                    f'sconnect_account_usage_bytes{{{labels},direction="rx"}} {usage["received_bytes"]}',
                    f'sconnect_account_usage_bytes{{{labels},direction="tx"}} {usage["sent_bytes"]}',
                    f'sconnect_account_usage_bytes{{{labels},direction="total"}} {usage["total_bytes"]}',
                ]
            )
            if serialized["suspended_reason"]:
                reason = str(serialized["suspended_reason"]).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'sconnect_account_suspended{{{labels},reason="{reason}"}} 1')
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    settings: Settings,
    service: AccountService,
    manager: AccountManager,
) -> ApiServer:
    server = ApiServer((settings.bind, settings.port), settings, service, manager)
    if settings.api_tls_cert and settings.api_tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(settings.api_tls_cert, settings.api_tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server

