"""Budget config: the single source of truth for a user's envelope budget.

The envelope→account map, monthly targets, and (in later pieces) the recurring
calendar are *data*, not code. They live in one user-supplied JSON file outside
the repo, so nothing about one person's budget is baked into the source — that
separation is what lets anyone run this system against their own budget.

This module parses and validates that file. It deliberately fails loud: a typo
that would silently misroute spend (an account claimed by two envelopes, a
target that is not a whole number of cents) raises :class:`BudgetConfigError`
with a specific message rather than producing a quietly-wrong budget.
"""

from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Forward-compatible: later pieces (forecast, allocation audit, subscription
# audit) add a "recurring" calendar to the same file. Unknown keys are ignored
# here so an older reader does not reject a config carrying newer sections.
SUPPORTED_VERSION = 1

# Recurring calendar cadences the projector understands. An unsupported cadence
# is rejected at parse time rather than silently mis-forecast.
SUPPORTED_CADENCES = frozenset({"monthly"})

# Lifecycle of a recurring bill. ``active`` (the default) is a live bill expected
# to keep charging. ``canceling`` means a cancellation was attempted and the bill
# is no longer expected to charge — a charge on/after ``cancel_effective`` is a
# warning that the cancellation may not have taken. ``canceled`` is a confirmed
# cancellation; any charge on/after ``cancel_effective`` is an anomaly. For the
# two non-active states a charge coming back is the alert, and the bill's absence
# is expected rather than reported missing.
LIFECYCLE_STATES = frozenset({"active", "canceling", "canceled"})


class BudgetConfigError(Exception):
    """A budget config file is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Envelope:
    """One spending envelope: a human name bound to one or more account ids.

    ``monthly_target_cents`` is the planned monthly spend in integer cents, or
    ``None`` when the envelope has no target (e.g. a hub or savings account that
    is tracked but not budgeted against). Money is carried in integer cents
    throughout so target-vs-spend comparisons never suffer binary-float drift.
    """

    name: str
    accounts: tuple[str, ...]
    monthly_target_cents: int | None = None
    role: str | None = None


@dataclass(frozen=True)
class RecurringBill:
    """One recurring outflow (a bill), optionally tied to a paying envelope.

    ``envelope`` is the canonical name of the paying envelope, resolved at parse
    time, or ``None`` for a standalone subscription that is only watched for
    posting and is not attributed to any budget envelope. ``day`` is the nominal
    day-of-month it is due (1–31); the projector clamps it to each month's
    actual length. ``amount_cents`` is positive. ``match`` is a keyword
    (normalized to tokens and matched as a token-subset against a transaction's
    merchant identity — description and payee only; ``memo`` is excluded except
    as a sole fallback when both are empty) that pins the bill to its merchant so
    the subscription audit can reliably tell whether the charge posted.

    A bill must carry at least one of ``envelope`` or ``match``: with neither it
    could not be matched to any charge. A bill with no ``envelope`` therefore
    always has a ``match`` keyword (enforced at parse time), so it is matched by
    merchant and never falls through to an envelope binding that does not exist.

    ``lifecycle`` is one of :data:`LIFECYCLE_STATES`. When it is ``canceling`` or
    ``canceled``, ``cancel_effective`` is the date from which the bill is no
    longer expected to charge (enforced present at parse time for those states);
    a matching charge on/after that date is surfaced as a "came back" alert and
    the bill's absence is no longer reported as missing. ``active`` bills always
    have ``cancel_effective`` of ``None``.

    ``variable`` marks a bill whose amount legitimately changes every cycle (a
    usage-based charge like metered insurance, or an escrow-adjusted mortgage).
    When ``True`` the subscription audit matches a charge to the bill by merchant
    keyword (or envelope) and due date alone, ignoring the amount entirely, and
    reports the most recent charge's actual amount rather than the stored
    ``amount_cents``. ``amount_cents`` is still required (it is the expected /
    typical figure shown when no charge has matched yet) but no longer gates the
    match, so a price swing never reads as a missing bill. ``False`` (the
    default) keeps the exact-or-within-tolerance amount match.
    """

    name: str
    envelope: str | None
    amount_cents: int
    cadence: str
    day: int
    match: str | None = None
    lifecycle: str = "active"
    cancel_effective: date | None = None
    variable: bool = False


@dataclass(frozen=True)
class ScheduledTransfer:
    """One scheduled inflow into an envelope each cycle.

    ``to_envelope`` is the canonical destination name. ``from_envelope`` is the
    canonical source name when the transfer is *internal* (e.g. a paycheck hub
    fanning out to a category envelope) — the projector then debits the source
    and credits the destination, conserving money. When ``from_envelope`` is
    ``None`` the transfer is an external inflow (a direct deposit), credit only.
    """

    name: str
    to_envelope: str
    amount_cents: int
    cadence: str
    day: int
    from_envelope: str | None = None


@dataclass(frozen=True)
class DebtAccount:
    """One liability account whose payments are audited for trouble.

    A debt (loan, mortgage, financed purchase) is identified by its own
    ``account_id`` because the feed carries no asset/liability type. ``label`` is
    the human name shown on the red-flags view. ``expected_amount_cents`` is the
    normal monthly payment in integer cents (``None`` when unknown); it is shown
    as context only and does not affect detection: the payment amount is never
    compared. ``due_day`` is the nominal day of month the payment posts; clamped to
    each audited month, it gates that month's missed check so a month is only
    evaluated once its due date plus grace has passed — no month, current or
    prior, is flagged before its payment is actually overdue.
    """

    account_id: str
    label: str
    expected_amount_cents: int | None = None
    due_day: int | None = None


@dataclass(frozen=True)
class BudgetConfig:
    version: int
    envelopes: tuple[Envelope, ...]
    recurring: tuple[RecurringBill, ...] = ()
    scheduled_transfers: tuple[ScheduledTransfer, ...] = ()
    recurring_amount_tolerance_pct: float = 0.0
    debt_accounts: tuple[DebtAccount, ...] = ()

    def account_index(self) -> dict[str, Envelope]:
        """Map every configured account id to its owning envelope.

        ``parse_config`` has already rejected an account claimed by two
        envelopes, so this mapping is unambiguous.
        """
        index: dict[str, Envelope] = {}
        for env in self.envelopes:
            for acct in env.accounts:
                index[acct] = env
        return index


def monthly_dates(day: int, start: date, end: date) -> list[date]:
    """Concrete dates for a monthly day-of-month within the closed window.

    ``day`` is clamped to each month's actual length (so 31 lands on Feb 28/29),
    and only occurrences inside ``[start, end]`` are returned. This is the single
    expansion of the recurring calendar's monthly cadence: the forecast
    projection and the allocation audit both consume it, so a scheduled
    occurrence is dated identically wherever it is reasoned about.
    """
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last = calendar.monthrange(y, m)[1]
        d = date(y, m, min(day, last))
        if start <= d <= end:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _money_to_cents(
    value: Any, *, where: str, label: str = "amount", allow_zero: bool = False
) -> int:
    """Parse a JSON money value to integer cents, failing loud on anything fishy.

    A value that is not a finite, whole number of cents (e.g. ``10.005``) is
    rejected rather than rounded, so the budget never silently invents a
    fraction of a cent the user did not write. Negative is always rejected;
    zero is rejected unless ``allow_zero``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BudgetConfigError(
            f"{where}: {label} must be a number, got {type(value).__name__}"
        )
    try:
        dollars = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetConfigError(f"{where}: {label} {value!r} is not a number") from exc
    if not dollars.is_finite():
        raise BudgetConfigError(f"{where}: {label} must be finite, got {value!r}")
    cents = dollars * 100
    if cents != cents.to_integral_value():
        raise BudgetConfigError(
            f"{where}: {label} {value!r} is not a whole number of cents"
        )
    cents_int = int(cents)
    if cents_int < 0:
        raise BudgetConfigError(f"{where}: {label} must not be negative, got {value!r}")
    if cents_int == 0 and not allow_zero:
        raise BudgetConfigError(
            f"{where}: {label} must be greater than zero, got {value!r}"
        )
    return cents_int


def _target_to_cents(value: Any, *, envelope: str) -> int | None:
    """Convert a JSON monthly target to integer cents, or None if absent."""
    if value is None:
        return None
    return _money_to_cents(
        value, where=f"envelope {envelope!r}", label="monthly_target", allow_zero=True
    )


def _require_name(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BudgetConfigError(f"{where}: name must be a non-empty string")
    return value.strip()


def _require_cadence(value: Any, *, where: str) -> str:
    if isinstance(value, str) and value.strip().lower() in SUPPORTED_CADENCES:
        return value.strip().lower()
    raise BudgetConfigError(
        f"{where}: cadence must be one of {sorted(SUPPORTED_CADENCES)}, got {value!r}"
    )


def _optional_match(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BudgetConfigError(
            f"{where}: match must be a non-empty string when present, got {value!r}"
        )
    cleaned = value.strip()
    # The subscription audit normalizes merchant text by lowercasing, dropping
    # non-ASCII-alnum separators, and dropping pure-digit tokens, so a keyword
    # with no surviving letter (e.g. "76", "365", or punctuation) would match
    # nothing and report the bill missing forever. Lowercase first (mirroring
    # that normalization) before checking for a surviving ASCII letter, so the
    # check is exactly equivalent to "normalization yields >= 1 token" — a few
    # non-ASCII letters case-fold to ASCII (e.g. U+0130, U+212A) and must be
    # accepted, not rejected.
    if not any(c.isascii() and c.isalpha() for c in cleaned.lower()):
        raise BudgetConfigError(
            f"{where}: match must contain at least one letter so it can match a "
            f"merchant (a digits-only keyword matches nothing), got {value!r}"
        )
    return cleaned


def _require_day(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetConfigError(f"{where}: day must be an integer 1-31, got {value!r}")
    if not 1 <= value <= 31:
        raise BudgetConfigError(f"{where}: day must be between 1 and 31, got {value}")
    return value


def _require_lifecycle(value: Any, *, where: str) -> str:
    # Absent lifecycle defaults to "active" so existing configs (and anyone who
    # never cancels anything) parse unchanged.
    if value is None:
        return "active"
    if isinstance(value, str) and value.strip().lower() in LIFECYCLE_STATES:
        return value.strip().lower()
    raise BudgetConfigError(
        f"{where}: lifecycle must be one of {sorted(LIFECYCLE_STATES)}, got {value!r}"
    )


def _optional_date(value: Any, *, where: str, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BudgetConfigError(
            f"{where}: {field} must be an ISO date string (YYYY-MM-DD), got {value!r}"
        )
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BudgetConfigError(
            f"{where}: {field} {value!r} is not a valid ISO date (YYYY-MM-DD)"
        ) from exc


def _require_variable(value: Any, *, where: str) -> bool:
    # Absent defaults to False so existing bills (and the common fixed-amount
    # case) parse unchanged. Only a real JSON boolean is accepted — a string or
    # number here is a malformed config, not a truthy guess, so it fails loud.
    if value is None:
        return False
    if not isinstance(value, bool):
        raise BudgetConfigError(
            f"{where}: variable must be true or false, got {value!r}"
        )
    return value


def _parse_tolerance_pct(value: Any) -> float:
    """Parse the global recurring-amount drift allowance as a 0..1 fraction.

    Absent defaults to ``0.0`` (exact-amount matching, the prior behavior). The
    value is a fraction of each bill's amount a charge may drift and still count
    as that bill — e.g. ``0.1`` lets a $50 bill match a $45–$55 charge, which
    absorbs escrow nudges and rounding without per-bill configuration. Rejected
    rather than clamped when out of range so a typo (``10`` meaning 10%) fails
    loud instead of silently allowing a 1000% tolerance that would match almost
    anything.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetConfigError(
            "recurring_amount_tolerance_pct must be a number between 0 and 1 "
            f"(a fraction), got {value!r}"
        )
    pct = float(value)
    if not math.isfinite(pct):
        raise BudgetConfigError(
            f"recurring_amount_tolerance_pct must be finite, got {value!r}"
        )
    if not 0.0 <= pct <= 1.0:
        raise BudgetConfigError(
            "recurring_amount_tolerance_pct must be between 0 and 1 (a fraction, "
            f"e.g. 0.1 for 10%), got {value!r}"
        )
    return pct


def parse_config(data: Any) -> BudgetConfig:
    """Validate an already-parsed JSON object into a :class:`BudgetConfig`.

    Separated from file IO so the validation rules are unit-testable without
    touching disk. Every failure raises :class:`BudgetConfigError` with a
    message naming the offending envelope or field.
    """
    if not isinstance(data, dict):
        raise BudgetConfigError("budget config must be a JSON object")

    version = data.get("version", SUPPORTED_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise BudgetConfigError(f"version must be an integer, got {version!r}")
    if version > SUPPORTED_VERSION:
        raise BudgetConfigError(
            f"budget config version {version} is newer than this tool supports "
            f"(max {SUPPORTED_VERSION}); upgrade finance-mcp"
        )

    raw_envelopes = data.get("envelopes")
    if not isinstance(raw_envelopes, list):
        raise BudgetConfigError("budget config must have an 'envelopes' list")

    envelopes: list[Envelope] = []
    seen_names: set[str] = set()
    account_owner: dict[str, str] = {}

    for i, raw in enumerate(raw_envelopes):
        where = f"envelopes[{i}]"
        if not isinstance(raw, dict):
            raise BudgetConfigError(f"{where} must be an object")

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BudgetConfigError(f"{where}: name must be a non-empty string")
        name = name.strip()
        key = name.lower()
        if key in seen_names:
            raise BudgetConfigError(f"duplicate envelope name {name!r}")
        seen_names.add(key)

        raw_accounts = raw.get("accounts")
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise BudgetConfigError(
                f"envelope {name!r}: accounts must be a non-empty list of account ids"
            )
        accounts: list[str] = []
        for acct in raw_accounts:
            if not isinstance(acct, str) or not acct.strip():
                raise BudgetConfigError(
                    f"envelope {name!r}: every account id must be a non-empty string"
                )
            acct = acct.strip()
            if acct in account_owner:
                raise BudgetConfigError(
                    f"account {acct!r} is claimed by two envelopes "
                    f"({account_owner[acct]!r} and {name!r}); each account belongs "
                    "to exactly one envelope"
                )
            account_owner[acct] = name
            accounts.append(acct)

        target_cents = _target_to_cents(raw.get("monthly_target"), envelope=name)

        role = raw.get("role")
        if role is not None and not isinstance(role, str):
            raise BudgetConfigError(f"envelope {name!r}: role must be a string")

        envelopes.append(
            Envelope(
                name=name,
                accounts=tuple(accounts),
                monthly_target_cents=target_cents,
                role=role.strip() if isinstance(role, str) else None,
            )
        )

    recurring = _parse_recurring(data, envelopes)
    scheduled = _parse_scheduled_transfers(data, envelopes)
    debt_accounts = _parse_debt_accounts(data)

    return BudgetConfig(
        version=version,
        envelopes=tuple(envelopes),
        recurring=recurring,
        scheduled_transfers=scheduled,
        recurring_amount_tolerance_pct=_parse_tolerance_pct(
            data.get("recurring_amount_tolerance_pct")
        ),
        debt_accounts=debt_accounts,
    )


def _envelope_resolver(envelopes: list[Envelope]):
    """Build a resolver that maps a calendar entry's envelope reference to the
    envelope's canonical name, using the same case-insensitive rule the envelope
    names are de-duplicated by. Resolving once at parse time guarantees that
    validation and the later projection bind to the same envelope."""
    canonical = {env.name.lower(): env.name for env in envelopes}

    def resolve(ref: Any, *, where: str, field: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise BudgetConfigError(
                f"{where}: {field} must be a non-empty envelope name"
            )
        got = canonical.get(ref.strip().lower())
        if got is None:
            raise BudgetConfigError(
                f"{where}: {field} {ref.strip()!r} does not match any configured envelope"
            )
        return got

    return resolve


def _parse_debt_accounts(data: dict) -> tuple[DebtAccount, ...]:
    """Validate the optional ``debt_accounts`` list into :class:`DebtAccount`s.

    Absent or empty yields ``()`` so existing configs parse unchanged. Each entry
    needs a non-empty ``account_id`` (unique across the list) and ``label``;
    ``expected_amount`` and ``due_day`` are optional. A duplicate account id is
    rejected rather than silently collapsed, since two rows for one debt would
    double-count its flags.
    """
    raw = data.get("debt_accounts", [])
    if not isinstance(raw, list):
        raise BudgetConfigError("budget config 'debt_accounts' must be a list")
    out: list[DebtAccount] = []
    seen: dict[str, str] = {}
    for i, row in enumerate(raw):
        where = f"debt_accounts[{i}]"
        if not isinstance(row, dict):
            raise BudgetConfigError(f"{where} must be an object")
        acct = row.get("account_id")
        if not isinstance(acct, str) or not acct.strip():
            raise BudgetConfigError(
                f"{where}: account_id must be a non-empty string"
            )
        acct = acct.strip()
        label = row.get("label")
        if not isinstance(label, str) or not label.strip():
            raise BudgetConfigError(f"{where}: label must be a non-empty string")
        label = label.strip()
        if acct in seen:
            raise BudgetConfigError(
                f"debt account {acct!r} is listed twice "
                f"({seen[acct]!r} and {label!r}); each account belongs once"
            )
        seen[acct] = label
        ctx = f"debt account {label!r}"
        expected = row.get("expected_amount")
        expected_cents = (
            None
            if expected is None
            else _money_to_cents(expected, where=ctx, label="expected_amount")
        )
        due = row.get("due_day")
        due_day = None if due is None else _require_day(due, where=ctx)
        out.append(
            DebtAccount(
                account_id=acct,
                label=label,
                expected_amount_cents=expected_cents,
                due_day=due_day,
            )
        )
    return tuple(out)


def _parse_recurring(
    data: dict, envelopes: list[Envelope]
) -> tuple[RecurringBill, ...]:
    raw_recurring = data.get("recurring", [])
    if not isinstance(raw_recurring, list):
        raise BudgetConfigError("budget config 'recurring' must be a list")
    resolve = _envelope_resolver(envelopes)
    bills: list[RecurringBill] = []
    for i, raw in enumerate(raw_recurring):
        where = f"recurring[{i}]"
        if not isinstance(raw, dict):
            raise BudgetConfigError(f"{where} must be an object")
        name = _require_name(raw.get("name"), where=where)
        ctx = f"recurring bill {name!r}"
        match = _optional_match(raw.get("match"), where=ctx)
        envelope = None
        if raw.get("envelope") is not None:
            envelope = resolve(raw.get("envelope"), where=ctx, field="envelope")
        if envelope is None and match is None:
            raise BudgetConfigError(
                f"{ctx}: a recurring bill needs either an 'envelope' (to attribute "
                "it to a budget and match it by account) or a 'match' keyword (to "
                "match it by merchant); it has neither"
            )
        lifecycle = _require_lifecycle(raw.get("lifecycle"), where=ctx)
        variable = _require_variable(raw.get("variable"), where=ctx)
        if variable and match is None:
            raise BudgetConfigError(
                f"{ctx}: a variable-amount bill must have a 'match' keyword. A "
                "variable bill ignores the amount when matching, so without a "
                "merchant keyword to pin it, it would match any charge in its "
                "envelope near the due date and silently hide a missing bill"
            )
        cancel_effective = _optional_date(
            raw.get("cancel_effective"), where=ctx, field="cancel_effective"
        )
        if lifecycle != "active" and cancel_effective is None:
            raise BudgetConfigError(
                f"{ctx}: a {lifecycle!r} bill needs a 'cancel_effective' date "
                "(the day the cancellation took effect) so a charge on/after it "
                "can be flagged as the bill coming back"
            )
        if lifecycle == "active" and cancel_effective is not None:
            raise BudgetConfigError(
                f"{ctx}: 'cancel_effective' is only meaningful for a 'canceling' "
                "or 'canceled' bill; an active bill must not set it"
            )
        bills.append(
            RecurringBill(
                name=name,
                envelope=envelope,
                amount_cents=_money_to_cents(raw.get("amount"), where=ctx),
                cadence=_require_cadence(raw.get("cadence"), where=ctx),
                day=_require_day(raw.get("day"), where=ctx),
                match=match,
                lifecycle=lifecycle,
                cancel_effective=cancel_effective,
                variable=variable,
            )
        )
    return tuple(bills)


def _parse_scheduled_transfers(
    data: dict, envelopes: list[Envelope]
) -> tuple[ScheduledTransfer, ...]:
    raw_transfers = data.get("scheduled_transfers", [])
    if not isinstance(raw_transfers, list):
        raise BudgetConfigError("budget config 'scheduled_transfers' must be a list")
    resolve = _envelope_resolver(envelopes)
    transfers: list[ScheduledTransfer] = []
    for i, raw in enumerate(raw_transfers):
        where = f"scheduled_transfers[{i}]"
        if not isinstance(raw, dict):
            raise BudgetConfigError(f"{where} must be an object")
        name = _require_name(raw.get("name"), where=where)
        ctx = f"scheduled transfer {name!r}"
        to_env = resolve(raw.get("to"), where=ctx, field="to")
        from_env = None
        if raw.get("from") is not None:
            from_env = resolve(raw.get("from"), where=ctx, field="from")
            if from_env == to_env:
                raise BudgetConfigError(
                    f"{ctx}: 'from' and 'to' are the same envelope {to_env!r}; a "
                    "transfer must move between two different envelopes"
                )
        transfers.append(
            ScheduledTransfer(
                name=name,
                to_envelope=to_env,
                from_envelope=from_env,
                amount_cents=_money_to_cents(raw.get("amount"), where=ctx),
                cadence=_require_cadence(raw.get("cadence"), where=ctx),
                day=_require_day(raw.get("day"), where=ctx),
            )
        )
    return tuple(transfers)


def load_config(path: Path) -> BudgetConfig:
    """Read and validate the budget config file at ``path``.

    Raises :class:`BudgetConfigError` if the file is missing, is not valid
    JSON, or fails validation.
    """
    if not path.exists():
        raise BudgetConfigError(
            f"budget config not found at {path}; create it (see "
            "docs/envelope-budgeting-design.md) or pass --config"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetConfigError(f"could not read budget config {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BudgetConfigError(f"budget config {path} is not valid JSON: {exc}") from exc
    return parse_config(data)
