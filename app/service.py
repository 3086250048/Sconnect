from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from .config import Settings
from .coturn import turn_urls
from .credentials import create_turn_credential, hash_api_key, new_api_key, new_turn_secret
from .db import Database
from .manager import AccountManager


class ValidationError(ValueError):
    pass


class ConflictError(RuntimeError):
    pass


class AccountService:
    def __init__(self, settings: Settings, db: Database, manager: AccountManager):
        self.settings = settings
        self.db = db
        self.manager = manager
        self._create_lock = threading.Lock()

    def create_account(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValidationError("name must be between 1 and 80 characters")

        max_bps = self._int_value(payload, "max_bps", 2_000_000, 10_000, 10_000_000_000)
        max_allocations = min(100, self.settings.relay_ports_per_account)
        allocation_quota = self._int_value(payload, "allocation_quota", 4, 1, max_allocations)
        traffic_quota_bytes = self._int_value(payload, "traffic_quota_bytes", 10 * 1024**3, 0, 10**15)
        credential_ttl = self._int_value(payload, "credential_ttl", 3600, 60, 86400)

        with self._create_lock:
            used_slots = self.db.used_slots()
            slot = next(
                (candidate for candidate in range(self.settings.max_accounts) if candidate not in used_slots),
                None,
            )
            if slot is None:
                raise ConflictError("maximum account count reached")

            account_id = "acct_" + secrets.token_hex(6)
            api_key = new_api_key()
            now = int(time.time())
            values = {
                "id": account_id,
                "name": name,
                "created_at": now,
                "updated_at": now,
                "enabled": 1,
                "suspended_reason": None,
                "max_bps": max_bps,
                "allocation_quota": allocation_quota,
                "traffic_quota_bytes": traffic_quota_bytes,
                "credential_ttl": credential_ttl,
                "api_key_hash": hash_api_key(api_key),
                "auth_secret": new_turn_secret(),
                "realm": f"sconnect-{account_id}",
                "slot": slot,
                "turn_port": self.settings.turn_port_base + slot,
                "tls_port": self.settings.tls_port_base + slot if self.settings.tls_enabled else None,
                "relay_min_port": self.settings.relay_port_base + slot * self.settings.relay_ports_per_account,
                "relay_max_port": self.settings.relay_port_base
                + (slot + 1) * self.settings.relay_ports_per_account
                - 1,
                "metrics_port": self.settings.metrics_port_base + slot,
                "cli_port": self.settings.cli_port_base + slot,
            }
            account = self.db.create_account(values)
        if self.settings.supervisor_enabled:
            self.manager.start_account(account_id)
            account = self.db.get_account(account_id) or account
        return account, api_key

    def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        account = self._require_account(account_id)
        updates: dict[str, Any] = {}
        restart = False

        if "name" in payload:
            name = str(payload["name"]).strip()
            if not name or len(name) > 80:
                raise ValidationError("name must be between 1 and 80 characters")
            updates["name"] = name
        if "max_bps" in payload:
            value = self._int_value(payload, "max_bps", int(account["max_bps"]), 10_000, 10_000_000_000)
            restart = restart or value != int(account["max_bps"])
            updates["max_bps"] = value
        if "allocation_quota" in payload:
            max_allocations = min(100, self.settings.relay_ports_per_account)
            value = self._int_value(
                payload, "allocation_quota", int(account["allocation_quota"]), 1, max_allocations
            )
            restart = restart or value != int(account["allocation_quota"])
            updates["allocation_quota"] = value
        if "traffic_quota_bytes" in payload:
            updates["traffic_quota_bytes"] = self._int_value(
                payload, "traffic_quota_bytes", int(account["traffic_quota_bytes"]), 0, 10**15
            )
        if "credential_ttl" in payload:
            updates["credential_ttl"] = self._int_value(
                payload, "credential_ttl", int(account["credential_ttl"]), 60, 86400
            )
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValidationError("enabled must be a boolean")
            updates["enabled"] = 1 if payload["enabled"] else 0

        updated = self.db.update_account(account_id, updates) or account
        usage = self.manager.account_usage(updated)
        quota = int(updated["traffic_quota_bytes"])
        if quota > 0 and usage["total_bytes"] < quota and updated["suspended_reason"] == "traffic_quota_exceeded":
            updated = self.db.update_account(account_id, {"suspended_reason": None}) or updated
        elif quota == 0 and updated["suspended_reason"] == "traffic_quota_exceeded":
            updated = self.db.update_account(account_id, {"suspended_reason": None}) or updated
        elif quota > 0 and usage["total_bytes"] >= quota:
            updated = self.db.update_account(account_id, {"suspended_reason": "traffic_quota_exceeded"}) or updated

        if not self.settings.supervisor_enabled:
            return self.db.get_account(account_id) or updated
        if not updated["enabled"] or updated["suspended_reason"]:
            self.manager.stop_account(account_id, refresh=True)
        elif restart:
            self.manager.restart_account(account_id)
        else:
            self.manager.start_account(account_id)
        return self.db.get_account(account_id) or updated

    def rotate_api_key(self, account_id: str) -> tuple[dict[str, Any], str]:
        self._require_account(account_id)
        api_key = new_api_key()
        updated = self.db.update_account(account_id, {"api_key_hash": hash_api_key(api_key)})
        if not updated:
            raise KeyError(account_id)
        return updated, api_key

    def reset_usage(self, account_id: str) -> dict[str, Any]:
        self._require_account(account_id)
        self.manager.reset_usage(account_id)
        account = self._require_account(account_id)
        return account

    def delete_account(self, account_id: str) -> None:
        self._require_account(account_id)
        self.manager.delete_account(account_id)

    def issue_credentials(self, account: dict[str, Any], ttl_seconds: int | None = None) -> dict[str, Any]:
        if not account["enabled"]:
            raise ConflictError("account is disabled")
        if account["suspended_reason"]:
            raise ConflictError(f"account is suspended: {account['suspended_reason']}")
        usage = self.manager.account_usage(account)
        quota = int(account["traffic_quota_bytes"])
        if quota > 0 and usage["total_bytes"] >= quota:
            raise ConflictError("traffic quota exceeded")
        if self.settings.supervisor_enabled and not self.manager.is_running(account["id"]):
            raise ConflictError("turn server is not running")

        ttl = int(ttl_seconds if ttl_seconds is not None else account["credential_ttl"])
        if ttl < 60 or ttl > 86400:
            raise ValidationError("ttl_seconds must be between 60 and 86400")
        credential = create_turn_credential(account["auth_secret"], account["id"], ttl)
        urls = turn_urls(account, self.settings.public_host, self.settings.tls_enabled)
        return {
            "username": credential.username,
            "credential": credential.password,
            "expires_at": credential.expires_at,
            "ttl_seconds": credential.ttl_seconds,
            "urls": urls,
            "ice_servers": [{"urls": urls, "username": credential.username, "credential": credential.password}],
        }

    def serialize_account(self, account: dict[str, Any]) -> dict[str, Any]:
        usage = self.manager.account_usage(account)
        runtime = self.manager.runtime_info(account)
        max_bps = int(account["max_bps"])
        allocation_quota = max(1, int(account["allocation_quota"]))
        return {
            "id": account["id"],
            "name": account["name"],
            "enabled": bool(account["enabled"]),
            "suspended_reason": account["suspended_reason"],
            "realm": account["realm"],
            "max_bps": max_bps,
            "session_max_bps": max(1, max_bps // allocation_quota),
            "allocation_quota": allocation_quota,
            "traffic_quota_bytes": int(account["traffic_quota_bytes"]),
            "credential_ttl": int(account["credential_ttl"]),
            "turn_port": int(account["turn_port"]),
            "tls_port": int(account["tls_port"]) if account["tls_port"] else None,
            "relay_port_range": [int(account["relay_min_port"]), int(account["relay_max_port"])],
            "urls": turn_urls(account, self.settings.public_host, self.settings.tls_enabled),
            "usage": usage,
            "runtime": runtime,
            "created_at": int(account["created_at"]),
            "updated_at": int(account["updated_at"]),
        }

    def _require_account(self, account_id: str) -> dict[str, Any]:
        account = self.db.get_account(account_id)
        if not account:
            raise KeyError(account_id)
        return account

    @staticmethod
    def _int_value(payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{key} must be an integer")
        if value < minimum or value > maximum:
            raise ValidationError(f"{key} must be between {minimum} and {maximum}")
        return value
