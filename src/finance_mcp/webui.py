"""Local web UI for reviewing the archive and budgeting reports.

Binds to localhost by default and serves a single-page app plus a small JSON
API. Every endpoint is backed by the same functions the MCP server exposes
(``server.py``), so the browser view and Copilot see identical data and this
surface adds no new analysis logic. The only mutation this surface offers is
marking a recurring bill's cancellation lifecycle (``POST
/api/subscriptions/mark``); it writes through the same ``subscriptions_mark``
function the CLI and MCP use. That write is guarded by the Host allowlist plus a
custom-header check so a cross-site page cannot drive it.

Start it with ``finance-mcp web`` (or ``finance-mcp-web``) and open the printed
URL. Because it serves real financial data, it binds to 127.0.0.1 by default;
override only with an explicit ``--host`` if you understand the exposure.
"""

from __future__ import annotations

import ipaddress
import json
import math
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import server

# Endpoint schema: api name -> (callable, {param: (kind, required)}).
# kind is one of "str" | "int" | "float" | "bool". Only listed params are
# forwarded; anything else in the query string is ignored. Empty strings are
# treated as "not supplied" so the underlying function's default applies.
_ENDPOINTS: dict[str, tuple] = {
    "accounts": (server.list_accounts, {}),
    "networth": (server.net_worth_history, {}),
    "stats": (server.archive_stats, {}),
    "categorization": (server.categorization_status, {}),
    "transactions": (
        server.get_transactions,
        {
            "start_date": ("str", False),
            "end_date": ("str", False),
            "account_id": ("str", False),
            "search": ("str", False),
            "category": ("str", False),
            "include_transfers": ("bool", False),
            "min_amount": ("float", False),
            "max_amount": ("float", False),
            "include_pending": ("bool", False),
            "limit": ("int", False),
        },
    ),
    "summary": (
        server.spending_summary,
        {
            "group_by": ("str", False),
            "start_date": ("str", False),
            "end_date": ("str", False),
            "include_pending": ("bool", False),
            "exclude_transfers": ("bool", False),
            "exclude_income": ("bool", False),
        },
    ),
    "burndown": (server.budget_burndown, {"month": ("str", True)}),
    "forecast": (
        server.budget_forecast,
        {"as_of": ("str", False), "through": ("str", False)},
    ),
    "allocation": (
        server.allocation_audit_report,
        {
            "start": ("str", False),
            "end": ("str", False),
            "day_tolerance": ("int", False),
        },
    ),
    "subscriptions": (
        server.subscription_audit_report,
        {
            "start": ("str", False),
            "end": ("str", False),
            "day_tolerance": ("int", False),
            "min_occurrences": ("int", False),
        },
    ),
    "transfers": (server.list_transfers, {"status": ("str", False)}),
    "redflags": (server.red_flags_report, {"as_of": ("str", False)}),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce_one(name: str, kind: str, raw: str):
    if kind == "str":
        return raw
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer, got {raw!r}")
    if kind == "float":
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {raw!r}")
        return value
    if kind == "bool":
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{name} must be a boolean, got {raw!r}")
    raise ValueError(f"unknown param kind {kind!r}")  # pragma: no cover


def _build_kwargs(params: dict[str, list[str]], schema: dict[str, tuple]) -> dict:
    """Coerce whitelisted query params into kwargs; raise ValueError on bad input."""
    kwargs: dict = {}
    for name, (kind, required) in schema.items():
        values = params.get(name)
        raw = values[0] if values else ""
        if raw == "":
            if required:
                raise ValueError(f"missing required parameter: {name}")
            continue
        kwargs[name] = _coerce_one(name, kind, raw)
    return kwargs


def handle_api(name: str, params: dict[str, list[str]]) -> tuple[int, dict]:
    """Dispatch one API call. Returns (http_status, json_body).

    Pure function (no socket) so it is unit-testable. Bad input yields a 400 with
    a structured error; an unexpected failure yields a 500 with the error class
    and message (the full traceback goes to stderr, never to the browser).
    """
    spec = _ENDPOINTS.get(name)
    if spec is None:
        return 404, {"ok": False, "error": f"unknown endpoint: {name}"}
    fn, schema = spec
    try:
        kwargs = _build_kwargs(params, schema)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    try:
        result = fn(**kwargs)
    except Exception as exc:  # boundary guard: keep the server thread alive
        traceback.print_exc(file=sys.stderr)
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return 200, result


# Mutating endpoints reachable via POST. Kept deliberately tiny: the web UI is a
# review surface, and the one write it offers is marking a recurring bill's
# cancellation lifecycle. Same coercion contract as the GET table above.
_POST_ENDPOINTS: dict[str, tuple] = {
    "subscriptions/mark": (
        server.subscriptions_mark,
        {
            "name": ("str", True),
            "lifecycle": ("str", True),
            "cancel_effective": ("str", False),
            "variable": ("bool", False),
        },
    ),
}

# Largest POST body we will read. The only writer takes three short strings, so
# anything beyond this is malformed or hostile; cap it before allocating.
_MAX_BODY = 64 * 1024

# Total wall-clock budget for reading a request body, in seconds. The per-read
# socket timeout below only bounds *inactivity* (it resets on every byte), so a
# client that dribbles one byte just under the timeout could otherwise pin a
# worker thread for body_length * timeout seconds. We enforce this as a single
# deadline across the whole read so the worst case is bounded regardless of how
# the bytes are paced.
_BODY_DEADLINE = 30.0

# CSRF defense for the write endpoint. A cross-site HTML form cannot set a custom
# request header, and a cross-site fetch that sets one triggers a CORS preflight
# this server never answers, so the browser blocks the real request. Same-origin
# requests from our own page set it freely. Combined with the Host allowlist,
# this keeps the mutation reachable only from the local UI.
_XRW_HEADER = "X-Requested-With"
_XRW_VALUE = "finance-mcp"


def _build_kwargs_json(body: dict, schema: dict[str, tuple]) -> dict:
    """Coerce a JSON object into kwargs; raise ValueError on bad/missing input.

    Mirrors ``_build_kwargs`` but reads a decoded JSON object instead of query
    params. ``null`` and empty strings are treated as "not supplied" so the
    callee's default applies; required params then raise.
    """
    kwargs: dict = {}
    for name, (kind, required) in schema.items():
        raw = body.get(name)
        if raw is None or raw == "":
            if required:
                raise ValueError(f"missing required parameter: {name}")
            continue
        if not isinstance(raw, str):
            # A JSON boolean is accepted directly for a bool param (the JS sends a
            # real ``true``/``false``); any other non-string is malformed.
            if kind == "bool" and isinstance(raw, bool):
                kwargs[name] = raw
                continue
            raise ValueError(f"{name} must be a string")
        kwargs[name] = _coerce_one(name, kind, raw)
    return kwargs


def handle_api_post(name: str, body) -> tuple[int, dict]:
    """Dispatch one mutating API call. Returns (http_status, json_body).

    Pure function (no socket) for unit-testing. Unknown endpoint -> 404; a body
    that is not a JSON object or fails validation -> 400; a callee that reports
    ``ok: False`` (e.g. no such bill) -> 400; an unexpected failure -> 500 with
    the error class and message (the traceback goes to stderr, never the browser).
    """
    spec = _POST_ENDPOINTS.get(name)
    if spec is None:
        return 404, {"ok": False, "error": f"unknown endpoint: {name}"}
    fn, schema = spec
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "request body must be a JSON object"}
    try:
        kwargs = _build_kwargs_json(body, schema)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    try:
        result = fn(**kwargs)
    except Exception as exc:  # boundary guard: keep the server thread alive
        traceback.print_exc(file=sys.stderr)
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    status = 200 if result.get("ok", True) else 400
    return status, result
# data, because a malicious page can point an attacker-controlled hostname at
# 127.0.0.1 and fetch the API. We additionally require the request's Host header
# to name a host we expect, and this check is ALWAYS enforced (it fails closed).
# A wildcard bind (0.0.0.0) does not weaken it: the allowlist is still just the
# loopback names plus any host the user explicitly passes via ``--allow-host``,
# so reaching the LAN-bound server requires naming the host you will use rather
# than accepting every Host header.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def _is_wildcard_name(name: str) -> bool:
    """True if ``name`` is empty or any spelling of the unspecified address.

    Catches the bare literals plus bracketed and long-form IPv6 (``[::]``,
    ``0:0:0:0:0:0:0:0``) so no spelling of "any host" can enter the allowlist.
    """
    if not name or name in _WILDCARD_HOSTS:
        return True
    candidate = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        return ipaddress.ip_address(candidate).is_unspecified
    except ValueError:
        return False


def _hostname_only(host_header: str) -> str:
    """Return the host portion of a Host header, stripping any :port."""
    h = host_header.strip()
    if h.startswith("["):  # bracketed IPv6, e.g. [::1]:8765
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if h.count(":") == 1:  # host:port (bare IPv6 is never valid unbracketed here)
        return h.rsplit(":", 1)[0]
    return h


def _host_policy(bind_host: str, extra_hosts: tuple[str, ...] = ()) -> frozenset[str]:
    """Return the set of Host names allowed to reach the API.

    Always includes the loopback names. A concrete (non-wildcard) bind host is
    allowed too. Anything else — including reaching a wildcard-bound server over
    the LAN — must be named explicitly in ``extra_hosts`` (``--allow-host``), so
    the allowlist never silently accepts an arbitrary Host header.
    """
    allowed = set(_LOOPBACK_HOSTS)
    if bind_host not in _WILDCARD_HOSTS:
        allowed.add(bind_host.lower())
    for h in extra_hosts:
        name = h.strip().lower()
        # Drop empties and every spelling of the unspecified address so a stray
        # --allow-host value can never widen the allowlist to "any host".
        if not _is_wildcard_name(name):
            allowed.add(name)
    return frozenset(allowed)


class _Handler(BaseHTTPRequestHandler):
    server_version = "finance-mcp-webui"

    # Bound how long a single connection may keep a worker thread blocked on a
    # slow or stalled client (e.g. a request that declares a Content-Length but
    # dribbles or never sends the body). Without this, ThreadingHTTPServer would
    # pin one thread per such connection indefinitely.
    timeout = 30

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[webui] " + (fmt % args) + "\n")

    def _host_allowed(self) -> bool:
        # Always enforced. Default to loopback-only so a handler constructed
        # without an explicit policy (e.g. in tests) still fails closed off-host.
        allowed = getattr(self.server, "allowed_hosts", _LOOPBACK_HOSTS)
        name = _hostname_only(self.headers.get("Host", "")).lower()
        return name in allowed

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This is a localhost review tool serving private data; forbid embedding
        # and disable client-side caching so a reload always reflects the archive.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if not self._host_allowed():
            # Reject foreign Host headers (DNS-rebinding) before touching data.
            self._send(403, b"forbidden host", "text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            name = path[len("/api/"):]
            params = parse_qs(parsed.query, keep_blank_values=True)
            status, body = handle_api(name, params)
            payload = json.dumps(body, default=str).encode("utf-8")
            self._send(status, payload, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def _read_body(self, length: int) -> bytes | None:
        """Read up to ``length`` body bytes under a single total deadline.

        ``_Handler.timeout`` only bounds inactivity (it resets on every byte), so
        a client dribbling one byte at a time could otherwise hold this worker
        thread for ``length * timeout`` seconds. We instead enforce one
        ``_BODY_DEADLINE`` budget across the entire read: before each chunk we
        shrink the socket timeout to the time left, so no single read can block
        past the deadline and the total read is bounded however the bytes arrive.

        Returns the bytes read (possibly shorter than ``length`` if the peer
        closed early — the caller rejects that as a short body). Returns ``None``
        after sending a 408 if the deadline elapses or the socket errors, so the
        caller must stop touching the connection.
        """
        deadline = time.monotonic() + _BODY_DEADLINE
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    self._send(408,
                               b'{"ok": false, "error": "request body read timed out"}',
                               "application/json; charset=utf-8")
                    return None
                # Cap this read so it cannot block past the overall deadline.
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break  # peer closed early; caller sees a short body -> 400
                chunks.append(chunk)
                remaining -= len(chunk)
        except (TimeoutError, OSError):
            # Slow/stalled client, or the peer dropped mid-body. Give up this
            # connection rather than block the worker; nothing was written, so
            # there is no partial state.
            self._send(408, b'{"ok": false, "error": "request body read timed out"}',
                       "application/json; charset=utf-8")
            return None
        finally:
            # Restore the steady-state inactivity timeout for any further use of
            # this connection (e.g. a keep-alive follow-up request).
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if not self._host_allowed():
            # Reject foreign Host headers (DNS-rebinding) before touching data.
            self._send(403, b"forbidden host", "text/plain; charset=utf-8")
            return
        # CSRF guard: a cross-site form cannot set this header, and a cross-site
        # fetch that sets it triggers a preflight we never answer. Fail closed.
        if self.headers.get(_XRW_HEADER) != _XRW_VALUE:
            self._send(403, b"missing or bad X-Requested-With",
                       "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        name = path[len("/api/"):]
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 0 or length > _MAX_BODY:
                raise ValueError
        except (TypeError, ValueError):
            self._send(400, b'{"ok": false, "error": "missing or bad Content-Length"}',
                       "application/json; charset=utf-8")
            return
        raw = b""
        if length:
            raw = self._read_body(length)
            if raw is None:
                # _read_body already sent a 408; the worker is freed.
                return
            if len(raw) != length:
                # Peer closed after sending fewer bytes than it promised; treat
                # the truncated body as a bad request rather than guessing.
                self._send(400, b'{"ok": false, "error": "short request body"}',
                           "application/json; charset=utf-8")
                return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"ok": false, "error": "invalid JSON body"}',
                       "application/json; charset=utf-8")
            return
        status, result = handle_api_post(name, body)
        payload = json.dumps(result, default=str).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8")


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_hosts: tuple[str, ...] = (),
) -> int:
    """Run the web UI until interrupted. Returns a process exit code.

    ``allow_hosts`` adds Host-header names that may reach the API (needed to
    reach a non-loopback or wildcard bind from another device); the loopback
    names are always allowed.
    """
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.allowed_hosts = _host_policy(host, tuple(allow_hosts))
    bound_host, bound_port = httpd.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"finance-mcp web UI on {url}")
    print("Read-only review surface. Press Ctrl+C to stop.")
    if not _is_loopback(host):
        print(
            f"WARNING: bound to {host} — this exposes your private financial data "
            "to the network with NO authentication. Use 127.0.0.1 unless you "
            "understand the exposure. Other devices must reach it via an "
            "--allow-host name; every other Host header is refused.",
            file=sys.stderr,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0


def _console_main() -> int:
    """Entry point for the ``finance-mcp-web`` console script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="finance-mcp-web",
        description="Serve the local read-only finance-mcp review UI.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; serves private data)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                        help="extra Host-header name allowed to reach the API "
                             "(repeatable; needed to reach a non-loopback bind)")
    args = parser.parse_args()
    return serve(host=args.host, port=args.port, allow_hosts=tuple(args.allow_host))


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>finance-mcp review</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2b3440; --fg:#e6edf3;
          --muted:#8b98a5; --accent:#4493f8; --good:#3fb950; --warn:#d29922;
          --bad:#f85149; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:12px; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .sub { color:var(--muted); font-size:12px; }
  nav { display:flex; flex-wrap:wrap; gap:4px; padding:10px 20px;
        border-bottom:1px solid var(--line); }
  nav button { background:transparent; color:var(--muted); border:1px solid transparent;
               padding:6px 12px; border-radius:6px; cursor:pointer; font-size:13px; }
  nav button:hover { color:var(--fg); }
  nav button.active { background:var(--panel); color:var(--fg); border-color:var(--line); }
  main { padding:20px; }
  .filters { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end;
             margin-bottom:14px; }
  .filters label { display:flex; flex-direction:column; font-size:11px;
                   color:var(--muted); gap:3px; }
  .filters input, .filters select { background:var(--panel); color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:5px 7px; font-size:13px; }
  .filters input[type=text], .filters input[type=date] { min-width:130px; }
  .subtabs { display:flex; gap:4px; width:100%; margin-bottom:4px; }
  .subtabs button { background:transparent; color:var(--muted); border:1px solid var(--line);
        border-radius:6px; padding:4px 12px; cursor:pointer; font-size:12px; text-transform:capitalize; }
  .subtabs button:hover { color:var(--fg); }
  .subtabs button.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .monthnav { display:flex; align-items:center; gap:6px; width:100%; flex-wrap:wrap; }
  .monthnav .arrow { background:var(--panel); color:var(--fg); border:1px solid var(--line);
        border-radius:6px; width:30px; height:30px; cursor:pointer; font-size:16px; line-height:1; padding:0; }
  .monthnav .arrow:hover { border-color:var(--accent); }
  .monthnav .mlabel { min-width:150px; text-align:center; font-weight:600; font-size:14px; }
  .monthnav .cal { background:var(--panel); color:var(--muted); border:1px solid var(--line);
        border-radius:6px; height:30px; padding:0 9px; cursor:pointer; font-size:14px; }
  .monthnav .cal:hover, .monthnav .cal.active { border-color:var(--accent); color:var(--fg); }
  .monthnav .custom { display:flex; align-items:center; gap:6px; }
  .monthnav .custom input { background:var(--panel); color:var(--fg); border:1px solid var(--line);
        border-radius:6px; padding:4px 7px; font-size:13px; }
  button.go { background:var(--accent); color:#fff; border:none; border-radius:6px;
              padding:7px 14px; cursor:pointer; font-size:13px; height:31px; }
  button.go:hover { filter:brightness(1.1); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--bg); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:var(--panel); }
  .neg { color:var(--bad); } .pos { color:var(--good); }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
          border:1px solid var(--line); color:var(--muted); }
  .pill.good { color:var(--good); border-color:var(--good); }
  .pill.warn { color:var(--warn); border-color:var(--warn); }
  .pill.bad  { color:var(--bad);  border-color:var(--bad); }
  .notice { padding:12px 14px; border:1px solid var(--warn); border-radius:8px;
            background:rgba(210,153,34,.08); color:var(--warn); margin-bottom:14px; }
  .err { border-color:var(--bad); background:rgba(248,81,73,.08); color:var(--bad); }
  .notice.ok { border-color:var(--good); background:rgba(63,185,80,.08); color:var(--good); }
  .badge { display:inline-block; min-width:16px; padding:0 5px; margin-left:6px;
           border-radius:9px; background:var(--bad); color:#fff; font-size:11px;
           font-weight:700; line-height:16px; text-align:center; }
  .flagbanner { margin:0; padding:12px 20px; background:var(--bad); color:#fff;
                font-size:13px; display:flex; align-items:center; gap:12px;
                cursor:pointer; font-weight:600; }
  .flagbanner[hidden] { display:none; }
  .flagbanner button { background:#fff; color:var(--bad); border:none; border-radius:6px;
                       padding:4px 11px; font-size:12px; font-weight:700; cursor:pointer; }
  .muted { color:var(--muted); }
  h2 { font-size:14px; margin:18px 0 8px; }
  details { margin-top:18px; }
  details summary { cursor:pointer; color:var(--muted); font-size:12px; }
  pre { background:var(--panel); border:1px solid var(--line); border-radius:8px;
        padding:12px; overflow:auto; font-size:12px; max-height:60vh; }
  .cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:10px 14px; min-width:120px; }
  .card .k { color:var(--muted); font-size:11px; }
  .card .v { font-size:18px; font-weight:600; }
  button.mini { background:transparent; color:var(--accent); border:1px solid var(--line);
                border-radius:6px; padding:3px 9px; cursor:pointer; font-size:12px; }
  button.mini:hover { border-color:var(--accent); }
  .overlay { position:fixed; inset:0; background:rgba(0,0,0,.55);
             display:flex; align-items:center; justify-content:center; z-index:50; }
  /* The hidden attribute must win over display:flex, or the modal shows on load. */
  .overlay[hidden] { display:none; }
  .modal { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:18px 20px; width:min(420px,92vw); }
  .modal h3 { margin:0 0 4px; font-size:15px; }
  .modal .name { color:var(--muted); font-size:12px; margin-bottom:14px;
                 word-break:break-word; }
  .modal label { display:flex; flex-direction:column; gap:4px; font-size:12px;
                 color:var(--muted); margin-bottom:12px; }
  .modal label.check { flex-direction:row; align-items:center; gap:8px; }
  .modal label.check input { width:auto; margin:0; }
  .modal select, .modal input { background:var(--bg); color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:6px 8px; font-size:13px; }
  .modal .row { display:flex; gap:8px; justify-content:flex-end; margin-top:6px; }
  .modal .hint { font-size:11px; color:var(--muted); margin:-6px 0 12px; }
</style>
</head>
<body>
<header>
  <h1>finance-mcp</h1>
  <span class="sub">local review</span>
  <span class="sub" id="syncedAt"></span>
</header>
<nav id="tabs"></nav>
<div id="redflagBanner" class="flagbanner" hidden></div>
<main>
  <div class="filters" id="filters"></div>
  <div id="content"><span class="muted">Loading&hellip;</span></div>
  <details id="rawWrap" hidden>
    <summary>Raw JSON</summary>
    <pre id="raw"></pre>
  </details>
</main>
<div id="markOverlay" class="overlay" hidden>
  <div class="modal">
    <h3>Mark subscription</h3>
    <div class="name" id="markName"></div>
    <label>Status
      <select id="markLifecycle">
        <option value="active">active</option>
        <option value="canceling">canceling (tried, unconfirmed)</option>
        <option value="canceled">canceled (confirmed)</option>
      </select>
    </label>
    <label>Cancellation effective date
      <input type="date" id="markEffective">
    </label>
    <div class="hint" id="markHint">Required when canceling or canceled &mdash; any charge on or after this date is flagged as the bill coming back.</div>
    <label class="check"><input type="checkbox" id="markVariable"> Variable amount (match by merchant &amp; date; don&rsquo;t alert on price changes)</label>
    <div id="markError" class="notice err" hidden></div>
    <div class="row">
      <button class="mini" onclick="closeMark()">Cancel</button>
      <button class="go" id="markSave" onclick="submitMark()">Save</button>
    </div>
  </div>
</div>
<script>
// Pure date helpers (also used to seed the audit tabs' trailing-window
// defaults, so they are defined before TABS).
function isoDate(d) {
  return d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0") +
         "-" + String(d.getDate()).padStart(2,"0");
}
function todayIso() { return isoDate(new Date()); }
// First day of the month N months before the current month.
function monthsAgoStart(n) {
  const d = new Date();
  return isoDate(new Date(d.getFullYear(), d.getMonth() - n, 1));
}
const TABS = [
  { id:"accounts",      label:"Accounts",      filters:[] },
  { id:"redflags",      label:"Red flags",     filters:[ {k:"as_of",type:"date"} ] },
  { id:"transactions",  label:"Transactions",
      range:{start:"start_date",end:"end_date"},
      filters:[
      {k:"search",type:"text",ph:"merchant / memo"},
      {k:"category",type:"text",ph:"category"},
      {k:"include_transfers",type:"bool",label:"transfers",def:false},
      {k:"limit",type:"number",def:200} ] },
  { id:"summary",       label:"Spending",
      range:{start:"start_date",end:"end_date"},
      subtabs:{k:"group_by",opts:["category","account","envelope","org","month"],yearNav:"month"},
      filters:[
      {k:"exclude_income",type:"bool",label:"exclude income",def:true} ] },
  { id:"networth",      label:"Net worth",     filters:[] },
  { id:"transfers",     label:"Transfers",     filters:[
      {k:"status",type:"select",opts:["","unconfirmed","inferred","confirmed","unmatched"]} ] },
  { id:"burndown",      label:"Burn-down",     month:"month", filters:[] },
  { id:"forecast",      label:"Forecast",      filters:[
      {k:"as_of",type:"date"}, {k:"through",type:"date"} ] },
  // Allocation and Subscriptions are multi-month audit windows, not month-scoped
  // views: a single month can't detect a recurring cadence and would falsely
  // report tracked bills as missing. They keep an explicit date range, defaulted
  // to a trailing ~6 months so the view loads populated.
  { id:"allocation",    label:"Allocation",    filters:[
      {k:"start",type:"date",def:monthsAgoStart(5)}, {k:"end",type:"date",def:todayIso()},
      {k:"day_tolerance",type:"number",def:7} ] },
  { id:"subscriptions", label:"Subscriptions", filters:[
      {k:"start",type:"date",def:monthsAgoStart(5)}, {k:"end",type:"date",def:todayIso()},
      {k:"day_tolerance",type:"number",def:7},
      {k:"min_occurrences",type:"number",def:3} ] },
];

let current = TABS[0];
const $ = (id) => document.getElementById(id);

function money(v) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (Number.isNaN(n)) return esc(v);
  const s = n.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
  return `<span class="${n<0?'neg':n>0?'pos':''}">${s}</span>`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function table(rows, cols) {
  if (!rows || !rows.length) return `<p class="muted">No rows.</p>`;
  const head = cols.map(c => `<th class="${c.num?'num':''}">${esc(c.label)}</th>`).join("");
  const body = rows.map(r => "<tr>" + cols.map(c => {
    const raw = c.get ? c.get(r) : r[c.k];
    const cell = c.money ? money(raw) : (c.html ? raw : esc(raw));
    return `<td class="${c.num?'num':''}">${cell ?? ""}</td>`;
  }).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function pill(text, cls) { return `<span class="pill ${cls||''}">${esc(text)}</span>`; }

// Tracked rows from the last subscriptions render, so the Mark button can look
// up the row it belongs to by index without re-encoding the name into markup.
let _trackedRows = [];
let _markIdx = -1;

function openMark(i) {
  const r = _trackedRows[i];
  if (!r) return;
  _markIdx = i;
  $("markName").textContent = r.name;
  $("markLifecycle").value = r.lifecycle || "active";
  $("markEffective").value = r.cancel_effective || "";
  $("markVariable").checked = !!r.variable;
  const err = $("markError"); err.hidden = true; err.textContent = "";
  $("markOverlay").hidden = false;
}

function closeMark() {
  $("markOverlay").hidden = true;
  _markIdx = -1;
}

async function submitMark() {
  const r = _trackedRows[_markIdx];
  if (!r) { closeMark(); return; }
  const lifecycle = $("markLifecycle").value;
  const effective = $("markEffective").value;
  const variable = $("markVariable").checked;
  const err = $("markError");
  if (lifecycle !== "active" && !effective) {
    err.textContent = "Pick a cancellation effective date for a canceling/canceled bill.";
    err.hidden = false;
    return;
  }
  const payload = { name: r.name, lifecycle, variable };
  if (lifecycle !== "active") payload.cancel_effective = effective;
  const save = $("markSave");
  save.disabled = true;
  try {
    const res = await fetch("/api/subscriptions/mark", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "finance-mcp" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      err.textContent = data.error || `Request failed (${res.status})`;
      err.hidden = false;
      return;
    }
    closeMark();
    load();
  } catch (e) {
    err.textContent = `Request failed: ${e}`;
    err.hidden = false;
  } finally {
    save.disabled = false;
  }
}

// Per-tab month-navigator state (session-only). Every range/monthly tab starts
// on the CURRENT calendar month; the arrows walk months and the calendar icon
// swaps in a custom start/end range. State persists while the page is open so
// flipping between tabs keeps your place, but a reload returns to this month.
const NAVSTATE = {};
function navFor(t) {
  if (!NAVSTATE[t.id]) {
    const n = new Date();
    NAVSTATE[t.id] = { mode:"month", y:n.getFullYear(), m:n.getMonth(), custom:{start:"",end:""} };
  }
  return NAVSTATE[t.id];
}
function monthBounds(y, m) {
  const start = new Date(y, m, 1), end = new Date(y, m+1, 0);
  return { start:isoDate(start), end:isoDate(end),
           ym: y + "-" + String(m+1).padStart(2,"0"),
           label: start.toLocaleString(undefined, {month:"long", year:"numeric"}) };
}
function shiftMonth(st, delta) {
  let m = st.m + delta, y = st.y;
  while (m < 0) { m += 12; y--; }
  while (m > 11) { m -= 12; y++; }
  st.m = m; st.y = y;
}
// Whole-calendar-year bounds, used when a tab groups by month (grouping by
// month only makes sense across a span of months, so its navigator steps by
// year rather than by month).
function yearBounds(y) {
  return { start: y + "-01-01", end: y + "-12-31", label: String(y) };
}
function setHidden(wrap, key, val) {
  let el = $("f_" + key);
  if (!el) { el = document.createElement("input"); el.type = "hidden"; el.id = "f_" + key; wrap.appendChild(el); }
  el.value = (val == null) ? "" : val;
}

// Per-tab subtab selection (e.g. Spending's group-by). Persisted so the tab
// reopens on the same grouping it was last viewed with.
const SUBSTATE = {};
function subState(t) {
  if (!SUBSTATE[t.id]) {
    let v = t.subtabs.opts[0];
    try { const s = localStorage.getItem("fmcp.sub." + t.id);
          if (s && t.subtabs.opts.includes(s)) v = s; } catch (e) {}
    SUBSTATE[t.id] = { value:v };
  }
  return SUBSTATE[t.id];
}
function renderSubtabs(t, wrap) {
  const st = subState(t);
  const row = document.createElement("div"); row.className = "subtabs";
  const buttons = [];
  for (const o of t.subtabs.opts) {
    const b = document.createElement("button");
    b.textContent = o;
    if (o === st.value) b.classList.add("active");
    b.onclick = () => {
      const prev = st.value;
      st.value = o;
      try { localStorage.setItem("fmcp.sub." + t.id, o); } catch (e) {}
      // A yearNav tab's navigator granularity (month vs year) depends on the
      // active subtab, so only a subtab change that crosses the month<->year
      // boundary needs the navigator rebuilt. Rebuild only on that crossing,
      // and carry manual filter values across the rebuild so switching subtabs
      // never resets user-entered filters (e.g. exclude_income).
      const crosses = t.subtabs.yearNav &&
        ((prev === t.subtabs.yearNav) !== (o === t.subtabs.yearNav));
      if (crosses) {
        const saved = {};
        for (const f of t.filters) { const el = $("f_" + f.k); if (el) saved[f.k] = el.value; }
        buildFilters();
        for (const f of t.filters) { const el = $("f_" + f.k); if (el && f.k in saved) el.value = saved[f.k]; }
        load();
        return;
      }
      for (const bb of buttons) bb.classList.toggle("active", bb.textContent === o);
      setHidden(wrap, t.subtabs.k, o);
      load();
    };
    buttons.push(b); row.appendChild(b);
  }
  wrap.appendChild(row);
  setHidden(wrap, t.subtabs.k, st.value);
}
function renderMonthNav(t, wrap) {
  const st = navFor(t);
  // Year granularity when the active subtab groups by month (see yearBounds).
  const yearMode = !!(t.subtabs && t.subtabs.yearNav && subState(t).value === t.subtabs.yearNav);
  const nav = document.createElement("div"); nav.className = "monthnav";
  wrap.appendChild(nav);
  function resolveHidden() {
    if (st.mode === "custom") {
      if (t.range) { setHidden(wrap, t.range.start, st.custom.start); setHidden(wrap, t.range.end, st.custom.end); }
      if (t.month) { setHidden(wrap, t.month, st.custom.start ? st.custom.start.slice(0,7) : ""); }
    } else if (yearMode) {
      const b = yearBounds(st.y);
      if (t.range) { setHidden(wrap, t.range.start, b.start); setHidden(wrap, t.range.end, b.end); }
    } else {
      const b = monthBounds(st.y, st.m);
      if (t.range) { setHidden(wrap, t.range.start, b.start); setHidden(wrap, t.range.end, b.end); }
      if (t.month) { setHidden(wrap, t.month, b.ym); }
    }
  }
  function draw() {
    nav.innerHTML = "";
    const cal = document.createElement("button");
    cal.className = "cal" + (st.mode === "custom" ? " active" : "");
    cal.title = "Custom date range"; cal.textContent = "\uD83D\uDCC5";
    if (st.mode === "custom") {
      const c = document.createElement("div"); c.className = "custom";
      if (t.range) {
        const s = document.createElement("input"); s.type = "date"; s.value = st.custom.start;
        const e = document.createElement("input"); e.type = "date"; e.value = st.custom.end;
        s.onchange = () => { st.custom.start = s.value; resolveHidden(); load(); };
        e.onchange = () => { st.custom.end = e.value; resolveHidden(); load(); };
        c.append(s, document.createTextNode("\u2192"), e);
      } else if (t.month) {
        const mi = document.createElement("input"); mi.type = "month";
        mi.value = st.custom.start ? st.custom.start.slice(0,7) : monthBounds(st.y, st.m).ym;
        mi.onchange = () => { st.custom.start = mi.value ? mi.value + "-01" : ""; resolveHidden(); load(); };
        c.append(mi);
      }
      const back = document.createElement("button"); back.className = "arrow";
      back.title = "Back to month view"; back.textContent = "\u21A9";
      back.onclick = () => { st.mode = "month"; resolveHidden(); draw(); load(); };
      cal.onclick = () => { st.mode = "month"; resolveHidden(); draw(); load(); };
      nav.append(c, cal, back);
    } else {
      const prev = document.createElement("button"); prev.className = "arrow";
      const next = document.createElement("button"); next.className = "arrow";
      const lbl = document.createElement("span"); lbl.className = "mlabel";
      if (yearMode) {
        prev.title = "Previous year"; prev.textContent = "\u2039";
        next.title = "Next year"; next.textContent = "\u203A";
        lbl.textContent = yearBounds(st.y).label;
        prev.onclick = () => { st.y -= 1; resolveHidden(); draw(); load(); };
        next.onclick = () => { st.y += 1; resolveHidden(); draw(); load(); };
      } else {
        prev.title = "Previous month"; prev.textContent = "\u2039";
        next.title = "Next month"; next.textContent = "\u203A";
        lbl.textContent = monthBounds(st.y, st.m).label;
        prev.onclick = () => { shiftMonth(st, -1); resolveHidden(); draw(); load(); };
        next.onclick = () => { shiftMonth(st, 1); resolveHidden(); draw(); load(); };
      }
      cal.onclick = () => {
        st.mode = "custom";
        if (!st.custom.start || !st.custom.end) {
          const b = yearMode ? yearBounds(st.y) : monthBounds(st.y, st.m);
          st.custom.start = b.start; st.custom.end = b.end;
        }
        resolveHidden(); draw(); load();
      };
      nav.append(prev, lbl, next, cal);
    }
  }
  resolveHidden();
  draw();
}

function buildFilters() {
  const wrap = $("filters"); wrap.innerHTML = "";
  if (current.subtabs) renderSubtabs(current, wrap);
  if (current.range || current.month) renderMonthNav(current, wrap);
  let hasManual = false;
  for (const f of current.filters) {
    hasManual = true;
    const lab = document.createElement("label");
    lab.textContent = f.label || f.k;
    let el;
    if (f.type === "select") {
      el = document.createElement("select");
      for (const o of f.opts)
        el.appendChild(new Option(o === "" ? "(all)" : o, o));
    } else if (f.type === "bool") {
      el = document.createElement("select");
      el.appendChild(new Option("yes","true")); el.appendChild(new Option("no","false"));
      el.value = f.def === false ? "false" : "true";
    } else {
      el = document.createElement("input");
      el.type = f.type === "month" ? "month" : f.type;
      if (f.ph) el.placeholder = f.ph;
      let def = f.def;
      if (def !== undefined) el.value = def;
    }
    el.id = "f_" + f.k;
    lab.appendChild(el); wrap.appendChild(lab);
  }
  if (hasManual) {
    const go = document.createElement("button");
    go.className = "go"; go.textContent = "Load"; go.onclick = load;
    wrap.appendChild(go);
  }
}
function collectParams() {
  const p = new URLSearchParams();
  const keys = [];
  if (current.subtabs) keys.push(current.subtabs.k);
  if (current.range) keys.push(current.range.start, current.range.end);
  if (current.month) keys.push(current.month);
  for (const f of current.filters) keys.push(f.k);
  for (const k of keys) {
    const v = ($("f_" + k) || {}).value;
    if (v !== undefined && v !== "") p.set(k, v);
  }
  return p.toString();
}

const RENDER = {
  redflags(d) {
    const s = d.summary || {};
    const flags = d.flags || [];
    const red = flags.filter(f => f.severity === "red");
    const info = flags.filter(f => f.severity === "info");
    let out = `<div class="cards">
      <div class="card"><div class="k">returned</div><div class="v">${s.returned||0}</div></div>
      <div class="card"><div class="k">missed</div><div class="v">${s.missed||0}</div></div>
      <div class="card"><div class="k">can&rsquo;t audit</div><div class="v">${s.unauditable||0}</div></div>
    </div>`;
    if (!d.debt_account_count) {
      out += `<p class="muted">No debt accounts configured. Add a <code>debt_accounts</code> list to your budget config to watch loan payments.</p>`;
      return out;
    }
    if (!red.length) {
      out += `<div class="notice ok">No returned or missed debt payments since ${esc(d.start||"")}. &#10003;</div>`;
    } else {
      out += `<div class="notice err"><strong>&#9888; ${red.length} debt-payment red flag${red.length>1?'s':''}.</strong> A loan payment was returned or missed &mdash; the debt may not have been paid.</div>`;
      out += table(red, [
        {label:"Flag",html:true,get:r=>pill(r.kind_label||r.kind,"bad")},
        {label:"Account",get:r=>r.account_label},
        {label:"When",get:r=>r.date || r.month},
        {label:"Amount",num:true,money:true,get:r=>r.actual},
        {label:"What happened",get:r=>r.detail},
      ]);
    }
    if (info.length) {
      out += `<h2>Can&rsquo;t audit</h2>`;
      out += `<p class="muted">These debts couldn&rsquo;t be fully verified &mdash; see each row for why.</p>`;
      out += table(info, [
        {label:"Account",get:r=>r.account_label},
        {label:"Why",get:r=>r.detail},
      ]);
    }
    return out;
  },
  accounts(d) {
    if (d.synced_at) $("syncedAt").textContent = "synced " + d.synced_at;
    const rows = (d.accounts||[]).slice().sort((a,b)=>(b.balance||0)-(a.balance||0));
    return `<div class="cards">
        <div class="card"><div class="k">accounts</div><div class="v">${d.account_count||0}</div></div>
      </div>` + table(rows, [
      {k:"org",label:"Institution"},
      {k:"account_name",label:"Account"},
      {k:"balance",label:"Balance",num:true,money:true},
      {k:"currency",label:"Cur"},
      {k:"balance_date",label:"As of"},
    ]);
  },
  transactions(d) {
    const note = `<p class="muted">${d.returned} of ${d.total_matches} matches shown.</p>`;
    return note + table(d.transactions, [
      {k:"posted",label:"Date"},
      {k:"account_name",label:"Account"},
      {label:"Description",get:r=>r.description||r.payee||r.memo||""},
      {k:"amount_float",label:"Amount",num:true,money:true},
      {k:"category",label:"Category"},
      {label:"Xfer",html:true,get:r=>r.is_transfer?pill("transfer"):""},
    ]);
  },
  summary(d) {
    const groups = d.groups || d.summary || [];
    const rows = Array.isArray(groups) ? groups
      : Object.entries(groups).map(([k,v])=>({key:k, ...(typeof v==='object'?v:{value:v})}));
    return table(rows, [
      {label:"Group",get:r=>r.key ?? r.group ?? r.name},
      {label:"Spent",num:true,money:true,get:r=>r.outflow},
      {label:"Returns",num:true,money:true,get:r=>r.inflow},
      {label:"Unclassified in",num:true,money:true,get:r=>r.unclassified_inflow},
      {label:"Net spent",num:true,money:true,get:r=>r.net},
      {label:"Count",num:true,get:r=>r.count},
    ]);
  },
  networth(d) {
    return table(d.history, [
      {label:"As of",get:r=>r.as_of ?? r.date},
      {label:"Total",num:true,money:true,get:r=>r.total ?? r.net_worth},
    ]);
  },
  transfers(d) {
    const sum = d.summary || {};
    const cards = Object.entries(sum).map(([k,v])=>
      `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
    return `<div class="cards">${cards||''}</div>` + table(d.transfers, [
      {k:"link_id",label:"#",num:true},
      {label:"Status",html:true,get:r=>{
        const c = r.status==="confirmed"?"good":r.status==="unconfirmed"?"warn":r.status==="unmatched"?"bad":"";
        return pill(r.status,c);}},
      {label:"From",get:r=>r.from_account||"?"},
      {label:"To",get:r=>r.to_account||"?"},
      {label:"Amount",num:true,money:true,get:r=>r.amount},
      {k:"why",label:"Why"},
    ]);
  },
  burndown(d) {
    const env = d.envelopes || [];
    let out = `<p class="muted">Period ${esc(d.period || d.month || "")}.</p>` + table(env, [
      {label:"Envelope",get:r=>r.envelope ?? r.name},
      {label:"Target",num:true,money:true,get:r=>r.monthly_target},
      {label:"Spent",num:true,money:true,get:r=>r.actual_spend},
      {label:"Remaining",num:true,money:true,get:r=>r.remaining},
      {label:"Over?",html:true,get:r=>r.over_budget?pill("over","bad"):""},
    ]);
    const unmapped = d.unmapped || [];
    if (unmapped.length) {
      out += `<h2>Unmapped spend <span class="muted">(no envelope)</span></h2>`;
      out += table(unmapped, [
        {label:"Account",get:r=>r.account_name ?? r.account ?? r.account_id},
        {label:"Spend",num:true,money:true,get:r=>r.actual_spend},
        {label:"Count",num:true,get:r=>r.txn_count},
      ]);
    }
    return out;
  },
  forecast(d) {
    return `<p class="muted">${esc(d.as_of||"")} &rarr; ${esc(d.through||"")}</p>` +
      table(d.envelopes||[], [
      {label:"Envelope",get:r=>r.envelope ?? r.name},
      {label:"Verdict",html:true,get:r=>{
        const c = r.verdict==="sufficient"?"good":r.verdict==="at_risk"?"bad":"warn";
        return pill(r.verdict,c);}},
      {label:"Current",num:true,money:true,get:r=>r.current_balance},
      {label:"Min balance",num:true,money:true,get:r=>r.projected_min_balance},
      {label:"At risk",get:r=>r.at_risk_date||""},
      {label:"Shortfall",num:true,money:true,get:r=>r.shortfall},
    ]);
  },
  allocation(d) {
    const w = d.window||{}; const s = d.summary||{};
    const cards = Object.entries(s).map(([k,v])=>
      `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
    let out = `<p class="muted">${esc(w.start||"")} &rarr; ${esc(w.end||"")} (tol ${d.day_tolerance}d)</p>
      <div class="cards">${cards}</div>`;
    for (const tr of (d.transfers||[])) {
      out += `<h2>${esc(tr.name)} <span class="muted">${esc(tr.from_envelope||"(external)")} &rarr; ${esc(tr.to_envelope)} &middot; $${esc(tr.amount)} [${esc(tr.kind)}]</span></h2>`;
      out += table(tr.occurrences||[], [
        {label:"Expected",get:r=>r.expected_date},
        {label:"Status",html:true,get:r=>{
          const c = r.status==="on_time"?"good":r.status==="missing"?"bad":"warn";
          return pill(r.status,c);}},
        {label:"Drift",num:true,get:r=>r.drift_days==null?"":(r.drift_days>0?"+":"")+r.drift_days+"d"},
        {label:"Actual",num:true,money:true,get:r=>r.actual_amount},
        {label:"On",get:r=>r.actual_date||""},
      ]);
    }
    if (!(d.transfers||[]).length) out += `<p class="muted">No scheduled transfers configured.</p>`;
    return out;
  },
  subscriptions(d) {
    const sm = d.summary||{};
    const cameBack = d.came_back||[];
    let out = `<div class="cards">
      <div class="card"><div class="k">tracked</div><div class="v">${sm.tracked||0}</div></div>
      <div class="card"><div class="k">missing</div><div class="v">${sm.missing_occurrences||0}</div></div>
      <div class="card"><div class="k">came back</div><div class="v">${sm.came_back||0}</div></div>
      <div class="card"><div class="k">candidates</div><div class="v">${sm.candidates||0}</div></div>
    </div>`;
    if (cameBack.length) {
      out += `<h2>⚠ Canceled bills that came back</h2>`;
      out += `<p class="muted">You marked these canceling or canceled, but a charge posted on or after the cancellation date &mdash; the cancellation may not have taken.</p>`;
      out += table(cameBack, [
        {label:"Subscription",get:r=>r.name},
        {label:"Amount",num:true,money:true,get:r=>r.amount},
        {label:"Marked",html:true,get:r=>pill(r.lifecycle,"bad")},
        {label:"Effective",get:r=>r.cancel_effective||""},
        {label:"Charged again",get:r=>r.came_back_on||r.last_seen||"?"},
      ]);
    }
    out += `<h2>Tracked subscriptions</h2>`;
    if (!sm.tracked) {
      out += `<p class="muted">No saved subscriptions yet &mdash; run <code>finance-mcp subscriptions detect</code> to save your recurring charges as a tracked list.</p>`;
    }
    _trackedRows = d.tracked||[];
    out += table(_trackedRows, [
      {label:"Subscription",get:r=>r.name},
      {label:"Envelope",get:r=>r.envelope||""},
      {label:"Amount",num:true,html:true,get:r=>{
        if (r.variable) {
          const v = money(r.last_amount || r.amount);
          return `${v} <span class="muted" title="amount varies each cycle">~var</span>`;
        }
        return money(r.amount);
      }},
      {label:"Due day",num:true,get:r=>r.day},
      {label:"Next due",get:r=>r.next_due||""},
      {label:"Last seen",get:r=>r.last_seen||"never"},
      {label:"Status",html:true,get:r=>{
        const lc = r.lifecycle||"active";
        if (lc !== "active") {
          if (r.came_back) return pill("came back","bad");
          return pill(lc, lc==="canceling"?"warn":"");
        }
        return pill(r.status, r.status==="overdue"?"bad":r.status==="active"?"good":"warn");
      }},
      {label:"",html:true,get:r=>`<button class="mini" onclick="openMark(${_trackedRows.indexOf(r)})">Mark&hellip;</button>`},
    ]);
    out += `<h2>Missing expected charges</h2>`;
    out += table(d.expected_missing||[], [
      {label:"Bill",get:r=>r.name},
      {label:"Envelope",get:r=>r.envelope},
      {label:"Expected",num:true,money:true,get:r=>r.expected_amount},
      {label:"Due",get:r=>r.expected_date},
      {label:"Last seen",get:r=>r.last_seen||"never"},
    ]);
    out += `<h2>Untracked recurring candidates</h2>`;
    out += table(d.candidate_new||[], [
      {label:"Merchant",get:r=>r.merchant},
      {label:"Amount",num:true,money:true,get:r=>r.amount},
      {label:"Times",num:true,get:r=>r.occurrences},
      {label:"Cadence",get:r=>r.cadence},
      {label:"Last seen",get:r=>r.last_seen},
    ]);
    return out;
  },
};

let loadSeq = 0;
async function load() {
  const myseq = ++loadSeq;
  const activeId = current.id;
  $("content").innerHTML = `<span class="muted">Loading&hellip;</span>`;
  $("rawWrap").hidden = true;
  const q = collectParams();
  const url = `/api/${current.id}${q ? "?" + q : ""}`;
  let status, data;
  try {
    const res = await fetch(url);
    status = res.status;
    data = await res.json();
  } catch (e) {
    if (myseq !== loadSeq) return;
    $("content").innerHTML = `<div class="notice err">Request failed: ${esc(e)}</div>`;
    return;
  }
  // Discard a response that lost the race to a newer load() (rapid month
  // navigation or tab switching), so the view always reflects the last request.
  if (myseq !== loadSeq) return;
  $("raw").textContent = JSON.stringify(data, null, 2);
  $("rawWrap").hidden = false;
  if (data && data.ok === false) {
    $("content").innerHTML = `<div class="notice">${esc(data.error)}</div>`;
    return;
  }
  try {
    $("content").innerHTML = (RENDER[activeId] || (() => ""))(data) || "";
  } catch (e) {
    $("content").innerHTML = `<div class="notice err">Render error: ${esc(e)}</div>`;
  }
  if (activeId === "redflags") applyRedFlags(data);
}

// Keep the always-visible banner and the nav-button badge in sync with the
// latest red-flag count, so a returned or missed debt payment is loud from any
// tab, not just the Red flags view.
function applyRedFlags(d) {
  const red = (d && d.summary && d.summary.red) || 0;
  const btn = [...$("tabs").children].find(b => b.dataset.id === "redflags");
  if (btn) {
    let badge = btn.querySelector(".badge");
    if (red > 0) {
      if (!badge) { badge = document.createElement("span"); badge.className = "badge"; btn.appendChild(badge); }
      badge.textContent = red;
    } else if (badge) { badge.remove(); }
  }
  const banner = $("redflagBanner");
  if (red > 0) {
    banner.innerHTML = `<span>&#9888; ${red} debt-payment red flag${red>1?'s':''}: a loan payment was returned or missed.</span><button>Review</button>`;
    banner.hidden = false;
    banner.onclick = () => { const t = TABS.find(t => t.id === "redflags"); if (t) selectTab(t); };
  } else {
    banner.hidden = true;
    banner.onclick = null;
  }
}

function selectTab(t) {
  current = t;
  try { localStorage.setItem("fmcp.lastTab", t.id); } catch (e) {}
  for (const b of $("tabs").children) b.classList.toggle("active", b.dataset.id === t.id);
  buildFilters();
  load();
}
function init() {
  const nav = $("tabs");
  for (const t of TABS) {
    const b = document.createElement("button");
    b.textContent = t.label; b.dataset.id = t.id;
    b.onclick = () => selectTab(t);
    nav.appendChild(b);
  }
  // Restore the last tab the user was on (falls back to the first tab).
  let startTab = TABS[0];
  try {
    const id = localStorage.getItem("fmcp.lastTab");
    const found = TABS.find(t => t.id === id);
    if (found) startTab = found;
  } catch (e) {}
  selectTab(startTab);
  // Loud-from-anywhere red-flag banner + nav badge, refreshed on load.
  fetch("/api/redflags").then(r => r.json()).then(d => { if (d && d.ok !== false) applyRedFlags(d); }).catch(() => {});
}
init();
</script>
</body>
</html>
"""
