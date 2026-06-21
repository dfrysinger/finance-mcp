"""Transfer matching engine — pure logic, no persistence.

Schwab's feed names only the *product type* a transfer went to ("...to Investor
Checking"), never which of the user's ~19 named envelope accounts received it.
This module reconstructs the hidden counterparty by pairing an outgoing leg
(debit) with the incoming leg (credit) that received the same money on a
*different* account the *same posted day*.

The matching is deliberately **global and bipartite**, not greedy per-debit: a
single credit could plausibly settle two same-amount debits, and a greedy walk
would silently pick the wrong one. Instead legs are bucketed by (date, integer
cents), each bucket's debit/credit graph is split into connected components, and
each component is resolved in honest stages:

* **Stage 1 — structurally forced.** A component with exactly one perfect
  matching (after excluding same-account self-transfers) has only one way its
  legs can pair, so those pairings are certain. The single-edge case (a debit
  whose sole candidate is a credit whose sole candidate is that debit) is
  reported as ``mutual-unique``; larger forced components as ``forced-perfect``.
* **Stage 2 — envelope set.** When a residual component has a single distinct
  source account (or a single distinct destination), the source→destination
  *flow* is determined even though the exact txn-to-txn pairing inside the group
  is arbitrary. This is the case the user cares about most: which envelope
  funded which.
* **Residue → needs-confirm.** Anything still ambiguous is surfaced with its
  candidate counterparties for the user to resolve, never guessed.

This module is pure: it returns :class:`TransferProposal` objects and never
touches the database. Persistence (idempotent reconcile, confirmed-wins,
promote/downgrade) and the keyword/account-type tie-breaker layer on top in
later pieces.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import groupby

from .normalize import amount_to_cents

# --- Status values (mirror the transfer_links.status column) -------------------
STATUS_INFERRED = "inferred"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_UNMATCHED = "unmatched"

# --- Confidence taxonomy (encodes WHY a link was drawn) ------------------------
CONF_STRUCTURAL = "inferred-structurally-forced"
CONF_ENVELOPE = "inferred-envelope-set"
CONF_UNCONFIRMED = "unconfirmed-tentative"
CONF_UNMATCHED = "unmatched"

# --- Method labels -------------------------------------------------------------
METHOD_MUTUAL_UNIQUE = "mutual-unique"
METHOD_FORCED_PERFECT = "forced-perfect"
METHOD_ENVELOPE_SET = "envelope-set"

DATE_RULE_SAME_DAY = "same-day"

# Safety caps so a pathologically large same-day same-amount collision cannot
# blow up the perfect-matching *enumeration*. Stage 1 counts matchings by
# backtracking, which is exponential in the worst case; beyond these bounds the
# component degrades to envelope-set or needs-confirm. This fails safe — an
# unresolved transfer is surfaced for confirmation, never linked wrongly.
# (Stage 2 uses an iterative augmenting-path matcher — polynomial and with no
# recursion-depth limit — so it needs no cap and resolves large single-source
# fan-outs, the common "Main funds many envelopes" case, without crashing.)
_MAX_COMPONENT_LEGS = 16
_MAX_MATCH_STEPS = 20000


class _MatchingTooComplex(Exception):
    """Internal signal that perfect-matching enumeration hit its step cap."""


@dataclass(frozen=True)
class _Leg:
    """One side of a potential transfer, reduced to what matching needs."""

    txn_id: str
    account_id: str | None
    account_name: str | None
    date: str  # YYYY-MM-DD (posted date; time-of-day is a useless placeholder)
    cents: int  # signed; negative = debit (money out), positive = credit (money in)


@dataclass
class TransferProposal:
    """A proposed reconciliation outcome for one transfer leg or pair.

    A *link* fills both ``debit_*`` and ``credit_*`` sides. A still-unresolved
    single leg fills only the side matching its sign and lists the txn ids of its
    plausible counterparties in ``candidate_txn_ids`` so a confirmation surface
    can show them. ``amount_cents`` is the positive transfer magnitude.
    """

    status: str
    confidence: str
    amount_cents: int | None
    debit_txn_id: str | None = None
    credit_txn_id: str | None = None
    debit_account_id: str | None = None
    debit_account_name: str | None = None
    credit_account_id: str | None = None
    credit_account_name: str | None = None
    method: str | None = None
    date_rule: str | None = DATE_RULE_SAME_DAY
    candidates_before: int | None = None
    candidates_after: int | None = None
    candidate_txn_ids: tuple[str, ...] = ()
    explanation: str = ""


def propose_transfer_links(
    transactions: list[dict], *, is_transfer_key: str = "is_transfer"
) -> list[TransferProposal]:
    """Reconstruct internal-transfer counterparties from archived transactions.

    Only transactions already flagged as internal transfers (``is_transfer_key``
    truthy) are considered — coincidental equal-and-opposite real spending (a
    refund vs a purchase) must not be paired. Returns one proposal per transfer
    leg: forced links and envelope-set links carry both sides; ambiguous and
    unmatched legs are surfaced individually. The input list is not mutated.
    """
    legs, unmatched = _build_legs(transactions, is_transfer_key)
    proposals: list[TransferProposal] = list(unmatched)

    keyed = sorted(legs, key=lambda lg: (lg.date, abs(lg.cents)))
    for _, bucket_iter in groupby(keyed, key=lambda lg: (lg.date, abs(lg.cents))):
        proposals.extend(_resolve_bucket(list(bucket_iter)))
    return proposals


def _sign_hint(txn: dict) -> int:
    """Best-effort sign (-1 debit / +1 credit) when cents can't be parsed."""
    af = txn.get("amount_float")
    if isinstance(af, (int, float)):
        return -1 if af < 0 else 1
    return -1 if str(txn.get("amount") or "").strip().startswith("-") else 1


def _duplicate_ids(transactions: list[dict], is_transfer_key: str) -> set:
    """Return ids that appear on more than one flagged transfer (malformed)."""
    counts: dict = {}
    for txn in transactions:
        if not txn.get(is_transfer_key):
            continue
        tid = txn.get("id")
        if tid is None:
            continue
        counts[tid] = counts.get(tid, 0) + 1
    return {tid for tid, n in counts.items() if n > 1}


def _build_legs(
    transactions: list[dict], is_transfer_key: str
) -> tuple[list[_Leg], list[TransferProposal]]:
    """Split flagged transfers into matchable legs vs unmatchable singletons.

    A transfer that has a stable id but lacks a posted date or a whole-cents
    amount cannot be bucketed; rather than drop it silently it is returned as an
    ``unmatched`` proposal so the user still sees it. A transfer with **no id**
    is skipped entirely: every archived transaction has one (it is the archive's
    primary key, and the upsert refuses id-less rows), and a leg with no id
    cannot be referenced by a confirmation or stored as a link — the link store
    requires at least one non-null leg id — so there is nothing actionable to
    surface for it. A txn id that appears on **more than one** flagged leg is
    likewise skipped wholesale: a duplicate id is malformed (the primary key
    forbids it in real data), and the duplicates cannot be told apart, so
    emitting proposals for any of them would create rows that collide on the
    link store's per-id uniqueness — better to surface none than guess.
    """
    duplicate_ids = _duplicate_ids(transactions, is_transfer_key)
    legs: list[_Leg] = []
    bad: list[TransferProposal] = []
    for txn in transactions:
        if not txn.get(is_transfer_key):
            continue
        tid = txn.get("id")
        if tid is None:
            # No id to key a proposal on (see this function's docstring). The
            # archive's primary key guarantees ids, so this is defensive.
            continue
        if tid in duplicate_ids:
            # Malformed duplicate id — skip every occurrence (see docstring).
            continue
        cents = amount_to_cents(txn.get("amount"))
        posted = txn.get("posted")
        date = posted[:10] if isinstance(posted, str) and len(posted) >= 10 else None
        if cents is None or cents == 0 or date is None:
            is_debit = _sign_hint(txn) < 0
            reason = "no posted date" if date is None else "amount not whole cents"
            bad.append(
                TransferProposal(
                    status=STATUS_UNMATCHED,
                    confidence=CONF_UNMATCHED,
                    amount_cents=None,
                    debit_txn_id=tid if is_debit else None,
                    credit_txn_id=None if is_debit else tid,
                    debit_account_id=txn.get("account_id") if is_debit else None,
                    debit_account_name=txn.get("account_name") if is_debit else None,
                    credit_account_id=None if is_debit else txn.get("account_id"),
                    credit_account_name=None if is_debit else txn.get("account_name"),
                    date_rule=None,
                    explanation=f"Cannot reconcile ({reason}); unmatched.",
                )
            )
            continue
        legs.append(
            _Leg(tid, txn.get("account_id"), txn.get("account_name"), date, cents)
        )
    return legs, bad


def _resolve_bucket(bucket: list[_Leg]) -> list[TransferProposal]:
    """Resolve all legs sharing one (date, magnitude) bucket."""
    debits = [lg for lg in bucket if lg.cents < 0]
    credits = [lg for lg in bucket if lg.cents > 0]

    # No opposite leg at all in this date+amount bucket → nothing can match.
    if not debits or not credits:
        return [_unmatched(lg, n_opposite=0) for lg in bucket]

    # Candidate edge = opposite sign + different account. A same-account move is
    # impossible (you cannot transfer money to yourself), so it is never an edge.
    # A missing account id can't prove "different", so it conservatively yields
    # no edge — such a leg is then reported as unmatched (we never auto-link
    # across an account we cannot verify is distinct), with an explanation that
    # says so honestly rather than claiming no counterparty exists.
    # The id guard rejects an edge between two legs that share a txn id (defense
    # in depth; duplicate ids are already dropped at intake): pairing them would
    # emit debit_txn_id == credit_txn_id, which the link store's CHECK rejects.
    adj: dict[_Leg, list[_Leg]] = {lg: [] for lg in bucket}
    for d in debits:
        for c in credits:
            if d.account_id is not None and c.account_id is not None \
                    and d.account_id != c.account_id and d.txn_id != c.txn_id:
                adj[d].append(c)
                adj[c].append(d)

    proposals: list[TransferProposal] = []
    for comp_debits, comp_credits in _components(debits, credits, adj):
        proposals.extend(
            _resolve_component(comp_debits, comp_credits, adj, len(debits), len(credits))
        )
    return proposals


def _components(
    debits: list[_Leg], credits: list[_Leg], adj: dict[_Leg, list[_Leg]]
) -> list[tuple[list[_Leg], list[_Leg]]]:
    """Partition the bucket graph into connected components (debits, credits)."""
    seen: set[_Leg] = set()
    out: list[tuple[list[_Leg], list[_Leg]]] = []
    for start in (*debits, *credits):
        if start in seen:
            continue
        stack = [start]
        comp: list[_Leg] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            for nbr in adj[node]:
                if nbr not in seen:
                    stack.append(nbr)
        out.append(
            ([lg for lg in comp if lg.cents < 0], [lg for lg in comp if lg.cents > 0])
        )
    return out


def _resolve_component(
    debits: list[_Leg],
    credits: list[_Leg],
    adj: dict[_Leg, list[_Leg]],
    bucket_debits: int,
    bucket_credits: int,
) -> list[TransferProposal]:
    """Resolve one connected component via Stage 1, then Stage 2, then residue."""
    # A degree-0 leg (its only same-amount same-day partners are on its own
    # account) sits alone in its component and can never link.
    if not debits or not credits:
        return [
            _unmatched(lg, n_opposite=(bucket_credits if lg.cents < 0 else bucket_debits))
            for lg in (*debits, *credits)
        ]

    # Stage 1 — structurally forced (the one and only perfect matching).
    if len(debits) + len(credits) <= _MAX_COMPONENT_LEGS:
        forced = _unique_perfect_matching(debits, credits, adj)
        if forced is not None:
            single_edge = len(debits) == 1 and len(credits) == 1
            method = METHOD_MUTUAL_UNIQUE if single_edge else METHOD_FORCED_PERFECT
            return [
                _link(
                    d, c, CONF_STRUCTURAL, method,
                    bucket_debits, bucket_credits, adj,
                )
                for d, c in forced
            ]

    # Stage 2 — envelope set (single distinct source or single distinct dest).
    envelope = _envelope_set(debits, credits, adj)
    if envelope is not None:
        pairs, single_source, single_dest = envelope
        group_size = len(pairs)
        return [
            _link(
                d, c, CONF_ENVELOPE, METHOD_ENVELOPE_SET,
                bucket_debits, bucket_credits, adj,
                envelope_source=single_source, envelope_dest=single_dest,
                group_size=group_size,
            )
            for d, c in pairs
        ]

    # Residue — genuinely ambiguous; surface each leg with its candidates.
    return [
        _needs_confirm(lg, adj, bucket_debits, bucket_credits)
        for lg in (*debits, *credits)
    ]


def _unique_perfect_matching(
    debits: list[_Leg], credits: list[_Leg], adj: dict[_Leg, list[_Leg]]
) -> list[tuple[_Leg, _Leg]] | None:
    """Return the sole perfect matching of the component, or None if not unique.

    "Unique" means exactly one way to pair every debit with a distinct credit
    using only candidate edges. Enumeration stops as soon as a second matching is
    found (ambiguous) or the step cap trips (too complex → treated as not unique,
    which fails safe to a lower-confidence stage).
    """
    if len(debits) != len(credits) or not debits:
        return None

    cred_idx = {c: i for i, c in enumerate(credits)}
    dadj = [[cred_idx[c] for c in adj[d]] for d in debits]
    n = len(debits)
    used = [False] * n
    current = [-1] * n
    found: list[tuple[int, ...]] = []
    steps = [0]

    def backtrack(i: int) -> None:
        if len(found) >= 2:
            return
        steps[0] += 1
        if steps[0] > _MAX_MATCH_STEPS:
            raise _MatchingTooComplex
        if i == n:
            found.append(tuple(current))
            return
        for ci in dadj[i]:
            if used[ci]:
                continue
            used[ci] = True
            current[i] = ci
            backtrack(i + 1)
            used[ci] = False
            current[i] = -1
            if len(found) >= 2:
                return

    try:
        backtrack(0)
    except _MatchingTooComplex:
        return None

    if len(found) != 1:
        return None
    chosen = found[0]
    return [(debits[i], credits[chosen[i]]) for i in range(n)]


def _envelope_set(
    debits: list[_Leg], credits: list[_Leg], adj: dict[_Leg, list[_Leg]]
) -> tuple[list[tuple[_Leg, _Leg]], bool, bool] | None:
    """Resolve a component whose source set (or dest set) is a single account.

    When every debit is from one account, each credit's source is known even if
    which exact debit funded which credit is arbitrary; symmetrically for a
    single destination. Requires a balanced, fully matchable component so the
    arbitrary pairing is a valid one-to-one assignment. Returns the chosen pairs
    plus whether the source and/or destination was the single-account side.
    """
    if len(debits) != len(credits):
        return None
    single_source = len({d.account_id for d in debits}) == 1
    single_dest = len({c.account_id for c in credits}) == 1
    if not (single_source or single_dest):
        return None
    pairs = _any_perfect_matching(debits, credits, adj)
    if pairs is None:
        return None
    return pairs, single_source, single_dest


def _any_perfect_matching(
    debits: list[_Leg], credits: list[_Leg], adj: dict[_Leg, list[_Leg]]
) -> list[tuple[_Leg, _Leg]] | None:
    """Find any one perfect matching via iterative augmenting paths (Kuhn's).

    Each free debit grows a breadth-first alternating tree to a free credit and
    flips it. The search is **iterative** — a single-source fan-out to hundreds
    of envelopes produces augmenting paths as long as the component, and a
    recursive search would overflow the stack on such a (legitimate) input.
    """
    if len(debits) != len(credits):
        return None
    cred_idx = {c: i for i, c in enumerate(credits)}
    dadj = [[cred_idx[c] for c in adj[d]] for d in debits]
    n_d, n_c = len(debits), len(credits)
    match_credit = [-1] * n_c  # credit index -> matched debit index
    match_debit = [-1] * n_d  # debit index  -> matched credit index

    for start in range(n_d):
        parent_debit = [-1] * n_c  # credit -> debit that reached it this search
        visited = [False] * n_c
        queue: deque[int] = deque([start])
        found = -1
        while queue and found == -1:
            u = queue.popleft()
            for v in dadj[u]:
                if visited[v]:
                    continue
                visited[v] = True
                parent_debit[v] = u
                if match_credit[v] == -1:
                    found = v
                    break
                queue.append(match_credit[v])
        if found == -1:
            continue
        # Flip the alternating path from the free credit back to `start`.
        v = found
        while v != -1:
            u = parent_debit[v]
            nxt = match_debit[u]
            match_credit[v] = u
            match_debit[u] = v
            v = nxt

    if any(m == -1 for m in match_debit):
        return None
    return [(debits[match_credit[v]], credits[v]) for v in range(n_c)]


# --- Proposal builders ---------------------------------------------------------


def _fmt(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _name(leg: _Leg) -> str:
    return leg.account_name or leg.account_id or "unknown account"


def _link(
    debit: _Leg,
    credit: _Leg,
    confidence: str,
    method: str,
    bucket_debits: int,
    bucket_credits: int,
    adj: dict[_Leg, list[_Leg]],
    *,
    envelope_source: bool = False,
    envelope_dest: bool = False,
    group_size: int = 0,
) -> TransferProposal:
    magnitude = abs(debit.cents)
    # before = same-day same-amount opposite legs; after = those on a different
    # account (the candidates the matcher actually weighed for this leg).
    before = bucket_credits
    after = len(adj[debit])
    if method == METHOD_ENVELOPE_SET:
        determined = []
        if envelope_source:
            determined.append(f"source {_name(debit)}")
        if envelope_dest:
            determined.append(f"destination {_name(credit)}")
        explanation = (
            f"Same-day {_fmt(magnitude)} transfer; {' and '.join(determined)} "
            f"determined (single-{'source' if envelope_source else 'destination'} "
            f"group of {group_size}); exact debit-to-credit pairing arbitrary "
            f"within the group."
        )
    elif method == METHOD_MUTUAL_UNIQUE:
        explanation = (
            f"Same-day exact-amount match: {_fmt(magnitude)} from {_name(debit)} "
            f"to {_name(credit)}. Sole mutually-unique pairing "
            f"({before} same-amount, {after} after excluding same-account transfers)."
        )
    else:  # forced-perfect
        explanation = (
            f"Same-day exact-amount match: {_fmt(magnitude)} from {_name(debit)} "
            f"to {_name(credit)}. Only possible pairing of its group "
            f"({before} same-amount, {after} after excluding same-account transfers)."
        )
    return TransferProposal(
        status=STATUS_INFERRED,
        confidence=confidence,
        amount_cents=magnitude,
        debit_txn_id=debit.txn_id,
        credit_txn_id=credit.txn_id,
        debit_account_id=debit.account_id,
        debit_account_name=debit.account_name,
        credit_account_id=credit.account_id,
        credit_account_name=credit.account_name,
        method=method,
        date_rule=DATE_RULE_SAME_DAY,
        candidates_before=before,
        candidates_after=after,
        explanation=explanation,
    )


def _needs_confirm(
    leg: _Leg,
    adj: dict[_Leg, list[_Leg]],
    bucket_debits: int,
    bucket_credits: int,
) -> TransferProposal:
    candidates = adj[leg]
    is_debit = leg.cents < 0
    before = bucket_credits if is_debit else bucket_debits
    after = len(candidates)
    explanation = (
        f"Same-day {_fmt(abs(leg.cents))} transfer with {after} equally-likely "
        f"counterpart{'s' if after != 1 else ''}; needs confirmation."
    )
    return TransferProposal(
        status=STATUS_UNCONFIRMED,
        confidence=CONF_UNCONFIRMED,
        amount_cents=abs(leg.cents),
        debit_txn_id=leg.txn_id if is_debit else None,
        credit_txn_id=None if is_debit else leg.txn_id,
        debit_account_id=leg.account_id if is_debit else None,
        debit_account_name=leg.account_name if is_debit else None,
        credit_account_id=None if is_debit else leg.account_id,
        credit_account_name=None if is_debit else leg.account_name,
        method=None,
        date_rule=DATE_RULE_SAME_DAY,
        candidates_before=before,
        candidates_after=after,
        candidate_txn_ids=tuple(c.txn_id for c in candidates),
        explanation=explanation,
    )


def _unmatched(leg: _Leg, *, n_opposite: int) -> TransferProposal:
    is_debit = leg.cents < 0
    # n_opposite is how many same-day same-amount opposite legs existed in the
    # bucket. Zero means there was genuinely no counterparty; a positive count
    # means one or more existed but none formed a valid edge (all on the same
    # account, or on an account we could not verify is distinct). Say which —
    # never assert "no counterparty" when same-amount opposite legs were present.
    if n_opposite > 0:
        explanation = (
            f"Same-day {_fmt(abs(leg.cents))} transfer(s) exist but none on a "
            f"verifiably different account; unmatched."
        )
    else:
        explanation = (
            f"No same-day {_fmt(abs(leg.cents))} counterparty on a different "
            f"account; unmatched."
        )
    return TransferProposal(
        status=STATUS_UNMATCHED,
        confidence=CONF_UNMATCHED,
        amount_cents=abs(leg.cents),
        debit_txn_id=leg.txn_id if is_debit else None,
        credit_txn_id=None if is_debit else leg.txn_id,
        debit_account_id=leg.account_id if is_debit else None,
        debit_account_name=leg.account_name if is_debit else None,
        credit_account_id=None if is_debit else leg.account_id,
        credit_account_name=None if is_debit else leg.account_name,
        method=None,
        date_rule=DATE_RULE_SAME_DAY,
        candidates_before=n_opposite,
        candidates_after=0,
        explanation=explanation,
    )


def summarize(proposals: list[TransferProposal]) -> dict:
    """Tally proposals for a reconcile report.

    Status and confidence are kept in separate sub-maps because they share label
    text (``"unmatched"`` is both a status and a confidence value) — flattening
    them into one dict would collide and double-count.
    """
    by_status: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    links = 0
    for p in proposals:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        by_confidence[p.confidence] = by_confidence.get(p.confidence, 0) + 1
        if p.debit_txn_id is not None and p.credit_txn_id is not None:
            links += 1
    return {
        "proposals": len(proposals),
        "links": links,
        "by_status": by_status,
        "by_confidence": by_confidence,
    }
