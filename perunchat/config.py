"""
PerunChat configuration / secrets loading.

- HMAC signing key and other secrets are loaded from environment variables or a
  `.env` file, never hard-coded in source.
- A safe dev-only default is provided only when `PERUNCHAT_ENV=test` is set.
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load a sibling .env file if present. Never required."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        with env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() and key.strip() not in os.environ:
                    os.environ.setdefault(key.strip(), value.strip().strip('"\''))
    except OSError:
        pass


_load_dotenv()


def _is_test_env() -> bool:
    return os.environ.get("PERUNCHAT_ENV", "").lower() == "test"


def secret_key() -> bytes:
    """Return the HMAC signing key."""
    value = os.environ.get("PERUNCHAT_SECRET")
    if value:
        return value.encode()
    if _is_test_env():
        # Safe default only for unit tests; never used in production.
        return b"test-secret-do-not-use-in-production"
    raise RuntimeError(
        "PERUNCHAT_SECRET environment variable is required. "
        "Set it before starting the gateway (e.g., in .env or systemd unit)."
    )


def db_path() -> str:
    """Return the configured SQLite database path."""
    env = os.environ.get("PERUNCHAT_DB_PATH")
    if env:
        return env
    if _is_test_env():
        return ":memory:"
    home = Path(os.environ.get("PERUN_HOME", Path.home() / ".perunchat"))
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "perunchat.db")


def public_rooms() -> list[str]:
    """Well-known public rooms advertised to clients.

    Used by ``GET /api/v1/rooms`` so dashboards and auto-join configuration
    can offer a discoverable room list instead of requiring hand-typed IDs.
    Comma-separated override via ``PERUNCHAT_PUBLIC_ROOMS``.
    """
    value = os.environ.get(
        "PERUNCHAT_PUBLIC_ROOMS",
        "pub:global:general,pub:global:tech,pub:global:random",
    )
    return [room.strip() for room in value.split(",") if room.strip()]


def api_bind_host() -> str:
    return os.environ.get("PERUNCHAT_HOST", "127.0.0.1")


def api_bind_port() -> int:
    return int(os.environ.get("PERUNCHAT_PORT", "8765"))


def dashboard_secret() -> str:
    """Return the shared secret required for the public dashboard to provision keys.

    When PERUNCHAT_DASHBOARD_SECRET is not set in production, key-provisioning
    endpoints are disabled (they return 403) to prevent open registration abuse.
    """
    value = os.environ.get("PERUNCHAT_DASHBOARD_SECRET")
    if value:
        return value
    if _is_test_env():
        return "dashboard-test-secret"
    raise RuntimeError(
        "PERUNCHAT_DASHBOARD_SECRET is required to enable the public agent dashboard. "
        "Set it before starting the gateway (e.g., in .env or systemd unit)."
    )
