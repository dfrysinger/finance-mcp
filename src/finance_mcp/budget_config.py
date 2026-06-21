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
    """One recurring outflow (a bill) due from an envelope each cycle.

    ``envelope`` is the canonical name of the paying envelope, resolved at parse
    time. ``day`` is the nominal day-of-month it is due (1–31); the projector
    clamps it to each month's actual length. ``amount_cents`` is positive.
    ``match`` is an optional keyword (normalized to tokens and matched as a
    token-subset against a transaction's merchant identity — description and
    payee only; ``memo`` is excluded except as a sole fallback when both are
    empty) that pins the bill to its merchant so the subscription audit can
    reliably tell whether the charge posted.
    """

    name: str
    envelope: str
    amount_cents: int
    cadence: str
    day: int
    match: str | None = None


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
class BudgetConfig:
    version: int
    envelopes: tuple[Envelope, ...]
    recurring: tuple[RecurringBill, ...] = ()
    scheduled_transfers: tuple[ScheduledTransfer, ...] = ()

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

    return BudgetConfig(
        version=version,
        envelopes=tuple(envelopes),
        recurring=recurring,
        scheduled_transfers=scheduled,
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
        bills.append(
            RecurringBill(
                name=name,
                envelope=resolve(raw.get("envelope"), where=ctx, field="envelope"),
                amount_cents=_money_to_cents(raw.get("amount"), where=ctx),
                cadence=_require_cadence(raw.get("cadence"), where=ctx),
                day=_require_day(raw.get("day"), where=ctx),
                match=_optional_match(raw.get("match"), where=ctx),
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
