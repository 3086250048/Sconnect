from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from tests.helpers import test_settings


class ConfigTests(unittest.TestCase):
    def test_overlapping_port_ranges_are_rejected(self) -> None:
        settings = replace(test_settings(Path("/tmp/unused")), tls_port_base=50010)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            settings.validate()

    def test_public_host_cannot_include_scheme(self) -> None:
        settings = replace(test_settings(Path("/tmp/unused")), public_host="https://turn.example.com")
        with self.assertRaisesRegex(ValueError, "without a URL scheme"):
            settings.validate()


if __name__ == "__main__":
    unittest.main()
