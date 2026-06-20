"""Paths and secret/cache storage, deliberately kept outside any synced folder.

The SimpleFIN access URL embeds Basic-Auth credentials that can read the user's
transactions, so it must never land in a Dropbox/iCloud-synced project directory.
Everything sensitive lives under ``FINANCE_MCP_HOME`` (default ``~/.finance-mcp``)
with restrictive permissions.
"""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    """Return the private storage directory, creating it with mode 0700."""
    override = os.environ.get("FINANCE_MCP_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".finance-mcp"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Tighten in case it pre-existed with looser permissions.
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def access_url_path() -> Path:
    """Path to the file holding the SimpleFIN access URL (the credential)."""
    return home_dir() / "access_url"


def cache_path() -> Path:
    """Path to the normalized transaction cache (transaction data, no credentials)."""
    return home_dir() / "cache.json"


def load_access_url() -> str | None:
    """Return the saved access URL, or None if setup has not been run."""
    # Environment wins so the URL can be injected without touching disk.
    env = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if env:
        return env.strip()
    path = access_url_path()
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def save_access_url(access_url: str) -> Path:
    """Persist the access URL with owner-only (0600) permissions."""
    path = access_url_path()
    path.write_text(access_url.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
