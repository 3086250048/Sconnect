from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


ALWAYS_DENIED_PEER_RANGES = (
    "0.0.0.0-0.255.255.255",
    "169.254.0.0-169.254.255.255",
    "192.0.0.0-192.0.0.255",
    "192.0.2.0-192.0.2.255",
    "198.18.0.0-198.19.255.255",
    "198.51.100.0-198.51.100.255",
    "203.0.113.0-203.0.113.255",
    "224.0.0.0-255.255.255.255",
    "fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
)

PRIVATE_PEER_RANGES = (
    "10.0.0.0-10.255.255.255",
    "100.64.0.0-100.127.255.255",
    "172.16.0.0-172.31.255.255",
    "192.168.0.0-192.168.255.255",
    "fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
)

LOOPBACK_PEER_RANGES = (
    "127.0.0.0-127.255.255.255",
    "::1-::1",
)

_METRIC_RE = re.compile(r"^(turn_total_traffic_(?:rcvb|sentb))(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", re.MULTILINE)
_CLI_USAGE_RE = re.compile(r"usage:\s+rp=(\d+),\s*rb=(\d+),\s*sp=(\d+),\s*sb=(\d+)")


@dataclass(frozen=True)
class TrafficSnapshot:
    received_bytes: int
    sent_bytes: int
    sessions: int = 0


def build_turnserver_config(account: Mapping[str, object], settings: Mapping[str, object]) -> str:
    account_id = str(account["id"])
    max_bps = int(account["max_bps"])
    allocation_quota = int(account["allocation_quota"])
    session_max_bps = max(1, max_bps // allocation_quota)

    lines = [
        f"realm={account['realm']}",
        "server-name=sconnect",
        "fingerprint",
        "lt-cred-mech",
        "use-auth-secret",
        f"static-auth-secret={account['auth_secret']}",
        f"listening-ip={settings['listen_ip']}",
        f"listening-port={account['turn_port']}",
        f"min-port={account['relay_min_port']}",
        f"max-port={account['relay_max_port']}",
        f"max-bps={session_max_bps}",
        f"bps-capacity={max_bps}",
        f"user-quota={allocation_quota}",
        f"total-quota={allocation_quota}",
        f"relay-threads={settings['relay_threads']}",
        "stale-nonce=600",
        "no-multicast-peers",
        "prometheus",
        "prometheus-address=127.0.0.1",
        f"prometheus-port={account['metrics_port']}",
        "cli",
        "cli-ip=127.0.0.1",
        f"cli-port={account['cli_port']}",
        f"cli-password={account['auth_secret']}",
        "log-file=stdout",
        f"pidfile={settings['data_dir']}/accounts/{account_id}/turnserver.pid",
    ]

    if settings.get("external_ip"):
        lines.append(f"external-ip={settings['external_ip']}")
    if settings.get("relay_ip"):
        lines.append(f"relay-ip={settings['relay_ip']}")
    if settings.get("tls_enabled"):
        lines.extend(
            [
                f"tls-listening-port={account['tls_port']}",
                f"cert={settings['tls_cert']}",
                f"pkey={settings['tls_key']}",
            ]
        )
    denied_peers = list(ALWAYS_DENIED_PEER_RANGES)
    if not settings.get("allow_private_peers"):
        denied_peers.extend(PRIVATE_PEER_RANGES)
    if not settings.get("allow_loopback_peers"):
        denied_peers.extend(LOOPBACK_PEER_RANGES)
    lines.extend(f"denied-peer-ip={value}" for value in denied_peers)
    if settings.get("allow_loopback_peers"):
        lines.append("allow-loopback-peers")

    return "\n".join(lines) + "\n"


def parse_metrics(text: str) -> TrafficSnapshot:
    values: dict[str, float] = {}
    for name, raw in _METRIC_RE.findall(text):
        values[name] = float(raw)
    return TrafficSnapshot(
        received_bytes=int(values.get("turn_total_traffic_rcvb", 0)),
        sent_bytes=int(values.get("turn_total_traffic_sentb", 0)),
    )


def parse_cli_ps(text: str) -> TrafficSnapshot:
    received = 0
    sent = 0
    sessions = 0
    for _rp, rb, _sp, sb in _CLI_USAGE_RE.findall(text):
        received += int(rb)
        sent += int(sb)
        sessions += 1
    return TrafficSnapshot(received_bytes=received, sent_bytes=sent, sessions=sessions)


def turn_urls(account: Mapping[str, object], public_host: str, tls_enabled: bool) -> list[str]:
    port = int(account["turn_port"])
    urls = [
        f"turn:{public_host}:{port}?transport=udp",
        f"turn:{public_host}:{port}?transport=tcp",
    ]
    if tls_enabled and account.get("tls_port"):
        tls_port = int(account["tls_port"])
        urls.append(f"turns:{public_host}:{tls_port}?transport=tcp")
    return urls

