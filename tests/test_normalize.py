from finance_mcp import normalize


def test_normalize_flattens_accounts_and_transactions(normalized):
    assert len(normalized["accounts"]) == 2
    assert len(normalized["transactions"]) == 5
    assert normalized["errors"] == []
    assert normalized["errlist"] == []


def test_account_carries_org_and_balance(normalized):
    checking = next(a for a in normalized["accounts"] if a["account_id"] == "ACT-checking")
    assert checking["org"] == "My Bank"
    assert checking["balance"] == "1234.56"
    assert checking["balance_float"] == 1234.56
    assert checking["balance_date"].startswith("2023-")


def test_transaction_amounts_signed_and_parsed(normalized):
    by_id = {t["id"]: t for t in normalized["transactions"]}
    assert by_id["t1"]["amount"] == "-45.00"
    assert by_id["t1"]["amount_float"] == -45.0
    assert by_id["t2"]["amount_float"] == 2000.0
    assert by_id["t1"]["org"] == "My Bank"
    assert by_id["t4"]["org"] == "Cyprus Credit Union"


def test_transactions_sorted_desc_by_posted(normalized):
    ts = [t["posted_ts"] for t in normalized["transactions"]]
    assert ts == sorted(ts, reverse=True)


def test_pending_flag_preserved(normalized):
    by_id = {t["id"]: t for t in normalized["transactions"]}
    assert by_id["t3"]["pending"] is True
    assert by_id["t1"]["pending"] is False


def test_iso_timestamp_is_utc(normalized):
    by_id = {t["id"]: t for t in normalized["transactions"]}
    assert by_id["t1"]["posted"].endswith("+00:00")


def test_merge_dedupes_by_id_incoming_wins():
    existing = [
        {"id": "t1", "posted_ts": 100, "pending": True, "amount": "-5.00"},
        {"id": "t2", "posted_ts": 200, "pending": False, "amount": "-1.00"},
    ]
    incoming = [
        {"id": "t1", "posted_ts": 100, "pending": False, "amount": "-5.00"},
        {"id": "t3", "posted_ts": 300, "pending": False, "amount": "-9.00"},
    ]
    merged = normalize.merge_transactions(existing, incoming)
    ids = [t["id"] for t in merged]
    assert sorted(ids) == ["t1", "t2", "t3"]
    t1 = next(t for t in merged if t["id"] == "t1")
    assert t1["pending"] is False  # incoming promoted pending->posted
    assert [t["posted_ts"] for t in merged] == sorted(
        [t["posted_ts"] for t in merged], reverse=True
    )


def test_merge_keeps_records_without_id():
    existing = [{"posted_ts": 50, "amount": "-1"}]
    incoming = [{"posted_ts": 60, "amount": "-2"}]
    merged = normalize.merge_transactions(existing, incoming)
    assert len(merged) == 2


def test_bad_amount_and_timestamp_are_tolerated():
    data = {
        "accounts": [
            {
                "org": {"name": "X"},
                "id": "a",
                "name": "A",
                "currency": "USD",
                "balance": "not-a-number",
                "balance-date": "bogus",
                "transactions": [
                    {"id": "z", "posted": "bad", "amount": "oops", "pending": False}
                ],
            }
        ]
    }
    norm = normalize.normalize(data)
    assert norm["accounts"][0]["balance_float"] is None
    assert norm["accounts"][0]["balance_date"] is None
    assert norm["transactions"][0]["amount_float"] is None
    assert norm["transactions"][0]["posted"] is None


def test_amount_to_cents_exact_for_decimal_strings():
    assert normalize.amount_to_cents("-70.00") == -7000
    assert normalize.amount_to_cents("100.00") == 10000
    assert normalize.amount_to_cents("0.01") == 1
    assert normalize.amount_to_cents("1,234.56") == 123456  # thousands separator
    assert normalize.amount_to_cents("  -5.5 ") == -550      # whitespace + 1 decimal
    assert normalize.amount_to_cents("200") == 20000         # no decimal point


def test_amount_to_cents_no_float_drift():
    # 70.00 + 0.10 style sums that bite naive floats must still pair exactly.
    assert normalize.amount_to_cents("0.10") == 10
    assert normalize.amount_to_cents("0.20") == 20
    assert normalize.amount_to_cents("0.30") == 30
    # equal magnitudes parse to identical ints regardless of sign.
    assert normalize.amount_to_cents("-12.34") == -1234
    assert abs(normalize.amount_to_cents("-12.34")) == normalize.amount_to_cents("12.34")


def test_amount_to_cents_handles_missing_and_garbage():
    assert normalize.amount_to_cents(None) is None
    assert normalize.amount_to_cents("") is None
    assert normalize.amount_to_cents("oops") is None


def test_amount_to_cents_rejects_non_finite():
    # NaN/Infinity are valid Decimal() inputs, so they must be caught explicitly
    # rather than crashing the later int() conversion.
    for bad in ("NaN", "nan", "Infinity", "inf", "-inf", "sNaN"):
        assert normalize.amount_to_cents(bad) is None


def test_amount_to_cents_rejects_subcent_precision():
    # Rounding a sub-cent amount would invent a magnitude the account never saw,
    # which could mis-pair a transfer. Reject instead.
    assert normalize.amount_to_cents("10.005") is None
    assert normalize.amount_to_cents("10.004") is None
    assert normalize.amount_to_cents("0.001") is None
    # Exact cents still parse.
    assert normalize.amount_to_cents("10.00") == 1000


def test_amount_to_cents_rejects_overflow_and_high_precision():
    # Finite-but-huge inputs must not crash (decimal.Overflow is not
    # InvalidOperation), and sub-cent precision must be rejected regardless of
    # the ambient Decimal context, not silently rounded.
    assert normalize.amount_to_cents("1e999999") is None
    assert normalize.amount_to_cents("9e999999999") is None
    assert normalize.amount_to_cents("123456789012345678901234567890.001") is None
    # Large but exact cents amounts still parse.
    assert normalize.amount_to_cents("12345678901234.56") == 1234567890123456


def test_amount_to_cents_immune_to_ambient_decimal_traps():
    # The helper must not depend on the caller's Decimal context. With the
    # ambient Rounded trap enabled, an exact amount like "10.00" must still parse
    # (to_integral_exact signals Rounded when discarding trailing zeros).
    import decimal

    ctx = decimal.getcontext()
    prev = ctx.traps[decimal.Rounded]
    ctx.traps[decimal.Rounded] = True
    try:
        assert normalize.amount_to_cents("10.00") == 1000
        assert normalize.amount_to_cents("10.005") is None
        assert normalize.amount_to_cents("1e999999") is None
    finally:
        ctx.traps[decimal.Rounded] = prev
