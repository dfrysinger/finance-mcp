"""Fixed, live Crew reads with a user-established private session."""

from __future__ import annotations

import fcntl
import getpass
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone

from . import client, config

_BASE = "https://api.trycrew.com/willow"
_TIMEOUT = 30.0
_PAGE_SIZE = 100
_ENDPOINTS = {
    "pockets": "/graphql",
    "activity": "/graphql",
    "transfers": "/graphql",
    "send_sms_otp": "/auth/send_sms_otp",
    "auth_sms_otp": "/auth/auth_sms_otp",
    "send_email_otp": "/auth/send_email_otp",
    "auth_email_otp": "/auth/auth_email_otp",
}
_ENDPOINT_FIELDS = """
    accountFrom { id name } accountTo { id name }
    subaccountFrom { id name } subaccountTo { id name }
"""
_QUERIES = {
    "pockets": """query FinancePockets {
      currentUser { accounts {
        id name type overallBalance clearedBalance balanceLastRefreshed
        subaccounts(types: [BILL, BILL_RESERVE, CREDIT, CREDIT_RESERVE, SAVINGS, SPENDING]) {
          id name subaccountType overallBalance clearedBalance
        }
      } }
    }""",
    "activity": """query FinancePocketActivity(
      $accountId: ID!, $filter: CashTransactionFilter!, $first: Int!, $after: String
    ) {
      node(id: $accountId) { ... on Account {
        id cashTransactions(first: $first, after: $after, searchFilters: $filter) {
          edges { node {
            id amount title memo status type occurredAt clearedAt
            subaccount { id name }
            transfer { id """ + _ENDPOINT_FIELDS + """ }
          } }
          pageInfo { hasNextPage endCursor }
        }
      } }
    }""",
    "transfers": """query FinanceTransfers($first: Int!, $after: String) {
      currentUser { transfers(first: $first, after: $after) {
        edges { node {
          id amount status type memo occurredAt """ + _ENDPOINT_FIELDS + """
        } }
        pageInfo { hasNextPage endCursor }
      } }
    }""",
}
_MESSAGES = {
    "auth_required": "Crew sign-in is required. Run `finance-mcp crew login` in a terminal.",
    "busy": "Another Crew operation is running. Try again after it finishes.",
    "storage_error": "Crew session storage is unavailable or not private. Check the finance home permissions.",
    "network_error": "Could not reach Crew. Try again later.",
    "provider_error": "Crew rejected the request. Try again later.",
    "protocol_error": "Crew returned an unsupported response. No complete result is available.",
    "redirect_refused": "Crew redirected the request. No credential was forwarded.",
    "terminal_required": "Run `finance-mcp crew login` yourself in an interactive terminal.",
    "login_incomplete": "Crew sign-in was not completed. No new session was saved.",
    "invalid_input": "Supply nonempty account and pocket IDs, and a nonempty cursor if provided.",
}


class CrewError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])

    def result(self) -> dict:
        return {"ok": False, "source": "crew_api",
                "error_code": self.code, "error": str(self)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_open = urllib.request.build_opener(_NoRedirect()).open


def _private_home():
    try:
        home = config.home_dir()
        if home.stat().st_mode & 0o777 != 0o700:
            raise CrewError("storage_error")
        return home
    except OSError:
        raise CrewError("storage_error") from None


@contextmanager
def _locked():
    descriptor = None
    try:
        _private_home()
        descriptor = os.open(config.crew_lock_path(),
                             os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CrewError("busy") from None
        yield
    except OSError:
        raise CrewError("storage_error") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _save_session(token: str, *, usable=True):
    temporary = None
    try:
        home = _private_home()
        descriptor, temporary = tempfile.mkstemp(prefix=".crew-session-", dir=home)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"token": token, "usable": usable}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config.crew_session_path())
    except OSError:
        raise CrewError("storage_error") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                raise CrewError("storage_error") from None


def _load_session() -> str:
    try:
        _private_home()
        path = config.crew_session_path()
        if path.stat().st_mode & 0o777 != 0o600:
            raise CrewError("storage_error")
        session = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(session, dict):
            raise CrewError("storage_error")
        if session.get("usable") is not True:
            raise CrewError("auth_required")
        token = session["token"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise CrewError("storage_error")
        return token
    except FileNotFoundError:
        raise CrewError("auth_required") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise CrewError("storage_error") from None


class _Client:
    def __init__(self, token="", *, persist=False):
        self.token = token
        self.persist = persist

    def _capture(self, headers, *, usable=True, required=False):
        token = (headers.get("Authorization", "") if headers else "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if required and (token.lower() == "bearer" or
                         re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None):
            raise CrewError("protocol_error")
        token = token or self.token
        if not token:
            return
        if any(c.isspace() for c in token):
            raise CrewError("protocol_error")
        if self.persist:
            _save_session(token, usable=usable)
        self.token = token

    def request(self, operation: str, payload: dict) -> dict:
        request = urllib.request.Request(
            _BASE + _ENDPOINTS[operation],
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": client.USER_AGENT},
            method="POST",
        )
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        if self.persist:
            # A lost response or failed rotation save must not resurrect the old token.
            _save_session(self.token, usable=False)
        try:
            with _open(request, timeout=_TIMEOUT) as response:
                self._capture(response.headers, required=operation == "auth_email_otp")
                if operation == "auth_email_otp":
                    return {}
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                self._capture(error.headers, usable=error.code != 401)
                code = ("auth_required" if error.code == 401 else
                        "redirect_refused" if 300 <= error.code < 400 else
                        "provider_error")
                raise CrewError(code) from None
            finally:
                error.close()
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            raise CrewError("network_error") from None
        except (ValueError, UnicodeError):
            raise CrewError("protocol_error") from None
        return _object(data)


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise CrewError("protocol_error")
    return value


def _list(value) -> list:
    if not isinstance(value, list):
        raise CrewError("protocol_error")
    return value


def _text(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise CrewError("protocol_error")
    return value


def _cents(value):
    if value is not None and type(value) is not int:
        raise CrewError("protocol_error")
    return value


def _time(value, *, required=False):
    value = _text(value, required=required)
    if value is not None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise CrewError("protocol_error") from None
    return value


def _identity(value):
    if value is None:
        return None
    row = _object(value)
    return {"id": _text(row["id"], required=True), "name": _text(row["name"])}


def _endpoints(row):
    return {key: _identity(row[field]) for key, field in (
        ("from_account", "accountFrom"), ("to_account", "accountTo"),
        ("from_pocket", "subaccountFrom"), ("to_pocket", "subaccountTo"),
    )}


def _event(row):
    row = _object(row)
    return {"id": _text(row["id"], required=True), "amount_cents": _cents(row["amount"]),
            "status": _text(row["status"], required=True),
            "type": _text(row["type"], required=True),
            "occurred_at": _time(row["occurredAt"], required=True),
            "memo": _text(row["memo"])}


def _page(connection, after, parse):
    connection = _object(connection)
    info = _object(connection["pageInfo"])
    more = info["hasNextPage"]
    cursor = _text(info["endCursor"])
    if type(more) is not bool or (more and (not cursor or cursor == after)):
        raise CrewError("protocol_error")
    rows = [parse(_object(edge)["node"]) for edge in _list(connection["edges"])]
    return {"items": rows, "returned": len(rows), "coverage": "page",
            "has_next_page": more, "end_cursor": cursor}


def _read(operation, variables, parse):
    try:
        with _locked():
            response = _Client(_load_session(), persist=True).request(
                operation, {"query": _QUERIES[operation], "variables": variables}
            )
            if response.get("errors"):
                raise CrewError("protocol_error")
            data = _object(response["data"])
            result = parse(data)
            return {"ok": True, "source": "crew_api",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(), **result}
    except CrewError as error:
        return error.result()
    except (KeyError, TypeError, ValueError):
        return CrewError("protocol_error").result()


def pockets() -> dict:
    def parse(data):
        accounts = []
        for value in _list(_object(data["currentUser"])["accounts"]):
            row = _object(value)
            account = _identity(row)
            account.update(
                type=_text(row["type"], required=True),
                overall_balance_cents=_cents(row["overallBalance"]),
                cleared_balance_cents=_cents(row["clearedBalance"]),
                balance_as_of=_time(row["balanceLastRefreshed"]),
            )
            children = []
            for value in _list(row["subaccounts"]):
                pocket = _object(value)
                children.append({
                    **_identity(pocket),
                    "type": _text(pocket["subaccountType"], required=True),
                    "overall_balance_cents": _cents(pocket["overallBalance"]),
                    "cleared_balance_cents": _cents(pocket["clearedBalance"]),
                })
            account["pockets"] = children
            accounts.append(account)
        return {"accounts": accounts}
    return _read("pockets", {}, parse)


def pocket_activity(account_id: str, pocket_id: str, after: str | None = None) -> dict:
    if not _valid_ids(account_id, pocket_id, after=after):
        return CrewError("invalid_input").result()

    def event(value):
        row = _object(value)
        pocket = _identity(row["subaccount"])
        if pocket is not None and pocket["id"] != pocket_id:
            raise CrewError("protocol_error")
        transfer = row["transfer"]
        return {**_event(row), "title": _text(row["title"]),
                "cleared_at": _time(row["clearedAt"]), "pocket": pocket,
                "transfer": None if transfer is None else {
                    "id": _text(_object(transfer)["id"], required=True),
                    **_endpoints(transfer),
                }}

    def parse(data):
        account = _object(data["node"])
        if account["id"] != account_id:
            raise CrewError("protocol_error")
        return {"account_id": account_id, "pocket_id": pocket_id,
                **_page(account["cashTransactions"], after, event)}

    return _read("activity", {
        "accountId": account_id, "filter": {"subaccountId": pocket_id},
        "first": _PAGE_SIZE, "after": after,
    }, parse)


def transfers(after: str | None = None) -> dict:
    if not _valid_ids(after=after):
        return CrewError("invalid_input").result()

    def event(value):
        row = _object(value)
        return {**_event(row), **_endpoints(row)}

    return _read("transfers", {"first": _PAGE_SIZE, "after": after}, lambda data:
                 _page(_object(data["currentUser"])["transfers"], after, event))


def _valid_ids(*values, after=None):
    return all(isinstance(v, str) and bool(v.strip()) for v in values) and (
        after is None or isinstance(after, str) and bool(after.strip())
    )


def _hidden_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt).strip()
    if not value:
        raise CrewError("login_incomplete")
    return value


def login() -> dict:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return {**CrewError("terminal_required").result(), "login_stage": "terminal_check"}
    stage = "session_lock"
    try:
        with _locked():
            connection = _Client()
            stage = "send_sms_otp"
            phone_id = _text(connection.request("send_sms_otp", {
                "phone": _hidden_input("Crew phone number (hidden): "),
            })["phone_id"], required=True)
            stage = "auth_sms_otp"
            verified = connection.request("auth_sms_otp", {
                "phone_id": phone_id, "otp": _hidden_input("SMS code (hidden): "),
            })
            single = verified["isSingleFactor"]
            if type(single) is not bool:
                raise CrewError("protocol_error")
            if not single:
                if not connection.token:
                    raise CrewError("protocol_error")
                stage = "send_email_otp"
                email_id = _text(connection.request("send_email_otp", {
                    "email": _hidden_input("Crew email (hidden): "),
                })["email_id"], required=True)
                stage = "auth_email_otp"
                connection.request("auth_email_otp", {
                    "email_id": email_id, "otp": _hidden_input("Email code (hidden): "),
                })
            if not connection.token:
                raise CrewError("protocol_error")
            stage = "save_session"
            _save_session(connection.token)
        return {"ok": True, "configured": True,
                "message": "Crew session saved. Read pockets to verify data access."}
    except CrewError as error:
        result = error.result()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        result = CrewError("login_incomplete").result()
    except (KeyError, TypeError, ValueError):
        result = CrewError("protocol_error").result()
    return {**result, "login_stage": stage}


def status() -> dict:
    try:
        _private_home()
        return {"ok": True, "configured":
                config.crew_session_path().exists(),
                "authentication_verified": False}
    except CrewError as error:
        return error.result()
    except OSError:
        return CrewError("storage_error").result()


def disconnect() -> dict:
    try:
        with _locked():
            config.crew_session_path().unlink(missing_ok=True)
        return {"ok": True, "configured": False,
                "message": "Local Crew session removed; upstream access was not revoked."}
    except CrewError as error:
        return error.result()
