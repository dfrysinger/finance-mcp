"""End-to-end tests for the Piece 5 MCP tool surfaces.

The tools are thin wrappers over the same pure modules the CLI uses, so these
tests assert the tool contract: each reads the durable archive + budget config
under ``FINANCE_MCP_HOME`` and returns a JSON-serializable dict (or a structured
``{"ok": False, "error": ...}`` on bad input) rather than raising.
"""

import json

from finance_mcp import archive, categories, config, server


# --- helpers ------------------------------------------------------------------

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


def _write_budget(monkeypatch, tmp_path, data):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    config.budget_config_path().write_text(json.dumps(data), encoding="utf-8")


def _seed_transfer(monkeypatch, tmp_path):
    from finance_mcp import reconcile

    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", on="2026-05-01", desc="Transfer to Main"),
            _txn("c1", "Main", "100.00", on="2026-05-01", desc="Transfer from Groceries"),
        ]})
        categories.set_manual_category(conn, "d1", "Transfer", is_transfer=True)
        categories.set_manual_category(conn, "c1", "Transfer", is_transfer=True)
    finally:
        conn.close()
    reconcile.reconcile()
    conn = archive.connect()
    try:
        link = next(l for l in archive.load_transfer_links(conn)
                    if l["status"] == "inferred")
        return link["link_id"]
    finally:
        conn.close()


# --- transfer tools -----------------------------------------------------------

def test_reconcile_transfers_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    out = server.reconcile_transfers()
    assert "links" in out and "needs_confirm" in out


def test_list_transfers_tool_names_legs(tmp_path, monkeypatch):
    _seed_transfer(monkeypatch, tmp_path)
    out = server.list_transfers()
    assert out["total"] == 1
    row = out["transfers"][0]
    assert row["from_account"] == "Groceries"
    assert row["to_account"] == "Main"


def test_list_transfers_status_filter(tmp_path, monkeypatch):
    _seed_transfer(monkeypatch, tmp_path)
    assert server.list_transfers(status="inferred")["total"] == 1
    assert server.list_transfers(status="confirmed")["total"] == 0


def test_confirm_transfer_tool(tmp_path, monkeypatch):
    link_id = _seed_transfer(monkeypatch, tmp_path)
    out = server.confirm_transfer(link_id)
    assert out["ok"] is True
    assert out["link"]["status"] == "confirmed"


def test_confirm_transfer_tool_unknown_link_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    out = server.confirm_transfer(999)
    assert out["ok"] is False
    assert "999" in out["error"]


# --- budget report tools ------------------------------------------------------

def test_budget_burndown_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("g1", "g", "-50.00", on="2026-05-10", desc="STORE"),
        ]})
    finally:
        conn.close()
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Groceries", "accounts": ["g"], "monthly_target": 750.00}],
    })
    out = server.budget_burndown("2026-05")
    assert out["period"] == "2026-05"
    assert any(e["envelope"] == "Groceries" for e in out["envelopes"])


def test_budget_burndown_bad_month_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {"version": 1, "envelopes": []})
    out = server.budget_burndown("nope")
    assert out["ok"] is False


def test_budget_forecast_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Groceries", "accounts": ["g"]}],
    })
    out = server.budget_forecast(as_of="2026-05-01", through="2026-06-01")
    assert out["as_of"] == "2026-05-01"
    assert "summary" in out


def test_budget_forecast_inverted_window_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {"version": 1, "envelopes": []})
    out = server.budget_forecast(as_of="2026-06-01", through="2026-05-01")
    assert out["ok"] is False


def test_allocation_audit_report_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [
            {"name": "Paycheck", "accounts": ["hub"]},
            {"name": "Groceries", "accounts": ["g"]},
        ],
        "scheduled_transfers": [
            {"name": "Fund groceries", "from": "Paycheck", "to": "Groceries",
             "amount": 500.00, "cadence": "monthly", "day": 1},
        ],
    })
    out = server.allocation_audit_report(start="2026-05-01", end="2026-05-31")
    assert out["summary"]["missing"] == 1


def test_subscription_audit_report_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("n1", "card", "-15.99", on="2026-01-10", desc="NETFLIX"),
            _txn("n2", "card", "-15.99", on="2026-02-10", desc="NETFLIX"),
            _txn("n3", "card", "-15.99", on="2026-03-10", desc="NETFLIX"),
        ]})
    finally:
        conn.close()
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Netflix", "envelope": "Card", "amount": 15.99,
             "cadence": "monthly", "day": 10, "match": "NETFLIX"},
        ],
    })
    out = server.subscription_audit_report(start="2026-01-01", end="2026-05-31")
    assert out["summary"]["tracked"] == 1
    assert len(out["expected_missing"]) >= 1


def test_report_tool_missing_config_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    out = server.allocation_audit_report()
    assert out["ok"] is False


# --- bad-input error paths (must return structured errors, not raise) ----------

def test_budget_burndown_out_of_range_month_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {"version": 1, "envelopes": []})
    out = server.budget_burndown("2026-13")
    assert out["ok"] is False
    assert "1..12" in out["error"]


def test_allocation_audit_report_inverted_window_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Paycheck", "accounts": ["hub"]}],
        "scheduled_transfers": [],
    })
    out = server.allocation_audit_report(start="2026-06-01", end="2026-05-01")
    assert out["ok"] is False
    assert "before" in out["error"]


def test_subscription_audit_report_inverted_window_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {
        "version": 1, "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    out = server.subscription_audit_report(start="2026-06-01", end="2026-05-01")
    assert out["ok"] is False
    assert "before" in out["error"]


def test_subscription_audit_report_bad_min_occurrences_returns_error(tmp_path, monkeypatch):
    _write_budget(monkeypatch, tmp_path, {
        "version": 1, "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    out = server.subscription_audit_report(
        start="2026-01-01", end="2026-05-31", min_occurrences=1
    )
    assert out["ok"] is False
    assert "min_occurrences" in out["error"]
