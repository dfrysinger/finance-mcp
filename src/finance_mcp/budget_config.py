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

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Forward-compatible: later pieces (forecast, allocation audit, subscription
# audit) add a "recurring" calendar to the same file. Unknown keys are ignored
# here so an older reader does not reject a config carrying newer sections.
SUPPORTED_VERSION = 1


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
class BudgetConfig:
    version: int
    envelopes: tuple[Envelope, ...]

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


def _target_to_cents(value: Any, *, envelope: str) -> int | None:
    """Convert a JSON monthly target to integer cents, or None if absent.

    A target that is not a whole number of cents (e.g. ``10.005``) is rejected
    rather than rounded, so the budget never silently invents a fraction of a
    cent the user did not write.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BudgetConfigError(
            f"envelope {envelope!r}: monthly_target must be a number, "
            f"got {type(value).__name__}"
        )
    try:
        dollars = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetConfigError(
            f"envelope {envelope!r}: monthly_target {value!r} is not a number"
        ) from exc
    if not dollars.is_finite():
        raise BudgetConfigError(
            f"envelope {envelope!r}: monthly_target must be finite, got {value!r}"
        )
    if dollars < 0:
        raise BudgetConfigError(
            f"envelope {envelope!r}: monthly_target must not be negative, got {value!r}"
        )
    cents = dollars * 100
    if cents != cents.to_integral_value():
        raise BudgetConfigError(
            f"envelope {envelope!r}: monthly_target {value!r} is not a whole "
            "number of cents"
        )
    return int(cents)


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

    return BudgetConfig(version=version, envelopes=tuple(envelopes))


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
