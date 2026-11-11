from __future__ import annotations

import unittest

from tests.helpers import RunningApi


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = RunningApi()
        self.admin = self.api.settings.admin_token

    def tearDown(self) -> None:
        self.api.close()

    def create_account(self, name: str = "demo") -> tuple[dict, str]:
        status, body = self.api.request(
            "POST",
            "/v1/accounts",
            {"name": name, "max_bps": 4_000_000, "allocation_quota": 4, "traffic_quota_bytes": 10_000_000},
            self.admin,
        )
        self.assertEqual(status, 201, body)
        assert body is not None
        return body, body["api_key"]

    def test_health_and_authentication(self) -> None:
        status, body = self.api.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})
        status, body = self.api.request("GET", "/v1/accounts")
        self.assertEqual(status, 401, body)

        status, body = self.api.request("GET", "/v1/accounts", token="无效令牌")
        self.assertEqual(status, 401, body)

    def test_account_lifecycle_and_tenant_isolation(self) -> None:
        account, account_key = self.create_account()
        other, other_key = self.create_account("other")
        account_id = account["id"]
        self.assertEqual(account["urls"][0], f"turn:turn.test:{account['turn_port']}?transport=udp")
        self.assertEqual(account["relay_port_range"], [50000, 50063])

        status, body = self.api.request("GET", f"/v1/accounts/{account_id}", token=account_key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["id"], account_id)

        status, body = self.api.request("GET", f"/v1/accounts/{other['id']}", token=account_key)
        self.assertEqual(status, 403, body)

        status, body = self.api.request("GET", f"/v1/accounts/{account_id}/ice-servers", token=account_key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["iceServers"][0]["username"].split(":", 1)[1], account_id)

        status, body = self.api.request("PATCH", f"/v1/accounts/{account_id}", {"enabled": False}, self.admin)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["enabled"])

        status, body = self.api.request("POST", f"/v1/accounts/{account_id}/credentials", token=account_key)
        self.assertEqual(status, 409, body)

    def test_rotate_key_invalidates_old_key(self) -> None:
        account, old_key = self.create_account()
        status, body = self.api.request("POST", f"/v1/accounts/{account['id']}/rotate-api-key", token=self.admin)
        self.assertEqual(status, 200, body)
        assert body is not None
        new_key = body["api_key"]
        status, _body = self.api.request("GET", f"/v1/accounts/{account['id']}", token=old_key)
        self.assertEqual(status, 401)
        status, _body = self.api.request("GET", f"/v1/accounts/{account['id']}", token=new_key)
        self.assertEqual(status, 200)

    def test_invalid_credential_ttl_returns_400(self) -> None:
        account, _key = self.create_account()
        status, body = self.api.request(
            "POST", f"/v1/accounts/{account['id']}/credentials", {"ttl_seconds": 30}, self.admin
        )
        self.assertEqual(status, 400, body)

    def test_quota_blocks_credentials_and_reset_clears_usage(self) -> None:
        account, _key = self.create_account()
        self.api.db.update_usage(account["id"], 6_000_000, 4_000_000)
        status, body = self.api.request("POST", f"/v1/accounts/{account['id']}/credentials", token=self.admin)
        self.assertEqual(status, 409, body)

        status, body = self.api.request("POST", f"/v1/accounts/{account['id']}/reset-usage", token=self.admin)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["usage"]["total_bytes"], 0)

        status, body = self.api.request("POST", f"/v1/accounts/{account['id']}/credentials", token=self.admin)
        self.assertEqual(status, 200, body)
        assert body is not None
        self.assertTrue(body["credential"])

    def test_lowering_quota_below_usage_suspends_account(self) -> None:
        account, _key = self.create_account()
        self.api.db.update_usage(account["id"], 6_000_000, 4_000_000)
        status, body = self.api.request(
            "PATCH",
            f"/v1/accounts/{account['id']}",
            {"traffic_quota_bytes": 5_000_000},
            self.admin,
        )
        self.assertEqual(status, 200, body)
        assert body is not None
        self.assertEqual(body["suspended_reason"], "traffic_quota_exceeded")

    def test_metrics_require_admin(self) -> None:
        account, account_key = self.create_account()
        status, _body = self.api.request("GET", "/metrics", token=account_key)
        self.assertEqual(status, 403)
        status, _body = self.api.request("GET", "/metrics", token=self.admin)
        self.assertEqual(status, 200)

    def test_account_limit(self) -> None:
        for index in range(4):
            self.create_account(f"demo-{index}")
        status, body = self.api.request("POST", "/v1/accounts", {"name": "overflow"}, self.admin)
        self.assertEqual(status, 409, body)


if __name__ == "__main__":
    unittest.main()
