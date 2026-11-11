from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnCredential:
    username: str
    password: str
    expires_at: int
    ttl_seconds: int


def new_api_key() -> str:
    return "sc_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_turn_secret() -> str:
    return secrets.token_hex(32)


def create_turn_credential(secret: str, account_id: str, ttl_seconds: int, now: int | None = None) -> TurnCredential:
    if ttl_seconds < 60 or ttl_seconds > 86400:
        raise ValueError("credential TTL must be between 60 and 86400 seconds")
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + ttl_seconds
    username = f"{expires_at}:{account_id}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode("ascii")
    return TurnCredential(username=username, password=password, expires_at=expires_at, ttl_seconds=ttl_seconds)

