from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.coturn import TrafficSnapshot
from app.db import Database
from app.manager import AccountManager, RuntimeState
from app.service import AccountService
from tests.helpers import test_settings


class _RunningProcess:
    pid = 4242

    @staticmethod
    def poll() -> None:
        return None


class ManagerUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self.temp.name))
        self.db = Database(self.settings.data_dir / "sconnect.db")
        self.manager = AccountManager(self.settings, self.db)
        self.service = AccountService(self.settings, self.db, self.manager)
        account, _key = self.service.create_account({"name": "usage"})
        self.account_id = account["id"]
        self.db.update_usage(self.account_id, 100, 200)
        self.manager._runtimes[self.account_id] = RuntimeState(
            process=_RunningProcess(),  # type: ignore[arg-type]
            base_rx_bytes=100,
            base_tx_bytes=200,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_metrics_and_cli_are_not_double_counted(self) -> None:
        metrics = TrafficSnapshot(1_000, 2_000)
        cli = TrafficSnapshot(1_000, 2_000, sessions=3)
        self.manager.apply_snapshot(self.account_id, 4242, metrics, cli)
        account = self.db.get_account(self.account_id)
        assert account is not None
        self.assertEqual(account["billed_rx_bytes"], 1_100)
        self.assertEqual(account["billed_tx_bytes"], 2_200)
        self.assertEqual(self.manager.account_usage(account)["active_sessions"], 3)

    def test_cli_is_used_when_metrics_are_unavailable(self) -> None:
        cli = TrafficSnapshot(700, 900, sessions=2)
        self.manager.apply_snapshot(self.account_id, 4242, None, cli)
        account = self.db.get_account(self.account_id)
        assert account is not None
        self.assertEqual(account["billed_rx_bytes"], 800)
        self.assertEqual(account["billed_tx_bytes"], 1_100)

    def test_quota_threshold(self) -> None:
        self.db.update_account(self.account_id, {"traffic_quota_bytes": 3_000})
        hit = self.manager.apply_snapshot(
            self.account_id,
            4242,
            TrafficSnapshot(1_400, 1_500),
            TrafficSnapshot(1_400, 1_500, sessions=1),
        )
        self.assertTrue(hit)

    def test_parallel_collection_keeps_account_ids(self) -> None:
        def fake_collect(account: dict[str, object]) -> tuple[TrafficSnapshot, None]:
            account_id = str(account["id"])
            return TrafficSnapshot(len(account_id), 1), None

        self.manager.collect_for_account = fake_collect  # type: ignore[method-assign]
        account = self.db.get_account(self.account_id)
        assert account is not None
        results = self.manager._collect_usage([(self.account_id, 4242, account)])
        self.assertEqual(results[self.account_id][0], TrafficSnapshot(len(self.account_id), 1))

    def test_enabling_tls_backfills_existing_account_port(self) -> None:
        tls_settings = replace(self.settings, tls_enabled=True)
        manager = AccountManager(tls_settings, self.db)
        manager.start()
        account = self.db.get_account(self.account_id)
        assert account is not None
        self.assertEqual(account["tls_port"], tls_settings.tls_port_base + int(account["slot"]))


if __name__ == "__main__":
    unittest.main()
