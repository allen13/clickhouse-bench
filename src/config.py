"""ClickHouse Cloud connection configuration.

Reads connection parameters from environment variables or a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class ClickHouseConfig:
    """Immutable ClickHouse connection configuration."""

    host: str = field(default_factory=lambda: os.getenv("CLICKHOUSE_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("CLICKHOUSE_PORT", "8443")))
    user: str = field(default_factory=lambda: os.getenv("CLICKHOUSE_USER", "default"))
    password: str = field(default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", ""))
    database: str = field(default_factory=lambda: os.getenv("CLICKHOUSE_DATABASE", "default"))
    secure: bool = field(default_factory=lambda: os.getenv("CLICKHOUSE_SECURE", "true").lower() == "true")

    def summary(self) -> dict[str, str]:
        """Return a safe-to-print summary (password masked)."""
        return {
            "host": self.host,
            "port": str(self.port),
            "user": self.user,
            "password": "***" if self.password else "(empty)",
            "database": self.database,
            "secure": str(self.secure),
        }


def get_client(cfg: ClickHouseConfig | None = None):
    """Create and return a ``clickhouse_connect`` client."""
    import clickhouse_connect

    cfg = cfg or ClickHouseConfig()
    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.user,
        password=cfg.password,
        database=cfg.database,
        secure=cfg.secure,
    )


# Module-level singleton for convenience
CONFIG = ClickHouseConfig()
