"""End-to-end tests for the read-only web UI surface.

The web UI is a thin surface over the same ``server.py`` functions the MCP layer
exposes, so these tests assert the routing/coercion contract: ``handle_api``
forwards whitelisted params, coerces them, returns structured 400/404 errors on
bad input, and never raises. One live-socket test exercises the HTTP handler
(index route, an API route, security headers, and a 404).
"""

import json
import urllib.request
import http.client
from http.server import ThreadingHTTPServer
from threading import Thread

from finance_mcp import archive, config, webui


def _txn(tid, account, amount, *, on, desc="", is_transfer=False):
    return {
        "id": tid,
        "account_id": account,
        "account_name": account,
        "amount": amount,
        "amount_float": float(amount),
        "posted": f"{on}T00:00:00+00:00",
        "description": desc,
        "payee": "",
        "is_transfer": is_transfer,
    }


def _seed_basic(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("t1", "Checking", "-50.00", on="2026-05-02", desc="Coffee shop"),
            _txn("t2", "Checking", "1000.00", on="2026-05-01", desc="Paycheck"),
        ]})
    finally:
        conn.close()


def _write_budget(monkeypatch, tmp_path, data):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    config.budget_config_path().write_text(json.dumps(data), encoding="utf-8")


# --- dispatch: success paths --------------------------------------------------

def test_accounts_endpoint_returns_payload(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("accounts", {})
    assert status == 200
    assert "accounts" in body and "account_count" in body


def test_transactions_endpoint_forwards_and_coerces_params(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    # limit coerced to int, include_transfers coerced to bool, search forwarded.
    status, body = webui.handle_api("transactions", {
        "limit": ["1"], "include_transfers": ["false"], "search": ["coffee"],
    })
    assert status == 200
    assert body["returned"] == 1
    assert body["transactions"][0]["id"] == "t1"


def test_summary_endpoint(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("summary", {"group_by": ["category"]})
    assert status == 200
    assert "groups" in body


def test_transfers_endpoint(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("transfers", {})
    assert status == 200
    assert "transfers" in body and "summary" in body


def test_burndown_endpoint_with_config(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Coffee", "accounts": ["Checking"],
                       "monthly_target": 100}],
    })
    status, body = webui.handle_api("burndown", {"month": ["2026-05"]})
    assert status == 200
    assert body.get("ok") is not False
    assert "envelopes" in body


# --- dispatch: error paths ----------------------------------------------------

def test_unknown_endpoint_is_404():
    status, body = webui.handle_api("does_not_exist", {})
    assert status == 404
    assert body["ok"] is False


def test_missing_required_param_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    status, body = webui.handle_api("burndown", {})
    assert status == 400
    assert "month" in body["error"]


def test_blank_required_param_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    status, body = webui.handle_api("burndown", {"month": [""]})
    assert status == 400
    assert "month" in body["error"]


def test_bad_int_coercion_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    status, body = webui.handle_api("allocation", {"day_tolerance": ["abc"]})
    assert status == 400
    assert "integer" in body["error"]


def test_bad_bool_coercion_is_400(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("transactions", {"include_transfers": ["maybe"]})
    assert status == 400
    assert "boolean" in body["error"]


def test_bad_float_coercion_is_400(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("transactions", {"min_amount": ["lots"]})
    assert status == 400
    assert "number" in body["error"]


def test_non_finite_float_is_400(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    for bad in ("nan", "inf", "-inf"):
        status, body = webui.handle_api("transactions", {"min_amount": [bad]})
        assert status == 400, bad
        assert "finite" in body["error"], bad


def test_missing_budget_config_returns_structured_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    status, body = webui.handle_api("allocation", {})
    # The underlying tool returns its own structured error (HTTP 200).
    assert status == 200
    assert body["ok"] is False
    assert "budget config" in body["error"]


def test_subscriptions_endpoint_without_config_shows_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("n1", "card", "-9.99", on="2026-01-05", desc="SPOTIFY"),
            _txn("n2", "card", "-9.99", on="2026-02-05", desc="SPOTIFY"),
            _txn("n3", "card", "-9.99", on="2026-03-05", desc="SPOTIFY"),
        ]})
    finally:
        conn.close()
    # No budget.json: subscriptions must still load and surface all detected
    # recurring merchants rather than returning a config-not-found error.
    status, body = webui.handle_api(
        "subscriptions", {"start": ["2026-01-01"], "end": ["2026-05-31"]}
    )
    assert status == 200
    assert body.get("ok") is not False
    assert body["summary"]["tracked"] == 0
    assert any("spotify" in c["merchant_key"] for c in body["candidate_new"])


def test_subscriptions_endpoint_surfaces_came_back(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("r1", "card", "-20.00", on="2026-04-10", desc="REPLIT"),
        ]})
    finally:
        conn.close()
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Replit", "envelope": "Card", "amount": 20.00,
             "cadence": "monthly", "day": 10, "match": "replit",
             "lifecycle": "canceled", "cancel_effective": "2026-03-01"},
        ],
    })
    status, body = webui.handle_api(
        "subscriptions", {"start": ["2026-01-01"], "end": ["2026-05-31"]}
    )
    assert status == 200
    assert body["summary"]["came_back"] == 1
    assert body["came_back"][0]["name"] == "Replit"
    assert body["tracked"][0]["lifecycle"] == "canceled"


def test_unknown_param_is_ignored(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    status, body = webui.handle_api("accounts", {"bogus": ["x"]})
    assert status == 200
    assert "accounts" in body


# --- live HTTP handler --------------------------------------------------------

def test_http_handler_routes_and_headers(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        with urllib.request.urlopen(f"{base}/") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert resp.headers["Cache-Control"] == "no-store"
            html = resp.read().decode("utf-8")
        assert "finance-mcp" in html

        with urllib.request.urlopen(f"{base}/api/accounts") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
        assert "accounts" in data

        try:
            urllib.request.urlopen(f"{base}/api/nope")
            raise AssertionError("expected HTTP 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            err = json.loads(exc.read().decode("utf-8"))
            assert err["ok"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --- host policy (DNS-rebinding defense) --------------------------------------

def test_host_policy_helpers():
    # Loopback bind: allow loopback names only.
    allowed = webui._host_policy("127.0.0.1")
    assert "127.0.0.1" in allowed and "localhost" in allowed
    # Wildcard bind: enforcement is NOT disabled — the allowlist is still just
    # loopback (a foreign Host is refused) until a host is named explicitly.
    allowed_wild = webui._host_policy("0.0.0.0")
    assert "evil.example.com" not in allowed_wild
    assert "127.0.0.1" in allowed_wild and "0.0.0.0" not in allowed_wild
    # A specific routable bind allows itself plus loopback.
    allowed_lan = webui._host_policy("192.168.1.5")
    assert "192.168.1.5" in allowed_lan and "127.0.0.1" in allowed_lan
    # Explicitly named extra hosts are allowed (case-insensitive, trimmed).
    allowed_named = webui._host_policy("0.0.0.0", (" Phone.local ",))
    assert "phone.local" in allowed_named
    # A stray empty/wildcard --allow-host value can never widen the allowlist,
    # in any spelling of the unspecified address (bracketed / long-form IPv6).
    allowed_junk = webui._host_policy(
        "0.0.0.0", ("", "  ", "0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0"))
    assert allowed_junk == webui._host_policy("0.0.0.0")
    assert "" not in allowed_junk and "0.0.0.0" not in allowed_junk
    assert "[::]" not in allowed_junk


def test_hostname_only_strips_port():
    assert webui._hostname_only("127.0.0.1:8765") == "127.0.0.1"
    assert webui._hostname_only("localhost") == "localhost"
    assert webui._hostname_only("[::1]:8765") == "[::1]"


def test_foreign_host_header_is_rejected(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    # Simulate a wildcard bind: enforcement must still refuse a foreign Host.
    httpd.allowed_hosts = webui._host_policy("0.0.0.0")
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # A loopback Host is allowed even when the API is requested.
        ok = http.client.HTTPConnection("127.0.0.1", port)
        ok.request("GET", "/api/accounts", headers={"Host": f"127.0.0.1:{port}"})
        assert ok.getresponse().status == 200
        ok.close()

        # An attacker-controlled Host (DNS rebinding) is refused before any data,
        # even though the server is wildcard-bound.
        bad = http.client.HTTPConnection("127.0.0.1", port)
        bad.request("GET", "/api/accounts", headers={"Host": "evil.example.com"})
        resp = bad.getresponse()
        assert resp.status == 403
        bad.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_named_host_is_allowed(tmp_path, monkeypatch):
    _seed_basic(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    httpd.allowed_hosts = webui._host_policy("0.0.0.0", ("data.lan",))
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/api/accounts", headers={"Host": "data.lan"})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --- mutating POST endpoint (subscriptions mark) ------------------------------

def _budget_with_replit(monkeypatch, tmp_path):
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Replit", "envelope": "Card", "amount": 20.00,
             "cadence": "monthly", "day": 10, "match": "replit"},
        ],
    })


def test_post_mark_canceling_persists(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    status, body = webui.handle_api_post(
        "subscriptions/mark",
        {"name": "replit", "lifecycle": "canceling",
         "cancel_effective": "2026-06-01"},
    )
    assert status == 200
    assert body["ok"] is True
    saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
    bill = saved["recurring"][0]
    assert bill["lifecycle"] == "canceling"
    assert bill["cancel_effective"] == "2026-06-01"


def test_post_mark_reactivate_clears_effective(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Replit", "envelope": "Card", "amount": 20.00,
             "cadence": "monthly", "day": 10, "match": "replit",
             "lifecycle": "canceled", "cancel_effective": "2026-03-01"},
        ],
    })
    status, body = webui.handle_api_post(
        "subscriptions/mark", {"name": "Replit", "lifecycle": "active"})
    assert status == 200 and body["ok"] is True
    saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
    bill = saved["recurring"][0]
    assert bill.get("lifecycle", "active") == "active"
    assert "cancel_effective" not in bill or bill["cancel_effective"] is None


def test_post_mark_unknown_endpoint_is_404():
    status, body = webui.handle_api_post("does/not/exist", {"name": "x"})
    assert status == 404 and body["ok"] is False


def test_post_mark_non_object_body_is_400():
    status, body = webui.handle_api_post("subscriptions/mark", ["not", "a", "dict"])
    assert status == 400 and body["ok"] is False
    assert "JSON object" in body["error"]


def test_post_mark_missing_required_is_400(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    status, body = webui.handle_api_post(
        "subscriptions/mark", {"name": "replit"})  # no lifecycle
    assert status == 400 and body["ok"] is False
    assert "lifecycle" in body["error"]


def test_post_mark_non_string_value_is_400(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    status, body = webui.handle_api_post(
        "subscriptions/mark",
        {"name": "replit", "lifecycle": 5, "cancel_effective": "2026-06-01"})
    assert status == 400 and body["ok"] is False


def test_post_mark_no_such_bill_is_400(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    status, body = webui.handle_api_post(
        "subscriptions/mark",
        {"name": "nope", "lifecycle": "canceling",
         "cancel_effective": "2026-06-01"})
    assert status == 400 and body["ok"] is False


def test_http_post_requires_csrf_header_and_persists(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps({
            "name": "replit", "lifecycle": "canceling",
            "cancel_effective": "2026-06-01"}).encode("utf-8")

        # Without the custom header (the cross-site case) the write is refused.
        no_hdr = http.client.HTTPConnection("127.0.0.1", port)
        no_hdr.request("POST", "/api/subscriptions/mark", body=payload,
                       headers={"Content-Type": "application/json"})
        assert no_hdr.getresponse().status == 403
        no_hdr.close()
        saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
        assert saved["recurring"][0].get("lifecycle", "active") == "active"

        # With the header (the same-origin UI case) the write goes through.
        ok = http.client.HTTPConnection("127.0.0.1", port)
        ok.request("POST", "/api/subscriptions/mark", body=payload,
                   headers={"Content-Type": "application/json",
                            "X-Requested-With": "finance-mcp"})
        resp = ok.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        ok.close()
        saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
        assert saved["recurring"][0]["lifecycle"] == "canceling"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_post_foreign_host_is_rejected(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    httpd.allowed_hosts = webui._host_policy("0.0.0.0")
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        bad = http.client.HTTPConnection("127.0.0.1", port)
        bad.request("POST", "/api/subscriptions/mark",
                    body=b'{"name":"replit","lifecycle":"canceling","cancel_effective":"2026-06-01"}',
                    headers={"Host": "evil.example.com",
                             "Content-Type": "application/json",
                             "X-Requested-With": "finance-mcp"})
        assert bad.getresponse().status == 403
        bad.close()
        saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
        assert saved["recurring"][0].get("lifecycle", "active") == "active"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_post_bad_json_is_400(tmp_path, monkeypatch):
    _budget_with_replit(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("POST", "/api/subscriptions/mark", body=b"{not json",
                     headers={"Content-Type": "application/json",
                              "X-Requested-With": "finance-mcp"})
        assert conn.getresponse().status == 400
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_post_short_body_is_rejected(tmp_path, monkeypatch):
    # A client that declares a Content-Length larger than the bytes it sends,
    # then closes, must get a clean 400 (short body) rather than a hung worker
    # or a persisted partial write.
    import socket

    _budget_with_replit(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = b'{"name":"replit"}'  # 17 bytes
        req = (
            f"POST /api/subscriptions/mark HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Requested-With: finance-mcp\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 100\r\n"  # lies: promises 100, sends 17
            f"Connection: close\r\n\r\n"
        ).encode("ascii") + body
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(req)
        s.shutdown(socket.SHUT_WR)  # close write side: peer sees EOF after 17 bytes
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        s.close()
        status_line = resp.split(b"\r\n", 1)[0]
        assert b"400" in status_line, status_line
        saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
        assert saved["recurring"][0].get("lifecycle", "active") == "active"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_post_slow_body_hits_total_deadline(tmp_path, monkeypatch):
    # A client that opens the body but then dribbles (or simply stops) must be
    # cut off by the *total* read deadline, not held for length*timeout seconds
    # by keeping the per-read inactivity timer alive. We shrink the deadline and
    # send a partial body without closing the write side, so the only way the
    # handler can respond is by enforcing the overall budget.
    import socket
    import time as _time

    _budget_with_replit(monkeypatch, tmp_path)
    monkeypatch.setattr(webui, "_BODY_DEADLINE", 0.4)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        head = (
            f"POST /api/subscriptions/mark HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Requested-With: finance-mcp\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 100\r\n"  # promises 100 bytes
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(head + b'{"name":')  # send only a few body bytes, then stall
        started = _time.monotonic()
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        elapsed = _time.monotonic() - started
        s.close()
        status_line = resp.split(b"\r\n", 1)[0]
        assert b"408" in status_line, status_line
        # The response must arrive on the order of the deadline, not the 30s
        # inactivity timeout — proving the total budget, not per-read, governs.
        assert elapsed < 5, elapsed
        saved = json.loads(config.budget_config_path().read_text(encoding="utf-8"))
        assert saved["recurring"][0].get("lifecycle", "active") == "active"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
