from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "sconnect.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_connection_is_closed_after_success(self) -> None:
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_connection_is_closed_after_error(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.db.connection() as conn:
                raise RuntimeError("boom")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
