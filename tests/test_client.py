import base64

import pytest

from finance_mcp import client


def test_split_access_url_extracts_credentials():
    ep = client._split_access_url("https://user:pass@bridge.example.org/simplefin")
    assert ep.base == "https://bridge.example.org/simplefin"
    assert ep.username == "user"
    assert ep.password == "pass"


def test_split_access_url_requires_credentials():
    with pytest.raises(client.SimpleFINError):
        client._split_access_url("https://bridge.example.org/simplefin")


def test_split_access_url_unquotes_credentials():
    ep = client._split_access_url("https://u%40s:p%2Fw@host.example/x")
    assert ep.username == "u@s"
    assert ep.password == "p/w"


def test_claim_rejects_non_base64():
    with pytest.raises(client.SimpleFINError):
        client.claim_setup_token("!!!not-base64!!!")


def test_claim_rejects_non_https_claim_url():
    token = base64.b64encode(b"http://insecure.example/claim").decode()
    with pytest.raises(client.SimpleFINError):
        client.claim_setup_token(token)
