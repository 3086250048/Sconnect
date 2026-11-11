from __future__ import annotations

import logging
import signal
import sys
import threading

from .api import create_server
from .config import Settings
from .db import Database
from .manager import AccountManager
from .service import AccountService


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logging.error("configuration error: %s", exc)
        return 2

    database = Database(settings.data_dir / "sconnect.db")
    manager = AccountManager(settings, database)
    service = AccountService(settings, database, manager)
    manager.start()
    try:
        server = create_server(settings, service, manager)
    except Exception:
        manager.stop()
        raise

    stopping = threading.Event()

    def shutdown(signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        logging.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, name="sconnect-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if not settings.external_ip:
        logging.warning(
            "TURN_EXTERNAL_IP is empty; set it when the server is behind cloud NAT or a load balancer"
        )
    logging.info("Sconnect API listening on %s:%s", settings.bind, settings.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
