"""Import exported bank/card statement CSVs into the durable archive.

SimpleFIN only exposes a rolling ~90-day window, so years of history that
predate that window can only come from downloaded statements. This module reads
each supported export schema and writes archive ``transactions`` rows shaped
exactly like :mod:`finance_mcp.normalize` output, so the existing query helpers,
categorizer, and transfer reconciler all run over imported rows unchanged.

Design contract (see ``docs/envelope-budgeting-design.md``):

- **One adapter per source schema.** A new export format is a new adapter, not
  an edit to a monolith. Adapters are matched by their header set.
- **Stable synthetic ids.** Each imported row gets a deterministic id derived
  from its stable natural key ``(source, account, date, amount, description,
  payee, occurrence)`` so re-importing the same file is idempotent (never
  duplicates) yet genuine same-day duplicate charges are preserved. Optional /
  version-dependent detail columns are stored in ``memo`` for display but kept
  OUT of the id, so an export that omits one does not re-key. The ``import:``
  prefix keeps these ids from ever colliding with a SimpleFIN id.
- **Archive sign convention.** Negative = money out, positive = money in — the
  same convention SimpleFIN uses. Each adapter maps its source's sign onto this
  explicitly; the convention is asserted per adapter, never inherited.
- **No category clobbering.** Importing only touches the ``transactions`` table;
  manual categories live in their own table and are applied at read time.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from . import archive

_ID_PREFIX = "import:"
_ID_HEX_LEN = 20


# --- Value parsing helpers -----------------------------------------------------


def _norm_header(name: str) -> str:
    """Lower-case, trim, and collapse internal whitespace in a header cell."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _parse_amount(raw: str | None) -> Decimal | None:
    """Parse a currency cell ("$1,234.56", "(12.00)", "-9.99") into a Decimal.

    Returns ``None`` for a blank cell so callers can skip amount-less rows (e.g.
    a pending statement line that has no posted amount yet). Parentheses denote a
    negative magnitude in some exports.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    # Reject non-finite values (NaN / Infinity parse fine as Decimals but would
    # poison every SUM(amount_float) the archive computes), mirroring the guard
    # in normalize.amount_to_cents.
    if not value.is_finite():
        return None
    # A *finite* Decimal can still overflow float() to +/-inf (e.g. "1e9999"),
    # and amount_float is stored as a REAL the archive sums -- so reject anything
    # that does not survive the float round-trip finitely, too.
    if not math.isfinite(float(value)):
        return None
    return -value if negative else value


def _parse_date(raw: str | None) -> datetime | None:
    """Parse a statement date (``MM/DD/YYYY`` or ``YYYY-MM-DD``) to UTC midnight.

    Statement exports carry a calendar date with no time or zone. We anchor each
    to midnight UTC so ``posted_ts`` is a stable integer and ordering is
    deterministic, matching how the archive treats SimpleFIN's day-level posts.
    """
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc)
    return None


def _amount_str(value: Decimal) -> str:
    """Render a Decimal as a canonical (non-scientific) signed decimal string.

    Used only to build the synthetic id, so the representation must be canonical:
    trailing-zero scale is stripped (5.0, 5.00 and 5 all render "5") so the same
    transaction re-exported with a different decimal scale keeps one id and stays
    idempotent. The canonicalization is pure string manipulation on the exact
    ``format(value, "f")`` rendering -- it must NOT use ``Decimal.normalize()``,
    which rounds to the active context precision (28 significant digits) and so
    could collapse two genuinely distinct high-precision amounts to one id.
    Every Decimal zero (incl. -0.00) renders "0" so sign-of-zero cannot fork an
    id from 0.00.
    """
    if value == 0:
        return "0"
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _join_detail(*parts: str | None) -> str | None:
    """Fold a schema's secondary per-row columns into one ``memo`` string.

    Joins the non-empty, stripped fragments with ``" | "`` (or returns ``None``
    when all are empty). This captures the distinguishing fields a schema offers
    beyond description -- a transaction type plus, e.g., a check number or
    cardholder -- so two otherwise-identical same-day rows that differ only in
    such a field get distinct synthetic ids instead of mis-binding by occurrence
    ordinal across overlapping partial exports. Skipping empty fragments keeps
    the common case (only a Type present) byte-identical to a bare Type string,
    so real exports do not re-key.
    """
    joined = " | ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


def _synthetic_id(
    source: str,
    account_id: str,
    date_key: str,
    amount_str: str,
    description: str,
    payee: str | None,
    occurrence: int,
) -> str:
    """Deterministic id for an imported row, built only from STABLE identity fields.

    Natural key: source, account, transaction date, amount, description, payee
    (the merchant, where a schema provides one), plus an ``occurrence`` index.
    Every component is present and byte-stable across *every* valid export of a
    schema, so the same transaction reproduces the same id on re-import and across
    overlapping exports, and the upsert dedupes it.

    Deliberately EXCLUDED from the id: the stored ``memo`` and any other
    optional / version-dependent detail column (transaction type, check number,
    cardholder), which are folded into ``memo`` for display and analysis only.
    Keying on them would break the headline idempotency contract: a valid export
    from an earlier schema era that omits such a column (e.g. an Apple statement
    predating the "Purchased By" column) would yield a different id for the same
    transaction and duplicate *every* overlapping row on re-import -- far worse
    than the rare residual below. Running balance is excluded for the same
    reason: it is cumulative and shifts for every row after any late-posting
    transaction.

    Components are serialized with ``json.dumps`` (a boundary-preserving
    encoding), not a delimiter join, so a value that itself contains the
    delimiter cannot make two distinct keys collide.

    Residual limit (accepted): two rows identical in every stable field
    (date + amount + description + payee) that differ only in an excluded detail
    column and never appear together in one export -- only possible with
    truncated partial exports -- bind by occurrence ordinal and may map to the
    wrong sibling or collapse. Such rows are indistinguishable by any stable
    data, and the blast radius is one rare row, versus the whole-file duplication
    that keying on an optional column would cause.
    """
    payload = json.dumps(
        [source, account_id, date_key, amount_str, description or "", payee or "", occurrence],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:_ID_HEX_LEN]
    return f"{_ID_PREFIX}{digest}"


# --- Adapters ------------------------------------------------------------------


@dataclass
class RawRow:
    """A source row reduced to the fields the archive needs, sign-normalized."""

    date: datetime  # transaction (economic) date — the id key and transacted_at
    amount: Decimal  # archive convention: negative = money out
    description: str
    payee: str | None = None
    memo: str | None = None
    pending: bool = False
    # When it cleared, if the source reports a posting/clearing date distinct
    # from the transaction date (cards do). Defaults to the transaction date.
    posted_date: datetime | None = None


class AccountIdentity(NamedTuple):
    """A statement file's derived account identity.

    ``ambiguous`` is True when ``account_id`` is a generic per-source fallback
    because no account suffix could be derived from the filename (a renamed or
    non-standard export). Such a file still imports, but the caller surfaces a
    loud warning: a generic id can file rows under a shared bucket that merges
    two distinct accounts, and -- because ``account_id`` is part of the synthetic
    id -- the same statement re-exported under its provider-default (suffixed)
    name would re-key and duplicate. Apple Card carries no per-account number and
    uses a fixed id by design; that fixed id is NOT ambiguous.
    """

    account_id: str
    account_name: str
    ambiguous: bool = False


class Adapter:
    """Base class: a source schema plus how to read it into :class:`RawRow`."""

    source = ""
    org = ""
    #: Header cells (normalized) that MUST all be present to claim a file.
    required: set[str] = set()
    #: Header cells that, if present, DISQUALIFY this adapter (disambiguates
    #: schemas that share a required core, e.g. Apple vs. Chase).
    forbidden: set[str] = set()

    def matches(self, headers: set[str]) -> bool:
        return self.required <= headers and not (self.forbidden & headers)

    def account_identity(self, path: Path) -> AccountIdentity:
        """Return the :class:`AccountIdentity` for a file of this schema."""
        return AccountIdentity(f"{self.source}:unknown", self.org, ambiguous=True)

    def parse_row(self, row: dict[str, str]) -> RawRow | None:
        raise NotImplementedError


def _digits_suffix(text: str) -> str | None:
    """Extract a 3-5 digit account suffix from a filename fragment.

    A 4-digit calendar year (1900-2099) is skipped, never returned: statement
    filenames routinely embed the statement period (e.g. ``..._2025_2026.csv``),
    and mistaking a year for the card suffix would file rows under the wrong
    account and -- because ``account_id`` is part of the synthetic id -- change
    every row's id when the period rolls over, breaking re-import idempotency.
    The first non-year run wins; if every run is a year, returns ``None`` so the
    caller can fall back to a stable per-source id.
    """
    for match in re.finditer(r"(?<!\d)\d{3,5}(?!\d)", text):
        digits = match.group(0)
        if len(digits) == 4 and 1900 <= int(digits) <= 2099:
            continue
        return digits
    return None


class SchwabAdapter(Adapter):
    """Schwab Bank per-account checking/savings export.

    The account number is not in the rows — it is in the filename
    (``Main_Checking_XXX617_Checking_Transactions_...``). Withdrawal and Deposit
    are separate positive-magnitude columns; a withdrawal is money out.
    """

    source = "schwab"
    org = "Schwab"
    required = {"date", "status", "description", "withdrawal", "deposit"}

    def account_identity(self, path: Path) -> AccountIdentity:
        stem = path.stem
        # Split on the XXX<digits> marker: name is to its left, the digits are
        # the account suffix. Underscores in the name become spaces.
        m = re.search(r"^(.*?)_X+(\d{3,5})_", stem)
        if m:
            name = m.group(1).replace("_", " ").strip()
            suffix = m.group(2)
            return AccountIdentity(f"{self.source}:{suffix}", name or f"Schwab {suffix}")
        suffix = _digits_suffix(stem)
        if suffix:
            return AccountIdentity(f"{self.source}:{suffix}", stem.replace("_", " "))
        # No derivable suffix: generic bucket, flagged ambiguous for the caller.
        return AccountIdentity(f"{self.source}:unknown", stem.replace("_", " "), ambiguous=True)

    def parse_row(self, row: dict[str, str]) -> RawRow | None:
        date = _parse_date(row.get("date"))
        if date is None:
            return None
        withdrawal = _parse_amount(row.get("withdrawal"))
        deposit = _parse_amount(row.get("deposit"))
        if withdrawal is not None:
            amount = -abs(withdrawal)
        elif deposit is not None:
            amount = abs(deposit)
        else:
            # No posted amount (e.g. a pending line) -> nothing to archive yet.
            return None
        status = (row.get("status") or "").strip().lower()
        return RawRow(
            date=date,
            amount=amount,
            description=(row.get("description") or "").strip(),
            memo=_join_detail(row.get("type"), row.get("checknumber")),
            pending=status == "pending",
        )


class AppleCardAdapter(Adapter):
    """Apple Card export. Purchases are POSITIVE in the file; payments/refunds
    are negative. Archive convention is the opposite, so every amount is negated.
    """

    source = "apple"
    org = "Apple Card"
    required = {"transaction date", "clearing date", "merchant", "amount (usd)"}

    def account_identity(self, path: Path) -> AccountIdentity:
        # Apple Card has no per-account number in the export and there is one card
        # per person, so a fixed id is correct by design -- NOT an ambiguous
        # fallback.
        return AccountIdentity(f"{self.source}:card", "Apple Card")

    def parse_row(self, row: dict[str, str]) -> RawRow | None:
        date = _parse_date(row.get("transaction date"))
        amount = _parse_amount(row.get("amount (usd)"))
        if date is None or amount is None:
            return None
        return RawRow(
            date=date,
            amount=-amount,  # purchase(+) -> money out(-)
            description=(row.get("description") or "").strip(),
            payee=(row.get("merchant") or "").strip() or None,
            memo=_join_detail(row.get("type"), row.get("purchased by")),
            posted_date=_parse_date(row.get("clearing date")),
        )


class ChaseAdapter(Adapter):
    """Chase card export. Amounts are already negative for spend, positive for
    payments/credits -- already the archive convention, so kept as-is.
    """

    source = "chase"
    org = "Chase"
    required = {"transaction date", "post date", "description", "amount"}
    # Apple also has "transaction date"; its "clearing date"/"merchant" columns
    # keep these two schemas from both claiming a file. "memo" is NOT required:
    # it is an optional/version-dependent column folded into the display memo
    # only, so an older Chase export that omits it must still be recognized
    # ("post date" + "amount" already separate Chase from Apple).
    forbidden = {"clearing date", "merchant"}

    def account_identity(self, path: Path) -> AccountIdentity:
        # The default Chase export names the file Chase<last4>_Activity..., so the
        # card last-4 sits immediately after "Chase". Require that adjacency: a
        # loose "[^0-9]* then first 4-digit run" would skip over to a statement
        # year (e.g. Chase_Activity_2025 -> 2025), making account_id roll over
        # each period and re-key every row. If no digits follow "Chase", fall back
        # to the year-guarded _digits_suffix scan of the whole name.
        m = re.search(r"[Cc]hase(\d{4})", path.stem)
        suffix = m.group(1) if m else _digits_suffix(path.stem)
        if suffix:
            return AccountIdentity(f"{self.source}:{suffix}", f"Chase ...{suffix}")
        # No derivable suffix: generic bucket, flagged ambiguous for the caller.
        return AccountIdentity(f"{self.source}:card", "Chase Card", ambiguous=True)

    def parse_row(self, row: dict[str, str]) -> RawRow | None:
        date = _parse_date(row.get("transaction date"))
        amount = _parse_amount(row.get("amount"))
        if date is None or amount is None:
            return None
        # Chase ships both a Type (Sale/Payment/Return) classifier and an
        # optional free-form Memo column; fold both into the display memo. Memo
        # is display-only (excluded from the id), so when it is absent or empty
        # -- the common case -- this reduces to the Type string unchanged.
        return RawRow(
            date=date,
            amount=amount,
            description=(row.get("description") or "").strip(),
            memo=_join_detail(row.get("type"), row.get("memo")),
            posted_date=_parse_date(row.get("post date")),
        )


class FidelityAdapter(Adapter):
    """Fidelity card export. Amounts are already signed (DEBIT negative, payment
    positive) -- already the archive convention, so kept as-is.
    """

    source = "fidelity"
    org = "Fidelity"
    required = {"date", "transaction", "name", "amount"}
    # "memo" is NOT required: it is an optional column folded into the display
    # memo only, so a Fidelity export that omits it must still be recognized
    # ("transaction" + "name" already separate Fidelity from the other schemas).

    def account_identity(self, path: Path) -> AccountIdentity:
        # The Fidelity export is named "Credit Card - <last4>_<dates>.csv", so the
        # card number sits right after "Credit Card - ". Capture it there and
        # accept it even when it looks like a year -- a real last-4 can fall in
        # 1900-2099, and the anchored position disambiguates it from the
        # statement-period years that follow. Fall back to the year-guarded
        # generic scan only for non-standard filenames.
        m = re.search(r"[Cc]redit\s+[Cc]ard\s*-\s*(\d{3,5})", path.stem)
        suffix = m.group(1) if m else _digits_suffix(path.stem)
        if suffix:
            return AccountIdentity(f"{self.source}:{suffix}", f"Fidelity ...{suffix}")
        # No derivable suffix: generic bucket, flagged ambiguous for the caller.
        return AccountIdentity(f"{self.source}:card", "Fidelity Card", ambiguous=True)

    def parse_row(self, row: dict[str, str]) -> RawRow | None:
        date = _parse_date(row.get("date"))
        amount = _parse_amount(row.get("amount"))
        if date is None or amount is None:
            return None
        return RawRow(
            date=date,
            amount=amount,
            description=(row.get("name") or "").strip(),
            memo=(row.get("memo") or "").strip() or None,
        )


ADAPTERS: list[Adapter] = [
    SchwabAdapter(),
    AppleCardAdapter(),
    ChaseAdapter(),
    FidelityAdapter(),
]


class ImportError_(Exception):
    """Raised when a file cannot be matched to a known schema or read."""


def detect_adapter(headers: list[str]) -> Adapter | None:
    """Return the single adapter whose schema matches these headers, or None."""
    header_set = {_norm_header(h) for h in headers}
    for adapter in ADAPTERS:
        if adapter.matches(header_set):
            return adapter
    return None


# --- File / directory parsing --------------------------------------------------


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV, skipping any pre-header metadata lines some exports prepend.

    Returns ``(headers, rows)`` where each row dict is keyed by the *normalized*
    header (lower-cased, whitespace-collapsed) so a file whose only difference is
    header casing or stray spaces — common across export versions — still parses
    every row, exactly the variance ``detect_adapter`` already tolerates. Schwab
    files occasionally carry institution metadata rows above the real header; we
    scan for the first line that looks like a header.
    """
    # Bank exports are usually UTF-8 (sometimes BOM-prefixed) but a few ship
    # Windows-1252 (e.g. a "café" merchant). Try strict UTF-8 first, then cp1252
    # for proper Windows punctuation, and finally latin-1 — a *total* codec that
    # maps all 256 byte values — so an odd byte routes through cleanly instead of
    # raising and aborting the whole import run. (cp1252 leaves five bytes
    # undefined, so it cannot be the final fallback.)
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    assert text is not None  # latin-1 cannot raise; this is a type guard
    # Feed csv a real character stream (newline="" disables line translation) so
    # the parser owns record splitting. Pre-splitting with str.splitlines() would
    # silently drop newlines embedded in quoted fields (legal CSV, e.g. a
    # multi-line memo), corrupting both the stored text and the synthetic id.
    # strict=True makes malformed quoting (e.g. an unterminated quoted field that
    # would otherwise swallow every following row into one field) raise csv.Error
    # -- caught by the per-file import guard, so the bad file is surfaced as
    # skipped instead of silently importing partial, mis-bound data.
    reader = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    header_idx = None
    for i, cells in enumerate(reader):
        norm = {_norm_header(c) for c in cells}
        if "date" in norm or "transaction date" in norm:
            if norm & {"amount", "amount (usd)", "withdrawal", "deposit"}:
                header_idx = i
                break
    if header_idx is None:
        return [], []
    headers = reader[header_idx]
    norm_headers = [_norm_header(h) for h in headers]
    dict_rows = [
        dict(zip(norm_headers, cells))
        for cells in reader[header_idx + 1 :]
        if any(c.strip() for c in cells)
    ]
    return headers, dict_rows


@dataclass
class ParsedFile:
    """The outcome of parsing one statement file."""

    source: str
    account_id: str
    transactions: list[dict]
    rows_skipped: int  # rows the adapter could not turn into a transaction
    account_name: str = ""
    account_ambiguous: bool = False  # account_id is a generic fallback (no suffix)


def parse_file_detailed(path: Path) -> ParsedFile:
    """Parse one statement file, reporting both transactions and skipped rows.

    Raises :class:`ImportError_` if the file has no recognizable header or its
    schema is unknown. A row the adapter returns ``None`` for (a pending/blank
    line, or a malformed date/amount) is counted in ``rows_skipped`` rather than
    dropped silently, so a caller can surface when a file contributed less than
    its row count.
    """
    headers, rows = _read_rows(path)
    if not headers:
        raise ImportError_(f"No recognizable header row in {path.name}")
    adapter = detect_adapter(headers)
    if adapter is None:
        raise ImportError_(f"Unrecognized statement format: {path.name}")
    identity = adapter.account_identity(path)
    account_id, account_name = identity.account_id, identity.account_name

    seen: dict[tuple[str, str, str, str | None], int] = {}
    out: list[dict] = []
    skipped = 0
    for row in rows:
        raw = adapter.parse_row(row)
        if raw is None:
            skipped += 1
            continue
        # The display amount keeps the source decimal scale (e.g. "1000.00"); the
        # id key uses the scale-canonical form so a re-export that varies the
        # scale (5.0 vs 5.00) maps to the same id and stays idempotent.
        amount_display = format(raw.amount, "f")
        amount_key = _amount_str(raw.amount)
        # The id is keyed on the transaction (economic) date, which is stable
        # across re-exports; the posting date may lag and is stored separately.
        date_key = raw.date.date().isoformat()
        posted = raw.posted_date or raw.date
        # Occurrence index is the final tiebreaker for rows identical in every
        # STABLE keyed field (day, amount, description, payee). It -- not the
        # optional detail in memo -- separates such rows, so the id stays stable
        # across exports that differ in which optional columns they carry.
        key = (date_key, amount_key, raw.description, raw.payee)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        txn_id = _synthetic_id(
            adapter.source, account_id, date_key, amount_key,
            raw.description, raw.payee, occurrence,
        )
        out.append(
            {
                "id": txn_id,
                "account_id": account_id,
                "account_name": account_name,
                "org": adapter.org,
                "posted": posted.isoformat(),
                "posted_ts": int(posted.timestamp()),
                "transacted_at": raw.date.isoformat(),
                "amount": amount_display,
                "amount_float": float(raw.amount),
                "description": raw.description,
                "payee": raw.payee,
                "memo": raw.memo,
                "pending": raw.pending,
                "currency": "USD",
            }
        )
    return ParsedFile(
        adapter.source,
        account_id,
        out,
        skipped,
        account_name=account_name,
        account_ambiguous=identity.ambiguous,
    )


def parse_file(path: Path) -> list[dict]:
    """Parse one statement file into normalized archive-transaction dicts."""
    return parse_file_detailed(path).transactions


def _iter_csv_files(path: Path) -> list[Path]:
    """Return the CSV files at ``path`` (a single file or a directory tree)."""
    if path.is_file():
        return [path]
    return sorted(
        p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"
    )


def import_paths(
    paths: list[Path],
    *,
    conn=None,
    dry_run: bool = False,
) -> dict:
    """Import every statement under ``paths`` into the archive.

    Returns a summary with per-file results and overall counts. Files whose
    schema is unrecognized are reported in ``skipped`` rather than aborting the
    whole run, so a mixed directory imports what it can. When ``dry_run`` is set,
    rows are parsed and counted but nothing is written.
    """
    files: list[Path] = []
    for p in paths:
        files.extend(_iter_csv_files(p))

    results: list[dict] = []
    skipped: list[dict] = []
    warnings: list[dict] = []
    all_txns: list[dict] = []
    rows_skipped_total = 0
    for f in files:
        try:
            parsed = parse_file_detailed(f)
        except (ImportError_, csv.Error, OSError, UnicodeError) as exc:
            # One unreadable or structurally broken file (a truncated download, an
            # unterminated quote that overflows csv's field limit, a permissions
            # error) must not abort the batch -- the archive write happens only
            # after this loop, so a stray exception here would discard every other
            # valid statement. Record it and keep going. Errors from the archive
            # write itself stay outside this guard and surface normally.
            skipped.append({"file": str(f), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not parsed.transactions:
            # A file that matched a known schema but yielded nothing is a data
            # loss signal (header drift, a renamed column, or all-malformed
            # rows), not a clean import -- surface it instead of hiding it as
            # "0 rows imported". Its skipped rows still count toward the total.
            rows_skipped_total += parsed.rows_skipped
            reason = (
                f"matched {parsed.source} but parsed 0 transactions "
                f"({parsed.rows_skipped} row(s) skipped)"
            )
            skipped.append({"file": str(f), "reason": reason})
            continue
        rows_skipped_total += parsed.rows_skipped
        if parsed.account_ambiguous:
            # The file imported, but its account_id is a generic fallback because
            # no suffix could be derived from the filename. Surface it loudly: a
            # generic id can merge two distinct accounts, and re-exporting the
            # same statement under its provider-default (suffixed) name would
            # re-key every row. The user should rename the file to its provider
            # default or supply an explicit account id.
            warnings.append(
                {
                    "file": str(f),
                    "account_id": parsed.account_id,
                    "reason": (
                        f"could not derive an account number from the filename; "
                        f"imported under generic account '{parsed.account_id}'. "
                        f"Rename to the provider default (e.g. include the card "
                        f"last-4) so rows file under a stable, distinct account."
                    ),
                }
            )
        results.append(
            {
                "file": str(f),
                "source": parsed.source,
                "rows": len(parsed.transactions),
                "rows_skipped": parsed.rows_skipped,
                "account_id": parsed.account_id,
                "account_ambiguous": parsed.account_ambiguous,
            }
        )
        all_txns.extend(parsed.transactions)

    added = 0
    if all_txns and not dry_run:
        own_conn = conn is None
        conn = conn or archive.connect()
        try:
            stats = archive.upsert(conn, {"transactions": all_txns, "accounts": []})
            added = stats.get("transactions_added", 0)
        finally:
            if own_conn:
                conn.close()

    return {
        "files_imported": len(results),
        "files_skipped": len(skipped),
        "files_warned": len(warnings),
        "rows_parsed": len(all_txns),
        "rows_skipped": rows_skipped_total,
        "transactions_added": added,
        "dry_run": dry_run,
        "results": results,
        "skipped": skipped,
        "warnings": warnings,
    }
