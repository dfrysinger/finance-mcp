"""Envelope sufficiency / forecast: will each envelope cover what's coming?

A deterministic projection that sits on top of the budget config's recurring
calendar. For one forward window it answers, per envelope: starting from the
current balance and applying the scheduled inflows and known upcoming bills in
date order, does the running balance ever go below zero — and if so, on what day?

Design decisions worth knowing:

- **Integer cents, never floats.** Balances and calendar amounts are carried as
  integer cents (the authoritative decimal *string* balance parsed via
  :func:`finance_mcp.normalize.amount_to_cents`), so a projection never flips a
  verdict on binary-float drift. Cents are converted to a 2-dp float only at the
  output boundary.
- **Verdict from the running minimum, not the end balance.** An envelope that
  dips below zero mid-window and recovers via a later inflow is still
  ``at_risk`` — the danger is the dip, not where the month ends.
- **Money is conserved.** An internal scheduled transfer (one with a source
  envelope) debits the source and credits the destination, so a paycheck fanned
  out from a hub can never credit the spending envelopes without drawing the hub
  down. An external inflow (no source) is a credit only.
- **Same-day funding is surfaced, not hidden.** When an inflow and a bill land on
  the same day the realistic walk applies the inflow first (a scheduled
  allocation is meant to fund that day's bills). But if that inflow is
  *load-bearing* — the day's bills would overdraw without it — the envelope is
  flagged ``same_day_funding_dependent`` so the user knows solvency depends on
  intraday settlement timing the bank, not the budget, controls.
- **Honest unknowns.** If *any* of an envelope's accounts has no parseable synced
  balance, the whole envelope is ``balance_unknown`` and gets no verdict. A
  partially-known envelope is never silently treated as if the missing account
  held zero.
- **Schedule-based, not reconciled.** This projects *scheduled* occurrences in the
  window; it does not check whether a bill already actually posted (that is the
  subscription audit). A bill paid early is therefore both reflected in the
  current balance and projected again — a pessimistic double-count that can only
  produce a false *at_risk*, never a false *sufficient* (the safe direction).
"""

from __future__ import annotations

from datetime import date, timedelta

from .budget_config import BudgetConfig, monthly_dates
from .normalize import amount_to_cents

# Default forward window length. A fixed duration (rather than a calendar
# boundary such as "end of next month") keeps the projected occurrence counts
# from swinging with the day the report happens to be run.
DEFAULT_HORIZON_DAYS = 60


def _cents(amount: int) -> float:
    return round(amount / 100, 2)


def _monthly_dates(day: int, as_of: date, through: date) -> list[date]:
    """Concrete due-dates for a monthly day-of-month within the closed window.

    Thin wrapper over the canonical calendar expansion in ``budget_config`` so
    the forecast and the allocation audit date every occurrence identically.
    """
    return monthly_dates(day, as_of, through)


def _events_for(
    config: BudgetConfig, env_name: str, as_of: date, through: date
) -> list[tuple[date, int, bool, str]]:
    """All dated cash events for one envelope: (date, delta_cents, is_outflow, name).

    A negative delta is an outflow (a bill, or a transfer *out* of this envelope);
    a positive delta is an inflow (a transfer *into* this envelope).
    """
    events: list[tuple[date, int, bool, str]] = []
    for bill in config.recurring:
        if bill.envelope == env_name:
            for d in _monthly_dates(bill.day, as_of, through):
                events.append((d, -bill.amount_cents, True, bill.name))
    for t in config.scheduled_transfers:
        if t.to_envelope == env_name:
            for d in _monthly_dates(t.day, as_of, through):
                events.append((d, t.amount_cents, False, t.name))
        if t.from_envelope == env_name:
            for d in _monthly_dates(t.day, as_of, through):
                events.append((d, -t.amount_cents, True, t.name))
    return events


def _walk(
    current: int, events: list[tuple[date, int, bool, str]], *, inflows_first: bool
) -> tuple[int, int, date | None]:
    """Walk events in date order from ``current``, sampling the running minimum.

    Returns ``(end_balance, min_balance, first_negative_date)``. Within one day,
    inflows are ordered before outflows when ``inflows_first`` (the realistic
    walk); the conservative walk reverses that to expose same-day funding
    dependence. The name is a final tie-break so the order is fully deterministic.
    """
    def key(e: tuple[date, int, bool, str]):
        d, _delta, is_out, name = e
        same_day_rank = is_out if inflows_first else not is_out
        return (d, same_day_rank, name)

    running = current
    min_bal = running
    first_negative: date | None = None
    for d, delta, _is_out, _name in sorted(events, key=key):
        running += delta
        if running < min_bal:
            min_bal = running
        if running < 0 and first_negative is None:
            first_negative = d
    return running, min_bal, first_negative


def forecast(
    config: BudgetConfig,
    account_balances: dict[str, int],
    *,
    as_of: date,
    through: date,
) -> dict:
    """Project each envelope's sufficiency over ``[as_of, through]``. Pure function.

    ``account_balances`` maps an account id to its current balance in integer
    cents and contains *only* accounts whose balance is known; an account absent
    from the map is treated as unknown (never as zero), making any envelope that
    owns it ``balance_unknown``.

    A ``sufficient`` verdict carries ``relies_on_projected_income`` when it would
    flip to a shortfall if none of the scheduled inflows actually arrive (i.e. if
    every projected inflow has already posted into the starting balance). This
    bounds the one direction in which schedule-vs-actuals drift is *unsafe*:
    double-counting an already-posted inflow optimistically overstates funds.
    """
    if through < as_of:
        raise ValueError(
            f"forecast window is empty: through ({through}) is before as_of ({as_of})"
        )

    envelopes_out: list[dict] = []
    n_at_risk = n_sufficient = n_unknown = 0

    for env in config.envelopes:
        events = _events_for(config, env.name, as_of, through)
        total_in = sum(delta for _d, delta, _o, _n in events if delta > 0)
        total_out = -sum(delta for _d, delta, _o, _n in events if delta < 0)
        n_inflows = sum(1 for _d, delta, _o, _n in events if delta > 0)
        n_outflows = sum(1 for _d, delta, _o, _n in events if delta < 0)

        known = all(acct in account_balances for acct in env.accounts)
        if not known:
            n_unknown += 1
            envelopes_out.append(
                {
                    "envelope": env.name,
                    "verdict": "balance_unknown",
                    "current_balance": None,
                    "total_in": _cents(total_in),
                    "total_out": _cents(total_out),
                    "projected_end_balance": None,
                    "projected_min_balance": None,
                    "at_risk_date": None,
                    "shortfall": None,
                    "same_day_funding_dependent": False,
                    "relies_on_projected_income": False,
                    "n_inflows": n_inflows,
                    "n_outflows": n_outflows,
                }
            )
            continue

        current = sum(account_balances[acct] for acct in env.accounts)
        end_bal, min_bal, first_neg = _walk(current, events, inflows_first=True)
        _, min_conservative, _ = _walk(current, events, inflows_first=False)

        # Pessimistic-reconciliation walk: the starting balance is the current
        # synced balance, so any scheduled inflow that already posted is *already*
        # reflected in it; crediting that inflow again would optimistically
        # overstate funds and could turn a real shortfall into a false
        # "sufficient". We can't tell which inflows already posted here (that is
        # the subscription/actuals audit's job), so we bound the risk: assume the
        # worst case where every projected inflow has already posted by dropping
        # all inflow events while keeping every outflow (an early-paid bill is the
        # *safe* direction — it can only ever make this stricter).
        outflow_events = [e for e in events if e[1] < 0]
        _, min_no_inflows, _ = _walk(current, outflow_events, inflows_first=True)

        at_risk = min_bal < 0
        # An envelope already underwater at the start is at risk as of today;
        # otherwise the at-risk date is the first day an event drives it negative.
        at_risk_date = as_of if current < 0 else first_neg
        same_day_dependent = (min_bal >= 0) and (min_conservative < 0)
        relies_on_projected_income = (min_bal >= 0) and (min_no_inflows < 0)

        if at_risk:
            n_at_risk += 1
            verdict = "at_risk"
        else:
            n_sufficient += 1
            verdict = "sufficient"

        envelopes_out.append(
            {
                "envelope": env.name,
                "verdict": verdict,
                "current_balance": _cents(current),
                "total_in": _cents(total_in),
                "total_out": _cents(total_out),
                "projected_end_balance": _cents(end_bal),
                "projected_min_balance": _cents(min_bal),
                "at_risk_date": at_risk_date.isoformat() if at_risk_date else None,
                "shortfall": _cents(-min_bal) if min_bal < 0 else 0.0,
                "same_day_funding_dependent": same_day_dependent,
                "relies_on_projected_income": relies_on_projected_income,
                "n_inflows": n_inflows,
                "n_outflows": n_outflows,
            }
        )

    return {
        "as_of": as_of.isoformat(),
        "through": through.isoformat(),
        "envelopes": envelopes_out,
        "summary": {
            "at_risk": n_at_risk,
            "sufficient": n_sufficient,
            "balance_unknown": n_unknown,
        },
    }


def account_balances_cents(accounts: list[dict]) -> dict[str, int]:
    """Map account id → current balance in integer cents, for known balances only.

    An account whose authoritative ``balance`` string is missing or unparseable
    is omitted (not defaulted to zero), so the forecast can honestly report the
    owning envelope as ``balance_unknown``.
    """
    out: dict[str, int] = {}
    for acct in accounts:
        aid = acct.get("account_id")
        if aid is None:
            continue
        cents = amount_to_cents(acct.get("balance"))
        if cents is not None:
            out[aid] = cents
    return out


def forecast_report(
    config: BudgetConfig,
    *,
    as_of: date | None = None,
    through: date | None = None,
) -> dict:
    """Load current account balances from the archive, then run the forecast.

    ``as_of`` defaults to today and ``through`` to a fixed horizon past it.
    """
    from . import store

    as_of = as_of or date.today()
    through = through or (as_of + timedelta(days=DEFAULT_HORIZON_DAYS))
    view = store.load_archive_view()
    balances = account_balances_cents(view.get("accounts", []))
    return forecast(config, balances, as_of=as_of, through=through)
