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
    # +2000 has no spending category, so it surfaces as unclassified inflow and is
    # NOT netted against spend; outflow = -45 -12.34 -89.10 = -146.44.
    assert result["total_inflow"] == 0.0
    assert result["total_unclassified_inflow"] == 2000.0
    assert result["total_outflow"] == -146.44
    assert result["net"] == -146.44
    groups = {g["group"]: g for g in result["groups"]}
    assert "Checking" in groups
    assert "Rewards Card" in groups


def test_spending_summary_by_month(normalized):
    result = queries.spending_summary(normalized["transactions"], group_by="month")
    assert all(len(g["group"]) == 7 or g["group"] == "unknown" for g in result["groups"])


def test_spending_summary_rejects_bad_group_by(normalized):
    import pytest

    with pytest.raises(ValueError):
        queries.spending_summary(normalized["transactions"], group_by="nonsense")


def test_spending_summary_by_envelope_buckets_unmapped(normalized):
    # The checking account is its own envelope ("Everyday"); the card belongs to
    # no envelope, so its spend must surface in the (unmapped) bucket rather than
    # vanish. Checking: +2000 uncategorized, -45 -12.34 out. Card: -89.10 out.
    result = queries.spending_summary(
        normalized["transactions"],
        group_by="envelope",
        envelope_index={"ACT-checking": "Everyday"},
    )
    assert result["group_by"] == "envelope"
    groups = {g["group"]: g for g in result["groups"]}
    assert set(groups) == {"Everyday", queries.UNMAPPED_ENVELOPE}
    # The +2000 has no spending category, so it is surfaced as unclassified inflow
    # and does not net against the envelope's spend.
    assert groups["Everyday"]["unclassified_inflow"] == 2000.0
    assert groups["Everyday"]["inflow"] == 0.0
    assert groups["Everyday"]["outflow"] == round(-45.0 - 12.34, 2)
    assert groups[queries.UNMAPPED_ENVELOPE]["outflow"] == -89.1
    # No money is dropped: bucket totals reconcile with the headline totals.
    assert result["total_outflow"] == round(-45.0 - 12.34 - 89.1, 2)


def test_spending_summary_envelope_requires_index(normalized):
    import pytest

    with pytest.raises(ValueError):
        queries.spending_summary(normalized["transactions"], group_by="envelope")


def test_filter_by_category(normalized):
    txns = [dict(t) for t in normalized["transactions"]]
    txns[0]["category"] = "Coffee"
    rows = queries.filter_transactions(txns, category="coffee")
    assert {t["id"] for t in rows} == {txns[0]["id"]}


def test_filter_exclude_transfers(normalized):
    txns = [dict(t) for t in normalized["transactions"]]
    txns[0]["is_transfer"] = True
    rows = queries.filter_transactions(txns, include_transfers=False)
    assert txns[0]["id"] not in {t["id"] for t in rows}


def test_spending_summary_by_category_excludes_transfers(normalized):
    txns = [dict(t) for t in normalized["transactions"]]
    for t in txns:
        t["category"] = "Transfer" if t["amount_float"] and t["amount_float"] < -40 else "Dining"
        t["is_transfer"] = t["category"] == "Transfer"
    result = queries.spending_summary(txns, group_by="category")
    assert result["exclude_transfers"] is True
    groups = {g["group"] for g in result["groups"]}
    assert "Transfer" not in groups

    included = queries.spending_summary(txns, group_by="category", exclude_transfers=False)
    assert "Transfer" in {g["group"] for g in included["groups"]}


def _txn(tid, account_id, amount, category, *, is_income=False, is_transfer=False):
    return {
        "id": tid,
        "account_id": account_id,
        "amount_float": amount,
        "category": category,
        "is_income": is_income,
        "is_transfer": is_transfer,
        "posted_ts": 1700000000,
    }


def test_filter_exclude_income():
    txns = [
        _txn("pay", "ACT-main", 2000.0, "Income", is_income=True),
        _txn("buy", "ACT-main", -50.0, "Groceries"),
    ]
    rows = queries.filter_transactions(txns, include_income=False)
    assert {t["id"] for t in rows} == {"buy"}
    # default keeps income
    assert {t["id"] for t in queries.filter_transactions(txns)} == {"pay", "buy"}


def test_spending_summary_excludes_income_by_default():
    txns = [
        _txn("pay", "ACT-main", 2000.0, "Income", is_income=True),
        _txn("buy", "ACT-main", -50.0, "Groceries"),
    ]
    result = queries.spending_summary(txns, group_by="account")
    assert result["exclude_income"] is True
    # Income is dropped: net spend is just the -50 outflow, never inflated by pay.
    assert result["total_inflow"] == 0.0
    assert result["total_outflow"] == -50.0
    assert result["net"] == -50.0
    # Opting income back in restores it as inflow.
    included = queries.spending_summary(txns, group_by="account", exclude_income=False)
    assert included["total_inflow"] == 2000.0


def test_spending_summary_refund_nets_unclassified_does_not():
    # A categorized return nets against its group; an uncategorized deposit is
    # surfaced separately and does NOT reduce spend.
    txns = [
        _txn("buy", "ACT-main", -100.0, "Groceries"),
        _txn("return", "ACT-main", 30.0, "Groceries"),
        _txn("mystery", "ACT-main", 500.0, "Uncategorized"),
    ]
    result = queries.spending_summary(txns, group_by="account")
    assert len(result["groups"]) == 1
    g = result["groups"][0]
    assert g["outflow"] == -100.0
    assert g["inflow"] == 30.0  # the return offsets spend
    assert g["unclassified_inflow"] == 500.0  # mystery deposit surfaced, not netted
    assert g["net"] == -70.0  # -100 + 30, mystery excluded
    assert result["total_unclassified_inflow"] == 500.0


def test_spending_summary_missing_category_is_unclassified_inflow():
    # A positive amount with no category at all is treated as unclassified.
    txns = [_txn("dep", "ACT-main", 75.0, None)]
    result = queries.spending_summary(txns, group_by="account")
    assert result["total_inflow"] == 0.0
    assert result["total_unclassified_inflow"] == 75.0
    assert result["net"] == 0.0



def test_bare_end_date_is_inclusive_through_end_of_day():
    # INV-QUERIES-001: archived rows are stamped at noon UTC. A bare-date
    # end_date must include that whole day (it previously parsed to midnight,
    # so a noon-stamped last-day transaction was silently dropped), while the
    # following day stays excluded.
    from datetime import datetime, timezone

    noon = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
    next_day = int(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    txns = [
        {**_txn("lastday", "ACT-main", -10.0, "Groceries"), "posted_ts": noon},
        {**_txn("nextday", "ACT-main", -20.0, "Groceries"), "posted_ts": next_day},
    ]
    rows = queries.filter_transactions(txns, end_date="2026-06-30")
    ids = {t["id"] for t in rows}
    assert "lastday" in ids  # noon on the boundary day is included
    assert "nextday" not in ids  # the next day is still excluded
