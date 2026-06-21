"""Tests for the statement importer (Piece A).

All CSV content here is synthetic — invented rows that exercise each export
schema's shape and sign convention. No real account data is used.
"""

from pathlib import Path
from decimal import Decimal
import csv

import pytest

from finance_mcp import archive, importer

# --- Synthetic fixtures, one per supported export schema ----------------------

SCHWAB_CSV = (
    '"Date","Status","Type","CheckNumber","Description","Withdrawal","Deposit","RunningBalance"\n'
    '"05/26/2026","Pending","","","PENDING THING","","",""\n'
    '"05/29/2026","Posted","VISA","","STORE A","$2.03","","$5,407.83"\n'
    '"05/29/2026","Posted","TRANSFER","","Transfer from Schwab Bank Investor Check","","$1,000.00","$6,407.83"\n'
    '"05/30/2026","Posted","VISA","","COFFEE","$4.00","","$6,403.83"\n'
    '"05/30/2026","Posted","VISA","","COFFEE","$4.00","","$6,399.83"\n'
)

APPLE_CSV = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
    '05/17/2026,05/20/2026,"CHEWY.COM PLANTATION FL USA","Chewy.com","Other","Purchase","104.67","Person One"\n'
    '05/01/2026,05/02/2026,"ACH DEPOSIT INTERNET TRANSFER","Apple","Payment","Payment","-325.28","Person One"\n'
)

CHASE_CSV = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "05/18/2026,05/19/2026,Amazon.com*ABC,Shopping,Sale,-40.76,\n"
    "05/02/2026,05/03/2026,AUTOMATIC PAYMENT - THANK,Payment,Payment,500.00,\n"
)

FIDELITY_CSV = (
    '"Date","Transaction","Name","Memo","Amount"\n'
    '"2026-05-11","CREDIT","PAYMENT MADE BY ACCOUNT","WEB AUTOMTC","186.59"\n'
    '"2026-05-11","DEBIT","TESLA SUBSCRIPTION US","ref123","-9.99"\n'
)


def _write(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.write_text(content, encoding="utf-8")
    return p


# --- Adapter detection ---------------------------------------------------------


def test_detects_each_schema(tmp_path):
    cases = {
        "Main_Checking_XXX617_Checking_Transactions_x.csv": (SCHWAB_CSV, "schwab"),
        "Apple Card Transactions.csv": (APPLE_CSV, "apple"),
        "Chase1199_Activity.csv": (CHASE_CSV, "chase"),
        "Credit Card - 6562_x.csv": (FIDELITY_CSV, "fidelity"),
    }
    for fname, (content, source) in cases.items():
        p = _write(tmp_path, fname, content)
        txns = importer.parse_file(p)
        assert txns, f"{fname} parsed no rows"
        assert all(t["account_id"].startswith(source) for t in txns)


def test_unrecognized_format_raises(tmp_path):
    p = _write(tmp_path, "weird.csv", "colA,colB\n1,2\n")
    with pytest.raises(importer.ImportError_):
        importer.parse_file(p)


# --- Sign normalization (archive convention: negative = money out) -------------


def test_schwab_withdrawal_negative_deposit_positive(tmp_path):
    p = _write(tmp_path, "Groceries_XXX294_Checking_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    by_desc = {t["description"]: t for t in txns}
    assert by_desc["STORE A"]["amount_float"] == -2.03
    assert by_desc["Transfer from Schwab Bank Investor Check"]["amount_float"] == 1000.00


def test_apple_amount_is_negated(tmp_path):
    p = _write(tmp_path, "Apple Card Transactions.csv", APPLE_CSV)
    txns = importer.parse_file(p)
    by_desc = {t["description"]: t for t in txns}
    # Purchase positive in source -> money out (negative) in archive.
    assert by_desc["CHEWY.COM PLANTATION FL USA"]["amount_float"] == -104.67
    # Payment negative in source -> money in (positive) in archive.
    assert by_desc["ACH DEPOSIT INTERNET TRANSFER"]["amount_float"] == 325.28


def test_chase_and_fidelity_signs_kept(tmp_path):
    chase = importer.parse_file(_write(tmp_path, "Chase1199_Activity.csv", CHASE_CSV))
    fid = importer.parse_file(_write(tmp_path, "Credit Card - 6562_x.csv", FIDELITY_CSV))
    chase_by = {t["description"]: t for t in chase}
    assert chase_by["Amazon.com*ABC"]["amount_float"] == -40.76
    assert chase_by["AUTOMATIC PAYMENT - THANK"]["amount_float"] == 500.00
    fid_by = {t["description"]: t for t in fid}
    assert fid_by["TESLA SUBSCRIPTION US"]["amount_float"] == -9.99
    assert fid_by["PAYMENT MADE BY ACCOUNT"]["amount_float"] == 186.59


# --- Account identity from filename -------------------------------------------


def test_schwab_account_identity_from_filename(tmp_path):
    p = _write(tmp_path, "My_Spending_XXX626_Checking_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    assert txns[0]["account_id"] == "schwab:626"
    assert txns[0]["account_name"] == "My Spending"


def test_chase_account_suffix_from_filename(tmp_path):
    p = _write(tmp_path, "Chase1199_Activity.csv", CHASE_CSV)
    txns = importer.parse_file(p)
    assert txns[0]["account_id"] == "chase:1199"
    assert txns[0]["account_name"] == "Chase ...1199"


# --- Row filtering -------------------------------------------------------------


def test_pending_amountless_rows_skipped(tmp_path):
    p = _write(tmp_path, "Bills_XXX401_Checking_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    assert "PENDING THING" not in {t["description"] for t in txns}
    # 4 real rows (1 store, 1 transfer, 2 coffees); the pending row is dropped.
    assert len(txns) == 4


def test_currency_symbols_and_commas_parsed(tmp_path):
    p = _write(tmp_path, "Tax_Savings_XXX665_Savings_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    transfer = next(t for t in txns if "Transfer" in t["description"])
    assert transfer["amount"] == "1000.00"


# --- Stable ids: idempotency + duplicate preservation --------------------------


def test_synthetic_ids_are_import_namespaced(tmp_path):
    p = _write(tmp_path, "Pets_XXX506_Checking_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    assert all(t["id"].startswith("import:") for t in txns)


def test_reparse_produces_identical_ids(tmp_path):
    p = _write(tmp_path, "Pets_XXX506_Checking_Transactions_x.csv", SCHWAB_CSV)
    ids1 = [t["id"] for t in importer.parse_file(p)]
    ids2 = [t["id"] for t in importer.parse_file(p)]
    assert ids1 == ids2


def test_identical_same_day_charges_kept_distinct(tmp_path):
    p = _write(tmp_path, "Restaurants_XXX377_Checking_Transactions_x.csv", SCHWAB_CSV)
    txns = importer.parse_file(p)
    coffees = [t for t in txns if t["description"] == "COFFEE"]
    assert len(coffees) == 2
    assert coffees[0]["id"] != coffees[1]["id"]


# --- End-to-end through the archive -------------------------------------------


def test_import_paths_writes_and_is_idempotent(tmp_path):
    src = tmp_path / "statements"
    src.mkdir()
    _write(src, "Main_Checking_XXX617_Checking_Transactions_x.csv", SCHWAB_CSV)
    _write(src, "Apple Card Transactions.csv", APPLE_CSV)
    _write(src, "Chase1199_Activity.csv", CHASE_CSV)
    _write(src, "Credit Card - 6562_x.csv", FIDELITY_CSV)
    _write(src, "ignore.txt", "not a csv")

    conn = archive.connect(tmp_path / "a.db")
    s1 = importer.import_paths([src], conn=conn)
    total = 4 + 2 + 2 + 2  # schwab(4 real) + apple + chase + fidelity
    assert s1["rows_parsed"] == total
    assert s1["transactions_added"] == total
    assert s1["files_imported"] == 4

    # Re-import: same ids, nothing new.
    s2 = importer.import_paths([src], conn=conn)
    assert s2["transactions_added"] == 0
    assert archive.stats(conn)["transactions"] == total


def test_import_skips_unreadable_format_without_aborting(tmp_path):
    src = tmp_path / "mixed"
    src.mkdir()
    _write(src, "Apple Card Transactions.csv", APPLE_CSV)
    _write(src, "garbage.csv", "foo,bar\n1,2\n")
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([src], conn=conn)
    assert summary["files_imported"] == 1
    assert summary["files_skipped"] == 1
    assert summary["transactions_added"] == 2


def test_dry_run_writes_nothing(tmp_path):
    p = _write(tmp_path, "Apple Card Transactions.csv", APPLE_CSV)
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([p], conn=conn, dry_run=True)
    assert summary["rows_parsed"] == 2
    assert summary["transactions_added"] == 0
    assert archive.stats(conn)["transactions"] == 0


def test_metadata_rows_above_header_are_skipped(tmp_path):
    content = (
        '"Schwab Bank export"\n'
        '"Account: My Spending"\n'
        + SCHWAB_CSV
    )
    p = _write(tmp_path, "My_Spending_XXX626_Checking_Transactions_x.csv", content)
    txns = importer.parse_file(p)
    assert len(txns) == 4


# --- Review hardening: header drift, non-finite amounts, silent loss ----------


def test_header_case_and_whitespace_drift_still_parses(tmp_path):
    # Same Chase schema, but headers upper-cased with stray spaces — a common
    # export artifact. Detection AND row extraction must both tolerate it.
    drifted = (
        " TRANSACTION DATE , Post Date ,DESCRIPTION,Category,Type, AMOUNT ,Memo\n"
        "05/18/2026,05/19/2026,Amazon.com*ABC,Shopping,Sale,-40.76,\n"
    )
    p = _write(tmp_path, "Chase1199_Activity.csv", drifted)
    txns = importer.parse_file(p)
    assert len(txns) == 1
    assert txns[0]["amount_float"] == -40.76


def test_non_finite_amounts_rejected(tmp_path):
    assert importer._parse_amount("NaN") is None
    assert importer._parse_amount("Infinity") is None
    assert importer._parse_amount("-inf") is None
    poison = (
        '"Date","Transaction","Name","Memo","Amount"\n'
        '"2026-05-11","DEBIT","BAD ROW","x","NaN"\n'
        '"2026-05-11","DEBIT","GOOD ROW","x","-9.99"\n'
    )
    p = _write(tmp_path, "Credit Card - 6562_x.csv", poison)
    detailed = importer.parse_file_detailed(p)
    descs = {t["description"] for t in detailed.transactions}
    assert descs == {"GOOD ROW"}
    assert detailed.rows_skipped == 1


def test_reimport_idempotent_when_an_earlier_transaction_is_inserted(tmp_path):
    # The failure mode that a running-balance discriminator would cause: a later
    # export inserts an earlier (different) transaction, shifting every running
    # balance. The occurrence key is independent of balance, so the shared
    # COFFEE row keeps its id and re-import dedupes it.
    export1 = (
        '"Date","Status","Type","CheckNumber","Description","Withdrawal","Deposit","RunningBalance"\n'
        '"05/30/2026","Posted","VISA","","COFFEE","$4.00","","$100.00"\n'
    )
    export2 = (
        '"Date","Status","Type","CheckNumber","Description","Withdrawal","Deposit","RunningBalance"\n'
        '"05/28/2026","Posted","ACH","","BACKDATED BILL","$50.00","","$50.00"\n'
        '"05/30/2026","Posted","VISA","","COFFEE","$4.00","","$54.00"\n'
    )
    d1, d2 = tmp_path / "e1", tmp_path / "e2"
    d1.mkdir()
    d2.mkdir()
    p1 = _write(d1, "Pets_XXX506_Checking_Transactions_x.csv", export1)
    p2 = _write(d2, "Pets_XXX506_Checking_Transactions_x.csv", export2)
    coffee1 = importer.parse_file(p1)[0]["id"]
    coffee2 = next(t for t in importer.parse_file(p2) if t["description"] == "COFFEE")["id"]
    assert coffee1 == coffee2  # same id despite the shifted running balance

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([p1], conn=conn)
    importer.import_paths([p2], conn=conn)
    # 2 distinct rows total: the shared COFFEE deduped, the backdated bill added.
    assert archive.stats(conn)["transactions"] == 2


def test_card_posted_date_vs_transaction_date(tmp_path):
    # Apple/Chase carry a clearing/post date distinct from the transaction date;
    # posted_ts should reflect when it cleared, transacted_at when it was made.
    apple = importer.parse_file(
        _write(tmp_path, "Apple Card Transactions.csv", APPLE_CSV)
    )
    purchase = next(t for t in apple if "CHEWY" in t["description"])
    assert purchase["transacted_at"].startswith("2026-05-17")  # transaction date
    assert purchase["posted"].startswith("2026-05-20")  # clearing date
    chase = importer.parse_file(_write(tmp_path, "Chase1199_Activity.csv", CHASE_CSV))
    sale = next(t for t in chase if "Amazon" in t["description"])
    assert sale["transacted_at"].startswith("2026-05-18")
    assert sale["posted"].startswith("2026-05-19")  # post date


def test_undefined_cp1252_byte_does_not_crash_the_run(tmp_path):
    # 0x81 is undefined in cp1252 (would raise) but valid in latin-1. A single
    # such byte must not abort the whole import.
    src = tmp_path / "mixed"
    src.mkdir()
    p = src / "Credit Card - 6562_x.csv"
    p.write_bytes(
        '"Date","Transaction","Name","Memo","Amount"\n'.encode("latin-1")
        + b'"2026-05-11","DEBIT","ODD\x81BYTE","x","-5.00"\n'
    )
    _write(src, "Apple Card Transactions.csv", APPLE_CSV)
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([src], conn=conn)  # must not raise
    assert summary["files_imported"] == 2
    assert summary["transactions_added"] == 3  # 1 fidelity + 2 apple


def test_zero_row_matched_file_is_surfaced_not_silent(tmp_path):
    # A file that matches the Schwab schema but has only pending (amount-less)
    # rows parses to nothing — it must be reported as skipped, not as a clean
    # import of 0 rows.
    only_pending = (
        '"Date","Status","Type","CheckNumber","Description","Withdrawal","Deposit","RunningBalance"\n'
        '"05/26/2026","Pending","","","PENDING ONE","","",""\n'
        '"05/27/2026","Pending","","","PENDING TWO","","",""\n'
    )
    p = _write(tmp_path, "Bills_XXX401_Checking_Transactions_x.csv", only_pending)
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([p], conn=conn)
    assert summary["files_imported"] == 0
    assert summary["files_skipped"] == 1
    assert "0 transactions" in summary["skipped"][0]["reason"]


def test_rows_skipped_reported_in_summary(tmp_path):
    # SCHWAB_CSV has 1 pending row that is skipped.
    p = _write(tmp_path, "Bills_XXX401_Checking_Transactions_x.csv", SCHWAB_CSV)
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([p], conn=conn)
    assert summary["rows_skipped"] == 1
    assert summary["results"][0]["rows_skipped"] == 1


def test_cp1252_encoded_file_does_not_corrupt(tmp_path):
    # A Windows-1252 byte (0xe9 = "é") must decode meaningfully, not become a
    # U+FFFD replacement char that changes the stored text and its id.
    p = tmp_path / "Credit Card - 6562_x.csv"
    p.write_bytes(
        '"Date","Transaction","Name","Memo","Amount"\n'.encode("cp1252")
        + '"2026-05-11","DEBIT","CAF\xe9 MERCHANT","x","-5.00"\n'.encode("cp1252")
    )
    txns = importer.parse_file(p)
    assert len(txns) == 1
    assert "\ufffd" not in txns[0]["description"]
    assert txns[0]["description"] == "CAFé MERCHANT"

def test_malformed_csv_does_not_abort_the_run(tmp_path):
    # A truncated download with an unterminated quote makes csv accumulate every
    # following line into one field until it overflows csv's field-size limit and
    # raises csv.Error. That single bad file must be skipped, not crash the whole
    # batch -- the archive write happens only after every file is parsed, so a
    # stray exception here would silently discard every other valid statement.
    src = tmp_path / "mixed"
    src.mkdir()
    _write(src, "Apple Card Transactions.csv", APPLE_CSV)
    broken = (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        '05/18/2026,05/19/2026,"UNTERMINATED' + "x" * 200000 + "\n"
    )
    _write(src, "Chase1199_Activity.csv", broken)
    conn = archive.connect(tmp_path / "a.db")
    summary = importer.import_paths([src], conn=conn)
    assert summary["files_imported"] == 1
    assert summary["files_skipped"] == 1
    assert summary["transactions_added"] == 2  # the Apple rows still landed
    assert any("Chase1199" in s["file"] for s in summary["skipped"])


def test_embedded_newline_in_quoted_field_is_preserved(tmp_path):
    # A newline inside a quoted field is legal CSV (multi-line memo/description).
    # Pre-splitting the text by lines before handing it to csv would silently
    # delete that newline, mutating both the stored text and the synthetic id.
    multiline = (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        '05/18/2026,05/19/2026,"AMAZON\nMARKETPLACE",Shopping,Sale,-40.76,\n'
    )
    p = _write(tmp_path, "Chase1199_Activity.csv", multiline)
    txns = importer.parse_file(p)
    assert len(txns) == 1
    assert txns[0]["description"] == "AMAZON\nMARKETPLACE"


def test_distinct_detail_rows_distinct_within_export_and_memo_stored(tmp_path):
    # Two same-day charges share amount and description but differ only in the
    # optional Memo detail. The detail is NOT part of the id (it is version-
    # dependent), so the two rows are separated by occurrence ordinal and still
    # get distinct ids, while the detail is preserved in memo for display.
    full = (
        '"Date","Transaction","Name","Memo","Amount"\n'
        '"2026-05-11","DEBIT","GENERIC POS","store-A","-9.99"\n'
        '"2026-05-11","DEBIT","GENERIC POS","store-B","-9.99"\n'
    )
    p = _write(tmp_path, "Credit Card - 6562_x.csv", full)
    full_txns = importer.parse_file(p)
    assert full_txns[0]["id"] != full_txns[1]["id"]  # distinct via occurrence
    assert {t["memo"] for t in full_txns} == {"store-A", "store-B"}  # detail kept

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([p], conn=conn)
    importer.import_paths([p], conn=conn)
    # Re-importing the same export is idempotent -- two rows, no duplicates.
    assert archive.stats(conn)["transactions"] == 2


def test_chase_memo_stored_for_display_not_keyed(tmp_path):
    # Chase ships a Type AND a free-form Memo column. The Memo is captured into
    # the stored memo for display, but is excluded from the id (it is optional /
    # version-dependent), so two same-day same-amount same-description rows are
    # separated by occurrence and re-import stays idempotent.
    full = (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        "05/18/2026,05/19/2026,GENERIC POS,Shopping,Sale,-9.99,note-A\n"
        "05/18/2026,05/19/2026,GENERIC POS,Shopping,Sale,-9.99,note-B\n"
    )
    p = _write(tmp_path, "Chase1199_Activity.csv", full)
    full_txns = importer.parse_file(p)
    assert full_txns[0]["id"] != full_txns[1]["id"]  # distinct via occurrence
    assert {t["memo"] for t in full_txns} == {"Sale | note-A", "Sale | note-B"}

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([p], conn=conn)
    importer.import_paths([p], conn=conn)
    assert archive.stats(conn)["transactions"] == 2


def test_chase_empty_memo_preserves_type_and_id_stability(tmp_path):
    # The common case: Memo blank. memo must still be the Type string (unchanged
    # from before the Memo column was folded in) so real exports do not re-key.
    p = _write(tmp_path, "Chase1199_Activity.csv", CHASE_CSV)
    txns = importer.parse_file(p)
    assert [t["memo"] for t in txns] == ["Sale", "Payment"]


def test_year_in_filename_not_mistaken_for_account_suffix(tmp_path):
    # A statement-period year must not be read as the card suffix: that would
    # file rows under the wrong account and, since account_id is part of the id,
    # re-key everything when the period rolls over.
    assert importer._digits_suffix("Statement_2025_2026") is None
    assert importer._digits_suffix("Credit Card - 6562_06-04-2025") == "6562"
    assert importer._digits_suffix("Acct_874_2026_period") == "874"

    # Real Fidelity filenames lead with the card number, so identity is stable
    # regardless of the embedded statement-period years.
    p1 = _write(tmp_path, "Credit Card - 6562_06-04-2025_05-27-2026.csv", FIDELITY_CSV)
    p2 = _write(tmp_path, "Credit Card - 6562_04-23-2026_05-27-2026.csv", FIDELITY_CSV)
    a1 = importer.FidelityAdapter().account_identity(p1).account_id
    a2 = importer.FidelityAdapter().account_identity(p2).account_id
    assert a1 == a2 == "fidelity:6562"
def test_digits_suffix_matches_whole_runs_not_substrings_of_dates(tmp_path):
    # A compact statement date (one long digit run) must not yield a 3-5 digit
    # substring as the account suffix -- only a standalone 3-5 digit run counts.
    assert importer._digits_suffix("Statement_20260527_Credit_Card_6562") == "6562"
    assert importer._digits_suffix("20260527") is None  # an 8-digit date alone
    assert importer._digits_suffix("Acct_874") == "874"


def test_finite_decimal_that_overflows_float_is_rejected(tmp_path):
    # "1e9999" is a finite Decimal but float() of it is +inf, which would poison
    # every SUM(amount_float). It must be skipped like any other malformed amount,
    # while the valid sibling row still imports.
    poison = (
        '"Date","Transaction","Name","Memo","Amount"\n'
        '"2026-05-11","DEBIT","OVERFLOW","x","1e9999"\n'
        '"2026-05-11","DEBIT","NORMAL","y","-5.00"\n'
    )
    p = _write(tmp_path, "Credit Card - 6562_x.csv", poison)
    parsed = importer.parse_file_detailed(p)
    descs = [t["description"] for t in parsed.transactions]
    assert descs == ["NORMAL"]
    assert parsed.rows_skipped == 1


def test_schwab_check_number_stored_for_display_not_keyed(tmp_path):
    # Two same-day same-amount same-description checks differ only by check
    # number. CheckNumber is folded into memo for display but kept OUT of the id,
    # so the rows are separated by occurrence and re-import stays idempotent.
    full = (
        '"Date","Status","Type","CheckNumber","Description","Withdrawal","Deposit","RunningBalance"\n'
        '"05/30/2026","Posted","CHECK","1001","CHECK PAYMENT","$50.00","","$100.00"\n'
        '"05/30/2026","Posted","CHECK","1002","CHECK PAYMENT","$50.00","","$50.00"\n'
    )
    p = _write(tmp_path, "Bills_XXX401_Checking_Transactions_x.csv", full)
    full_txns = importer.parse_file(p)
    assert full_txns[0]["id"] != full_txns[1]["id"]  # distinct via occurrence
    assert {t["memo"] for t in full_txns} == {"CHECK | 1001", "CHECK | 1002"}

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([p], conn=conn)
    importer.import_paths([p], conn=conn)
    assert archive.stats(conn)["transactions"] == 2


def test_apple_purchased_by_stored_for_display_not_keyed(tmp_path):
    # Two cardholders make an identical same-day purchase. "Purchased By" is a
    # version-dependent column (added with Apple Card Family); it is stored in
    # memo for display but excluded from the id, so the two rows are separated by
    # occurrence and re-import stays idempotent.
    shared = (
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
        '05/17/2026,05/20/2026,"COFFEE","Cafe","Other","Purchase","5.00","Person One"\n'
        '05/17/2026,05/20/2026,"COFFEE","Cafe","Other","Purchase","5.00","Person Two"\n'
    )
    p = _write(tmp_path, "Apple Card Transactions.csv", shared)
    txns = importer.parse_file(p)
    assert txns[0]["id"] != txns[1]["id"]  # distinct via occurrence
    assert {t["memo"] for t in txns} == {"Purchase | Person One", "Purchase | Person Two"}

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([p], conn=conn)
    importer.import_paths([p], conn=conn)
    assert archive.stats(conn)["transactions"] == 2


def test_apple_id_stable_across_optional_column_schema_eras(tmp_path):
    # The headline idempotency contract: an older Apple export that predates the
    # "Purchased By" column and a newer one that includes it describe the SAME
    # transaction and MUST yield the SAME id -- otherwise every overlapping row
    # duplicates on re-import. Optional columns are display-only, never keyed.
    old_era = (
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)\n"
        '05/17/2026,05/20/2026,"COFFEE","Cafe","Other","Purchase","5.00"\n'
    )
    new_era = (
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
        '05/17/2026,05/20/2026,"COFFEE","Cafe","Other","Purchase","5.00","Person One"\n'
    )
    d1, d2 = tmp_path / "old", tmp_path / "new"
    d1.mkdir()
    d2.mkdir()
    old_txn = importer.parse_file(_write(d1, "Apple Card Transactions.csv", old_era))[0]
    new_txn = importer.parse_file(_write(d2, "Apple Card Transactions.csv", new_era))[0]
    assert old_txn["id"] == new_txn["id"]  # same transaction across schema eras

    conn = archive.connect(tmp_path / "a.db")
    importer.import_paths([d1 / "Apple Card Transactions.csv"], conn=conn)
    importer.import_paths([d2 / "Apple Card Transactions.csv"], conn=conn)
    assert archive.stats(conn)["transactions"] == 1  # no duplicate across eras


def test_delimiter_in_field_does_not_collide_ids(tmp_path):
    # The id payload is serialized with json.dumps, not a raw delimiter join, so
    # two rows whose stable fields would concatenate to the same string but split
    # at a different boundary get distinct ids. Here description+merchant(payee)
    # both flatten to "A,B,C" under a naive join but are kept distinct by JSON.
    combined = (
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)\n"
        '05/11/2026,05/12/2026,"A","B,C","Other","Purchase","9.99"\n'
        '05/11/2026,05/12/2026,"A,B","C","Other","Purchase","9.99"\n'
    )
    txns = importer.parse_file(_write(tmp_path, "Apple Card Transactions.csv", combined))
    assert txns[0]["id"] != txns[1]["id"]


def test_fidelity_year_like_last4_kept_as_account_suffix(tmp_path):
    # A real card last-4 can fall in the 1900-2099 range. The anchored
    # "Credit Card - <last4>" position disambiguates it from the statement-period
    # years that follow, so a year-like last-4 is preserved as the account id.
    p = _write(tmp_path, "Credit Card - 2026_06-04-2025_05-27-2026.csv", FIDELITY_CSV)
    account_id = importer.FidelityAdapter().account_identity(p).account_id
    assert account_id == "fidelity:2026"


def test_chase_identity_does_not_take_year_as_suffix(tmp_path):
    A = importer.ChaseAdapter()
    # Default export: last-4 adjacent to "Chase".
    assert A.account_identity(Path("Chase1199_Activity20250501.csv"))[0] == "chase:1199"
    # No number after "Chase": must not grab the statement year; stable fallback.
    assert A.account_identity(Path("Chase_Activity_2025.csv"))[0] == "chase:card"
    # Year present but real last-4 later: the year is skipped, the last-4 wins.
    assert A.account_identity(Path("Chase_Activity_2025_1199.csv"))[0] == "chase:1199"


def test_amount_scale_does_not_fork_id_across_exports(tmp_path):
    # The same transaction re-exported with a different decimal scale (5.0 vs
    # 5.00) must yield ONE id, or overlapping imports duplicate it. _amount_str
    # canonicalizes scale, so the two single-row Fidelity exports below map to
    # the same id and the second import adds nothing.
    a = (
        '"Date","Transaction","Name","Memo","Amount"\n'
        '"2026-05-11","DEBIT","COFFEE","x","-5.0"\n'
    )
    b = (
        '"Date","Transaction","Name","Memo","Amount"\n'
        '"2026-05-11","DEBIT","COFFEE","x","-5.00"\n'
    )
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    ta = importer.parse_file(_write(d1, "Credit Card - 6562_x.csv", a))[0]
    tb = importer.parse_file(_write(d2, "Credit Card - 6562_x.csv", b))[0]
    assert ta["id"] == tb["id"]

    conn = archive.connect(tmp_path / "db.db")
    importer.import_paths([d1 / "Credit Card - 6562_x.csv"], conn=conn)
    importer.import_paths([d2 / "Credit Card - 6562_x.csv"], conn=conn)
    assert archive.stats(conn)["transactions"] == 1


def test_amount_normalize_preserves_sub_cent_distinct_amounts(tmp_path):
    # Canonicalizing scale must NOT round: two genuinely different amounts that
    # share a cents prefix (5.001 vs 5.009) stay distinct.
    assert importer._amount_str(Decimal("5.001")) != importer._amount_str(Decimal("5.009"))
    # Negative zero collapses to plain zero so -0.00 cannot fork an id from 0.00.
    assert importer._amount_str(Decimal("-0.00")) == importer._amount_str(Decimal("0.00"))


def test_chase_export_without_memo_column_is_recognized(tmp_path):
    # The Memo column is optional/version-dependent; an older Chase export that
    # omits it must still be recognized and imported (memo None), not silently
    # dropped as an unrecognized format.
    no_memo = (
        "Transaction Date,Post Date,Description,Category,Type,Amount\n"
        "05/18/2026,05/19/2026,GENERIC POS,Shopping,Sale,-9.99\n"
    )
    txns = importer.parse_file(_write(tmp_path, "Chase1199_Activity.csv", no_memo))
    assert len(txns) == 1
    assert txns[0]["memo"] == "Sale"  # folds to Type alone when Memo absent


def test_fidelity_export_without_memo_column_is_recognized(tmp_path):
    # Same principle for Fidelity: a missing optional Memo column must not make
    # the file unrecognized.
    no_memo = (
        '"Date","Transaction","Name","Amount"\n'
        '"2026-05-11","DEBIT","COFFEE","-5.00"\n'
    )
    txns = importer.parse_file(_write(tmp_path, "Credit Card - 6562_x.csv", no_memo))
    assert len(txns) == 1
    assert txns[0]["memo"] is None


def test_high_precision_amounts_do_not_collide_in_id_key(tmp_path):
    # Amount canonicalization must NOT round: two distinct amounts that differ
    # only beyond the default 28-significant-digit Decimal context must keep
    # distinct id keys. (Decimal.normalize() would round and collapse them.)
    a = Decimal("0.123456789012345678901234567891")
    b = Decimal("0.123456789012345678901234567892")
    assert a != b
    assert importer._amount_str(a) != importer._amount_str(b)


def test_unterminated_quote_skips_file_not_silently_swallows_rows(tmp_path):
    # A row whose quoted field is never closed must NOT silently absorb every
    # following transaction into one field. strict CSV parsing raises csv.Error,
    # the per-file guard records the file as skipped, and the valid row is not
    # mis-bound into the broken row's memo.
    bad = (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        '05/18/2026,05/19/2026,STORE A,Shopping,Sale,-9.99,"oops unterminated\n'
        "05/20/2026,05/21/2026,STORE B,Shopping,Sale,-4.00,note\n"
    )
    p = _write(tmp_path, "Chase1199_Activity.csv", bad)
    # Direct parse surfaces the malformed quoting as csv.Error (not a silent
    # partial result); the batch path's per-file guard turns that into a skip.
    with pytest.raises(csv.Error):
        importer.parse_file_detailed(p)

    # And through the batch import path the file is surfaced as skipped, not
    # silently treated as a successful partial import.
    conn = archive.connect(tmp_path / "db.db")
    summary = importer.import_paths([p], conn=conn)
    assert summary["files_imported"] == 0
    assert summary["files_skipped"] == 1
    assert archive.stats(conn)["transactions"] == 0


def test_apple_fixed_account_id_is_not_flagged_ambiguous(tmp_path):
    # Apple Card has no per-account number; its fixed id is correct by design and
    # must NOT be reported as an ambiguous fallback.
    identity = importer.AppleCardAdapter().account_identity(Path("Apple Card Transactions.csv"))
    assert identity.account_id == "apple:card"
    assert identity.ambiguous is False


def test_chase_and_fidelity_generic_fallback_flagged_ambiguous(tmp_path):
    # When no suffix can be derived from the filename, the generic id is flagged
    # so the caller can warn instead of silently misfiling.
    ch = importer.ChaseAdapter().account_identity(Path("Chase_Activity_2025.csv"))
    assert ch.account_id == "chase:card" and ch.ambiguous is True
    fi = importer.FidelityAdapter().account_identity(Path("statement_export.csv"))
    assert fi.account_id == "fidelity:card" and fi.ambiguous is True
    sw = importer.SchwabAdapter().account_identity(Path("renamed_export.csv"))
    assert sw.account_id == "schwab:unknown" and sw.ambiguous is True


def test_import_warns_loudly_on_ambiguous_account_but_still_imports(tmp_path):
    # A file whose account id is a generic fallback must STILL import (not be
    # blocked), but be surfaced in the summary warnings so it is never silently
    # misfiled. A normally-named file produces no warning.
    amb_dir, ok_dir = tmp_path / "amb", tmp_path / "ok"
    amb_dir.mkdir()
    ok_dir.mkdir()
    ambiguous = _write(amb_dir, "Chase_Activity_2025.csv", CHASE_CSV)
    clean = _write(ok_dir, "Chase1199_Activity.csv", CHASE_CSV)
    conn = archive.connect(tmp_path / "db.db")
    summary = importer.import_paths([ambiguous, clean], conn=conn)
    assert summary["files_imported"] == 2  # both imported, not blocked
    assert summary["files_warned"] == 1
    assert len(summary["warnings"]) == 1
    w = summary["warnings"][0]
    assert w["account_id"] == "chase:card"
    assert str(ambiguous) == w["file"]
