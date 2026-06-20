from finance_mcp import queries


def test_filter_by_search_matches_description(normalized):
    rows = queries.filter_transactions(normalized["transactions"], search="coffee")
    assert len(rows) == 1
    assert rows[0]["id"] == "t1"


def test_filter_exclude_pending(normalized):
    rows = queries.filter_transactions(
        normalized["transactions"], include_pending=False
    )
    assert all(not t["pending"] for t in rows)
    assert "t3" not in {t["id"] for t in rows}


def test_filter_by_account(normalized):
    rows = queries.filter_transactions(
        normalized["transactions"], account_id="ACT-card"
    )
    assert {t["id"] for t in rows} == {"t4", "t5"}


def test_filter_amount_bounds_spending_only(normalized):
    rows = queries.filter_transactions(normalized["transactions"], max_amount=0)
    assert all(t["amount_float"] <= 0 for t in rows)
    assert "t2" not in {t["id"] for t in rows}  # the +2000 payroll excluded


def test_filter_by_date_range(normalized):
    # t4 posted earliest (1699800000 -> 2023-11-12). Bound it out.
    rows = queries.filter_transactions(
        normalized["transactions"], start_date="2023-11-14"
    )
    assert "t4" not in {t["id"] for t in rows}


def test_limit(normalized):
    rows = queries.filter_transactions(normalized["transactions"], limit=2)
    assert len(rows) == 2


def test_spending_summary_by_account(normalized):
    result = queries.spending_summary(normalized["transactions"], group_by="account")
    assert result["group_by"] == "account"
    # inflow = 2000 (payroll); outflow = -45 -12.34 -89.10 -0 = -146.44
    assert result["total_inflow"] == 2000.0
    assert result["total_outflow"] == -146.44
    assert result["net"] == round(2000.0 - 146.44, 2)
    groups = {g["group"]: g for g in result["groups"]}
    assert "Checking" in groups
    assert "Rewards Card" in groups


def test_spending_summary_by_month(normalized):
    result = queries.spending_summary(normalized["transactions"], group_by="month")
    assert all(len(g["group"]) == 7 or g["group"] == "unknown" for g in result["groups"])


def test_spending_summary_rejects_bad_group_by(normalized):
    import pytest

    with pytest.raises(ValueError):
        queries.spending_summary(normalized["transactions"], group_by="category")
