"""Minimal SimpleFIN Bridge client built on the standard library only.

Implements the two network operations of the SimpleFIN protocol:

* ``claim_setup_token`` — exchange a one-time setup token for a durable access
  URL (the credential).
* ``fetch_accounts`` — read accounts + transactions for a date window.

See https://www.simplefin.org/protocol.html and
https://beta-bridge.simplefin.org/info/developers.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__

# SimpleFIN caps the /accounts date range at 90 days per request.
MAX_RANGE_DAYS = 90

# SimpleFIN Bridge sits behind Cloudflare, which rejects the default
# "Python-urllib/x.y" agent with error 1010. Send an explicit identifying agent.
USER_AGENT = f"finance-mcp/{__version__}"


class SimpleFINError(RuntimeError):
    """Raised when a SimpleFIN request fails at the transport/HTTP level."""


@dataclass(frozen=True)
class _Endpoint:
    base: str
    username: str
    password: str


def _split_access_url(access_url: str) -> _Endpoint:
    """Split ``https://user:pass@host/path`` into base URL + credentials."""
    parsed = urllib.parse.urlparse(access_url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise SimpleFINError("Access URL is not a valid URL.")
    if parsed.username is None or parsed.password is None:
        raise SimpleFINError("Access URL is missing embedded credentials.")
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    base = f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")
    return _Endpoint(
        base=base,
        username=urllib.parse.unquote(parsed.username),
        password=urllib.parse.unquote(parsed.password),
    )


def claim_setup_token(setup_token: str, *, timeout: float = 30.0) -> str:
    """Exchange a base64 setup token for an access URL.

    This is a one-time operation: once claimed, the setup token is dead, so the
    returned access URL must be saved immediately.
    """
    token = setup_token.strip()
    try:
        claim_url = base64.b64decode(token).decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - surface a clear protocol error
        raise SimpleFINError(f"Setup token is not valid base64: {exc}") from exc

    if not claim_url.lower().startswith("https://"):
        raise SimpleFINError("Decoded setup token is not an https claim URL.")

    request = urllib.request.Request(
        claim_url,
        data=b"",
        method="POST",
        headers={"Content-Length": "0", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            access_url = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        raise SimpleFINError(
            f"Claim failed ({exc.code}). The setup token may already be used."
        ) from exc
    except urllib.error.URLError as exc:
        raise SimpleFINError(f"Could not reach SimpleFIN: {exc.reason}") from exc

    if not access_url.lower().startswith("https://"):
        raise SimpleFINError("Claim did not return an https access URL.")
    return access_url


def fetch_accounts(
    access_url: str,
    *,
    start_date: int | None = None,
    end_date: int | None = None,
    pending: bool = True,
    account: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Fetch raw account + transaction data for one date window.

    ``start_date``/``end_date`` are Unix timestamps (seconds). The window must
    not exceed 90 days; use :func:`finance_mcp.sync.sync` for longer ranges.
    Returns the parsed JSON dict including any ``errors``/``errlist`` entries.
    """
    endpoint = _split_access_url(access_url)
    params: list[tuple[str, str]] = [("pending", "1" if pending else "0")]
    if start_date is not None:
        params.append(("start-date", str(int(start_date))))
    if end_date is not None:
        params.append(("end-date", str(int(end_date))))
    if account is not None:
        params.append(("account", account))

    url = f"{endpoint.base}/accounts?{urllib.parse.urlencode(params)}"

    token = base64.b64encode(
        f"{endpoint.username}:{endpoint.password}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "User-Agent": USER_AGENT},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SimpleFINError(
            f"Fetch failed ({exc.code}): {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SimpleFINError(f"Could not reach SimpleFIN: {exc.reason}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SimpleFINError(f"SimpleFIN returned non-JSON response: {exc}") from exc
    if not isinstance(data, dict):
        raise SimpleFINError("SimpleFIN response was not a JSON object.")
    return data
