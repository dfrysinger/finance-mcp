"""End-to-end tests for the Piece 5 CLI surfaces.

Each test drives the real ``cli.main(argv)`` entry point against a temporary
archive + budget config under ``FINANCE_MCP_HOME``, asserting on both the exit
code and the rendered text / JSON. These exercise the wiring (arg parsing,
report loading, rendering, error handling) the unit tests do not.
"""

import json
from datetime import date

import pytest

from finance_mcp import archive, budget_config, categories, cli


# --- helpers ------------------------------------------------------------------

def _txn(tid, account, amount, *, on, desc="", payee="", is_transfer=False):
    return {
        "id": tid,
        "account_id": account,
        "account_name": account,
        "amount": amount,
        "amount_float": float(amount),
        "posted": f"{on}T00:00:00+00:00",
        "description": desc,
        "payee": payee,
        "is_transfer": is_transfer,
    }


def _write_budget(tmp_path, data):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seed_transfer(monkeypatch, tmp_path):
    """Seed one reconciled inferred link; return its link_id."""
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


# --- reconcile / transfers / confirm ------------------------------------------

def test_reconcile_command_reports_one_link(tmp_path, monkeypatch, capsys):
    _seed_transfer(monkeypatch, tmp_path)
    rc = cli.main(["reconcile", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["links"] == 1


def test_transfers_command_renders_from_to_why(tmp_path, monkeypatch, capsys):
    _seed_transfer(monkeypatch, tmp_path)
    rc = cli.main(["transfers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Groceries -> Main" in out
    assert "$100.00" in out


def test_transfers_status_filter(tmp_path, monkeypatch, capsys):
    _seed_transfer(monkeypatch, tmp_path)
    rc = cli.main(["transfers", "--status", "confirmed", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 0


def test_confirm_command_promotes_link(tmp_path, monkeypatch, capsys):
    link_id = _seed_transfer(monkeypatch, tmp_path)
    rc = cli.main(["confirm", str(link_id), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "confirmed"


def test_confirm_unknown_link_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    rc = cli.main(["confirm", "999"])
    assert rc == 1
    assert "No such link" in capsys.readouterr().err


# --- allocation ---------------------------------------------------------------

def test_allocation_command_flags_missing_transfer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()  # empty archive -> the scheduled transfer never fired
    cfg = _write_budget(tmp_path, {
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
    rc = cli.main(["allocation", "--start", "2026-05-01", "--end", "2026-05-31",
                   "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fund groceries" in out
    assert "MISSING" in out


def test_allocation_rejects_bad_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    cfg = _write_budget(tmp_path, {"version": 1, "envelopes": [], "scheduled_transfers": []})
    rc = cli.main(["allocation", "--start", "2026-05-31", "--end", "2026-05-01",
                   "--config", str(cfg)])
    assert rc == 1
    assert "Invalid window" in capsys.readouterr().err


def test_allocation_missing_config_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    rc = cli.main(["allocation", "--config", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "Budget config error" in capsys.readouterr().err


# --- subscriptions ------------------------------------------------------------

def test_subscriptions_command_flags_missing_bill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        # Netflix posted Jan-Mar, then stopped: April + May are missing.
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("n1", "card", "-15.99", on="2026-01-10", desc="NETFLIX"),
            _txn("n2", "card", "-15.99", on="2026-02-10", desc="NETFLIX"),
            _txn("n3", "card", "-15.99", on="2026-03-10", desc="NETFLIX"),
        ]})
    finally:
        conn.close()
    cfg = _write_budget(tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Netflix", "envelope": "Card", "amount": 15.99,
             "cadence": "monthly", "day": 10, "match": "NETFLIX"},
        ],
    })
    rc = cli.main(["subscriptions", "--start", "2026-01-01", "--end", "2026-05-31",
                   "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Netflix" in out
    assert "MISSING" in out


def test_subscriptions_json_surfaces_candidate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        # An untracked merchant charged 3x monthly -> a candidate.
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("s1", "card", "-9.99", on="2026-01-05", desc="SPOTIFY USA"),
            _txn("s2", "card", "-9.99", on="2026-02-05", desc="SPOTIFY USA"),
            _txn("s3", "card", "-9.99", on="2026-03-05", desc="SPOTIFY USA"),
        ]})
    finally:
        conn.close()
    cfg = _write_budget(tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    rc = cli.main(["subscriptions", "--start", "2026-01-01", "--end", "2026-05-31",
                   "--config", str(cfg), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["candidates"] >= 1


def test_subscriptions_bad_min_occurrences_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    cfg = _write_budget(tmp_path, {
        "version": 1, "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    rc = cli.main(["subscriptions", "--min-occurrences", "1", "--config", str(cfg)])
    assert rc == 1
    assert "Subscription audit error" in capsys.readouterr().err


def test_subscriptions_mark_canceled_persists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    cfg = _write_budget(tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Sketch", "envelope": "Card", "amount": 5.00,
                       "cadence": "monthly", "day": 10, "match": "sketch"}],
    })
    rc = cli.main(["subscriptions", "mark", "--name", "Sketch",
                   "--lifecycle", "canceled", "--effective", "2026-04-01",
                   "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sketch" in out and "canceled" in out
    reloaded = budget_config.load_config(cfg)
    assert reloaded.recurring[0].lifecycle == "canceled"
    assert reloaded.recurring[0].cancel_effective == date(2026, 4, 1)


def test_subscriptions_mark_requires_name_and_lifecycle(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    cfg = _write_budget(tmp_path, {
        "version": 1, "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    rc = cli.main(["subscriptions", "mark", "--name", "Sketch", "--config", str(cfg)])
    assert rc == 1
    assert "requires --name and --lifecycle" in capsys.readouterr().err


def test_subscriptions_mark_unknown_name_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    archive.connect().close()
    cfg = _write_budget(tmp_path, {
        "version": 1, "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [],
    })
    rc = cli.main(["subscriptions", "mark", "--name", "Ghost",
                   "--lifecycle", "canceled", "--effective", "2026-04-01",
                   "--config", str(cfg)])
    assert rc == 1
    assert "Subscription mark error" in capsys.readouterr().err


def test_subscriptions_audit_warns_when_canceled_bill_comes_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("r1", "card", "-20.00", on="2026-04-10", desc="REPLIT"),
        ]})
    finally:
        conn.close()
    cfg = _write_budget(tmp_path, {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Replit", "envelope": "Card", "amount": 20.00,
                       "cadence": "monthly", "day": 10, "match": "replit",
                       "lifecycle": "canceled", "cancel_effective": "2026-03-01"}],
    })
    rc = cli.main(["subscriptions", "--start", "2026-01-01", "--end", "2026-05-31",
                   "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CAME BACK" in out and "Replit" in out


def test_subscriptions_detect_honors_day_tolerance(tmp_path, monkeypatch, capsys):
    # An envelope-only bill due on day 1 should suppress same-merchant charges
    # that post on day 10 only when --day-tolerance is wide enough to cover the
    # 9-day drift. This pins the CLI wiring: detect must forward --day-tolerance,
    # or suppression silently uses the default 7 and re-proposes a tracked bill.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("n1", "card", "-15.99", on="2026-01-10", desc="NETFLIX"),
            _txn("n2", "card", "-15.99", on="2026-02-10", desc="NETFLIX"),
            _txn("n3", "card", "-15.99", on="2026-03-10", desc="NETFLIX"),
            _txn("n4", "card", "-15.99", on="2026-04-10", desc="NETFLIX"),
        ]})
    finally:
        conn.close()
    budget = {
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Streaming", "envelope": "Card", "amount": 15.99,
                       "cadence": "monthly", "day": 1}],
    }

    # Default tolerance (7d): the day-10 charges sit outside the day-1 bill's
    # window, so they are NOT suppressed and detect proposes a duplicate.
    narrow_cfg = tmp_path / "narrow.json"
    narrow_cfg.write_text(json.dumps(budget), encoding="utf-8")
    rc = cli.main(["subscriptions", "detect", "--start", "2026-01-01",
                   "--end", "2026-05-31", "--config", str(narrow_cfg), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1

    # Wide tolerance (10d): the 9-day drift is now within the bill's window, so
    # the charges are suppressed and detect proposes nothing.
    wide_cfg = tmp_path / "wide.json"
    wide_cfg.write_text(json.dumps(budget), encoding="utf-8")
    rc = cli.main(["subscriptions", "detect", "--start", "2026-01-01",
                   "--end", "2026-05-31", "--day-tolerance", "10",
                   "--config", str(wide_cfg), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["added"] == 0
