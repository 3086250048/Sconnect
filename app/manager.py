from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from .config import Settings
from .coturn import (
    TrafficSnapshot,
    build_turnserver_config,
    parse_cli_ps,
    parse_metrics,
    turn_urls,
)
from .db import Database


@dataclass
class RuntimeState:
    process: subprocess.Popen[bytes] | None = None
    log_handle: BinaryIO | None = None
    base_rx_bytes: int = 0
    base_tx_bytes: int = 0
    usage: TrafficSnapshot = field(default_factory=lambda: TrafficSnapshot(0, 0))
    last_error: str = ""
    last_start_attempt: float = 0.0


class AccountManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._lock = threading.RLock()
        self._runtimes: dict[str, RuntimeState] = {}
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / "accounts").mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        if self.settings.tls_enabled:
            for account in self.db.list_accounts():
                expected_tls_port = self.settings.tls_port_base + int(account["slot"])
                if account["tls_port"] != expected_tls_port:
                    self.db.update_account(account["id"], {"tls_port": expected_tls_port})
        if not self.settings.supervisor_enabled:
            return
        self._start_thread("supervisor", self._supervisor_loop)
        self._start_thread("usage", self._usage_loop)

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=3)
        with self._lock:
            runtimes = list(self._runtimes.items())
        for _account_id, runtime in runtimes:
            if runtime.process and runtime.process.poll() is None:
                runtime.process.terminate()
        for account_id, runtime in runtimes:
            process = runtime.process
            if process and process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if runtime.log_handle:
                runtime.log_handle.close()
            with self._lock:
                self._runtimes.pop(account_id, None)

    def _start_thread(self, name: str, target: Any) -> None:
        thread = threading.Thread(target=target, name=f"sconnect-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _supervisor_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            for account in self.db.list_accounts():
                account_id = account["id"]
                if not account["enabled"] or account["suspended_reason"]:
                    if self.is_running(account_id):
                        self.stop_account(account_id, refresh=True)
                    continue
                if not self.is_running(account_id):
                    self.start_account(account_id)

    def _usage_loop(self) -> None:
        while not self._stop_event.wait(self.settings.usage_poll_seconds):
            jobs: list[tuple[str, int, dict[str, Any]]] = []
            with self._lock:
                for account_id, runtime in list(self._runtimes.items()):
                    if runtime.process and runtime.process.poll() is None:
                        account = self.db.get_account(account_id)
                        if account:
                            jobs.append((account_id, runtime.process.pid, account))
            if not jobs:
                continue

            results = self._collect_usage(jobs)

            for account_id, pid, _account in jobs:
                metrics, cli = results.get(account_id, (None, None))
                if metrics is None and cli is None:
                    continue
                quota_hit = self.apply_snapshot(account_id, pid, metrics, cli)
                if quota_hit:
                    self._suspend_for_quota(account_id)

    def _collect_usage(
        self, jobs: list[tuple[str, int, dict[str, Any]]]
    ) -> dict[str, tuple[TrafficSnapshot | None, TrafficSnapshot | None]]:
        if not jobs:
            return {}
        workers = min(8, len(jobs))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sconnect-scrape") as pool:
            futures = {
                pool.submit(self.collect_for_account, account): account_id
                for account_id, _pid, account in jobs
            }
            return {account_id: future.result() for future, account_id in futures.items()}

    def collect_for_account(self, account: dict[str, Any]) -> tuple[TrafficSnapshot | None, TrafficSnapshot | None]:
        metrics = self._fetch_metrics(int(account["metrics_port"]))
        cli = self._fetch_cli_usage(int(account["cli_port"]), str(account["auth_secret"]))
        return metrics, cli

    def _fetch_metrics(self, port: int) -> TrafficSnapshot | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=0.6) as response:
                text = response.read(2_000_000).decode("utf-8", errors="replace")
            return parse_metrics(text)
        except Exception:
            return None

    def _fetch_cli_usage(self, port: int, password: str) -> TrafficSnapshot | None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6) as sock:
                sock.settimeout(0.6)
                self._recv_available(sock)
                sock.sendall(password.encode("utf-8") + b"\n")
                sock.sendall(b"ps\n")
                chunks: list[bytes] = []
                deadline = time.monotonic() + 1.2
                while time.monotonic() < deadline:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"Total sessions" in b"".join(chunks):
                        break
            return parse_cli_ps(b"".join(chunks).decode("utf-8", errors="replace"))
        except Exception:
            return None

    @staticmethod
    def _recv_available(sock: socket.socket) -> None:
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            try:
                if not sock.recv(65536):
                    return
            except socket.timeout:
                return

    def is_running(self, account_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(account_id)
            return bool(runtime and runtime.process and runtime.process.poll() is None)

    def start_account(self, account_id: str) -> bool:
        with self._lock:
            account = self.db.get_account(account_id)
            if not account or not account["enabled"] or account["suspended_reason"]:
                return False
            runtime = self._runtimes.get(account_id)
            if runtime and runtime.process and runtime.process.poll() is None:
                return True
            if runtime and time.monotonic() - runtime.last_start_attempt < 3.0:
                return False

            runtime = runtime or RuntimeState()
            if runtime.log_handle:
                runtime.log_handle.close()
                runtime.log_handle = None
            runtime.last_start_attempt = time.monotonic()
            runtime.last_error = ""
            account_dir = self.settings.data_dir / "accounts" / account_id
            account_dir.mkdir(parents=True, exist_ok=True)
            config_path = account_dir / "turnserver.conf"
            config_text = build_turnserver_config(account, self.settings.turnserver_settings())
            temp_path = config_path.with_suffix(".tmp")
            temp_path.write_text(config_text, encoding="utf-8")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, config_path)

            log_handle = self._open_log(account_id)
            try:
                process = subprocess.Popen(
                    [self.settings.turn_binary, "-c", str(config_path)],
                    cwd=str(self.settings.data_dir),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                log_handle.close()
                runtime.last_error = str(exc)
                self._runtimes[account_id] = runtime
                return False

            runtime.process = process
            runtime.log_handle = log_handle
            runtime.base_rx_bytes = int(account["billed_rx_bytes"])
            runtime.base_tx_bytes = int(account["billed_tx_bytes"])
            runtime.usage = TrafficSnapshot(0, 0)
            self._runtimes[account_id] = runtime
            return True

    def _open_log(self, account_id: str) -> BinaryIO:
        log_path = self.settings.data_dir / "logs" / f"{account_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            os.replace(log_path, log_path.with_suffix(".log.1"))
        return open(log_path, "ab", buffering=0)

    def stop_account(self, account_id: str, refresh: bool = False) -> None:
        with self._lock:
            runtime = self._runtimes.get(account_id)
            if not runtime:
                return
            account = self.db.get_account(account_id)
            if refresh and account and runtime.process and runtime.process.poll() is None:
                self._record_collected_usage(account_id, runtime)
            process = runtime.process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if runtime.log_handle:
                runtime.log_handle.close()
            self._runtimes.pop(account_id, None)

    def restart_account(self, account_id: str) -> None:
        self.stop_account(account_id, refresh=True)
        self.start_account(account_id)

    def suspend_account(self, account_id: str, reason: str) -> None:
        self.db.update_account(account_id, {"suspended_reason": reason})
        self.stop_account(account_id, refresh=False)

    def _suspend_for_quota(self, account_id: str) -> None:
        self.suspend_account(account_id, "traffic_quota_exceeded")

    def apply_snapshot(
        self,
        account_id: str,
        pid: int,
        metrics: TrafficSnapshot | None,
        cli: TrafficSnapshot | None,
    ) -> bool:
        with self._lock:
            runtime = self._runtimes.get(account_id)
            if not runtime or not runtime.process or runtime.process.pid != pid:
                return False
            account = self.db.get_account(account_id)
            if not account:
                return False
            snapshot = metrics if metrics is not None else cli
            if snapshot is not None:
                sessions = cli.sessions if cli is not None else runtime.usage.sessions
                runtime.usage = TrafficSnapshot(
                    max(runtime.usage.received_bytes, snapshot.received_bytes),
                    max(runtime.usage.sent_bytes, snapshot.sent_bytes),
                    sessions,
                )
            rx_bytes = runtime.base_rx_bytes + runtime.usage.received_bytes
            tx_bytes = runtime.base_tx_bytes + runtime.usage.sent_bytes
            self.db.update_usage(account_id, rx_bytes, tx_bytes)
            quota = int(account["traffic_quota_bytes"])
            return bool(quota > 0 and rx_bytes + tx_bytes >= quota)

    def _record_collected_usage(self, account_id: str, runtime: RuntimeState) -> None:
        account = self.db.get_account(account_id)
        if not account:
            return
        metrics, cli = self.collect_for_account(account)
        snapshot = metrics if metrics is not None else cli
        if snapshot is not None:
            sessions = cli.sessions if cli is not None else runtime.usage.sessions
            runtime.usage = TrafficSnapshot(
                max(runtime.usage.received_bytes, snapshot.received_bytes),
                max(runtime.usage.sent_bytes, snapshot.sent_bytes),
                sessions,
            )
        rx_bytes = runtime.base_rx_bytes + runtime.usage.received_bytes
        tx_bytes = runtime.base_tx_bytes + runtime.usage.sent_bytes
        self.db.update_usage(account_id, rx_bytes, tx_bytes)

    def account_usage(self, account: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtimes.get(account["id"])
            if runtime and runtime.process and runtime.process.poll() is None:
                rx_bytes = runtime.base_rx_bytes + runtime.usage.received_bytes
                tx_bytes = runtime.base_tx_bytes + runtime.usage.sent_bytes
                sessions = runtime.usage.sessions
            else:
                rx_bytes = int(account["billed_rx_bytes"])
                tx_bytes = int(account["billed_tx_bytes"])
                sessions = 0
        total = rx_bytes + tx_bytes
        quota = int(account["traffic_quota_bytes"])
        remaining = max(0, quota - total) if quota > 0 else None
        return {
            "received_bytes": rx_bytes,
            "sent_bytes": tx_bytes,
            "total_bytes": total,
            "remaining_bytes": remaining,
            "active_sessions": sessions,
        }

    def runtime_info(self, account: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtimes.get(account["id"])
            if not runtime:
                return {"running": False, "pid": None, "error": ""}
            running = bool(runtime.process and runtime.process.poll() is None)
            pid = runtime.process.pid if running and runtime.process else None
            return {"running": running, "pid": pid, "error": runtime.last_error}

    def reset_usage(self, account_id: str) -> None:
        with self._lock:
            self.stop_account(account_id, refresh=False)
            self.db.update_usage(account_id, 0, 0)
            self.db.update_account(account_id, {"suspended_reason": None})
            account = self.db.get_account(account_id)
            if account and account["enabled"]:
                self.start_account(account_id)

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            self.stop_account(account_id, refresh=False)
            self.db.delete_account(account_id)
            account_dir = self.settings.data_dir / "accounts" / account_id
            if account_dir.exists():
                shutil.rmtree(account_dir, ignore_errors=True)
            log_path = self.settings.data_dir / "logs" / f"{account_id}.log"
            if log_path.exists():
                log_path.unlink(missing_ok=True)

    def public_urls(self, account: dict[str, Any]) -> list[str]:
        return turn_urls(account, self.settings.public_host, self.settings.tls_enabled)
