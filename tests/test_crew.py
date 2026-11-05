import io
import http.client
import json
import os
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from finance_mcp import crew


@pytest.fixture
def crew_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    crew._save_session("fixture-token")
    return tmp_path


def _reply(data, token=None):
    class Response(io.BytesIO):
        headers = {"Authorization": token} if token else {}

    return Response(json.dumps(data).encode())


def _pocket_response(balance=12500):
    return {"data": {"currentUser": {"accounts": [{
        "id": "account-1", "name": "Fixture checking", "type": "SPEND",
        "overallBalance": 20000, "clearedBalance": 19000,
        "balanceLastRefreshed": None,
        "subaccounts": [{
            "id": "pocket-1", "name": "Fixture savings",
            "subaccountType": "SAVINGS", "overallBalance": balance,
            "clearedBalance": balance,
        }],
    }]}}}


def test_pockets_live_read_preserves_cents_null_and_rotation(crew_home, monkeypatch):
    requests = []

    def request(req, *, timeout):
        requests.append(req)
        return _reply(_pocket_response(None), "Bearer fixture-rotated")

    monkeypatch.setattr(crew, "_open", request)
    result = crew.pockets()
    assert result["ok"] is True
    assert result["source"] == "crew_api"
    assert result["retrieved_at"]
    assert result["accounts"][0]["pockets"][0]["overall_balance_cents"] is None
    assert json.loads((crew_home / "crew_session.json").read_text())["token"] == "fixture-rotated"
    assert requests[0].full_url == "https://api.trycrew.com/willow/graphql"
    assert os.stat(crew_home / "crew_session.json").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("data", [
    {"data": {"currentUser": None}},
    {"data": {"currentUser": {"accounts": None}}},
    {"data": {}, "errors": [{"message": "secret-sentinel"}]},
    _pocket_response(True),
])
def test_bad_provider_data_is_not_empty_success(crew_home, monkeypatch, data):
    monkeypatch.setattr(crew, "_open", lambda *a, **k: _reply(data))
    result = crew.pockets()
    assert result["ok"] is False
    assert result["error_code"] == "protocol_error"
    assert "secret-sentinel" not in json.dumps(result)


def test_no_session_returns_login_guidance_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.pockets()["error_code"] == "auth_required"


def test_noninteractive_login_sends_no_otp(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.login()["error_code"] == "terminal_required"
    assert not (tmp_path / "crew_session.json").exists()


def test_failed_rotation_save_prevents_next_request(crew_home, monkeypatch):
    calls = []

    def request(req, **kwargs):
        calls.append(req)
        return _reply(_pocket_response(), "Bearer fixture-rotated")

    save = crew._save_session

    def fail_rotation(token, **kwargs):
        if token == "fixture-rotated":
            raise crew.CrewError("storage_error")
        return save(token, **kwargs)

    monkeypatch.setattr(crew, "_open", request)
    monkeypatch.setattr(crew, "_save_session", fail_rotation)
    assert crew.pockets()["error_code"] == "storage_error"
    assert crew.pockets()["error_code"] == "auth_required"
    assert len(calls) == 1


def _transfer(**changes):
    row = {
        "id": "transfer-1", "amount": 5000, "status": "PENDING", "type": "INTERNAL",
        "occurredAt": "2026-01-15T12:00:00Z", "memo": None,
        "accountFrom": {"id": "account-1", "name": "Fixture checking"},
        "accountTo": {"id": "account-1", "name": "Fixture checking"},
        "subaccountFrom": {"id": "pocket-1", "name": "Fixture savings"},
        "subaccountTo": {"id": "pocket-2", "name": "Fixture spending"},
    }
    return {**row, **changes}


def _connection(items, *, more=False, cursor=None):
    return {"edges": [{"node": row} for row in items],
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def test_transfer_pages_preserve_direction_status_and_unknown_endpoint(crew_home, monkeypatch):
    pages = [
        _connection([_transfer()], more=True, cursor="page-2"),
        _connection([_transfer(id="transfer-2", status="CANCELED", subaccountTo=None)]),
        _connection([]),
    ]
    requests = []

    def request(req, **kwargs):
        requests.append(json.loads(req.data))
        return _reply({"data": {"currentUser": {"transfers": pages.pop(0)}}})

    monkeypatch.setattr(crew, "_open", request)
    first = crew.transfers()
    assert first["coverage"] == "page" and first["has_next_page"]
    assert first["items"][0]["from_pocket"]["id"] == "pocket-1"
    assert first["items"][0]["to_pocket"]["id"] == "pocket-2"
    assert first["items"][0]["status"] == "PENDING"
    second = crew.transfers(after=first["end_cursor"])
    assert second["items"][0]["status"] == "CANCELED"
    assert second["items"][0]["to_pocket"] is None
    assert not second["has_next_page"]
    assert requests[1]["variables"]["after"] == "page-2"
    assert crew.transfers()["items"] == []


@pytest.mark.parametrize("cursor", [None, "", "same-cursor"])
def test_bad_next_cursor_is_protocol_error(crew_home, monkeypatch, cursor):
    data = {"data": {"currentUser": {"transfers":
            _connection([], more=True, cursor=cursor)}}}
    monkeypatch.setattr(crew, "_open", lambda *a, **k: _reply(data))
    assert crew.transfers(after="same-cursor")["error_code"] == "protocol_error"


def test_activity_uses_variables_and_preserves_signed_amount(crew_home, monkeypatch):
    attack = 'pocket-1") { mutation { transfer } } #'
    row = {
        "id": "activity-1", "amount": -1234, "title": "Fixture purchase", "memo": None,
        "status": "CLEARED", "type": "CARD", "occurredAt": "2026-01-15T12:00:00Z",
        "clearedAt": None, "subaccount": {"id": attack, "name": "Fixture pocket"},
        "transfer": None,
    }
    requests = []

    def request(req, **kwargs):
        requests.append(json.loads(req.data))
        return _reply({"data": {"node": {
            "id": "account-1", "cashTransactions": _connection([row]),
        }}})

    monkeypatch.setattr(crew, "_open", request)
    result = crew.pocket_activity("account-1", attack)
    assert result["ok"] and result["items"][0]["amount_cents"] == -1234
    assert result["items"][0]["cleared_at"] is None
    assert requests[0]["query"] == crew._QUERIES["activity"]
    assert attack not in requests[0]["query"]
    assert requests[0]["variables"]["filter"] == {"subaccountId": attack}
    row["subaccount"]["id"] = "wrong-pocket"
    assert crew.pocket_activity("account-1", attack)["error_code"] == "protocol_error"


@pytest.mark.parametrize("kind", ["body", "graphql", "http"])
def test_error_responses_save_rotation_before_reporting_failure(crew_home, monkeypatch, kind):
    def request(req, **kwargs):
        if kind == "http":
            raise urllib.error.HTTPError(
                req.full_url, 500, "secret-sentinel",
                {"Authorization": "Bearer fixture-new"}, io.BytesIO(b"secret-sentinel"),
            )
        if kind == "body":
            result = _reply(None, "Bearer fixture-new")
            result.seek(0)
            result.truncate()
            result.write(b"not-json secret-sentinel")
            result.seek(0)
            return result
        return _reply({"errors": [{"message": "secret-sentinel"}],
                       "data": _pocket_response()["data"]}, "Bearer fixture-new")

    monkeypatch.setattr(crew, "_open", request)
    result = crew.pockets()
    assert result["ok"] is False
    assert "secret-sentinel" not in json.dumps(result)
    saved = json.loads((crew_home / "crew_session.json").read_text())
    assert saved == {"token": "fixture-new", "usable": True}

    def next_request(req, **kwargs):
        assert req.get_header("Authorization") == "Bearer fixture-new"
        return _reply(_pocket_response())

    monkeypatch.setattr(crew, "_open", next_request)
    assert crew.pockets()["ok"]


def test_auth_failure_and_lost_response_require_attended_login(crew_home, monkeypatch):
    def unauthorized(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 401, "private", {}, io.BytesIO())

    monkeypatch.setattr(crew, "_open", unauthorized)
    assert crew.pockets()["error_code"] == "auth_required"
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("stale request"))
    assert crew.pockets()["error_code"] == "auth_required"
    crew._save_session("fixture-token")

    def lost(*a, **k):
        raise urllib.error.URLError("private-detail")

    monkeypatch.setattr(crew, "_open", lost)
    assert crew.pockets()["error_code"] == "network_error"
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("stale request"))
    assert crew.pockets()["error_code"] == "auth_required"


def test_truncated_response_retains_rotation_and_returns_redacted_error(crew_home, monkeypatch):
    class Truncated(io.BytesIO):
        headers = {"Authorization": "Bearer fixture-after-headers"}

        def read(self, *args):
            raise http.client.IncompleteRead(b"secret-sentinel", 20)

    monkeypatch.setattr(crew, "_open", lambda *a, **k: Truncated())
    result = crew.pockets()
    assert result["error_code"] == "network_error"
    assert "secret-sentinel" not in json.dumps(result)
    assert json.loads((crew_home / "crew_session.json").read_text()) == {
        "token": "fixture-after-headers", "usable": True,
    }


def test_bad_http_status_requires_login_without_exposing_provider_text(crew_home, monkeypatch):
    def malformed(*a, **k):
        raise http.client.BadStatusLine("secret-sentinel")

    monkeypatch.setattr(crew, "_open", malformed)
    result = crew.pockets()
    assert result["error_code"] == "network_error"
    assert "secret-sentinel" not in json.dumps(result)
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("stale request"))
    assert crew.pockets()["error_code"] == "auth_required"


def test_lock_contends_with_same_process_and_another_process(crew_home, monkeypatch):
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("busy network"))
    with crew._locked():
        assert crew.pockets()["error_code"] == "busy"
        assert crew.status()["configured"]
        result = subprocess.run(
            [sys.executable, "-c",
             "import json;from finance_mcp import crew;print(json.dumps(crew.pockets()))"],
            text=True, capture_output=True, check=True, timeout=15,
        )
        assert json.loads(result.stdout)["error_code"] == "busy"


def test_status_is_metadata_only_and_disconnect_is_local(crew_home, monkeypatch):
    monkeypatch.setattr(type(crew_home), "read_text", lambda *a, **k: pytest.fail("read"))
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.status() == {"ok": True, "configured": True, "authentication_verified": False}
    assert crew.disconnect()["ok"]
    assert not crew.status()["configured"]
    assert (crew_home / "crew.lock").exists()


@pytest.mark.parametrize("single_factor,final_body", [
    (True, b""), (False, b""), (False, b"OK"), (False, b"null"),
])
def test_attended_login_publishes_only_complete_session(
    tmp_path, monkeypatch, capsys, single_factor, final_body,
):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(crew.sys.stderr, "isatty", lambda: True)
    inputs = iter(["fixture-phone", "fixture-sms", "fixture-email", "fixture-email-otp"])
    monkeypatch.setattr(crew.getpass, "getpass", lambda prompt: next(inputs))
    steps = []

    def request(req, **kwargs):
        assert not (tmp_path / "crew_session.json").exists()
        steps.append(req.full_url.rsplit("/", 1)[-1])
        if steps[-1] == "send_sms_otp":
            return _reply({"phone_id": "fixture-phone-id"})
        if steps[-1] == "auth_sms_otp":
            return _reply({"isSingleFactor": single_factor}, "Bearer fixture-intermediate")
        if steps[-1] == "send_email_otp":
            assert req.get_header("Authorization") == "Bearer fixture-intermediate"
            return _reply({"email_id": "fixture-email-id"}, "Bearer fixture-step-3")
        response = _reply({}, "Bearer fixture-complete")
        response.truncate(0)
        response.write(final_body)
        response.seek(0)
        return response

    monkeypatch.setattr(crew, "_open", request)
    assert crew.login()["ok"]
    saved = json.loads((tmp_path / "crew_session.json").read_text())
    assert saved["token"] == ("fixture-intermediate" if single_factor else "fixture-complete")
    assert len(steps) == (2 if single_factor else 4)
    assert capsys.readouterr() == ("", "")
    assert set(saved) == {"token", "usable"}


def _email_login_setup(tmp_path, monkeypatch, responses):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(crew.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(crew.getpass, "getpass", lambda prompt: "private-input-sentinel")

    def request(req, **kwargs):
        assert not (tmp_path / "crew_session.json").exists()
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(crew, "_open", request)


def _email_login_responses():
    return [
        _reply({"phone_id": "fixture-phone-id"}),
        _reply({"isSingleFactor": False}, "Bearer fixture-intermediate"),
        _reply({"email_id": "fixture-email-id"}, "Bearer fixture-next"),
        _reply({}, "Bearer fixture-complete"),
    ]


@pytest.mark.parametrize("token", [
    None, " ", "Bearer", "Bearer ", "bad token",
    "Bearer a,b", "Bearer abc=def", "Bearer caf\u00e9",
])
def test_email_login_requires_usable_token_from_final_response(tmp_path, monkeypatch, token):
    responses = _email_login_responses()
    responses[-1] = _reply({}, token)
    _email_login_setup(tmp_path, monkeypatch, responses)
    result = crew.login()
    assert result["ok"] is False
    assert result["error_code"] == "protocol_error"
    assert result["login_stage"] == "auth_email_otp"
    assert not (tmp_path / "crew_session.json").exists()
    assert "private-input-sentinel" not in json.dumps(result)


@pytest.mark.parametrize("step,stage", [
    (0, "send_sms_otp"), (1, "auth_sms_otp"), (2, "send_email_otp"),
])
def test_email_login_keeps_earlier_json_validation(tmp_path, monkeypatch, step, stage):
    responses = _email_login_responses()
    responses[step] = _reply(None)
    _email_login_setup(tmp_path, monkeypatch, responses)
    result = crew.login()
    assert result["error_code"] == "protocol_error"
    assert result["login_stage"] == stage
    assert not (tmp_path / "crew_session.json").exists()


@pytest.mark.parametrize("status,code", [
    (401, "auth_required"), (302, "redirect_refused"), (500, "provider_error"),
])
def test_email_login_preserves_http_failures(tmp_path, monkeypatch, status, code):
    responses = _email_login_responses()
    responses[-1] = urllib.error.HTTPError(
        "https://api.trycrew.com/willow/auth/auth_email_otp", status,
        "private-provider-sentinel", {"Authorization": "Bearer fixture-error-token"},
        io.BytesIO(b"private-provider-sentinel"),
    )
    _email_login_setup(tmp_path, monkeypatch, responses)
    result = crew.login()
    assert result["error_code"] == code
    assert result["login_stage"] == "auth_email_otp"
    assert not (tmp_path / "crew_session.json").exists()
    assert "private-provider-sentinel" not in json.dumps(result)


def test_email_login_identifies_final_storage_failure(tmp_path, monkeypatch):
    _email_login_setup(tmp_path, monkeypatch, _email_login_responses())

    def failed_save(*args, **kwargs):
        raise crew.CrewError("storage_error")

    monkeypatch.setattr(crew, "_save_session", failed_save)
    result = crew.login()
    assert result["error_code"] == "storage_error"
    assert result["login_stage"] == "save_session"
    assert not (tmp_path / "crew_session.json").exists()


def test_interrupted_login_never_publishes_intermediate_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(crew.sys.stderr, "isatty", lambda: True)
    calls = []

    def hidden(prompt):
        if calls:
            raise KeyboardInterrupt
        return "fixture-phone"

    def request(req, **kwargs):
        calls.append(req)
        return _reply({"phone_id": "fixture-id"}, "Bearer fixture-partial")

    monkeypatch.setattr(crew.getpass, "getpass", hidden)
    monkeypatch.setattr(crew, "_open", request)
    assert crew.login()["error_code"] == "login_incomplete"
    assert not (tmp_path / "crew_session.json").exists()


def test_redirect_does_not_forward_authorization(crew_home, monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", "/credential-leak")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(crew, "_BASE", f"http://127.0.0.1:{httpd.server_port}")
        assert crew.pockets()["error_code"] == "redirect_refused"
        assert received == ["/graphql"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_cli_and_mcp_share_reads_without_changing_archive(
    crew_home, monkeypatch, capsys, normalized,
):
    from finance_mcp import archive, cli, server

    conn = archive.connect()
    try:
        archive.upsert(conn, normalized)
    finally:
        conn.close()
    (crew_home / "cache.json").write_text(json.dumps(normalized))
    (crew_home / "budget.json").write_text('{"envelopes": []}')
    (crew_home / "access_url").write_text("fixture-access-not-a-credential")
    reports = (server.account_balances(), server.spending_summary())
    untouched = {name: (crew_home / name).read_bytes() for name in
                 ("archive.db", "cache.json", "budget.json", "access_url")}
    monkeypatch.setattr(crew, "_open", lambda *a, **k: _reply(_pocket_response()))
    assert cli.main(["crew", "pockets"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert server.crew_pockets()["accounts"][0]["pockets"]
    assert (server.account_balances(), server.spending_summary()) == reports
    for name, value in untouched.items():
        assert (crew_home / name).read_bytes() == value


def test_private_storage_failure_precedes_request(crew_home, monkeypatch):
    (crew_home / "crew_session.json").chmod(0o644)
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.pockets()["error_code"] == "storage_error"


def test_failed_preflight_write_sends_no_request(crew_home, monkeypatch):
    def failed(*a, **k):
        raise crew.CrewError("storage_error")

    monkeypatch.setattr(crew, "_save_session", failed)
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.pockets()["error_code"] == "storage_error"


def test_getpass_fallback_is_refused_before_sending_otp(tmp_path, monkeypatch):
    import warnings

    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(crew.sys.stderr, "isatty", lambda: True)

    def fallback(prompt):
        warnings.warn("fixture-no-tty", crew.getpass.GetPassWarning)
        pytest.fail("fallback read")

    monkeypatch.setattr(crew.getpass, "getpass", fallback)
    monkeypatch.setattr(crew, "_open", lambda *a, **k: pytest.fail("network"))
    assert crew.login()["error_code"] == "login_incomplete"
    assert not (tmp_path / "crew_session.json").exists()


@pytest.mark.parametrize("data", [
    {"data": {"currentUser": {"transfers": None}}},
    {"data": {"currentUser": {"transfers": _connection([
        _transfer(occurredAt="not-a-time"),
    ])}}},
    {"data": {"currentUser": {"transfers": _connection([
        _transfer(amount=False),
    ])}}},
])
def test_malformed_transfer_fields_fail_closed(crew_home, monkeypatch, data):
    monkeypatch.setattr(crew, "_open", lambda *a, **k: _reply(data))
    assert crew.transfers()["error_code"] == "protocol_error"


def test_empty_source_is_valid_but_missing_source_is_not(crew_home, monkeypatch):
    monkeypatch.setattr(crew, "_open", lambda *a, **k:
                        _reply({"data": {"currentUser": {"accounts": []}}}))
    assert crew.pockets()["accounts"] == []
    monkeypatch.setattr(crew, "_open", lambda *a, **k: _reply({"data": {}}))
    assert crew.pockets()["error_code"] == "protocol_error"


def test_cli_login_has_no_credential_flags(monkeypatch, capsys):
    from finance_mcp import cli

    monkeypatch.setattr(crew.sys.stdin, "isatty", lambda: False)
    assert cli.main(["crew", "login"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "terminal_required"
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["crew", "login", "--token", "fixture"])
    assert error.value.code == 2
