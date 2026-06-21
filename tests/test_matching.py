from finance_mcp import matching
from finance_mcp.matching import (
    CONF_ENVELOPE,
    CONF_STRUCTURAL,
    CONF_UNCONFIRMED,
    CONF_UNMATCHED,
    METHOD_ENVELOPE_SET,
    METHOD_FORCED_PERFECT,
    METHOD_MUTUAL_UNIQUE,
    STATUS_INFERRED,
    STATUS_UNCONFIRMED,
    STATUS_UNMATCHED,
    propose_transfer_links,
    summarize,
)


def _txn(tid, account, amount, date="2026-06-19", *, is_transfer=True, name=None):
    """Build a minimal archive-shaped transfer transaction."""
    return {
        "id": tid,
        "account_id": account,
        "account_name": name or account,
        "amount": amount,
        "posted": f"{date}T12:00:00+00:00",
        "is_transfer": is_transfer,
    }


def _links(proposals):
    return [
        p for p in proposals if p.debit_txn_id is not None and p.credit_txn_id is not None
    ]


def test_mutual_unique_pairs_debit_to_credit():
    txns = [
        _txn("d", "Groceries", "-100.00"),
        _txn("c", "Main", "100.00"),
    ]
    props = propose_transfer_links(txns)
    assert len(props) == 1
    p = props[0]
    assert p.debit_txn_id == "d"
    assert p.credit_txn_id == "c"
    assert p.amount_cents == 10000
    assert p.status == STATUS_INFERRED
    assert p.confidence == CONF_STRUCTURAL
    assert p.method == METHOD_MUTUAL_UNIQUE
    assert p.candidates_before == 1
    assert p.candidates_after == 1


def test_same_account_self_transfer_is_never_an_edge():
    # A debit and credit of equal magnitude on the SAME account cannot be a
    # transfer to oneself, so neither links — both surface as unmatched.
    txns = [
        _txn("d", "Main", "-50.00"),
        _txn("c", "Main", "50.00"),
    ]
    props = propose_transfer_links(txns)
    assert _links(props) == []
    assert {p.status for p in props} == {STATUS_UNMATCHED}


def test_forced_perfect_matching_locks_unique_assignment():
    # d1=Main can only reach c2 (c1 is on Main too, excluded); d2=Spending can
    # reach both. The only perfect matching is d1->c2, d2->c1.
    txns = [
        _txn("d1", "Main", "-30.00"),
        _txn("d2", "Spending", "-30.00"),
        _txn("c1", "Main", "30.00"),
        _txn("c2", "Housekeeping", "30.00"),
    ]
    props = propose_transfer_links(txns)
    links = _links(props)
    assert len(links) == 2
    assert all(p.confidence == CONF_STRUCTURAL for p in links)
    assert all(p.method == METHOD_FORCED_PERFECT for p in links)
    pairs = {(p.debit_txn_id, p.credit_txn_id) for p in links}
    assert pairs == {("d1", "c2"), ("d2", "c1")}


def test_ambiguous_complete_bipartite_needs_confirmation():
    # Two distinct sources, two distinct dests, every cross-pairing valid: K2,2
    # has two perfect matchings and no single source/dest, so nothing is forced.
    txns = [
        _txn("d1", "Main", "-40.00"),
        _txn("d2", "Spending", "-40.00"),
        _txn("c1", "Groceries", "40.00"),
        _txn("c2", "Housekeeping", "40.00"),
    ]
    props = propose_transfer_links(txns)
    assert _links(props) == []
    assert len(props) == 4
    assert {p.status for p in props} == {STATUS_UNCONFIRMED}
    assert {p.confidence for p in props} == {CONF_UNCONFIRMED}
    for p in props:
        assert set(p.candidate_txn_ids)  # each leg lists its plausible partners
        assert p.candidates_after == 2


def test_anti_greedy_shared_credit_is_not_auto_claimed():
    # Both debits' only candidate is the single credit c. A greedy per-debit
    # walk would wrongly claim c for d1. The credit is not mutually unique to
    # either, so all three legs need confirmation instead.
    txns = [
        _txn("d1", "Groceries", "-25.00"),
        _txn("d2", "Spending", "-25.00"),
        _txn("c", "Main", "25.00"),
    ]
    props = propose_transfer_links(txns)
    assert _links(props) == []
    assert {p.status for p in props} == {STATUS_UNCONFIRMED}
    by_id = {(p.debit_txn_id or p.credit_txn_id): p for p in props}
    assert set(by_id["d1"].candidate_txn_ids) == {"c"}
    assert set(by_id["c"].candidate_txn_ids) == {"d1", "d2"}


def test_envelope_set_single_source_determines_flow():
    # Main funds two different envelopes by equal amounts the same day. The exact
    # txn pairing is arbitrary, but every credit's source is Main.
    txns = [
        _txn("d1", "Main", "-100.00"),
        _txn("d2", "Main", "-100.00"),
        _txn("c1", "Groceries", "100.00"),
        _txn("c2", "Housekeeping", "100.00"),
    ]
    props = propose_transfer_links(txns)
    links = _links(props)
    assert len(links) == 2
    assert all(p.confidence == CONF_ENVELOPE for p in links)
    assert all(p.method == METHOD_ENVELOPE_SET for p in links)
    assert {p.debit_account_name for p in links} == {"Main"}
    assert {p.credit_account_name for p in links} == {"Groceries", "Housekeeping"}
    # A valid one-to-one assignment: each leg used exactly once.
    assert {p.debit_txn_id for p in links} == {"d1", "d2"}
    assert {p.credit_txn_id for p in links} == {"c1", "c2"}
    assert all("arbitrary" in p.explanation for p in links)


def test_envelope_set_single_destination_determines_flow():
    # Two envelopes both top up Main by equal amounts: the destination is known.
    txns = [
        _txn("d1", "Groceries", "-70.00"),
        _txn("d2", "Spending", "-70.00"),
        _txn("c1", "Main", "70.00"),
        _txn("c2", "Main", "70.00"),
    ]
    props = propose_transfer_links(txns)
    links = _links(props)
    assert len(links) == 2
    assert all(p.confidence == CONF_ENVELOPE for p in links)
    assert {p.credit_account_name for p in links} == {"Main"}
    assert {p.debit_account_name for p in links} == {"Groceries", "Spending"}


def test_envelope_group_size_reflects_component_not_bucket():
    # A balanced single-destination component (Groceries, Spending -> Main) shares
    # its same-day/same-amount bucket with an unrelated isolated debit on Main
    # itself (no valid counterparty -> unmatched). The envelope explanation must
    # report the component's group size (2), not the bucket-wide debit count (3).
    txns = [
        _txn("d1", "Groceries", "-70.00"),
        _txn("d2", "Spending", "-70.00"),
        _txn("c1", "Main", "70.00"),
        _txn("c2", "Main", "70.00"),
        _txn("d3", "Main", "-70.00"),  # isolated: only same-account opposites
    ]
    props = propose_transfer_links(txns)
    links = _links(props)
    assert len(links) == 2
    assert all(p.confidence == CONF_ENVELOPE for p in links)
    assert all("group of 2)" in p.explanation for p in links)
    assert all("group of 3)" not in p.explanation for p in links)
    unmatched = [p for p in props if p.status == STATUS_UNMATCHED]
    assert [p.debit_txn_id for p in unmatched] == ["d3"]


def test_unmatched_when_no_opposite_leg():
    txns = [_txn("d", "Groceries", "-10.00")]
    props = propose_transfer_links(txns)
    assert len(props) == 1
    assert props[0].status == STATUS_UNMATCHED
    assert props[0].confidence == CONF_UNMATCHED
    assert props[0].debit_txn_id == "d"
    assert props[0].credit_txn_id is None


def test_different_amounts_do_not_match():
    txns = [
        _txn("d", "Groceries", "-10.00"),
        _txn("c", "Main", "11.00"),
    ]
    props = propose_transfer_links(txns)
    assert _links(props) == []
    assert {p.status for p in props} == {STATUS_UNMATCHED}


def test_different_dates_do_not_match_same_day_only():
    txns = [
        _txn("d", "Groceries", "-10.00", date="2026-06-19"),
        _txn("c", "Main", "10.00", date="2026-06-20"),
    ]
    props = propose_transfer_links(txns)
    assert _links(props) == []


def test_non_transfer_transactions_are_ignored():
    txns = [
        _txn("d", "Groceries", "-10.00", is_transfer=False),
        _txn("c", "Main", "10.00", is_transfer=False),
    ]
    assert propose_transfer_links(txns) == []


def test_cents_from_string_avoids_float_drift():
    # "70.00" and "70.0" are the same money; matching must not care about the
    # textual form or binary-float representation.
    txns = [
        _txn("d", "Groceries", "-70.00"),
        _txn("c", "Main", "70.0"),
    ]
    links = _links(propose_transfer_links(txns))
    assert len(links) == 1
    assert links[0].amount_cents == 7000


def test_unparseable_amount_surfaces_as_unmatched_not_dropped():
    # A negative but sub-cent amount can't reduce to whole cents; it must still
    # surface (as a debit, by its sign) rather than be silently dropped.
    txns = [_txn("d", "Groceries", "-99.999")]
    props = propose_transfer_links(txns)
    assert len(props) == 1
    assert props[0].status == STATUS_UNMATCHED
    assert props[0].amount_cents is None
    assert props[0].debit_txn_id == "d"
    assert "Cannot reconcile" in props[0].explanation


def test_missing_posted_date_surfaces_as_unmatched():
    txn = _txn("d", "Groceries", "-10.00")
    txn["posted"] = None
    props = propose_transfer_links([txn])
    assert len(props) == 1
    assert props[0].status == STATUS_UNMATCHED
    assert "no posted date" in props[0].explanation


def test_each_transaction_appears_in_exactly_one_proposal():
    txns = [
        _txn("d1", "Main", "-100.00"),
        _txn("d2", "Main", "-100.00"),
        _txn("c1", "Groceries", "100.00"),
        _txn("c2", "Housekeeping", "100.00"),
        _txn("d3", "Spending", "-5.00"),  # unmatched singleton
    ]
    props = propose_transfer_links(txns)
    seen = []
    for p in props:
        if p.debit_txn_id:
            seen.append(p.debit_txn_id)
        if p.credit_txn_id:
            seen.append(p.credit_txn_id)
    assert sorted(seen) == ["c1", "c2", "d1", "d2", "d3"]
    assert len(seen) == len(set(seen))  # no transaction claimed twice


def test_separate_amount_buckets_do_not_interfere():
    txns = [
        _txn("d1", "Groceries", "-10.00"),
        _txn("c1", "Main", "10.00"),
        _txn("d2", "Housekeeping", "-20.00"),
        _txn("c2", "Main", "20.00"),
    ]
    links = _links(propose_transfer_links(txns))
    pairs = {(p.debit_txn_id, p.credit_txn_id) for p in links}
    assert pairs == {("d1", "c1"), ("d2", "c2")}


def test_id_less_transfer_is_skipped_not_surfaced():
    # A leg with no id can't be keyed by a confirmation or stored as a link, so
    # it is intentionally dropped (the real archive guarantees ids anyway).
    txn = _txn("d", "Groceries", "-10.00")
    txn["id"] = None
    assert propose_transfer_links([txn]) == []


def test_duplicate_txn_id_is_skipped_wholesale():
    # Malformed input: the same id appears as a debit and a credit on different
    # accounts. The duplicates can't be told apart and can't all persist, so
    # every occurrence is dropped — no proposal references a duplicated id.
    txns = [
        _txn("dup", "Groceries", "-10.00"),
        _txn("dup", "Main", "10.00"),
    ]
    assert propose_transfer_links(txns) == []


def test_unverifiable_account_unmatched_explanation_is_honest():
    # A leg whose account_id is missing can't be proven distinct, so it stays
    # unmatched — but the explanation must not claim "no counterparty" when a
    # same-day same-amount opposite leg actually exists.
    a = _txn("a", None, "-50.00")
    a["account_id"] = None
    b = _txn("b", "Main", "50.00")
    props = propose_transfer_links([a, b])
    assert _links(props) == []
    assert {p.status for p in props} == {STATUS_UNMATCHED}
    by_id = {(p.debit_txn_id or p.credit_txn_id): p for p in props}
    assert "exist but none on a verifiably different account" in by_id["a"].explanation
    assert "exist but none on a verifiably different account" in by_id["b"].explanation


def test_unmatched_no_opposite_leg_keeps_plain_explanation():
    props = propose_transfer_links([_txn("d", "Groceries", "-10.00")])
    assert len(props) == 1
    assert "No same-day" in props[0].explanation


def test_large_single_source_fanout_resolves_without_recursion_error():
    # Main funds many envelopes by an equal amount the same day — a legitimate
    # single-source component far larger than the recursion limit. It must
    # resolve as envelope-set, not crash the reconcile.
    n = 1100
    txns = [_txn(f"d{i}", "Main", "-50.00") for i in range(n)]
    txns += [_txn(f"c{i}", f"Env{i}", "50.00") for i in range(n)]
    links = _links(propose_transfer_links(txns))
    assert len(links) == n
    assert all(p.confidence == CONF_ENVELOPE for p in links)
    assert {p.debit_account_name for p in links} == {"Main"}
    # A valid one-to-one assignment: every debit and every credit used once.
    assert len({p.debit_txn_id for p in links}) == n
    assert len({p.credit_txn_id for p in links}) == n

    txns = [
        _txn("d", "Groceries", "-100.00"),
        _txn("c", "Main", "100.00"),
        _txn("d2", "Spending", "-5.00"),  # unmatched
    ]
    props = propose_transfer_links(txns)
    counts = summarize(props)
    assert counts["links"] == 1
    assert counts["proposals"] == 2
    assert counts["by_status"][STATUS_INFERRED] == 1
    assert counts["by_status"][STATUS_UNMATCHED] == 1
    assert counts["by_confidence"][CONF_STRUCTURAL] == 1
