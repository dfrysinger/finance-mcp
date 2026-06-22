"""Local, read-only web UI for reviewing the archive and budgeting reports.

Binds to localhost by default and serves a single-page app plus a small JSON
API. Every endpoint is backed by the same functions the MCP server exposes
(``server.py``), so the browser view and Copilot see identical data and this
surface adds no new analysis logic. Nothing is mutated — this is a review-only
surface, so there is no confirm/sync endpoint here.

Start it with ``finance-mcp web`` (or ``finance-mcp-web``) and open the printed
URL. Because it serves real financial data, it binds to 127.0.0.1 by default;
override only with an explicit ``--host`` if you understand the exposure.
"""

from __future__ import annotations

import ipaddress
import json
import math
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import server

# Endpoint schema: api name -> (callable, {param: (kind, required)}).
# kind is one of "str" | "int" | "float" | "bool". Only listed params are
# forwarded; anything else in the query string is ignored. Empty strings are
# treated as "not supplied" so the underlying function's default applies.
_ENDPOINTS: dict[str, tuple] = {
    "accounts": (server.list_accounts, {}),
    "networth": (server.net_worth_history, {}),
    "stats": (server.archive_stats, {}),
    "categorization": (server.categorization_status, {}),
    "transactions": (
        server.get_transactions,
        {
            "start_date": ("str", False),
            "end_date": ("str", False),
            "account_id": ("str", False),
            "search": ("str", False),
            "category": ("str", False),
            "include_transfers": ("bool", False),
            "min_amount": ("float", False),
            "max_amount": ("float", False),
            "include_pending": ("bool", False),
            "limit": ("int", False),
        },
    ),
    "summary": (
        server.spending_summary,
        {
            "group_by": ("str", False),
            "start_date": ("str", False),
            "end_date": ("str", False),
            "include_pending": ("bool", False),
            "exclude_transfers": ("bool", False),
        },
    ),
    "burndown": (server.budget_burndown, {"month": ("str", True)}),
    "forecast": (
        server.budget_forecast,
        {"as_of": ("str", False), "through": ("str", False)},
    ),
    "allocation": (
        server.allocation_audit_report,
        {
            "start": ("str", False),
            "end": ("str", False),
            "day_tolerance": ("int", False),
        },
    ),
    "subscriptions": (
        server.subscription_audit_report,
        {
            "start": ("str", False),
            "end": ("str", False),
            "day_tolerance": ("int", False),
            "min_occurrences": ("int", False),
        },
    ),
    "transfers": (server.list_transfers, {"status": ("str", False)}),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce_one(name: str, kind: str, raw: str):
    if kind == "str":
        return raw
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer, got {raw!r}")
    if kind == "float":
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {raw!r}")
        return value
    if kind == "bool":
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{name} must be a boolean, got {raw!r}")
    raise ValueError(f"unknown param kind {kind!r}")  # pragma: no cover


def _build_kwargs(params: dict[str, list[str]], schema: dict[str, tuple]) -> dict:
    """Coerce whitelisted query params into kwargs; raise ValueError on bad input."""
    kwargs: dict = {}
    for name, (kind, required) in schema.items():
        values = params.get(name)
        raw = values[0] if values else ""
        if raw == "":
            if required:
                raise ValueError(f"missing required parameter: {name}")
            continue
        kwargs[name] = _coerce_one(name, kind, raw)
    return kwargs


def handle_api(name: str, params: dict[str, list[str]]) -> tuple[int, dict]:
    """Dispatch one API call. Returns (http_status, json_body).

    Pure function (no socket) so it is unit-testable. Bad input yields a 400 with
    a structured error; an unexpected failure yields a 500 with the error class
    and message (the full traceback goes to stderr, never to the browser).
    """
    spec = _ENDPOINTS.get(name)
    if spec is None:
        return 404, {"ok": False, "error": f"unknown endpoint: {name}"}
    fn, schema = spec
    try:
        kwargs = _build_kwargs(params, schema)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    try:
        result = fn(**kwargs)
    except Exception as exc:  # boundary guard: keep the server thread alive
        traceback.print_exc(file=sys.stderr)
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return 200, result


# Browser DNS-rebinding defense: a 127.0.0.1 bind alone does NOT protect the
# data, because a malicious page can point an attacker-controlled hostname at
# 127.0.0.1 and fetch the API. We additionally require the request's Host header
# to name a host we expect, and this check is ALWAYS enforced (it fails closed).
# A wildcard bind (0.0.0.0) does not weaken it: the allowlist is still just the
# loopback names plus any host the user explicitly passes via ``--allow-host``,
# so reaching the LAN-bound server requires naming the host you will use rather
# than accepting every Host header.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def _is_wildcard_name(name: str) -> bool:
    """True if ``name`` is empty or any spelling of the unspecified address.

    Catches the bare literals plus bracketed and long-form IPv6 (``[::]``,
    ``0:0:0:0:0:0:0:0``) so no spelling of "any host" can enter the allowlist.
    """
    if not name or name in _WILDCARD_HOSTS:
        return True
    candidate = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        return ipaddress.ip_address(candidate).is_unspecified
    except ValueError:
        return False


def _hostname_only(host_header: str) -> str:
    """Return the host portion of a Host header, stripping any :port."""
    h = host_header.strip()
    if h.startswith("["):  # bracketed IPv6, e.g. [::1]:8765
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if h.count(":") == 1:  # host:port (bare IPv6 is never valid unbracketed here)
        return h.rsplit(":", 1)[0]
    return h


def _host_policy(bind_host: str, extra_hosts: tuple[str, ...] = ()) -> frozenset[str]:
    """Return the set of Host names allowed to reach the API.

    Always includes the loopback names. A concrete (non-wildcard) bind host is
    allowed too. Anything else — including reaching a wildcard-bound server over
    the LAN — must be named explicitly in ``extra_hosts`` (``--allow-host``), so
    the allowlist never silently accepts an arbitrary Host header.
    """
    allowed = set(_LOOPBACK_HOSTS)
    if bind_host not in _WILDCARD_HOSTS:
        allowed.add(bind_host.lower())
    for h in extra_hosts:
        name = h.strip().lower()
        # Drop empties and every spelling of the unspecified address so a stray
        # --allow-host value can never widen the allowlist to "any host".
        if not _is_wildcard_name(name):
            allowed.add(name)
    return frozenset(allowed)


class _Handler(BaseHTTPRequestHandler):
    server_version = "finance-mcp-webui"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[webui] " + (fmt % args) + "\n")

    def _host_allowed(self) -> bool:
        # Always enforced. Default to loopback-only so a handler constructed
        # without an explicit policy (e.g. in tests) still fails closed off-host.
        allowed = getattr(self.server, "allowed_hosts", _LOOPBACK_HOSTS)
        name = _hostname_only(self.headers.get("Host", "")).lower()
        return name in allowed

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This is a localhost review tool serving private data; forbid embedding
        # and disable client-side caching so a reload always reflects the archive.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if not self._host_allowed():
            # Reject foreign Host headers (DNS-rebinding) before touching data.
            self._send(403, b"forbidden host", "text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            name = path[len("/api/"):]
            params = parse_qs(parsed.query, keep_blank_values=True)
            status, body = handle_api(name, params)
            payload = json.dumps(body, default=str).encode("utf-8")
            self._send(status, payload, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_hosts: tuple[str, ...] = (),
) -> int:
    """Run the web UI until interrupted. Returns a process exit code.

    ``allow_hosts`` adds Host-header names that may reach the API (needed to
    reach a non-loopback or wildcard bind from another device); the loopback
    names are always allowed.
    """
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.allowed_hosts = _host_policy(host, tuple(allow_hosts))
    bound_host, bound_port = httpd.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"finance-mcp web UI on {url}")
    print("Read-only review surface. Press Ctrl+C to stop.")
    if not _is_loopback(host):
        print(
            f"WARNING: bound to {host} — this exposes your private financial data "
            "to the network with NO authentication. Use 127.0.0.1 unless you "
            "understand the exposure. Other devices must reach it via an "
            "--allow-host name; every other Host header is refused.",
            file=sys.stderr,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0


def _console_main() -> int:
    """Entry point for the ``finance-mcp-web`` console script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="finance-mcp-web",
        description="Serve the local read-only finance-mcp review UI.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; serves private data)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                        help="extra Host-header name allowed to reach the API "
                             "(repeatable; needed to reach a non-loopback bind)")
    args = parser.parse_args()
    return serve(host=args.host, port=args.port, allow_hosts=tuple(args.allow_host))


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>finance-mcp review</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2b3440; --fg:#e6edf3;
          --muted:#8b98a5; --accent:#4493f8; --good:#3fb950; --warn:#d29922;
          --bad:#f85149; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:12px; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .sub { color:var(--muted); font-size:12px; }
  nav { display:flex; flex-wrap:wrap; gap:4px; padding:10px 20px;
        border-bottom:1px solid var(--line); }
  nav button { background:transparent; color:var(--muted); border:1px solid transparent;
               padding:6px 12px; border-radius:6px; cursor:pointer; font-size:13px; }
  nav button:hover { color:var(--fg); }
  nav button.active { background:var(--panel); color:var(--fg); border-color:var(--line); }
  main { padding:20px; }
  .filters { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end;
             margin-bottom:14px; }
  .filters label { display:flex; flex-direction:column; font-size:11px;
                   color:var(--muted); gap:3px; }
  .filters input, .filters select { background:var(--panel); color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:5px 7px; font-size:13px; }
  .filters input[type=text], .filters input[type=date] { min-width:130px; }
  button.go { background:var(--accent); color:#fff; border:none; border-radius:6px;
              padding:7px 14px; cursor:pointer; font-size:13px; height:31px; }
  button.go:hover { filter:brightness(1.1); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--bg); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:var(--panel); }
  .neg { color:var(--bad); } .pos { color:var(--good); }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
          border:1px solid var(--line); color:var(--muted); }
  .pill.good { color:var(--good); border-color:var(--good); }
  .pill.warn { color:var(--warn); border-color:var(--warn); }
  .pill.bad  { color:var(--bad);  border-color:var(--bad); }
  .notice { padding:12px 14px; border:1px solid var(--warn); border-radius:8px;
            background:rgba(210,153,34,.08); color:var(--warn); margin-bottom:14px; }
  .err { border-color:var(--bad); background:rgba(248,81,73,.08); color:var(--bad); }
  .muted { color:var(--muted); }
  h2 { font-size:14px; margin:18px 0 8px; }
  details { margin-top:18px; }
  details summary { cursor:pointer; color:var(--muted); font-size:12px; }
  pre { background:var(--panel); border:1px solid var(--line); border-radius:8px;
        padding:12px; overflow:auto; font-size:12px; max-height:60vh; }
  .cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:10px 14px; min-width:120px; }
  .card .k { color:var(--muted); font-size:11px; }
  .card .v { font-size:18px; font-weight:600; }
</style>
</head>
<body>
<header>
  <h1>finance-mcp</h1>
  <span class="sub">local review &middot; read-only</span>
  <span class="sub" id="syncedAt"></span>
</header>
<nav id="tabs"></nav>
<main>
  <div class="filters" id="filters"></div>
  <div id="content"><span class="muted">Loading&hellip;</span></div>
  <details id="rawWrap" hidden>
    <summary>Raw JSON</summary>
    <pre id="raw"></pre>
  </details>
</main>
<script>
const TABS = [
  { id:"accounts",      label:"Accounts",      filters:[] },
  { id:"transactions",  label:"Transactions",  filters:[
      {k:"search",type:"text",ph:"merchant / memo"},
      {k:"category",type:"text",ph:"category"},
      {k:"start_date",type:"date"}, {k:"end_date",type:"date"},
      {k:"include_transfers",type:"bool",label:"transfers",def:false},
      {k:"limit",type:"number",def:200} ] },
  { id:"summary",       label:"Spending",      filters:[
      {k:"group_by",type:"select",opts:["category","account","org","month"]},
      {k:"start_date",type:"date"}, {k:"end_date",type:"date"} ] },
  { id:"networth",      label:"Net worth",     filters:[] },
  { id:"transfers",     label:"Transfers",     filters:[
      {k:"status",type:"select",opts:["","unconfirmed","inferred","confirmed","unmatched"]} ] },
  { id:"burndown",      label:"Burn-down",     filters:[
      {k:"month",type:"month"} ] },
  { id:"forecast",      label:"Forecast",      filters:[
      {k:"as_of",type:"date"}, {k:"through",type:"date"} ] },
  { id:"allocation",    label:"Allocation",    filters:[
      {k:"start",type:"date"}, {k:"end",type:"date"},
      {k:"day_tolerance",type:"number",def:7} ] },
  { id:"subscriptions", label:"Subscriptions", filters:[
      {k:"start",type:"date"}, {k:"end",type:"date"},
      {k:"day_tolerance",type:"number",def:7},
      {k:"min_occurrences",type:"number",def:3} ] },
];

let current = TABS[0];
let DEFAULTS = {};
const $ = (id) => document.getElementById(id);

function money(v) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (Number.isNaN(n)) return esc(v);
  const s = n.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
  return `<span class="${n<0?'neg':n>0?'pos':''}">${s}</span>`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function table(rows, cols) {
  if (!rows || !rows.length) return `<p class="muted">No rows.</p>`;
  const head = cols.map(c => `<th class="${c.num?'num':''}">${esc(c.label)}</th>`).join("");
  const body = rows.map(r => "<tr>" + cols.map(c => {
    const raw = c.get ? c.get(r) : r[c.k];
    const cell = c.money ? money(raw) : (c.html ? raw : esc(raw));
    return `<td class="${c.num?'num':''}">${cell ?? ""}</td>`;
  }).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function pill(text, cls) { return `<span class="pill ${cls||''}">${esc(text)}</span>`; }

function buildFilters() {
  const wrap = $("filters"); wrap.innerHTML = "";
  for (const f of current.filters) {
    const lab = document.createElement("label");
    lab.textContent = f.label || f.k;
    let el;
    if (f.type === "select") {
      el = document.createElement("select");
      for (const o of f.opts)
        el.appendChild(new Option(o === "" ? "(all)" : o, o));
    } else if (f.type === "bool") {
      el = document.createElement("select");
      el.appendChild(new Option("yes","true")); el.appendChild(new Option("no","false"));
      el.value = f.def === false ? "false" : "true";
    } else {
      el = document.createElement("input");
      el.type = f.type === "month" ? "month" : f.type;
      if (f.ph) el.placeholder = f.ph;
      let def = f.def;
      // A month filter with no explicit default lands on the archive's latest
      // month (where the data actually is), so the view loads populated instead
      // of on an empty month the browser would otherwise pick.
      if (def === undefined && f.type === "month" && DEFAULTS.month) def = DEFAULTS.month;
      if (def !== undefined) el.value = def;
    }
    el.id = "f_" + f.k;
    lab.appendChild(el); wrap.appendChild(lab);
  }
  const go = document.createElement("button");
  go.className = "go"; go.textContent = "Load"; go.onclick = load;
  wrap.appendChild(go);
}
function collectParams() {
  const p = new URLSearchParams();
  for (const f of current.filters) {
    const v = ($("f_" + f.k) || {}).value;
    if (v !== undefined && v !== "") p.set(f.k, v);
  }
  return p.toString();
}

const RENDER = {
  accounts(d) {
    if (d.synced_at) $("syncedAt").textContent = "synced " + d.synced_at;
    const rows = (d.accounts||[]).slice().sort((a,b)=>(b.balance||0)-(a.balance||0));
    return `<div class="cards">
        <div class="card"><div class="k">accounts</div><div class="v">${d.account_count||0}</div></div>
      </div>` + table(rows, [
      {k:"org",label:"Institution"},
      {k:"account_name",label:"Account"},
      {k:"balance",label:"Balance",num:true,money:true},
      {k:"currency",label:"Cur"},
      {k:"balance_date",label:"As of"},
    ]);
  },
  transactions(d) {
    const note = `<p class="muted">${d.returned} of ${d.total_matches} matches shown.</p>`;
    return note + table(d.transactions, [
      {k:"posted",label:"Date"},
      {k:"account_name",label:"Account"},
      {label:"Description",get:r=>r.description||r.payee||r.memo||""},
      {k:"amount_float",label:"Amount",num:true,money:true},
      {k:"category",label:"Category"},
      {label:"Xfer",html:true,get:r=>r.is_transfer?pill("transfer"):""},
    ]);
  },
  summary(d) {
    const groups = d.groups || d.summary || [];
    const rows = Array.isArray(groups) ? groups
      : Object.entries(groups).map(([k,v])=>({key:k, ...(typeof v==='object'?v:{value:v})}));
    return table(rows, [
      {label:"Group",get:r=>r.key ?? r.group ?? r.name},
      {label:"Outflow",num:true,money:true,get:r=>r.outflow},
      {label:"Inflow",num:true,money:true,get:r=>r.inflow},
      {label:"Net",num:true,money:true,get:r=>r.net},
      {label:"Count",num:true,get:r=>r.count},
    ]);
  },
  networth(d) {
    return table(d.history, [
      {label:"As of",get:r=>r.as_of ?? r.date},
      {label:"Total",num:true,money:true,get:r=>r.total ?? r.net_worth},
    ]);
  },
  transfers(d) {
    const sum = d.summary || {};
    const cards = Object.entries(sum).map(([k,v])=>
      `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
    return `<div class="cards">${cards||''}</div>` + table(d.transfers, [
      {k:"link_id",label:"#",num:true},
      {label:"Status",html:true,get:r=>{
        const c = r.status==="confirmed"?"good":r.status==="unconfirmed"?"warn":r.status==="unmatched"?"bad":"";
        return pill(r.status,c);}},
      {label:"From",get:r=>r.from_account||"?"},
      {label:"To",get:r=>r.to_account||"?"},
      {label:"Amount",num:true,money:true,get:r=>r.amount},
      {k:"why",label:"Why"},
    ]);
  },
  burndown(d) {
    const env = d.envelopes || [];
    let out = `<p class="muted">Period ${esc(d.period || d.month || "")}.</p>` + table(env, [
      {label:"Envelope",get:r=>r.envelope ?? r.name},
      {label:"Target",num:true,money:true,get:r=>r.monthly_target},
      {label:"Spent",num:true,money:true,get:r=>r.actual_spend},
      {label:"Remaining",num:true,money:true,get:r=>r.remaining},
      {label:"Over?",html:true,get:r=>r.over_budget?pill("over","bad"):""},
    ]);
    const unmapped = d.unmapped || [];
    if (unmapped.length) {
      out += `<h2>Unmapped spend <span class="muted">(no envelope)</span></h2>`;
      out += table(unmapped, [
        {label:"Account",get:r=>r.account_name ?? r.account ?? r.account_id},
        {label:"Spend",num:true,money:true,get:r=>r.actual_spend},
        {label:"Count",num:true,get:r=>r.txn_count},
      ]);
    }
    return out;
  },
  forecast(d) {
    return `<p class="muted">${esc(d.as_of||"")} &rarr; ${esc(d.through||"")}</p>` +
      table(d.envelopes||[], [
      {label:"Envelope",get:r=>r.envelope ?? r.name},
      {label:"Verdict",html:true,get:r=>{
        const c = r.verdict==="sufficient"?"good":r.verdict==="at_risk"?"bad":"warn";
        return pill(r.verdict,c);}},
      {label:"Current",num:true,money:true,get:r=>r.current_balance},
      {label:"Min balance",num:true,money:true,get:r=>r.projected_min_balance},
      {label:"At risk",get:r=>r.at_risk_date||""},
      {label:"Shortfall",num:true,money:true,get:r=>r.shortfall},
    ]);
  },
  allocation(d) {
    const w = d.window||{}; const s = d.summary||{};
    const cards = Object.entries(s).map(([k,v])=>
      `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
    let out = `<p class="muted">${esc(w.start||"")} &rarr; ${esc(w.end||"")} (tol ${d.day_tolerance}d)</p>
      <div class="cards">${cards}</div>`;
    for (const tr of (d.transfers||[])) {
      out += `<h2>${esc(tr.name)} <span class="muted">${esc(tr.from_envelope||"(external)")} &rarr; ${esc(tr.to_envelope)} &middot; $${esc(tr.amount)} [${esc(tr.kind)}]</span></h2>`;
      out += table(tr.occurrences||[], [
        {label:"Expected",get:r=>r.expected_date},
        {label:"Status",html:true,get:r=>{
          const c = r.status==="on_time"?"good":r.status==="missing"?"bad":"warn";
          return pill(r.status,c);}},
        {label:"Drift",num:true,get:r=>r.drift_days==null?"":(r.drift_days>0?"+":"")+r.drift_days+"d"},
        {label:"Actual",num:true,money:true,get:r=>r.actual_amount},
        {label:"On",get:r=>r.actual_date||""},
      ]);
    }
    if (!(d.transfers||[]).length) out += `<p class="muted">No scheduled transfers configured.</p>`;
    return out;
  },
  subscriptions(d) {
    const sm = d.summary||{};
    const cameBack = d.came_back||[];
    let out = `<div class="cards">
      <div class="card"><div class="k">tracked</div><div class="v">${sm.tracked||0}</div></div>
      <div class="card"><div class="k">missing</div><div class="v">${sm.missing_occurrences||0}</div></div>
      <div class="card"><div class="k">came back</div><div class="v">${sm.came_back||0}</div></div>
      <div class="card"><div class="k">candidates</div><div class="v">${sm.candidates||0}</div></div>
    </div>`;
    if (cameBack.length) {
      out += `<h2>⚠ Canceled bills that came back</h2>`;
      out += `<p class="muted">You marked these canceling or canceled, but a charge posted on or after the cancellation date &mdash; the cancellation may not have taken.</p>`;
      out += table(cameBack, [
        {label:"Subscription",get:r=>r.name},
        {label:"Amount",num:true,money:true,get:r=>r.amount},
        {label:"Marked",html:true,get:r=>pill(r.lifecycle,"bad")},
        {label:"Effective",get:r=>r.cancel_effective||""},
        {label:"Charged again",get:r=>r.came_back_on||r.last_seen||"?"},
      ]);
    }
    out += `<h2>Tracked subscriptions</h2>`;
    if (!sm.tracked) {
      out += `<p class="muted">No saved subscriptions yet &mdash; run <code>finance-mcp subscriptions detect</code> to save your recurring charges as a tracked list.</p>`;
    }
    out += table(d.tracked||[], [
      {label:"Subscription",get:r=>r.name},
      {label:"Envelope",get:r=>r.envelope||""},
      {label:"Amount",num:true,money:true,get:r=>r.amount},
      {label:"Due day",num:true,get:r=>r.day},
      {label:"Next due",get:r=>r.next_due||""},
      {label:"Last seen",get:r=>r.last_seen||"never"},
      {label:"Status",html:true,get:r=>{
        const lc = r.lifecycle||"active";
        if (lc !== "active") {
          if (r.came_back) return pill("came back","bad");
          return pill(lc, lc==="canceling"?"warn":"");
        }
        return pill(r.status, r.status==="overdue"?"bad":r.status==="active"?"good":"warn");
      }},
    ]);
    out += `<h2>Missing expected charges</h2>`;
    out += table(d.expected_missing||[], [
      {label:"Bill",get:r=>r.name},
      {label:"Envelope",get:r=>r.envelope},
      {label:"Expected",num:true,money:true,get:r=>r.expected_amount},
      {label:"Due",get:r=>r.expected_date},
      {label:"Last seen",get:r=>r.last_seen||"never"},
    ]);
    out += `<h2>Untracked recurring candidates</h2>`;
    out += table(d.candidate_new||[], [
      {label:"Merchant",get:r=>r.merchant},
      {label:"Amount",num:true,money:true,get:r=>r.amount},
      {label:"Times",num:true,get:r=>r.occurrences},
      {label:"Cadence",get:r=>r.cadence},
      {label:"Last seen",get:r=>r.last_seen},
    ]);
    return out;
  },
};

async function load() {
  $("content").innerHTML = `<span class="muted">Loading&hellip;</span>`;
  $("rawWrap").hidden = true;
  const q = collectParams();
  const url = `/api/${current.id}${q ? "?" + q : ""}`;
  let status, data;
  try {
    const res = await fetch(url);
    status = res.status;
    data = await res.json();
  } catch (e) {
    $("content").innerHTML = `<div class="notice err">Request failed: ${esc(e)}</div>`;
    return;
  }
  $("raw").textContent = JSON.stringify(data, null, 2);
  $("rawWrap").hidden = false;
  if (data && data.ok === false) {
    $("content").innerHTML = `<div class="notice">${esc(data.error)}</div>`;
    return;
  }
  try {
    $("content").innerHTML = (RENDER[current.id] || (() => ""))(data) || "";
  } catch (e) {
    $("content").innerHTML = `<div class="notice err">Render error: ${esc(e)}</div>`;
  }
}

function selectTab(t) {
  current = t;
  for (const b of $("tabs").children) b.classList.toggle("active", b.dataset.id === t.id);
  buildFilters();
  load();
}
function init() {
  const nav = $("tabs");
  for (const t of TABS) {
    const b = document.createElement("button");
    b.textContent = t.label; b.dataset.id = t.id;
    b.onclick = () => selectTab(t);
    nav.appendChild(b);
  }
  // Learn the latest month present in the archive so month-filtered views
  // (burn-down) default to where the data is. Best-effort: any failure just
  // leaves the browser's own default in place.
  fetch("/api/stats").then(r => r.json()).then(s => {
    const latest = s && s.latest_transaction;
    if (typeof latest === "string" && latest.length >= 7) DEFAULTS.month = latest.slice(0, 7);
  }).catch(() => {}).finally(() => selectTab(TABS[0]));
}
init();
</script>
</body>
</html>
"""
