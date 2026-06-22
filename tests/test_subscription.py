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


def _bill(name, envelope, amount, day, *, match=None, lifecycle=None, cancel_effective=None):
    raw = {
        "name": name,
        "envelope": envelope,
        "amount": amount,
        "cadence": "monthly",
        "day": day,
    }
    if match is not None:
        raw["match"] = match
    if lifecycle is not None:
        raw["lifecycle"] = lifecycle
    if cancel_effective is not None:
        raw["cancel_effective"] = cancel_effective
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
        {"day_tolerance": 367},
        {"grace_days": 367},
    ],
)
def test_invalid_params_raise(kwargs):
    cfg = _config([CARD], [])
    with pytest.raises(ValueError):
        subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END, **kwargs)


@pytest.mark.parametrize("kwargs", [{"day_tolerance": 800_000}, {"grace_days": 800_000}])
def test_huge_tolerance_raises_valueerror_not_overflow(kwargs):
    # A large day-count must be rejected with the documented ValueError contract,
    # not crash with an opaque OverflowError from ``end - timedelta(days=...)``
    # underflowing date.min (and not amplify monthly_dates to tens of thousands
    # of occurrences per bill). These values are well past date.min underflow.
    cfg = _config([CARD], [])
    with pytest.raises(ValueError):
        subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END, **kwargs)


def test_audit_boundary_dates_do_not_overflow():
    # A pathological window at the representable date edges (reachable via the
    # CLI/MCP date parsers) must not raise OverflowError from the look-back
    # (``end - lookback``), match-window (``start - day_tolerance``), the
    # per-occurrence coverage gate (``occ - day_tolerance``), or the next_due
    # projection (``end + 62``). The date.min case carries a parseable
    # NON-matching transaction and a day-1 bill so the coverage gate's occurrence
    # arithmetic (``occ`` lands on date.min, ``occ - day_tolerance`` underflows)
    # is actually exercised. The occurrence's full in-tolerance payment window
    # reaches before any representable data, so coverage is unprovable and the
    # bill must read "unseen" — NOT a false "overdue" from clamping the window
    # start up to date.min. (An empty list or a matching txn would mask this.)
    import datetime

    cfg_min = _config([CARD], [_bill("Rent", "Card", 9.99, 1, match="RENT")])
    near_min = subscription.subscription_audit(
        cfg_min,
        [_txn("t1", "card", "-5.00", on="0001-01-01", desc="GROCERY")],
        start=datetime.date.min,
        end=datetime.date.min + datetime.timedelta(days=10),
    )
    assert near_min["summary"]["tracked"] == 1
    assert near_min["tracked"][0]["status"] == "unseen"

    # date.max with a day-31 bill: the next_due projection clamps end+1/end+62
    # back to date.max, but the strictly-after-end filter must yield no future
    # occurrence rather than surfacing the window-end date itself.
    cfg_max = _config([CARD], [_bill("Rent", "Card", 9.99, 31, match="RENT")])
    near_max = subscription.subscription_audit(
        cfg_max, [], start=datetime.date.max, end=datetime.date.max
    )
    assert near_max["summary"]["tracked"] == 1
    assert near_max["tracked"][0]["next_due"] is None


def test_detect_boundary_dates_do_not_overflow():
    # detect_subscriptions widens its charge window by ``start - day_tolerance``;
    # start at date.min must clamp rather than underflow.
    import datetime

    out = subscription.detect_subscriptions(
        [], start=datetime.date.min, end=datetime.date.min
    )
    assert out["bills"] == []


def test_report_is_json_serializable():
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [_txn("t1", "card", "-15.99", on="2026-03-10", desc="NETFLIX")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    round_tripped = json.loads(json.dumps(report))
    assert round_tripped["summary"]["tracked"] == 1


# --- tracked roster -----------------------------------------------------------


def test_tracked_lists_every_bill_once_with_due_fields():
    cfg = _config(
        [CARD],
        [
            _bill("Netflix", "Card", 15.99, 10, match="NETFLIX"),
            _bill("Spotify", "Card", 11.99, 5, match="SPOTIFY"),
        ],
    )
    txns = [_txn("t1", "card", "-15.99", on="2026-03-10", desc="NETFLIX")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    tracked = report["tracked"]
    assert _names(tracked, "name") == ["Netflix", "Spotify"]
    netflix = tracked[0]
    assert netflix["amount"] == "15.99"
    assert netflix["day"] == 10
    assert netflix["envelope"] == "Card"
    # next due is the first scheduled occurrence strictly after the window end.
    assert netflix["next_due"] == "2026-06-10"


def test_tracked_status_active_when_seen_overdue_when_missing():
    cfg = _config(
        [CARD],
        [
            _bill("Netflix", "Card", 15.99, 10, match="NETFLIX"),
            _bill("Spotify", "Card", 11.99, 5, match="SPOTIFY"),
        ],
    )
    # An early-January anchor charge establishes data coverage from the start of
    # the window, so Spotify's never-posted occurrences are genuinely overdue
    # rather than merely beyond the earliest available data.
    txns = [
        _txn("a0", "card", "-5.00", on="2026-01-02", desc="GROCERY"),
        _txn("t1", "card", "-15.99", on="2026-05-10", desc="NETFLIX"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    by_name = {t["name"]: t for t in report["tracked"]}
    assert by_name["Netflix"]["status"] == "active"
    assert by_name["Netflix"]["last_seen"] == "2026-05-10"
    assert by_name["Spotify"]["status"] == "overdue"
    assert by_name["Spotify"]["last_seen"] is None


def test_tracked_early_in_tolerance_payment_is_active_not_overdue():
    # A charge that clears a few days BEFORE the nominal due day still satisfies
    # the occurrence (within day_tolerance), so status must read "active", never
    # "overdue" — and must agree with expected_missing, which omits the occurrence.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    # Single charge on the 5th of a day-10 bill; the May occurrence is past grace
    # (it would be mislabeled overdue if status compared the post date, 05-05, to
    # the scheduled due date, 05-10).
    txns = [_txn("t1", "card", "-15.99", on="2026-05-05", desc="NETFLIX")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 5, 1), end=date(2026, 5, 31), day_tolerance=7
    )
    netflix = report["tracked"][0]
    assert netflix["status"] == "active"
    assert netflix["last_seen"] == "2026-05-05"
    # The May-10 occurrence was satisfied, so it must NOT be reported missing.
    assert report["expected_missing"] == []


def test_tracked_status_unseen_when_not_yet_overdue():
    # A bill due on the 28th, audited with end just two days later: its January
    # occurrence is inside the grace window, so it is neither missing nor seen ->
    # "unseen", not "overdue".
    cfg = _config([CARD], [_bill("Gym", "Card", 30.00, 28, match="GYM")])
    report = subscription.subscription_audit(
        cfg, [], start=date(2026, 1, 1), end=date(2026, 1, 30), day_tolerance=7
    )
    gym = report["tracked"][0]
    assert gym["status"] == "unseen"
    assert gym["last_seen"] is None
    assert report["expected_missing"] == []


def test_tracked_status_unseen_on_empty_archive_though_occurrence_missing():
    # Empty archive: status requires evidence, so an unpaid in-window occurrence
    # reads "unseen" (an early payment we never synced cannot be ruled out) even
    # though it is still listed in expected_missing as a factual per-occurrence
    # absence. status and expected_missing are deliberately distinct here.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_audit(
        cfg, [], start=date(2026, 5, 1), end=date(2026, 5, 31), day_tolerance=7
    )
    netflix = report["tracked"][0]
    assert netflix["status"] == "unseen"
    assert [m["expected_date"] for m in report["expected_missing"]] == ["2026-05-10"]
    # A narrow window that opens AFTER this month's due day yields no in-window
    # occurrence, yet the bill is genuinely overdue. The status look-back reaches
    # back to the most recent should-have-posted occurrence (here May 10) and,
    # finding no payment, reports "overdue" — even though expected_missing stays
    # scoped to the requested window and is therefore empty.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    # Unrelated charge in March establishes the earliest-data floor; no Netflix
    # charge ever posts, so the day-10 bill is unpaid.
    txns = [_txn("t1", "card", "-9.99", on="2026-03-15", desc="COFFEE SHOP")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 5, 15), end=date(2026, 5, 31), day_tolerance=7
    )
    netflix = report["tracked"][0]
    assert netflix["status"] == "overdue"
    assert netflix["last_seen"] is None
    # The overdue May-10 occurrence predates the window start, so it is NOT
    # reported as a missing occurrence — status and expected_missing stay distinct.
    assert report["expected_missing"] == []


def test_tracked_status_lookback_never_invents_overdue_before_data():
    # A day-10 bill audited in a narrow late-month window whose look-back reaches
    # the May-10 occurrence must NOT read "overdue" when data only begins May 12:
    # a May-10 charge could have posted as early as May 3 (within tolerance) on a
    # day we hold no data for, so payment can't be denied. The occurrence is still
    # generated (so a real charge could match), but it does not count toward
    # overdue because its payment window isn't covered -> status stays "unseen".
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [_txn("t1", "card", "-9.99", on="2026-05-12", desc="COFFEE SHOP")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 5, 15), end=date(2026, 5, 31), day_tolerance=7
    )
    netflix = report["tracked"][0]
    assert netflix["status"] == "unseen"
    assert report["expected_missing"] == []


def test_tracked_status_late_in_tolerance_payment_matches_via_lookback():
    # The look-back must GENERATE the most recent occurrence even when data begins
    # after the due day, so a genuine late-but-in-tolerance charge still matches.
    # Bill due the 10th, the matching charge posts the 12th (2 days late, within
    # tolerance 7), data begins the 12th, window opens the 15th -> "active", not a
    # false "unseen" from suppressing the occurrence.
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    txns = [_txn("t1", "card", "-15.99", on="2026-05-12", desc="NETFLIX")]
    report = subscription.subscription_audit(
        cfg, txns, start=date(2026, 5, 15), end=date(2026, 5, 31), day_tolerance=7
    )
    netflix = report["tracked"][0]
    assert netflix["status"] == "active"
    assert netflix["last_seen"] == "2026-05-12"


def test_tracked_status_overdue_requires_full_payment_window_coverage():
    # Coverage boundary: an unmatched past-grace occurrence is overdue only when
    # data covers its earliest in-tolerance payment date (occ - day_tolerance).
    # Day-10 bill, tolerance 7 -> earliest satisfying charge is May 3. With data
    # beginning exactly May 3 the window is fully observed -> "overdue"; beginning
    # one day later (May 4) leaves May 3 unobserved -> "unseen".
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    covered = subscription.subscription_audit(
        cfg,
        [_txn("a", "card", "-1.00", on="2026-05-03", desc="COFFEE")],
        start=date(2026, 5, 15), end=date(2026, 5, 31), day_tolerance=7,
    )
    assert covered["tracked"][0]["status"] == "overdue"
    uncovered = subscription.subscription_audit(
        cfg,
        [_txn("a", "card", "-1.00", on="2026-05-04", desc="COFFEE")],
        start=date(2026, 5, 15), end=date(2026, 5, 31), day_tolerance=7,
    )
    assert uncovered["tracked"][0]["status"] == "unseen"


def test_tracked_status_lookback_spans_large_grace():
    # The look-back must reach the most recent past-grace occurrence even when
    # grace_days far exceeds the default. A day-15 bill with a 40-day grace,
    # audited in a narrow late-June window with full data coverage from January
    # and no matching charge, is genuinely overdue on its April-15 occurrence
    # (66 days past a 40-day grace) — a fixed two-month look-back would drop that
    # occurrence and mislabel the bill "unseen".
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 15, match="NETFLIX")])
    txns = [_txn("a0", "card", "-5.00", on="2026-01-02", desc="GROCERY")]
    report = subscription.subscription_audit(
        cfg, txns,
        start=date(2026, 6, 15), end=date(2026, 6, 20),
        day_tolerance=7, grace_days=40,
    )
    assert report["tracked"][0]["status"] == "overdue"


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


def test_report_boundary_end_does_not_overflow(tmp_path, monkeypatch):
    # subscription_report computes its own default start (``end - window``) before
    # delegating to the audit; an ``end`` at date.min with no start must clamp
    # rather than raise OverflowError from the subtraction.
    import datetime

    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_report(cfg, start=None, end=datetime.date.min)
    assert report["summary"]["tracked"] == 1


# --- detect_subscriptions + merge_subscriptions_into_file --------------------

def _monthly(tid_prefix, account, amount, *, day, months, desc):
    """Build one debit per month on the given day-of-month."""
    out = []
    for i, m in enumerate(months):
        out.append(_txn(f"{tid_prefix}{i}", account, amount,
                        on=f"2026-{m:02d}-{day:02d}", desc=desc))
    return out


def test_detect_subscriptions_proposes_monthly_bill():
    txns = _monthly("nf", "card", "-15.99", day=10, months=[1, 2, 3, 4], desc="NETFLIX")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert len(out["bills"]) == 1
    bill = out["bills"][0]
    assert bill["cadence"] == "monthly"
    assert bill["amount"] == "15.99"
    assert bill["day"] == 10
    assert "netflix" in bill["match"]
    # A proposed bill, having a match keyword, parses without any envelope.
    cfg = _config([], [bill])
    assert cfg.recurring[0].envelope is None


def test_detect_does_not_repropose_merchant_covered_by_envelope_only_bill():
    # An existing envelope-only bill (no match keyword, user-chosen name) already
    # covers the NETFLIX charges via its envelope->account binding. Detect must
    # NOT propose a second keyword bill for the same merchant, which would leave
    # two bills competing for one charge and cry a false "missing" every cycle.
    cfg = _config([CARD], [_bill("Streaming bundle", "Card", 15.99, 10)])
    txns = _monthly("nf", "card", "-15.99", day=10, months=[1, 2, 3, 4],
                    desc="NETFLIX")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31), config=cfg
    )
    assert out["bills"] == []


def test_detect_does_not_repropose_merchant_tracked_by_keyword_bill():
    # Same merchant already tracked by a keyword bill on a different account is
    # suppressed too (token-set suppression), regardless of where it posts.
    cfg = _config([], [_bill("Netflix", None, 15.99, 10, match="netflix")])
    txns = _monthly("nf", "card", "-15.99", day=10, months=[1, 2, 3, 4],
                    desc="NETFLIX")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31), config=cfg
    )
    assert out["bills"] == []


def test_detect_skips_non_monthly_cadence():
    # Weekly charges recur but are not a monthly bill the schema can track.
    weekly = [
        _txn(f"w{i}", "card", "-9.00", on=d, desc="GYM")
        for i, d in enumerate(
            ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"]
        )
    ]
    out = subscription.detect_subscriptions(
        weekly, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert out["bills"] == []
    assert any(s["cadence"] == "weekly" for s in out["skipped"])


def test_merge_creates_file_when_absent(tmp_path):
    path = tmp_path / "budget.json"
    bills = [{"name": "Netflix", "match": "netflix", "amount": "15.99",
              "cadence": "monthly", "day": 10}]
    summary = subscription.merge_subscriptions_into_file(path, bills)
    assert summary["added"] == 1
    assert path.exists()
    cfg = budget_config.load_config(path)
    assert cfg.envelopes == ()
    assert cfg.recurring[0].name == "Netflix"


def test_merge_is_idempotent_on_match_keyword(tmp_path):
    path = tmp_path / "budget.json"
    bills = [{"name": "Netflix", "match": "netflix", "amount": "15.99",
              "cadence": "monthly", "day": 10}]
    subscription.merge_subscriptions_into_file(path, bills)
    # Re-running with the same merchant (different display name) adds nothing.
    again = subscription.merge_subscriptions_into_file(
        path, [{"name": "NETFLIX.COM", "match": "netflix", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    assert again["added"] == 0
    assert again["already_tracked"] == 1
    assert again["tracked_total"] == 1


def test_merge_dedup_is_subset_aware(tmp_path):
    # The audit matcher treats a keyword as the same merchant when its token set
    # is a subset OR superset of another's. Merge must dedup the same way, or a
    # later run with a slightly different keyword would double-track the merchant
    # and cry a false "missing-charge" alert every cycle.
    path = tmp_path / "budget.json"
    subscription.merge_subscriptions_into_file(
        path, [{"name": "Netflix", "match": "netflix com", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    # A subset of the tracked keyword is the same merchant -> not added.
    subset_run = subscription.merge_subscriptions_into_file(
        path, [{"name": "Netflix monthly", "match": "netflix", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    assert subset_run["added"] == 0
    assert subset_run["tracked_total"] == 1


def test_merge_dedup_is_superset_aware(tmp_path):
    # Reverse direction: a tracked minimal keyword suppresses a detected superset.
    path = tmp_path / "budget.json"
    subscription.merge_subscriptions_into_file(
        path, [{"name": "Netflix", "match": "netflix", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    superset_run = subscription.merge_subscriptions_into_file(
        path, [{"name": "NETFLIX.COM", "match": "netflix com", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    assert superset_run["added"] == 0
    assert superset_run["tracked_total"] == 1


def test_detect_match_key_is_group_intersection_not_widened_by_sibling():
    # A merchant that recurs as the fuller "NETFLIX COM" (3x) plus a single
    # sub-threshold "NETFLIX" charge: the saved keyword is the recurring group's
    # own shared tokens ("com netflix"), NOT widened by the one-off sibling.
    # Widening to "netflix" was tried and reverted: it collapses distinct
    # same-amount merchants that share a generic prefix and can pin a keyword to a
    # sibling on a different billing day. The bill matches all of its own grouped
    # charges; a genuine descriptor change resurfaces as a new audit candidate.
    txns = _monthly("nf", "card", "-15.99", day=10, months=[1, 2, 3],
                    desc="NETFLIX COM")
    txns += [_txn("nfx", "card", "-15.99", on="2026-04-10", desc="NETFLIX")]
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert len(out["bills"]) == 1
    assert set(out["bills"][0]["match"].split()) == {"com", "netflix"}


def test_detect_keyword_unaffected_by_disjoint_same_amount_oneoffs():
    # Coincidental same-amount one-offs that share no token with the merchant's
    # recurring form (a recurring "NETFLIX COM" plus disjoint one-offs "NETFLIX"
    # and "COM") must not corrupt the saved keyword. It stays the recurring
    # group's own shared tokens, and the merchant stays tracked.
    txns = _monthly("nf", "card", "-15.99", day=10, months=[1, 2, 3],
                    desc="NETFLIX COM")
    txns += [
        _txn("a", "card", "-15.99", on="2026-04-10", desc="NETFLIX"),
        _txn("b", "card", "-15.99", on="2026-04-20", desc="COM"),
    ]
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert len(out["bills"]) == 1
    assert set(out["bills"][0]["match"].split()) == {"com", "netflix"}


def test_detect_keeps_distinctive_token_when_generic_stray_shares_amount():
    # Two distinct same-amount merchants ("POS PURCHASE / HULU" and
    # "POS PURCHASE / DISNEY", both $9.99) plus a bare "POS PURCHASE" stray at the
    # same amount. Each detected keyword must keep its own distinctive token and
    # never collapse to the shared generic "pos purchase" — that keyword would
    # false-match any $9.99 POS charge in the audit, and would make the two bills
    # identical so amount-aware dedup silently drops the second. Both merchants
    # must stay tracked with distinctive keywords.
    txns = []
    for i in (1, 2, 3):
        h = _txn(f"h{i}", "card", "-9.99", on=f"2026-0{i}-05", desc="POS PURCHASE")
        h["payee"] = "HULU"
        d = _txn(f"d{i}", "card", "-9.99", on=f"2026-0{i}-20", desc="POS PURCHASE")
        d["payee"] = "DISNEY"
        txns += [h, d]
    stray = _txn("g1", "card", "-9.99", on="2026-02-15", desc="POS PURCHASE")
    txns.append(stray)
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    matches = sorted(b["match"] for b in out["bills"])
    # Both merchants tracked, each keyword carries its distinctive token.
    assert len(out["bills"]) == 2
    assert any("hulu" in m.split() for m in matches)
    assert any("disney" in m.split() for m in matches)
    # Neither bill is pinned to the bare generic prefix.
    assert all(set(m.split()) != {"pos", "purchase"} for m in matches)


def test_merge_keeps_same_merchant_at_different_amounts(tmp_path):
    # Two genuinely distinct subscriptions billed under one descriptor at
    # different prices (e.g. two Apple plans) must both be tracked. The audit
    # matches a charge by keyword AND amount, so same-keyword bills at different
    # amounts do not compete; deduping them on keyword alone would silently drop
    # the second and then hide it from future detection.
    path = tmp_path / "budget.json"
    subscription.merge_subscriptions_into_file(
        path, [{"name": "Apple", "match": "apple bill", "amount": "0.99",
                "cadence": "monthly", "day": 10}]
    )
    second = subscription.merge_subscriptions_into_file(
        path, [{"name": "Apple", "match": "apple bill", "amount": "9.99",
                "cadence": "monthly", "day": 10}]
    )
    assert second["added"] == 1
    assert second["tracked_total"] == 2
    # ...but re-running the same price stays idempotent (no duplicate).
    third = subscription.merge_subscriptions_into_file(
        path, [{"name": "Apple", "match": "apple bill", "amount": "9.99",
                "cadence": "monthly", "day": 10}]
    )
    assert third["added"] == 0
    assert third["tracked_total"] == 2


def test_merge_keeps_distinct_keywords_under_same_generic_name(tmp_path):
    # When the merchant lives in the payee under a generic description, detect
    # names both bills by that generic display ("POS PURCHASE") but gives them
    # distinct keywords. Same name + same amount must NOT dedup them away: the
    # disjoint keywords prove they are different merchants. The display name is
    # only a tie-break for a keyword-less (envelope-only) existing bill.
    path = tmp_path / "budget.json"
    subscription.merge_subscriptions_into_file(
        path, [{"name": "POS PURCHASE", "match": "disney pos purchase",
                "amount": "9.99", "cadence": "monthly", "day": 20}]
    )
    second = subscription.merge_subscriptions_into_file(
        path, [{"name": "POS PURCHASE", "match": "hulu pos purchase",
                "amount": "9.99", "cadence": "monthly", "day": 5}]
    )
    assert second["added"] == 1
    assert second["tracked_total"] == 2
    # An existing keyword-less (envelope-only) bill still dedups by name+amount.
    env_path = tmp_path / "env.json"
    env_path.write_text(json.dumps({
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Streaming", "envelope": "Card", "amount": 9.99,
                       "cadence": "monthly", "day": 5}],
    }), encoding="utf-8")
    env_run = subscription.merge_subscriptions_into_file(
        env_path, [{"name": "Streaming", "match": "netflix", "amount": "9.99",
                    "cadence": "monthly", "day": 5}]
    )
    assert env_run["added"] == 0
    assert env_run["tracked_total"] == 1


def test_detect_match_key_includes_payee_merchant():
    # Real-world shape: a generic description ("POS PURCHASE") with the merchant
    # in the payee. The saved match must include the distinctive merchant token,
    # not just the generic description tokens (which would match any same-amount
    # POS purchase and hide a genuinely-missing charge).
    txns = []
    for i, m in enumerate([1, 2, 3, 4]):
        t = _txn(f"nf{i}", "card", "-15.99", on=f"2026-{m:02d}-10",
                 desc="POS PURCHASE")
        t["payee"] = "NETFLIX"
        txns.append(t)
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert len(out["bills"]) == 1
    match_tokens = set(out["bills"][0]["match"].split())
    assert "netflix" in match_tokens
    # The saved bill is not the over-broad generic-only keyword.
    assert match_tokens != {"pos", "purchase"}


def test_detect_skips_merchant_with_no_common_token():
    # A cluster whose charges share no token common to *all* of them (full-text
    # "ALPHA BETA" one month, truncated "ALPHA" the next, "BETA" the next) cannot
    # be pinned by any single keyword that still matches every one of its charges.
    # Detect must skip auto-tracking it (with a reason) rather than fabricate a
    # keyword that would miss the merchant's own charges and cry "missing".
    txns = [
        _txn("ab0", "card", "-5.00", on="2026-01-15", desc="ALPHA BETA"),
        _txn("ab1", "card", "-5.00", on="2026-02-15", desc="ALPHA"),
        _txn("ab2", "card", "-5.00", on="2026-03-15", desc="BETA"),
    ]
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert out["bills"] == []
    assert any("vary" in s["reason"] or "varies" in s["reason"]
               for s in out["skipped"])


def test_merge_wraps_filesystem_errors_as_config_error(tmp_path):
    # A missing/unwritable parent directory must surface as BudgetConfigError so
    # the CLI/server report a structured error, not an unhandled traceback.
    path = tmp_path / "no-such-dir" / "budget.json"
    with pytest.raises(budget_config.BudgetConfigError):
        subscription.merge_subscriptions_into_file(
            path, [{"name": "Netflix", "match": "netflix", "amount": "15.99",
                    "cadence": "monthly", "day": 10}]
        )


def test_merge_write_is_atomic_no_temp_leftover(tmp_path):
    path = tmp_path / "budget.json"
    subscription.merge_subscriptions_into_file(
        path, [{"name": "Netflix", "match": "netflix", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    # The atomic temp file is renamed into place, never left behind. The sibling
    # ``.lock`` file (the cross-process write lock) is the only other expected
    # artifact and is meant to persist.
    leftovers = [
        p.name for p in tmp_path.iterdir()
        if p.name not in ("budget.json", "budget.json.lock")
    ]
    assert leftovers == []


def test_merge_preserves_existing_envelopes_and_bills(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Spotify", "envelope": "Card", "amount": 9.99,
                       "cadence": "monthly", "day": 3, "match": "spotify"}],
    }), encoding="utf-8")
    subscription.merge_subscriptions_into_file(
        path, [{"name": "Netflix", "match": "netflix", "amount": "15.99",
                "cadence": "monthly", "day": 10}]
    )
    cfg = budget_config.load_config(path)
    assert {e.name for e in cfg.envelopes} == {"Card"}
    assert {b.name for b in cfg.recurring} == {"Spotify", "Netflix"}


def test_merge_rejects_malformed_existing_file_without_writing(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(budget_config.BudgetConfigError):
        subscription.merge_subscriptions_into_file(
            path, [{"name": "X", "match": "x", "amount": "1.00",
                    "cadence": "monthly", "day": 1}]
        )
    # The bad file is left untouched, not half-rewritten.
    assert path.read_text(encoding="utf-8") == "not json"


def test_report_clamps_window_to_earliest_transaction(tmp_path, monkeypatch):
    # Archive data starts 2026-03-01, but the audit window opens 2026-01-01. The
    # Jan/Feb occurrences predate all data and must NOT be reported missing
    # (noise), while a genuine in-data gap still surfaces.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("n1", "card", "-15.99", on="2026-03-10", desc="NETFLIX"),
            # April is genuinely missing (data exists around it, charge does not).
            _txn("n2", "card", "-15.99", on="2026-05-10", desc="NETFLIX"),
            _txn("x1", "card", "-1.00", on="2026-04-15", desc="OTHER"),
        ]})
    finally:
        conn.close()
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_report(
        cfg, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    missing = _names(report["expected_missing"], "expected_date")
    # No pre-data Jan/Feb misses; the in-data April gap is still reported.
    assert all(d >= "2026-03" for d in missing)
    assert "2026-04-10" in missing
    assert report["window"]["start"] == "2026-03-10"


def test_detect_skips_subthreshold_merchant_that_only_recurs_via_generic_strays():
    # A single distinctive charge ("POS PURCHASE / HULU", $9.99) plus two bare
    # same-amount "POS PURCHASE" strays. The distinctive bucket is sub-threshold
    # on its own and would only reach the occurrence threshold by folding the
    # generic strays in -- but the resulting shared key collapses to the bare
    # "pos purchase", which would false-match any $9.99 POS charge. Detect must
    # NOT auto-pin a purely-generic keyword: it surfaces the cluster for review.
    hulu = _txn("h0", "card", "-9.99", on="2026-01-05", desc="POS PURCHASE")
    hulu["payee"] = "HULU"
    strays = [
        _txn("g0", "card", "-9.99", on="2026-02-05", desc="POS PURCHASE"),
        _txn("g1", "card", "-9.99", on="2026-03-05", desc="POS PURCHASE"),
    ]
    out = subscription.detect_subscriptions(
        [hulu, *strays], start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    # No bill is auto-pinned to the generic key.
    assert all(set(b["match"].split()) != {"pos", "purchase"} for b in out["bills"])
    assert out["bills"] == []
    assert any("generic" in s["reason"] for s in out["skipped"])


def test_detect_tracks_subthreshold_defrag_with_distinctive_token():
    # The same structural shape as the generic-stray case, but the folded subset
    # carries a distinctive token: a bare "NETFLIX" charge (sub-threshold) plus
    # two "NETFLIX COM" charges defragment into one Netflix subscription. The
    # shared key keeps the distinctive "netflix" token, so it pins reliably and
    # IS tracked (the generic guard only rejects an all-boilerplate key).
    bare = _txn("n0", "card", "-9.99", on="2026-01-10", desc="NETFLIX")
    com = [
        _txn("n1", "card", "-9.99", on="2026-02-10", desc="NETFLIX COM"),
        _txn("n2", "card", "-9.99", on="2026-03-10", desc="NETFLIX COM"),
    ]
    out = subscription.detect_subscriptions(
        [bare, *com], start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert len(out["bills"]) == 1
    assert "netflix" in out["bills"][0]["match"].split()


def test_detect_skips_card_network_boilerplate_key():
    # "VISA PURCHASE" recurs monthly but its only shared token is the card
    # network plus generic boilerplate -- pinning "purchase visa" would
    # false-match unrelated $12 card charges. Detect must skip it for review,
    # not auto-track, and tag it needs_review (it is monthly, not a cadence skip).
    txns = _monthly("v", "card", "-12.00", day=5, months=[1, 2, 3, 4],
                    desc="VISA PURCHASE")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert out["bills"] == []
    review = [s for s in out["skipped"] if s["kind"] == "needs_review"]
    assert len(review) == 1
    assert "generic" in review[0]["reason"]


def test_detect_tags_unsupported_cadence_kind():
    # Non-monthly (weekly) recurrence is a cadence the schema cannot track and is
    # tagged unsupported_cadence -- distinct from monthly-but-unpinnable skips.
    weekly = [
        _txn(f"w{i}", "card", "-9.00", on=d, desc="GYM")
        for i, d in enumerate(
            ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"]
        )
    ]
    out = subscription.detect_subscriptions(
        weekly, start=date(2026, 1, 1), end=date(2026, 5, 31)
    )
    assert out["skipped"]
    assert all(s["kind"] == "unsupported_cadence" for s in out["skipped"])


@pytest.mark.parametrize("day_tolerance", [-1, 800_000])
def test_detect_rejects_out_of_range_day_tolerance(day_tolerance):
    # detect_subscriptions widens its charge window by ``start - day_tolerance``;
    # a negative value is invalid and an enormous one would underflow date.min
    # with an opaque OverflowError. Both must surface as the documented ValueError.
    with pytest.raises(ValueError):
        subscription.detect_subscriptions(
            [], start=date(2026, 1, 1), end=date(2026, 5, 31),
            day_tolerance=day_tolerance,
        )


def test_detect_surfaces_recurring_charge_at_different_price_for_review():
    # An Apple sub is already tracked at $0.99. A *second* recurring run of Apple
    # charges posts monthly at $9.99 (a price change, or a distinct second plan).
    # It shares the tracked "apple" keyword, so it is suppressed from candidates
    # and must NOT be auto-written (that would create a second competing bill).
    # But silently dropping it would hide a real recurring charge -- so detect
    # surfaces it under needs_review with the different-price reason.
    cfg = _config([], [_bill("Apple iCloud", None, 0.99, 10, match="apple")])
    txns = _monthly("ap", "card", "-9.99", day=10, months=[1, 2, 3, 4],
                    desc="APPLE")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31), config=cfg
    )
    assert out["bills"] == []
    review = [s for s in out["skipped"] if s["kind"] == "needs_review"]
    assert len(review) == 1
    assert "different price" in review[0]["reason"]
    assert "9.99" in review[0]["reason"]


def test_detect_same_price_tracked_sub_not_flagged_for_review():
    # The idempotent case: the recurring Apple charges post at the SAME $0.99 the
    # tracked bill expects. They are consumed by the existing bill, so there is
    # no price mismatch and nothing is surfaced for review.
    cfg = _config([], [_bill("Apple iCloud", None, 0.99, 10, match="apple")])
    txns = _monthly("ap", "card", "-0.99", day=10, months=[1, 2, 3, 4],
                    desc="APPLE")
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31), config=cfg
    )
    assert out["bills"] == []
    assert [s for s in out["skipped"] if s["kind"] == "needs_review"] == []


def test_detect_surfaces_different_price_run_despite_descriptor_variation():
    # The different-price run posts under two descriptors ("APPLE COM BILL" /
    # "APPLE ICLOUD") that share the tracked "apple" keyword but differ in their
    # other tokens. Grouping must key on the matched tracked keyword, not the raw
    # token set -- otherwise the run fragments into sub-threshold buckets and the
    # needs_review signal is silently dropped.
    cfg = _config([], [_bill("Apple iCloud", None, 0.99, 10, match="apple")])
    txns = [
        _txn("a0", "card", "-9.99", on="2026-01-10", desc="APPLE COM BILL"),
        _txn("a1", "card", "-9.99", on="2026-02-10", desc="APPLE ICLOUD"),
        _txn("a2", "card", "-9.99", on="2026-03-10", desc="APPLE COM BILL"),
        _txn("a3", "card", "-9.99", on="2026-04-10", desc="APPLE ICLOUD"),
    ]
    out = subscription.detect_subscriptions(
        txns, start=date(2026, 1, 1), end=date(2026, 5, 31), config=cfg
    )
    assert out["bills"] == []
    review = [s for s in out["skipped"] if s["kind"] == "needs_review"]
    assert len(review) == 1
    assert "9.99" in review[0]["reason"]


# --- Cancellation watch: lifecycle audit behavior -----------------------------


def test_canceled_bill_absence_is_not_reported_missing():
    # A bill canceled effective March 1: its March/April/May occurrences are
    # expected to be absent and must NOT surface as missing.
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    report = subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END)
    missing_dates = _names(report["expected_missing"], "expected_date")
    # Jan/Feb (pre-cancel) may still be missing; March onward must be suppressed.
    assert "2026-03-10" not in missing_dates
    assert "2026-04-10" not in missing_dates
    assert "2026-05-10" not in missing_dates


def test_canceled_bill_charge_before_effective_does_not_come_back():
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-5.00", on="2026-02-10", desc="SKETCH")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["came_back"] == []
    tracked = report["tracked"][0]
    assert tracked["came_back"] is False
    assert tracked["lifecycle"] == "canceled"


def test_canceled_bill_charge_on_or_after_effective_comes_back():
    cfg = _config([CARD], [_bill("Replit", "Card", 20.00, 10, match="REPLIT",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-20.00", on="2026-04-10", desc="REPLIT")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["came_back"]) == 1
    assert report["came_back"][0]["name"] == "Replit"
    assert report["summary"]["came_back"] == 1
    assert report["tracked"][0]["came_back"] is True


def test_canceled_charge_exactly_on_effective_date_comes_back():
    cfg = _config([CARD], [_bill("Study", "Card", 30.00, 1, match="STUDY",
                                 lifecycle="canceled", cancel_effective="2026-04-01")])
    txns = [_txn("t1", "card", "-30.00", on="2026-04-01", desc="STUDY.COM")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["tracked"][0]["came_back"] is True


def test_canceled_bill_is_never_overdue():
    # No charges at all after cancellation -> the bill must not read "overdue".
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-01-01")])
    report = subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END)
    assert report["tracked"][0]["status"] != "overdue"


def test_active_bill_carries_lifecycle_fields():
    cfg = _config([CARD], [_bill("Netflix", "Card", 15.99, 10, match="NETFLIX")])
    report = subscription.subscription_audit(cfg, [], start=WIN_START, end=WIN_END)
    tracked = report["tracked"][0]
    assert tracked["lifecycle"] == "active"
    assert tracked["cancel_effective"] is None
    assert tracked["came_back"] is False


# --- Cancellation watch: set_bill_lifecycle writer ----------------------------


def _seed_budget(path):
    path.write_text(json.dumps({
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [{"name": "Sketch", "envelope": "Card", "amount": 5.00,
                       "cadence": "monthly", "day": 10, "match": "sketch"}],
    }), encoding="utf-8")


def test_set_lifecycle_marks_canceled(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    result = subscription.set_bill_lifecycle(
        path, "Sketch", "canceled", cancel_effective="2026-04-01"
    )
    assert result["lifecycle"] == "canceled"
    assert result["cancel_effective"] == "2026-04-01"
    cfg = budget_config.load_config(path)
    b = cfg.recurring[0]
    assert b.lifecycle == "canceled"
    assert b.cancel_effective == date(2026, 4, 1)


def test_set_lifecycle_is_case_insensitive_on_name(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    subscription.set_bill_lifecycle(
        path, "sketch", "canceling", cancel_effective="2026-04-01"
    )
    assert budget_config.load_config(path).recurring[0].lifecycle == "canceling"


def test_set_lifecycle_reactivate_clears_effective(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    subscription.set_bill_lifecycle(
        path, "Sketch", "canceled", cancel_effective="2026-04-01"
    )
    subscription.set_bill_lifecycle(path, "Sketch", "active")
    b = budget_config.load_config(path).recurring[0]
    assert b.lifecycle == "active"
    assert b.cancel_effective is None


def test_set_lifecycle_reactivate_rejects_effective(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    with pytest.raises(budget_config.BudgetConfigError, match="must be omitted"):
        subscription.set_bill_lifecycle(
            path, "Sketch", "active", cancel_effective="2026-04-01"
        )


def test_set_lifecycle_canceled_requires_effective(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    with pytest.raises(budget_config.BudgetConfigError, match="is required"):
        subscription.set_bill_lifecycle(path, "Sketch", "canceled")


def test_set_lifecycle_unknown_name_errors(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    with pytest.raises(budget_config.BudgetConfigError, match="no recurring bill named"):
        subscription.set_bill_lifecycle(
            path, "Nonexistent", "canceled", cancel_effective="2026-04-01"
        )


def test_set_lifecycle_invalid_lifecycle_errors(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    with pytest.raises(budget_config.BudgetConfigError, match="lifecycle must be one of"):
        subscription.set_bill_lifecycle(path, "Sketch", "paused")


def test_set_lifecycle_ambiguous_name_errors(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": "Apple", "envelope": "Card", "amount": 0.99, "cadence": "monthly",
             "day": 10, "match": "apple"},
            {"name": "Apple", "envelope": "Card", "amount": 9.99, "cadence": "monthly",
             "day": 10, "match": "apple"},
        ],
    }), encoding="utf-8")
    with pytest.raises(budget_config.BudgetConfigError, match="names must be unique"):
        subscription.set_bill_lifecycle(
            path, "Apple", "canceled", cancel_effective="2026-04-01"
        )


def test_set_lifecycle_missing_file_errors(tmp_path):
    path = tmp_path / "budget.json"
    with pytest.raises(budget_config.BudgetConfigError, match="does not exist"):
        subscription.set_bill_lifecycle(
            path, "Sketch", "canceled", cancel_effective="2026-04-01"
        )


def test_set_lifecycle_invalid_effective_date_rejected_without_writing(tmp_path):
    path = tmp_path / "budget.json"
    _seed_budget(path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(budget_config.BudgetConfigError):
        subscription.set_bill_lifecycle(
            path, "Sketch", "canceled", cancel_effective="not-a-date"
        )
    # Bad input must never overwrite the working config.
    assert path.read_text(encoding="utf-8") == before


def test_came_back_detects_off_schedule_return():
    # A canceled bill due day 10 that re-charges on day 25 (drift 15 > the
    # default 7-day tolerance) must still warn: the scheduled matcher would never
    # match it, and its absence is suppressed, so the identity scan is the only
    # signal. Regresses a gap where came_back keyed on the scheduled match alone.
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-5.00", on="2026-03-25", desc="SKETCH")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["came_back"]) == 1
    assert report["came_back"][0]["came_back_on"] == "2026-03-25"
    assert report["expected_missing"] == [] or all(
        m["name"] != "Sketch" or m["expected_date"] < "2026-03-01"
        for m in report["expected_missing"]
    )


def test_came_back_detects_different_amount_return():
    # A re-bill at a changed price (annual renewal, price hike) is still the bill
    # coming back; came_back is amount-blind.
    cfg = _config([CARD], [_bill("Replit", "Card", 20.00, 10, match="REPLIT",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-200.00", on="2026-04-10", desc="REPLIT ANNUAL")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert len(report["came_back"]) == 1
    assert report["came_back"][0]["came_back_on"] == "2026-04-10"


def test_came_back_envelope_only_bill_off_schedule():
    # An envelope-only canceled bill (no match keyword) detects a return by the
    # account binding, ignoring day/amount.
    cfg = _config([CARD], [_bill("CardSub", "Card", 5.00, 10,
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-7.50", on="2026-03-22", desc="WHATEVER")]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["tracked"][0]["came_back"] is True
    assert report["tracked"][0]["came_back_on"] == "2026-03-22"


def test_came_back_on_is_latest_return_charge():
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [
        _txn("t1", "card", "-5.00", on="2026-03-25", desc="SKETCH"),
        _txn("t2", "card", "-5.00", on="2026-04-25", desc="SKETCH"),
    ]
    report = subscription.subscription_audit(cfg, txns, start=WIN_START, end=WIN_END)
    assert report["tracked"][0]["came_back_on"] == "2026-04-25"


def test_came_back_is_scoped_to_audit_window_not_lookback():
    # A return before the requested window start must NOT warn, and the result
    # must not flip when start is nudged by a day across the return — came-back is
    # scoped to the audited window, not to the status look-back internals.
    cfg = _config([CARD], [_bill("Sketch", "Card", 5.00, 10, match="SKETCH",
                                 lifecycle="canceled", cancel_effective="2026-03-01")])
    txns = [_txn("t1", "card", "-5.00", on="2026-03-25", desc="SKETCH")]
    # Window starts after the return: not in view, no warning.
    after = subscription.subscription_audit(
        cfg, txns, start=date(2026, 4, 1), end=date(2026, 6, 30)
    )
    assert after["came_back"] == []
    # One-day nudge does not change the verdict (no look-back coupling).
    nudged = subscription.subscription_audit(
        cfg, txns, start=date(2026, 4, 2), end=date(2026, 6, 30)
    )
    assert nudged["came_back"] == []
    # Window that includes the return: warns.
    including = subscription.subscription_audit(
        cfg, txns, start=date(2026, 3, 1), end=date(2026, 6, 30)
    )
    assert len(including["came_back"]) == 1
    assert including["came_back"][0]["came_back_on"] == "2026-03-25"


def test_concurrent_marks_do_not_lose_updates(tmp_path):
    # The web server is threaded, and CLI/MCP write the same file. Two markers
    # that each read the same starting config must not have the later write
    # clobber the earlier one's change. Mark many distinct bills concurrently
    # and assert every mark survived (this races and loses updates without the
    # read-through-write lock).
    import threading

    names = [f"Bill{i}" for i in range(12)]
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "version": 1,
        "envelopes": [{"name": "Card", "accounts": ["card"]}],
        "recurring": [
            {"name": n, "envelope": "Card", "amount": 5.00,
             "cadence": "monthly", "day": 10, "match": n.lower()}
            for n in names
        ],
    }), encoding="utf-8")

    start = threading.Barrier(len(names))
    errors: list[Exception] = []

    def mark(n: str) -> None:
        start.wait()
        try:
            subscription.set_bill_lifecycle(
                path, n, "canceling", cancel_effective="2026-06-01"
            )
        except Exception as exc:  # noqa: BLE001 - surface in assertion
            errors.append(exc)

    threads = [threading.Thread(target=mark, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    cfg = budget_config.load_config(path)
    marked = {b.name for b in cfg.recurring if b.lifecycle == "canceling"}
    assert marked == set(names)


def test_lock_acquisition_failure_fails_closed(tmp_path, monkeypatch):
    # On a platform that supports the cross-process file lock, an error taking
    # it must fail the write loudly rather than silently downgrade to
    # process-local-only serialization (which would not guard another process).
    import fcntl

    path = tmp_path / "budget.json"
    _seed_budget(path)

    def boom(*_a, **_k):
        raise OSError("no locks available")

    monkeypatch.setattr(fcntl, "flock", boom)
    with pytest.raises(budget_config.BudgetConfigError) as exc:
        subscription.set_bill_lifecycle(
            path, "Sketch", "canceling", cancel_effective="2026-06-01"
        )
    assert "lock" in str(exc.value).lower()
    # The config was not modified by the refused write.
    cfg = budget_config.load_config(path)
    assert cfg.recurring[0].lifecycle == "active"
