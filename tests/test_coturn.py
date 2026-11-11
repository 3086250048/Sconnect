from __future__ import annotations

import unittest
from pathlib import Path

from app.coturn import build_turnserver_config, parse_cli_ps, parse_metrics, turn_urls


class CoturnTests(unittest.TestCase):
    def test_metrics_parser(self) -> None:
        text = """
turn_total_traffic_rcvb 1234
turn_total_traffic_sentb 5678
turn_total_traffic_peer_rcvb 9999
"""
        snapshot = parse_metrics(text)
        self.assertEqual(snapshot.received_bytes, 1234)
        self.assertEqual(snapshot.sent_bytes, 5678)

    def test_cli_parser(self) -> None:
        text = """
    1) id=000000000000000001, user <1:acct>:
      usage: rp=10, rb=1000, sp=20, sb=2000
    2) id=000000000000000002, user <1:acct>:
      usage: rp=30, rb=3000, sp=40, sb=4000
  Total sessions: 2
"""
        snapshot = parse_cli_ps(text)
        self.assertEqual(snapshot.received_bytes, 4000)
        self.assertEqual(snapshot.sent_bytes, 6000)
        self.assertEqual(snapshot.sessions, 2)

    def test_config_applies_account_limits_and_loopback_management(self) -> None:
        account = {
            "id": "acct_demo",
            "realm": "sconnect-acct_demo",
            "auth_secret": "secret",
            "turn_port": 34780,
            "relay_min_port": 50000,
            "relay_max_port": 50199,
            "metrics_port": 39100,
            "cli_port": 39000,
            "tls_port": None,
            "max_bps": 2_000_000,
            "allocation_quota": 4,
        }
        settings = {
            "data_dir": Path("/data"),
            "external_ip": "203.0.113.10",
            "relay_ip": "",
            "listen_ip": "0.0.0.0",
            "tls_enabled": False,
            "tls_cert": "",
            "tls_key": "",
            "allow_private_peers": False,
            "allow_loopback_peers": True,
            "relay_threads": 1,
        }
        config = build_turnserver_config(account, settings)
        self.assertIn("max-bps=500000", config)
        self.assertIn("bps-capacity=2000000", config)
        self.assertIn("user-quota=4", config)
        self.assertIn("prometheus-address=127.0.0.1", config)
        self.assertIn("cli-ip=127.0.0.1", config)
        self.assertIn("cli-password=secret", config)
        self.assertIn("external-ip=203.0.113.10", config)
        self.assertIn("allow-loopback-peers", config)
        self.assertNotIn("denied-peer-ip=127.0.0.0-127.255.255.255", config)
        self.assertIn("denied-peer-ip=10.0.0.0-10.255.255.255", config)
        self.assertIn("static-auth-secret=secret\n", config)

    def test_turn_urls(self) -> None:
        account = {"turn_port": 34780, "tls_port": 53490}
        self.assertEqual(
            turn_urls(account, "turn.example.com", True),
            [
                "turn:turn.example.com:34780?transport=udp",
                "turn:turn.example.com:34780?transport=tcp",
                "turns:turn.example.com:53490?transport=tcp",
            ],
        )

    def test_private_peer_switch_does_not_allow_loopback(self) -> None:
        account = {
            "id": "acct_demo",
            "realm": "sconnect-acct_demo",
            "auth_secret": "secret",
            "turn_port": 34780,
            "relay_min_port": 50000,
            "relay_max_port": 50063,
            "metrics_port": 39100,
            "cli_port": 39000,
            "tls_port": None,
            "max_bps": 2_000_000,
            "allocation_quota": 4,
        }
        settings = {
            "data_dir": Path("/data"),
            "external_ip": "",
            "relay_ip": "",
            "listen_ip": "0.0.0.0",
            "tls_enabled": False,
            "tls_cert": "",
            "tls_key": "",
            "allow_private_peers": True,
            "allow_loopback_peers": False,
            "relay_threads": 1,
        }
        config = build_turnserver_config(account, settings)
        self.assertNotIn("denied-peer-ip=10.0.0.0-10.255.255.255", config)
        self.assertIn("denied-peer-ip=127.0.0.0-127.255.255.255", config)
        self.assertIn("denied-peer-ip=169.254.0.0-169.254.255.255", config)


if __name__ == "__main__":
    unittest.main()

