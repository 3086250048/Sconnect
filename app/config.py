from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_token: str
    bind: str
    port: int
    public_host: str
    external_ip: str
    listen_ip: str
    relay_ip: str
    max_accounts: int
    turn_port_base: int
    tls_port_base: int
    relay_port_base: int
    relay_ports_per_account: int
    metrics_port_base: int
    cli_port_base: int
    usage_poll_seconds: float
    tls_enabled: bool
    tls_cert: str
    tls_key: str
    turn_binary: str
    supervisor_enabled: bool
    allow_private_peers: bool
    allow_loopback_peers: bool
    relay_threads: int
    api_tls_cert: str
    api_tls_key: str

    @classmethod
    def from_env(cls) -> "Settings":
        admin_token = os.getenv("SCONNECT_ADMIN_TOKEN", "")
        if len(admin_token) < 16:
            raise ValueError("SCONNECT_ADMIN_TOKEN must be at least 16 characters")

        settings = cls(
            data_dir=Path(os.getenv("SCONNECT_DATA_DIR", "/data")),
            admin_token=admin_token,
            bind=os.getenv("SCONNECT_BIND", "127.0.0.1"),
            port=_env_int("SCONNECT_PORT", 8080),
            public_host=os.getenv("TURN_PUBLIC_HOST", "127.0.0.1"),
            external_ip=os.getenv("TURN_EXTERNAL_IP", ""),
            listen_ip=os.getenv("TURN_LISTEN_IP", "0.0.0.0"),
            relay_ip=os.getenv("TURN_RELAY_IP", ""),
            max_accounts=_env_int("SCONNECT_MAX_ACCOUNTS", 20),
            turn_port_base=_env_int("TURN_PORT_BASE", 34780),
            tls_port_base=_env_int("TURN_TLS_PORT_BASE", 54000),
            relay_port_base=_env_int("TURN_RELAY_PORT_BASE", 50000),
            relay_ports_per_account=_env_int("TURN_RELAY_PORTS_PER_ACCOUNT", 64),
            metrics_port_base=_env_int("TURN_METRICS_PORT_BASE", 39100),
            cli_port_base=_env_int("TURN_CLI_PORT_BASE", 39000),
            usage_poll_seconds=float(os.getenv("SCONNECT_USAGE_POLL_SECONDS", "5")),
            tls_enabled=_env_bool("TURN_TLS_ENABLED", False),
            tls_cert=os.getenv("TURN_TLS_CERT", "/certs/fullchain.pem"),
            tls_key=os.getenv("TURN_TLS_KEY", "/certs/privkey.pem"),
            turn_binary=os.getenv("TURN_BINARY", "turnserver"),
            supervisor_enabled=_env_bool("SCONNECT_SUPERVISOR_ENABLED", True),
            allow_private_peers=_env_bool("TURN_ALLOW_PRIVATE_PEERS", False),
            allow_loopback_peers=_env_bool("TURN_ALLOW_LOOPBACK_PEERS", False),
            relay_threads=_env_int("TURN_RELAY_THREADS", 1),
            api_tls_cert=os.getenv("SCONNECT_API_TLS_CERT", ""),
            api_tls_key=os.getenv("SCONNECT_API_TLS_KEY", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.bind.strip():
            raise ValueError("SCONNECT_BIND must not be empty")
        if self.port < 1 or self.port > 65535:
            raise ValueError("SCONNECT_PORT must be between 1 and 65535")
        if self.max_accounts < 1:
            raise ValueError("SCONNECT_MAX_ACCOUNTS must be at least 1")
        if self.relay_ports_per_account < 10:
            raise ValueError("TURN_RELAY_PORTS_PER_ACCOUNT must be at least 10")
        if not self.public_host.strip():
            raise ValueError("TURN_PUBLIC_HOST must not be empty")
        if "://" in self.public_host:
            raise ValueError("TURN_PUBLIC_HOST must be a hostname or IP address, without a URL scheme")
        if not self.listen_ip.strip():
            raise ValueError("TURN_LISTEN_IP must not be empty")
        if not self.turn_binary.strip():
            raise ValueError("TURN_BINARY must not be empty")
        if self.usage_poll_seconds < 1:
            raise ValueError("SCONNECT_USAGE_POLL_SECONDS must be at least 1")
        if self.relay_threads < 1:
            raise ValueError("TURN_RELAY_THREADS must be at least 1")
        if self.tls_enabled and (not self.tls_cert or not self.tls_key):
            raise ValueError("TURN_TLS_CERT and TURN_TLS_KEY are required when TLS is enabled")
        if bool(self.api_tls_cert) != bool(self.api_tls_key):
            raise ValueError("SCONNECT_API_TLS_CERT and SCONNECT_API_TLS_KEY must be set together")
        self._validate_port_range(self.turn_port_base, "TURN_PORT_BASE")
        self._validate_port_range(self.tls_port_base, "TURN_TLS_PORT_BASE")
        self._validate_port_range(self.metrics_port_base, "TURN_METRICS_PORT_BASE")
        self._validate_port_range(self.cli_port_base, "TURN_CLI_PORT_BASE")
        for base, name in (
            (self.turn_port_base, "TURN_PORT_BASE"),
            (self.tls_port_base, "TURN_TLS_PORT_BASE"),
            (self.metrics_port_base, "TURN_METRICS_PORT_BASE"),
            (self.cli_port_base, "TURN_CLI_PORT_BASE"),
        ):
            if base + self.max_accounts - 1 > 65535:
                raise ValueError(f"{name} plus max accounts exceeds 65535")
        if self.relay_port_base < 1024:
            raise ValueError("TURN_RELAY_PORT_BASE must be above 1024")
        relay_top = self.relay_port_base + self.max_accounts * self.relay_ports_per_account - 1
        if relay_top > 65535:
            raise ValueError("relay port range exceeds 65535; reduce account count or ports per account")

        ranges = {
            "TURN": (self.turn_port_base, self.turn_port_base + self.max_accounts - 1),
            "TLS": (self.tls_port_base, self.tls_port_base + self.max_accounts - 1),
            "metrics": (self.metrics_port_base, self.metrics_port_base + self.max_accounts - 1),
            "CLI": (self.cli_port_base, self.cli_port_base + self.max_accounts - 1),
            "relay": (self.relay_port_base, relay_top),
        }
        items = list(ranges.items())
        for index, (left_name, (left_start, left_end)) in enumerate(items):
            for right_name, (right_start, right_end) in items[index + 1 :]:
                if left_start <= right_end and right_start <= left_end:
                    raise ValueError(f"{left_name} port range overlaps {right_name} port range")

    @staticmethod
    def _validate_port_range(base: int, name: str) -> None:
        if base < 1 or base > 65535:
            raise ValueError(f"{name} must be between 1 and 65535")

    def turnserver_settings(self) -> dict[str, object]:
        return {
            "data_dir": self.data_dir,
            "external_ip": self.external_ip,
            "relay_ip": self.relay_ip,
            "listen_ip": self.listen_ip,
            "tls_enabled": self.tls_enabled,
            "tls_cert": self.tls_cert,
            "tls_key": self.tls_key,
            "allow_private_peers": self.allow_private_peers,
            "allow_loopback_peers": self.allow_loopback_peers,
            "relay_threads": self.relay_threads,
        }

