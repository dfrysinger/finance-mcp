from finance_mcp import matching
from finance_mcp.matching import (
    CONF_ENVELOPE,
    CONF_KEYWORD,
    CONF_STRUCTURAL,
    CONF_UNCONFIRMED,
    CONF_UNMATCHED,
    METHOD_ENVELOPE_SET,
    METHOD_FORCED_PERFECT,
    METHOD_KEYWORD_TYPE,
    METHOD_MUTUAL_UNIQUE,
    PRODUCT_CHECKING,
    PRODUCT_SAVINGS,
    STATUS_INFERRED,
    STATUS_UNCONFIRMED,
    STATUS_UNMATCHED,
    destination_type,
    propose_transfer_links,
    summarize,
)


def _txn(tid, account, amount, date="2026-06-19", *, is_transfer=True, name=None, desc=None):
    """Build a minimal archive-shaped transfer transaction."""
    return {
        "id": tid,
        "account_id": account,
        "account_name": name or account,
        "amount": amount,
        "posted": f"{date}T12:00:00+00:00",
        "is_transfer": is_transfer,
        "description": desc,
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


# --- Destination product-type keyword extraction -------------------------------


def test_destination_type_extracts_checking_and_savings():
    assert destination_type("Transfer to Schwab Bank Investor Checking") == PRODUCT_CHECKING
    assert destination_type("Transfer to Schwab Bank Investor Savings") == PRODUCT_SAVINGS


def test_destination_type_is_prefix_tolerant_to_truncation():
    # Schwab's feed truncates the trailing letters; a prefix must still resolve.
    assert destination_type("Transfer to Schwab Bank Investor Checkin") == PRODUCT_CHECKING


def test_destination_type_reads_source_keyword_on_a_credit():
    assert destination_type("Transfer from Investor Savings") == PRODUCT_SAVINGS


def test_destination_type_none_when_no_type_token():
    assert destination_type("Online Transfer") is None
    assert destination_type("Groceries") is None
    assert destination_type(None) is None


def test_destination_type_extracts_from_account_name_for_hints():
    assert destination_type("Emergency Savings") == PRODUCT_SAVINGS
    assert destination_type("Main Checking") == PRODUCT_CHECKING
    assert destination_type("Groceries") is None


# --- Stage 3: keyword/type tie-breaker -----------------------------------------


def _ambiguous_collision():
    # The user's real validated example: two $100 debits, two $100 credits, all
    # same day. Structurally there are two perfect matchings, so without type
    # information this is genuinely ambiguous.
    return [
        _txn("d_groc", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("d_house", "Housekeeping", "-100.00", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c_main", "Main", "100.00", desc="Transfer from Schwab Bank"),
        _txn("c_emerg", "Emergency", "100.00", desc="Transfer from Schwab Bank"),
    ]


def test_ambiguous_collision_without_type_map_needs_confirm():
    props = propose_transfer_links(_ambiguous_collision())
    assert _links(props) == []
    assert all(p.confidence == CONF_UNCONFIRMED for p in props)
    assert len(props) == 4


def test_keyword_type_resolves_ambiguous_collision():
    account_types = {
        "Main": {"product_type": PRODUCT_CHECKING, "source": "confirmed"},
        "Emergency": {"product_type": PRODUCT_SAVINGS, "source": "confirmed"},
    }
    props = propose_transfer_links(_ambiguous_collision(), account_types=account_types)
    links = _links(props)
    assert len(links) == 2
    paired = {(p.debit_account_name, p.credit_account_name) for p in links}
    assert paired == {("Groceries", "Main"), ("Housekeeping", "Emergency")}
    assert all(p.confidence == CONF_KEYWORD for p in links)
    assert all(p.method == METHOD_KEYWORD_TYPE for p in links)
    # type_source flows through from the map so the UI can show how sure we are.
    assert all(p.type_source == "confirmed" for p in links)
    # keyword records the destination type the debit named.
    by_debit = {p.debit_account_name: p for p in links}
    assert by_debit["Groceries"].keyword == PRODUCT_CHECKING
    assert by_debit["Housekeeping"].keyword == PRODUCT_SAVINGS


def test_keyword_map_accepts_plain_string_shape():
    account_types = {"Main": PRODUCT_CHECKING, "Emergency": PRODUCT_SAVINGS}
    links = _links(propose_transfer_links(_ambiguous_collision(), account_types=account_types))
    assert len(links) == 2
    assert all(p.confidence == CONF_KEYWORD for p in links)
    # No source in a plain-string map → type_source is None, not a crash.
    assert all(p.type_source is None for p in links)


def test_keyword_map_that_does_not_narrow_still_needs_confirm():
    # Two debits both naming "Checking", two credits both on Checking accounts:
    # the type map is fully known but cannot break the symmetry, so the matcher
    # honestly stays needs-confirm rather than guessing a pairing.
    txns = [
        _txn("d1", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("d2", "Housekeeping", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c_main", "Main", "100.00", desc="Transfer from Schwab Bank"),
        _txn("c_backup", "Backup", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Main": PRODUCT_CHECKING, "Backup": PRODUCT_CHECKING}
    props = propose_transfer_links(txns, account_types=account_types)
    assert _links(props) == []
    assert all(p.confidence == CONF_UNCONFIRMED for p in props)


def test_keyword_map_with_partial_coverage_resolves_by_elimination():
    # Typing only ONE credit account is enough here: it removes the contradicting
    # edge (Housekeeping->Main), which forces the rest of the assignment.
    account_types = {"Main": PRODUCT_CHECKING}
    props = propose_transfer_links(_ambiguous_collision(), account_types=account_types)
    links = _links(props)
    assert len(links) == 2
    paired = {(p.debit_account_name, p.credit_account_name) for p in links}
    assert paired == {("Groceries", "Main"), ("Housekeeping", "Emergency")}
    assert all(p.confidence == CONF_KEYWORD for p in links)


# --- Contradiction guard -------------------------------------------------------


def test_type_guard_flags_structural_match_that_contradicts_a_known_type():
    # The sole structural pairing says Groceries -> Main, but the debit names
    # destination "Savings" while Main is a known Checking account. The guard
    # refuses to auto-link and flags it for review.
    txns = [
        _txn("d", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c", "Main", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Main": {"product_type": PRODUCT_CHECKING, "source": "confirmed"}}
    props = propose_transfer_links(txns, account_types=account_types)
    assert _links(props) == []
    assert all(p.confidence == CONF_UNCONFIRMED for p in props)
    assert any("conflicts with a known account type" in p.explanation for p in props)


def test_type_guard_does_not_fire_without_a_map():
    # Same data, no map: the structural match stands (graceful degrade).
    txns = [
        _txn("d", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c", "Main", "100.00", desc="Transfer from Schwab Bank"),
    ]
    links = _links(propose_transfer_links(txns))
    assert len(links) == 1
    assert links[0].confidence == CONF_STRUCTURAL


def test_consistent_structural_match_keeps_structural_confidence():
    # A keyword-consistent structural match must NOT be downgraded to the weaker
    # keyword-type confidence — structural is the stronger claim.
    txns = [
        _txn("d", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c", "Main", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Main": PRODUCT_CHECKING}
    links = _links(propose_transfer_links(txns, account_types=account_types))
    assert len(links) == 1
    assert links[0].confidence == CONF_STRUCTURAL
    assert links[0].method == METHOD_MUTUAL_UNIQUE


# --- Regression guards for keyword-stage fixes ---------------------------------


def test_stage3_keyword_filter_respects_component_size_cap():
    # A large ambiguous multi-account component (every debit can pair with every
    # same-type credit) would recurse past Python's stack limit if Stage 3 ran
    # its recursive enumerator unguarded. The size cap must skip it so it fails
    # safe to confirmation instead of raising RecursionError.
    n = 1100  # exceeds the default recursion limit
    txns = [
        _txn(f"d{i}", f"Src{i}", "-50.00", desc="Transfer to Schwab Bank Investor Checking")
        for i in range(n)
    ]
    txns += [
        _txn(f"c{i}", f"Dst{i}", "50.00", desc="Transfer from Schwab Bank") for i in range(n)
    ]
    # Every destination account is Checking, matching every debit's keyword, so
    # the type filter removes no edges and the component stays fully ambiguous.
    account_types = {f"Dst{i}": PRODUCT_CHECKING for i in range(n)}
    props = propose_transfer_links(txns, account_types=account_types)  # must not raise
    assert _links(props) == []
    assert all(p.confidence == CONF_UNCONFIRMED for p in props)


def test_envelope_set_prefers_keyword_consistent_pairing():
    # Single source funds two destinations of different types. Without using the
    # keyword the arbitrary pairing could cross the types; with the map the
    # representative pairs must respect "to Checking" -> Checking account.
    txns = [
        _txn("d_check", "Main", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("d_save", "Main", "-100.00", desc="Transfer to Schwab Bank Investor Savings"),
        # Credit input order reversed so a naive matcher would cross them.
        _txn("c_save", "Emergency", "100.00", desc="Transfer from Schwab Bank"),
        _txn("c_check", "Backup", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Backup": PRODUCT_CHECKING, "Emergency": PRODUCT_SAVINGS}
    links = _links(propose_transfer_links(txns, account_types=account_types))
    assert len(links) == 2
    assert all(p.confidence == CONF_ENVELOPE for p in links)
    paired = {(p.debit_txn_id, p.credit_account_name) for p in links}
    # The Checking debit lands on the Checking account, Savings on Savings.
    assert ("d_check", "Backup") in paired
    assert ("d_save", "Emergency") in paired


def test_stage3_records_credit_side_keyword_when_it_drove_the_match():
    # Only the CREDIT descriptions name a (source) type; the debit descriptions
    # are bare. The emitted link must record the credit keyword, not None.
    txns = [
        _txn("d_groc", "Groceries", "-100.00", desc="Transfer to Schwab Bank"),
        _txn("d_house", "Housekeeping", "-100.00", desc="Transfer to Schwab Bank"),
        _txn("c_main", "Main", "100.00", desc="Transfer from Schwab Bank Investor Checking"),
        _txn("c_emerg", "Emergency", "100.00", desc="Transfer from Schwab Bank Investor Savings"),
    ]
    # The credit keyword names the SOURCE type = the debit account's type.
    account_types = {"Groceries": PRODUCT_CHECKING, "Housekeeping": PRODUCT_SAVINGS}
    links = _links(propose_transfer_links(txns, account_types=account_types))
    assert len(links) == 2
    by_credit = {p.credit_txn_id: p for p in links}
    assert by_credit["c_main"].keyword == PRODUCT_CHECKING
    assert by_credit["c_emerg"].keyword == PRODUCT_SAVINGS
    assert all(p.keyword is not None for p in links)
    assert all("None" not in p.explanation for p in links)


def test_envelope_contradiction_surfaces_needs_confirm():
    # A single source funds two destinations and every debit names "to Checking",
    # but one destination is a known Savings account: no keyword-consistent
    # assignment exists. Rather than emit a type-crossing envelope pairing, the
    # whole component is flagged for confirmation.
    txns = [
        _txn("d1", "Main", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("d2", "Main", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c1", "Checking", "100.00", desc="Transfer from Schwab Bank"),
        _txn("c2", "Savings", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Checking": PRODUCT_CHECKING, "Savings": PRODUCT_SAVINGS}
    props = propose_transfer_links(txns, account_types=account_types)
    assert _links(props) == []
    assert all(p.confidence == CONF_UNCONFIRMED for p in props)
    # The audit reason is the envelope-specific one, not Stage 1's single-pairing
    # message (multiple pairings exist here; none is type-consistent).
    assert all("no account-type-consistent pairing exists" in p.explanation for p in props)
    assert all("only structural pairing" not in p.explanation for p in props)
    # Regression guard: without the type map this is a normal envelope set.
    base = _links(propose_transfer_links(txns))
    assert len(base) == 2
    assert all(p.confidence == CONF_ENVELOPE for p in base)


def test_stage3_prefers_the_keyword_whose_counterpart_is_typed():
    # Both sides carry keywords, but only the debit ACCOUNTS are typed, so each
    # credit's source keyword is the real constraint; the debit's destination
    # keyword names an untyped account and narrowed nothing. The link must record
    # the credit keyword with a real type source, not the debit keyword + None.
    txns = [
        _txn("d1", "AcctA", "-100.00", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("d2", "AcctB", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c1", "Dst1", "100.00", desc="Transfer from Schwab Bank Investor Checking"),
        _txn("c2", "Dst2", "100.00", desc="Transfer from Schwab Bank Investor Savings"),
    ]
    account_types = {
        "AcctA": {"product_type": PRODUCT_CHECKING, "source": "confirmed"},
        "AcctB": {"product_type": PRODUCT_SAVINGS, "source": "confirmed"},
    }
    links = _links(propose_transfer_links(txns, account_types=account_types))
    assert len(links) == 2
    by_debit = {p.debit_txn_id: p for p in links}
    # The credit source keyword drove each match (debit destinations were untyped).
    assert by_debit["d1"].keyword == PRODUCT_CHECKING
    assert by_debit["d2"].keyword == PRODUCT_SAVINGS
    assert all(p.type_source == "confirmed" for p in links)
    assert all("None" not in p.explanation for p in links)


def test_tuple_shaped_account_type_entry_degrades_without_crashing():
    # The pure suggestion path returns (product_type, source) tuples; if such a
    # shape is ever passed straight to the matcher it must degrade to "unknown
    # type" rather than raise AttributeError on the dict access.
    txns = [
        _txn("d", "Groceries", "-100.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c", "Main", "100.00", desc="Transfer from Schwab Bank"),
    ]
    account_types = {"Main": (PRODUCT_CHECKING, "inferred")}
    links = _links(propose_transfer_links(txns, account_types=account_types))
    # The lone structural match still links; the unrecognized entry is ignored.
    assert len(links) == 1
    assert links[0].confidence == CONF_STRUCTURAL
