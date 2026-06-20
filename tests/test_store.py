import json

from finance_mcp import store


def test_load_missing_returns_empty(tmp_path):
    cache = store.load_cache(tmp_path / "nope.json")
    assert cache["transactions"] == []
    assert cache["accounts"] == []
    assert cache["synced_at"] is None


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    cache = dict(store.EMPTY_CACHE)
    cache["transactions"] = [{"id": "t1", "amount_float": -5.0}]
    store.save_cache(cache, path)
    loaded = store.load_cache(path)
    assert loaded["transactions"][0]["id"] == "t1"
    assert loaded["synced_at"] is not None


def test_save_sets_owner_only_permissions(tmp_path):
    path = tmp_path / "cache.json"
    store.save_cache(dict(store.EMPTY_CACHE), path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupt_cache_falls_back_to_empty(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    cache = store.load_cache(path)
    assert cache["transactions"] == []


def test_load_backfills_missing_keys(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"transactions": []}), encoding="utf-8")
    cache = store.load_cache(path)
    assert "accounts" in cache and "errors" in cache and "errlist" in cache
