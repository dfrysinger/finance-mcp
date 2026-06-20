import json
from pathlib import Path

from finance_mcp import sync

FIXTURE = Path(__file__).parent / "fixtures" / "sample_accounts.json"


def test_date_windows_under_90_days_single_window():
    windows = sync._date_windows(0, 10 * 86400)
    assert windows == [(0, 10 * 86400)]


def test_date_windows_splits_long_ranges():
    end = 200 * 86400
    windows = sync._date_windows(0, end)
    assert len(windows) >= 3
    # Contiguous and covering the full range.
    assert windows[0][0] == 0
    assert windows[-1][1] == end
    for a, b in zip(windows, windows[1:]):
        assert a[1] == b[0]
        assert (a[1] - a[0]) <= 90 * 86400


def test_sync_raises_without_access_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("SIMPLEFIN_ACCESS_URL", raising=False)
    try:
        sync.sync(days=30)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "access URL" in str(exc)


def test_sync_fetches_normalizes_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "https://u:p@bridge.example/simplefin")
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    calls = []

    def fake_fetch(url, *, start_date, end_date, pending, **kw):
        calls.append((start_date, end_date))
        return raw

    monkeypatch.setattr(sync.client, "fetch_accounts", fake_fetch)

    summary = sync.sync(days=30)
    assert summary["account_count"] == 2
    assert summary["transaction_count"] == 5
    assert len(calls) == 1  # 30 days -> one window

    # Re-sync must not duplicate transactions (merge by id).
    summary2 = sync.sync(days=30)
    assert summary2["transaction_count"] == 5
