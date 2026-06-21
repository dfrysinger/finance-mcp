"""Tests for the subscription audit: the pure auditor and an end-to-end report."""

import json
from datetime import date

import pytest

from finance_mcp import archive, budget_config, categories, subscription

# --- fixtures / helpers -------------------------------------------------------

WIN_START = date(2026, 1, 1)
WIN_END = date(2026, 5, 31)

CARD = {"name": "Card", "accounts": ["card"]}
SAVINGS = {"name": "Savings", "accounts": ["sav"]}


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


def _bill(name, envelope, amount, day, *, match=None):
    raw = {
        "name": name,
        "envelope": envelope,
        "amount": amount,
        "cadence": "monthly",
        "day": day,
    }
    if match is not None:
        raw["match"] = match
    return raw


def _config(envelopes, recurring):
    return budget_config.parse_config(
        {"version": 1, "envelopes": list(envelopes), "recurring": list(recurring)}
    )


def _names(items, key):
    return [item[key] for item in items]


# --- expected-but-missing -----------------------------------------------------


def test_tracked_charge_posted_is_not_missing():
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [_txn("t1", "card", "-15.99", on="2026-03-10", desc="NETFLIX.COM")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    # March occurrence is satisfied; only Jan/Feb/Apr/May should be missing,
    # and March must NOT appear.
    missing_dates = _names(report["expected_missing"], "expected_date")
    assert "2026-03-10" not in missing_dates
    assert "2026-04-10" in missing_dates
    assert report["expected_missing"][0]["name"] == "Netflix"


def test_tracked_charge_absent_is_missing_when_overdue():
    cfg = _config([CARD], [_bill("Spotify", "Card", 11.99, 5, match="SPOTIFY")])
    report = subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END)
    missing = report["expected_missing"]
    # Jan..May = five occurrences, all overdue relative to end=May 31.
    assert len(missing) == 5
    assert all(m["name"] == "Spotify" for m in missing)
    assert missing[0]["expected_amount"] == "11.99"
    assert missing[0]["last_seen"] is None


def test_recent_occurrence_within_grace_is_not_flagged():
    # day=28, end=May 31 -> the May occurrence is only 3 days old; it may still
    # post, so it must not be reported missing even with no matching charge.
    cfg = _config([CARD], [_bill("Late biller", "Card", 9.00, 28, match="LATE")])
    report = subscription.subscription_audit(cfg, [], start=date(2026, 5, 1), end=date(2026, 5, 31))
    assert report["expected_missing"] == []


def test_overdue_exactly_at_grace_is_flagged():
    # day=24, end=May 31 -> exactly 7 days old. At exactly grace the occurrence's
    # match window [occ-tol, occ+tol] ends at `end`, so no future charge can
    # still arrive to match it: it is genuinely missing and must be flagged.
    cfg = _config([CARD], [_bill("Biller", "Card", 9.00, 24, match="BILL")])
    report = subscription.subscription_audit(cfg, [], start=date(2026, 5, 1), end=date(2026, 5, 31))
    assert _names(report["expected_missing"], "expected_date") == ["2026-05-24"]


def test_overdue_just_past_grace_is_flagged():
    # day=20, end=May 31 -> 11 days old, beyond the default 7-day grace.
    cfg = _config([CARD], [_bill("Biller", "Card", 9.00, 20, match="BILL")])
    report = subscription.subscription_audit(cfg, [], start=date(2026, 5, 1), end=date(2026, 5, 31))
    assert _names(report["expected_missing"], "expected_date") == ["2026-05-20"]


def test_last_seen_reports_most_recent_match():
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [
        _txn("t1", "card", "-15.99", on="2026-01-10", desc="NETFLIX"),
        _txn("t2", "card", "-15.99", on="2026-02-10", desc="NETFLIX"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    # March onward missing; each missing occurrence records the latest prior hit.
    march = next(m for m in report["expected_missing"] if m["expected_date"] == "2026-03-10")
    assert march["last_seen"] == "2026-02-10"


# --- matching modes (keyword vs envelope) ------------------------------------


def test_early_charge_just_before_window_satisfies_start_occurrence():
    # An occurrence on the window's first day can be satisfied by a charge that
    # posted a few days BEFORE the window opened (within day_tolerance). Such a
    # paid bill must not be flagged missing at the leading edge.
    cfg = _config([CARD], [_bill("Svc", "Card", 10.00, 1, match="SVC")])
    txns = [_txn("t1", "card", "-10.00", on="2025-12-30", desc="SVC CO")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 1, 1), end=date(2026, 1, 31), day_tolerance=7
    )
    assert report["expected_missing"] == []
    # The pre-window charge is match-only; it must not surface as a candidate.
    assert report["candidate_new"] == []


def test_unrelated_merchant_does_not_satisfy_bill():
    # A same-amount, same-date "BATTERY WORLD" debit must NOT satisfy a bill
    # keyed on "ATT" — a false match there would hide a genuinely-missing bill.
    cfg = _config([CARD], [_bill("AT&T", "Card", 12.00, 10, match="ATT")])
    txns = [_txn("t1", "card", "-12.00", on="2026-03-10", desc="BATTERY WORLD")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert _names(report["expected_missing"], "expected_date") == ["2026-03-10"]


def test_no_keyword_matches_by_envelope_account():
    cfg = _config([CARD], [_bill("Gym", "Card", 40.00, 15)])  # no match keyword
    txns = [_txn("t1", "card", "-40.00", on="2026-03-15", desc="ANYTHING")]
    report = subscription.subscription_audit(cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31))
    assert report["expected_missing"] == []


def test_no_keyword_charge_on_other_envelope_is_missing():
    # The gym is billed to a card bound to a different envelope; with no keyword
    # the envelope binding can't see it, so it reports missing (the documented
    # reason a keyword is recommended).
    cfg = _config([CARD, SAVINGS], [_bill("Gym", "Card", 40.00, 15)])
    txns = [_txn("t1", "sav", "-40.00", on="2026-03-15", desc="GYM")]
    report = subscription.subscription_audit(cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31))
    assert _names(report["expected_missing"], "expected_date") == ["2026-03-15"]


def test_keyword_matches_regardless_of_card():
    # With a keyword the same charge on any envelope's account is matched.
    cfg = _config([CARD, SAVINGS], [_bill("Gym", "Card", 40.00, 15, match="GYM")])
    txns = [_txn("t1", "sav", "-40.00", on="2026-03-15", desc="CITY GYM")]
    report = subscription.subscription_audit(cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31))
    assert report["expected_missing"] == []


# --- tolerances ---------------------------------------------------------------


def test_amount_within_tolerance_matches():
    cfg = _config([CARD], [_bill("Svc", "Card", 10.00, 10, match="SVC")])
    txns = [_txn("t1", "card", "-10.05", on="2026-03-10", desc="SVC CO")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31), amount_tolerance_cents=5
    )
    assert report["expected_missing"] == []


def test_amount_beyond_tolerance_is_missing():
    cfg = _config([CARD], [_bill("Svc", "Card", 10.00, 10, match="SVC")])
    txns = [_txn("t1", "card", "-12.00", on="2026-03-10", desc="SVC CO")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31), amount_tolerance_cents=5
    )
    assert _names(report["expected_missing"], "expected_date") == ["2026-03-10"]


def test_day_beyond_tolerance_is_missing():
    cfg = _config([CARD], [_bill("Svc", "Card", 10.00, 10, match="SVC")])
    txns = [_txn("t1", "card", "-10.00", on="2026-03-20", desc="SVC CO")]  # 10 days off
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31), day_tolerance=7
    )
    assert _names(report["expected_missing"], "expected_date") == ["2026-03-10"]


def test_one_charge_cannot_satisfy_two_occurrences():
    cfg = _config([CARD], [_bill("Svc", "Card", 10.00, 10, match="SVC")])
    # A single charge near the boundary of two months' occurrences.
    txns = [_txn("t1", "card", "-10.00", on="2026-03-10", desc="SVC")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 4, 30), day_tolerance=7
    )
    # March satisfied, April missing (the charge was consumed by March).
    assert _names(report["expected_missing"], "expected_date") == ["2026-04-10"]


# --- candidate-new ------------------------------------------------------------


def test_charges_outside_window_are_ignored():
    # A monthly merchant whose three charges all predate the requested window
    # must NOT surface — candidate detection is scoped to [start, end], not to
    # whatever multi-year history the archive holds.
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2025-01-05", desc="HULU"),
        _txn("b", "card", "-9.99", on="2025-02-05", desc="HULU"),
        _txn("c", "card", "-9.99", on="2025-03-05", desc="HULU"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_match_keyword_searched_in_payee():
    # The keyword lives in payee while description is a generic POS string.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [
        {
            "id": "t1",
            "account_id": "card",
            "account_name": "card",
            "amount": "-15.99",
            "amount_float": -15.99,
            "posted": "2026-03-10T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "NETFLIX.COM",
            "memo": "",
            "is_transfer": False,
        }
    ]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert report["expected_missing"] == []


def test_brand_in_memo_of_unrelated_charge_does_not_satisfy_bill():
    # Netflix is genuinely missing this month. An unrelated cafe debit of the
    # same amount on the same day happens to mention "Netflix" in its catch-all
    # memo. The memo is NOT a merchant field, so it must not satisfy the bill —
    # the bill must still be reported missing (no hidden miss).
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [
        {
            "id": "t1",
            "account_id": "card",
            "account_name": "card",
            "amount": "-15.99",
            "amount_float": -15.99,
            "posted": "2026-03-10T00:00:00+00:00",
            "description": "CORNER CAFE",
            "payee": "CORNER CAFE",
            "memo": "gift card for netflix",
            "is_transfer": False,
        }
    ]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert [m["name"] for m in report["expected_missing"]] == ["Netflix"]


def test_payee_merchant_under_generic_description_surfaces_as_candidate():
    # Three monthly HULU debits whose description is a generic POS string but
    # whose payee names the merchant. Grouping/labeling uses the stable display
    # string, so the candidate still SURFACES (it is never hidden); it is grouped
    # under the description. The point is that it is surfaced for the assistant
    # to judge, not dropped.
    cfg = _config([CARD], [])
    txns = [
        {
            "id": f"t{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-7.99",
            "amount_float": -7.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "HULU",
            "memo": "",
            "is_transfer": False,
        }
        for i in (1, 2, 3)
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["candidate_new"]) == 1
    assert report["candidate_new"][0]["occurrences"] == 3


def test_numeric_description_payee_merchant_surfaces():
    # The description is an all-numeric reference number; the merchant is named
    # only in the payee. The candidate must still surface (grouped on the payee
    # identity) and be labeled from the payee, not the numeric reference.
    cfg = _config([CARD], [])
    txns = [
        {
            "id": f"t{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "000123456789",
            "payee": "NETFLIX.COM",
            "memo": "",
            "is_transfer": False,
        }
        for i in (1, 2, 3)
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["candidate_new"]) == 1
    cand = report["candidate_new"][0]
    assert cand["occurrences"] == 3
    assert cand["merchant"] == "NETFLIX.COM"


def test_two_payee_merchants_under_same_generic_description_both_surface():
    # Two distinct recurring merchants named only in the payee, both with the
    # same generic description and the same amount but on different days, must
    # NOT collapse into one irregular group that hides both — each surfaces.
    cfg = _config([CARD], [])
    txns = []
    for i in (1, 2, 3):
        txns.append({
            "id": f"h{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "HULU",
            "memo": "",
            "is_transfer": False,
        })
        txns.append({
            "id": f"d{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-20T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "DISNEY",
            "memo": "",
            "is_transfer": False,
        })
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    keys = sorted(c["merchant_key"] for c in report["candidate_new"])
    assert keys == ["disney pos purchase", "hulu pos purchase"]
    assert all(c["occurrences"] == 3 for c in report["candidate_new"])


def test_stray_charge_with_own_payee_does_not_contaminate_merchant():
    # A stray non-recurring charge that shares a real merchant's generic
    # description and amount but carries its OWN payee (real bank feeds populate
    # payee on every row) has an incomparable identity token set, so it never
    # folds into the merchant and never corrupts its cadence — the real monthly
    # candidate surfaces cleanly and the one-off stray is dropped sub-threshold.
    cfg = _config([CARD], [])
    txns = []
    for i in (1, 2, 3):
        txns.append({
            "id": f"d{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-20T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "DISNEY",
            "memo": "",
            "is_transfer": False,
        })
    txns.append({
        "id": "w1",
        "account_id": "card",
        "account_name": "card",
        "amount": "-9.99",
        "amount_float": -9.99,
        "posted": "2026-02-02T00:00:00+00:00",
        "description": "POS PURCHASE",
        "payee": "WALMART",
        "memo": "",
        "is_transfer": False,
    })
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    disney = [c for c in report["candidate_new"] if "disney" in c["merchant_key"]]
    assert len(disney) == 1
    assert disney[0]["occurrences"] == 3
    assert disney[0]["cadence"] == "monthly"


def test_bare_generic_stray_under_two_merchants_stays_standalone():
    # The round-8 regression: a bare-identity generic charge ({pos, purchase})
    # that is a subset of TWO distinct merchants ({pos,purchase,hulu} and
    # {pos,purchase,disney}) at the same amount is genuinely ambiguous. It must
    # NOT be folded into one of them arbitrarily (which would inject an
    # off-cadence date and hide or fake that merchant's cadence). Both merchants
    # must surface clean at their true occurrence count.
    cfg = _config([CARD], [])
    txns = []
    for i in (1, 2, 3):
        txns.append({
            "id": f"h{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "HULU",
            "memo": "",
            "is_transfer": False,
        })
        txns.append({
            "id": f"d{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-20T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "DISNEY",
            "memo": "",
            "is_transfer": False,
        })
    txns.append({
        "id": "g1",
        "account_id": "card",
        "account_name": "card",
        "amount": "-9.99",
        "amount_float": -9.99,
        "posted": "2026-02-15T00:00:00+00:00",
        "description": "POS PURCHASE",
        "payee": "",
        "memo": "",
        "is_transfer": False,
    })
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    surfaced = sorted(
        (c["merchant_key"], c["occurrences"]) for c in report["candidate_new"]
    )
    assert surfaced == [("disney pos purchase", 3), ("hulu pos purchase", 3)]


def test_already_recurring_candidate_not_demoted_by_offcadence_superset():
    # A merchant that already recurs on its own ({netflix} x3 monthly) must keep
    # surfacing even when a single off-cadence same-amount charge whose identity
    # is a strict superset ({netflix, promo}) exists: folding the remnant in would
    # corrupt the cadence to irregular and hide a merchant that was visible. The
    # two-phase grouping emits the recurring bucket first and never demotes it.
    cfg = _config([CARD], [])
    txns = [
        {
            "id": f"n{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-10T00:00:00+00:00",
            "description": "NETFLIX",
            "payee": "NETFLIX",
            "memo": "",
            "is_transfer": False,
        }
        for i in (1, 2, 3)
    ]
    txns.append({
        "id": "promo",
        "account_id": "card",
        "account_name": "card",
        "amount": "-9.99",
        "amount_float": -9.99,
        "posted": "2026-02-12T00:00:00+00:00",
        "description": "NETFLIX PROMO",
        "payee": "NETFLIX PROMO",
        "memo": "",
        "is_transfer": False,
    })
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    netflix = [c for c in report["candidate_new"] if "netflix" in c["merchant_key"]]
    assert len(netflix) == 1
    assert netflix[0]["merchant_key"] == "netflix"
    assert netflix[0]["occurrences"] == 3
    assert netflix[0]["cadence"] == "monthly"


def test_subthreshold_chain_merges_to_reach_recurring():
    # Neither half recurs alone, but they are the same merchant split by an
    # auxiliary token ({com, wix} x1 and {com, wix, www} x2). Merging the
    # sub-threshold buckets reunites them into a recurring monthly candidate —
    # the legitimate recovery the merge exists for.
    cfg = _config([CARD], [])
    txns = [
        {
            "id": "w1",
            "account_id": "card",
            "account_name": "card",
            "amount": "-25.78",
            "amount_float": -25.78,
            "posted": "2026-01-09T00:00:00+00:00",
            "description": "WIX.COM",
            "payee": "Wix.com",
            "memo": "",
            "is_transfer": False,
        },
        {
            "id": "w2",
            "account_id": "card",
            "account_name": "card",
            "amount": "-25.78",
            "amount_float": -25.78,
            "posted": "2026-02-09T00:00:00+00:00",
            "description": "WIX.COM WWW",
            "payee": "Wix.com",
            "memo": "",
            "is_transfer": False,
        },
        {
            "id": "w3",
            "account_id": "card",
            "account_name": "card",
            "amount": "-25.78",
            "amount_float": -25.78,
            "posted": "2026-03-09T00:00:00+00:00",
            "description": "WIX.COM WWW",
            "payee": "Wix.com",
            "memo": "",
            "is_transfer": False,
        },
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    wix = [c for c in report["candidate_new"] if "wix" in c["merchant_key"]]
    assert len(wix) == 1
    assert wix[0]["occurrences"] == 3
    assert wix[0]["cadence"] == "monthly"


def test_intermittent_payee_does_not_split_candidate():
    # The same recurring merchant at the same price, with payee populated on only
    # some rows, must group as ONE candidate — not split into sub-threshold
    # groups that silently drop below min_occurrences and vanish.
    cfg = _config([CARD], [])
    txns = []
    for i in (1, 2, 3, 4):
        txn = {
            "id": f"t{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-9.99",
            "amount_float": -9.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "NETFLIX",
            "payee": "NETFLIX.COM" if i % 2 == 0 else "",
            "memo": "",
            "is_transfer": False,
        }
        txns.append(txn)
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["candidate_new"]) == 1
    assert report["candidate_new"][0]["occurrences"] == 4


def test_blank_merchant_debits_are_not_grouped_by_account():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2026-01-05", desc=""),
        _txn("b", "card", "-9.99", on="2026-02-05", desc=""),
        _txn("c", "card", "-9.99", on="2026-03-05", desc=""),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_tracked_keyword_in_payee_excludes_candidate():
    # Netflix is tracked via keyword; its charges carry the merchant only in the
    # payee (description is a generic POS string) and the amount has drifted out
    # of match tolerance. It must NOT resurface as a new candidate.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [
        {
            "id": f"t{i}",
            "account_id": "card",
            "account_name": "card",
            "amount": "-17.99",
            "amount_float": -17.99,
            "posted": f"2026-0{i}-05T00:00:00+00:00",
            "description": "POS PURCHASE",
            "payee": "NETFLIX.COM",
            "memo": "",
            "is_transfer": False,
        }
        for i in (1, 2, 3)
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_short_tracked_keyword_does_not_suppress_unrelated_candidate():
    # A tracked bill keyed on "ATT" must not hide a real "BATTERY WORLD" sub.
    cfg = _config([CARD], [_bill("AT&T", "Card", 80.00, 1, match="ATT")])
    txns = [
        _txn("a", "card", "-12.00", on="2026-01-05", desc="BATTERY WORLD"),
        _txn("b", "card", "-12.00", on="2026-02-05", desc="BATTERY WORLD"),
        _txn("c", "card", "-12.00", on="2026-03-05", desc="BATTERY WORLD"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert [c["merchant"] for c in report["candidate_new"]] == ["BATTERY WORLD"]


def test_monthly_candidate_surfaced():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2026-01-05", desc="HULU"),
        _txn("b", "card", "-9.99", on="2026-02-05", desc="HULU"),
        _txn("c", "card", "-9.99", on="2026-03-05", desc="HULU"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["candidate_new"]) == 1
    cand = report["candidate_new"][0]
    assert cand["merchant"] == "HULU"
    assert cand["amount"] == "9.99"
    assert cand["occurrences"] == 3
    assert cand["cadence"] == "monthly"
    assert cand["first_seen"] == "2026-01-05"
    assert cand["last_seen"] == "2026-03-05"


def test_weekly_candidate_detected():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-3.00", on="2026-03-01", desc="COFFEE SUB"),
        _txn("b", "card", "-3.00", on="2026-03-08", desc="COFFEE SUB"),
        _txn("c", "card", "-3.00", on="2026-03-15", desc="COFFEE SUB"),
        _txn("d", "card", "-3.00", on="2026-03-22", desc="COFFEE SUB"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=date(2026, 3, 1), end=date(2026, 3, 31))
    assert report["candidate_new"][0]["cadence"] == "weekly"


def test_irregular_spacing_not_a_candidate():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2026-01-05", desc="STORE"),
        _txn("b", "card", "-9.99", on="2026-01-09", desc="STORE"),
        _txn("c", "card", "-9.99", on="2026-04-20", desc="STORE"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_below_min_occurrences_not_a_candidate():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2026-01-05", desc="RARE"),
        _txn("b", "card", "-9.99", on="2026-02-05", desc="RARE"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_tracked_keyword_excluded_from_candidates():
    # Netflix is tracked (different amount over the year); the price-change
    # charges must not resurface as a "new" subscription.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [
        _txn("a", "card", "-17.99", on="2026-01-10", desc="NETFLIX.COM"),
        _txn("b", "card", "-17.99", on="2026-02-10", desc="NETFLIX.COM"),
        _txn("c", "card", "-17.99", on="2026-03-10", desc="NETFLIX.COM"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_merchant_key_normalizes_varying_numeric_codes():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-12.00", on="2026-01-05", desc="SQ *COFFEE 1234"),
        _txn("b", "card", "-12.00", on="2026-02-05", desc="SQ *COFFEE 5678"),
        _txn("c", "card", "-12.00", on="2026-03-05", desc="SQ *COFFEE 9012"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["candidate_new"]) == 1
    assert report["candidate_new"][0]["occurrences"] == 3


def test_credits_and_transfers_ignored():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "9.99", on="2026-01-05", desc="REFUND"),  # credit
        _txn("b", "card", "9.99", on="2026-02-05", desc="REFUND"),
        _txn("c", "card", "9.99", on="2026-03-05", desc="REFUND"),
        _txn("d", "card", "-9.99", on="2026-01-05", desc="XFER", is_transfer=True),
        _txn("e", "card", "-9.99", on="2026-02-05", desc="XFER", is_transfer=True),
        _txn("f", "card", "-9.99", on="2026-03-05", desc="XFER", is_transfer=True),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["candidate_new"] == []


def test_different_amounts_split_into_separate_groups():
    cfg = _config([CARD], [])
    txns = [
        _txn("a", "card", "-9.99", on="2026-01-05", desc="X"),
        _txn("b", "card", "-9.99", on="2026-02-05", desc="X"),
        _txn("c", "card", "-19.99", on="2026-03-05", desc="X"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    # Neither group reaches min_occurrences=3 on its own.
    assert report["candidate_new"] == []


# --- validation + serialization ----------------------------------------------


def test_reversed_window_raises():
    cfg = _config([CARD], [])
    with pytest.raises(ValueError):
        subscription.subscription_audit(cfg, [], start=WIN_END, end=WIN_START)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"day_tolerance": -1},
        {"amount_tolerance_cents": -1},
        {"min_occurrences": 1},
        {"grace_days": -1},
    ],
)
def test_invalid_params_raise(kwargs):
    cfg = _config([CARD], [])
    with pytest.raises(ValueError):
        subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END, **kwargs)


def test_report_is_json_serializable():
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [_txn("t1", "card", "-15.99", on="2026-03-10", desc="NETFLIX")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    round_tripped = json.loads(json.dumps(report))
    assert round_tripped["summary"]["tracked"] == 1


# --- end-to-end over the archive ---------------------------------------------


def test_e2e_report_over_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(
            conn,
            {
                "accounts": [],
                "transactions": [
                    # Netflix posts every month -> tracked bill is satisfied.
                    _txn("n1", "card", "-15.99", on="2026-01-10", desc="NETFLIX.COM"),
                    _txn("n2", "card", "-15.99", on="2026-02-10", desc="NETFLIX.COM"),
                    _txn("n3", "card", "-15.99", on="2026-03-10", desc="NETFLIX.COM"),
                    # Hulu is untracked but recurring -> a new candidate.
                    _txn("h1", "card", "-9.99", on="2026-01-05", desc="HULU"),
                    _txn("h2", "card", "-9.99", on="2026-02-05", desc="HULU"),
                    _txn("h3", "card", "-9.99", on="2026-03-05", desc="HULU"),
                ],
            },
        )
    finally:
        conn.close()

    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_report(
        cfg, start=date(2026, 1, 1), end=date(2026, 3, 31)
    )
    # Netflix posted every month -> not missing; Hulu is an untracked candidate.
    assert report["expected_missing"] == []
    assert [c["merchant"] for c in report["candidate_new"]] == ["HULU"]


def test_e2e_report_no_archive_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_report(
        cfg, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    # No transactions at all -> the one March occurrence is overdue and missing.
    assert _names(report["expected_missing"], "expected_date") == ["2026-03-10"]
    assert report["candidate_new"] == []
