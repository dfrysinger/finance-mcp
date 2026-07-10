"""Architecture fitness functions for the web-UI date-navigation feature.

These deterministic guards pin the invariants recorded in
``docs/architecture/INVARIANTS.md`` (INV-WEBUI-001..005) so the build fails if
the embedded single-page app drifts from the locked design in
``docs/design/webui-date-navigation.md``. No LLM is involved — every check is
plain parsing / a Node syntax-or-behavior run.
"""

import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from finance_mcp import webui

HTML = webui.INDEX_HTML
_SCRIPT_MATCH = re.search(r"<script>(.*)</script>", HTML, re.DOTALL)
assert _SCRIPT_MATCH, "INDEX_HTML must contain a <script> block"
SCRIPT = _SCRIPT_MATCH.group(1)

_TABS_MATCH = re.search(r"const TABS = \[(.*?)\n\];", SCRIPT, re.DOTALL)
assert _TABS_MATCH, "SCRIPT must define a TABS array"
TABS_TEXT = _TABS_MATCH.group(1)

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not available")


def _tab_block(tab_id: str) -> str:
    """Return the descriptor text for one tab (up to the next tab or end)."""
    marker = f'id:"{tab_id}"'
    start = TABS_TEXT.find(marker)
    assert start != -1, f"TABS must define a {tab_id!r} tab"
    nxt = TABS_TEXT.find('{ id:"', start + len(marker))
    return TABS_TEXT[start : nxt if nxt != -1 else len(TABS_TEXT)]


def _extract_function(name: str) -> str:
    """Pull one column-0-braced function declaration out of SCRIPT."""
    m = re.search(rf"^function {name}\(.*?^\}}", SCRIPT, re.DOTALL | re.MULTILINE)
    assert m, f"SCRIPT must define function {name}"
    return m.group(0)


def _run_node(source: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(source)
        path = fh.name
    try:
        return subprocess.run(
            [_NODE, path], capture_output=True, text=True, timeout=30
        )
    finally:
        Path(path).unlink(missing_ok=True)


def test_month_scoped_tabs_use_navigator_not_manual_date_filters():
    """INV-WEBUI-001: month-scoped tabs drive dates via the navigator.

    Transactions and Spending declare a ``range:`` (rendered as the month
    navigator) and never expose ``start``/``end`` as manual filter inputs;
    Burn-down declares ``month:``. The multi-month audit tabs (Subscriptions,
    Allocation) are explicitly NOT month-scoped — they keep manual date
    filters — because a single month can't detect a recurring cadence.
    """
    txn = _tab_block("transactions")
    assert 'range:{start:"start_date",end:"end_date"}' in txn
    # The navigator owns the date keys; a re-added manual date filter would
    # declare them with a {k:"start_date"...} descriptor, colliding on the
    # f_start_date/f_end_date input ids. Assert those descriptors are absent.
    assert '{k:"start_date"' not in txn and '{k:"end_date"' not in txn

    summary = _tab_block("summary")
    assert 'range:{start:"start_date",end:"end_date"}' in summary
    assert '{k:"start_date"' not in summary and '{k:"end_date"' not in summary

    burndown = _tab_block("burndown")
    assert 'month:"month"' in burndown

    for audit in ("subscriptions", "allocation"):
        block = _tab_block(audit)
        assert "range:" not in block, f"{audit} must not be month-scoped"
        assert "month:" not in block, f"{audit} must not be month-scoped"
        assert '{k:"start",type:"date"' in block
        assert '{k:"end",type:"date"' in block


def test_spending_group_by_is_subtabs_not_dropdown():
    """INV-WEBUI-002: Spending exposes group_by as subtabs, not a dropdown."""
    summary = _tab_block("summary")
    assert 'subtabs:{k:"group_by"' in summary
    # Scoped to the Spending tab: the invariant is about how Spending renders
    # group_by, not a repo-wide ban on the word (another tab may legitimately
    # gain a group_by control later without violating this rule).
    assert '{k:"group_by",type:"select"' not in summary
    assert '{k:"group_by",type:"dropdown"' not in summary


def test_last_tab_is_persisted_to_localstorage():
    """INV-WEBUI-003: the active tab is saved to and restored from storage."""
    assert 'localStorage.setItem("fmcp.lastTab"' in SCRIPT
    assert 'localStorage.getItem("fmcp.lastTab"' in SCRIPT


@_needs_node
def test_embedded_script_is_valid_javascript():
    """INV-WEBUI-004: the embedded UI script parses as valid JavaScript."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(SCRIPT)
        path = fh.name
    try:
        res = subprocess.run(
            [_NODE, "--check", path], capture_output=True, text=True, timeout=30
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr


@_needs_node
def test_date_helpers_compute_correct_bounds_and_year_wrap():
    """INV-WEBUI-005: pure date helpers are correct across month/year edges."""
    harness = (
        _extract_function("isoDate")
        + "\n"
        + _extract_function("monthBounds")
        + "\n"
        + _extract_function("shiftMonth")
        + "\n"
        + textwrap.dedent(
            """
            const assert = (c, m) => { if (!c) { console.error("FAIL: " + m); process.exit(1); } };

            // Month bounds, including a leap-February end-of-month.
            const jan = monthBounds(2026, 0);
            assert(jan.start === "2026-01-01", "jan start " + jan.start);
            assert(jan.end === "2026-01-31", "jan end " + jan.end);
            assert(jan.ym === "2026-01", "jan ym " + jan.ym);
            const feb = monthBounds(2024, 1);
            assert(feb.end === "2024-02-29", "leap feb end " + feb.end);

            // isoDate zero-pads month and day.
            assert(isoDate(new Date(2026, 2, 5)) === "2026-03-05", "isoDate pad");

            // shiftMonth wraps across the year boundary in both directions.
            const back = { y: 2026, m: 0 };
            shiftMonth(back, -1);
            assert(back.y === 2025 && back.m === 11, "back wrap " + back.y + "/" + back.m);
            const fwd = { y: 2026, m: 11 };
            shiftMonth(fwd, 1);
            assert(fwd.y === 2027 && fwd.m === 0, "fwd wrap " + fwd.y + "/" + fwd.m);

            console.log("OK");
            """
        )
    )
    res = _run_node(harness)
    assert res.returncode == 0, res.stderr + res.stdout


def test_spending_month_subtab_declares_year_navigator():
    """INV-WEBUI-006 (structural): Spending's group_by subtabs opt into year
    navigation for the ``month`` grouping."""
    summary = _tab_block("summary")
    assert 'subtabs:{k:"group_by"' in summary
    assert 'yearNav:"month"' in summary


@_needs_node
def test_year_navigator_bounds_for_group_by_month():
    """INV-WEBUI-006 (behavioral): yearBounds scopes to a whole calendar year."""
    harness = (
        _extract_function("yearBounds")
        + "\n"
        + textwrap.dedent(
            """
            const assert = (c, m) => { if (!c) { console.error("FAIL: " + m); process.exit(1); } };

            const b = yearBounds(2026);
            assert(b.start === "2026-01-01", "year start " + b.start);
            assert(b.end === "2026-12-31", "year end " + b.end);
            assert(b.label === "2026", "year label " + b.label);

            console.log("OK");
            """
        )
    )
    res = _run_node(harness)
    assert res.returncode == 0, res.stderr + res.stdout


@_needs_node
def test_year_navigator_wiring_resolves_year_bounds_for_month_subtab():
    """INV-WEBUI-006 (behavioral, integration): drives the real
    ``renderMonthNav`` against a DOM stub and asserts the hidden date params it
    resolves *and* that its arrows step by year. When the active subtab is the
    ``yearNav`` grouping (``month``) the window must be whole-calendar-year
    bounds and the ‹/› controls must move the year; any other subtab keeps month
    bounds. This fails if the ``yearMode`` gate/branch is removed OR if the
    year arrows are reverted to month stepping — neither of which the
    unit-level helper test can catch. State is pinned to a fixed mid-year month
    so a month-stepping regression is caught deterministically (never masked by
    a January/December year-crossing on the real calendar)."""
    harness = (
        _extract_function("isoDate")
        + "\n"
        + _extract_function("monthBounds")
        + "\n"
        + _extract_function("yearBounds")
        + "\n"
        + _extract_function("shiftMonth")
        + "\n"
        + _extract_function("navFor")
        + "\n"
        + _extract_function("subState")
        + "\n"
        + _extract_function("setHidden")
        + "\n"
        + _extract_function("renderMonthNav")
        + "\n"
        + textwrap.dedent(
            """
            const assert = (c, m) => { if (!c) { console.error("FAIL: " + m); process.exit(1); } };

            // Minimal DOM/state stubs so the real renderMonthNav can run.
            const NAVSTATE = {};
            const SUBSTATE = {};
            const localStorage = { getItem() { return null; }, setItem() {} };
            const reg = {};
            const created = [];
            const $ = (id) => reg[id];
            const makeEl = () => {
              const e = { className:"", title:"", textContent:"", type:"", value:"", id:"",
                innerHTML:"", onclick:null, onchange:null,
                classList:{ add(){}, toggle(){} }, appendChild(){}, append(){} };
              created.push(e); return e;
            };
            const document = { createElement: () => makeEl() };
            const wrap = { appendChild(e){ if (e && e.id) reg[e.id] = e; }, append(){}, innerHTML:"" };
            function load(){}
            // Most-recently-created control bearing a given title (draw() rebuilds
            // the arrows on every step, so always click the freshest one).
            const latest = (title) => {
              for (let i = created.length - 1; i >= 0; i--) if (created[i].title === title) return created[i];
              return null;
            };
            const startEnd = () => [reg["f_start_date"].value, reg["f_end_date"].value];

            const t = { id:"summary",
                        range:{ start:"start_date", end:"end_date" },
                        subtabs:{ k:"group_by",
                                  opts:["category","account","envelope","org","month"],
                                  yearNav:"month" } };

            // Active subtab is the yearNav grouping -> whole-calendar-year window.
            // Pin to a mid-year month so month-stepping never crosses a year.
            NAVSTATE[t.id] = { mode:"month", y:2026, m:5, custom:{start:"",end:""} };
            SUBSTATE[t.id] = { value:"month" };
            renderMonthNav(t, wrap);
            assert(startEnd().join("|") === "2026-01-01|2026-12-31", "year init " + startEnd());

            // The arrows must step by YEAR, not month.
            latest("Previous year").onclick();
            assert(startEnd().join("|") === "2025-01-01|2025-12-31", "prev year " + startEnd());
            latest("Next year").onclick();
            assert(startEnd().join("|") === "2026-01-01|2026-12-31", "next year (back to 2026) " + startEnd());
            latest("Next year").onclick();
            assert(startEnd().join("|") === "2027-01-01|2027-12-31", "next year " + startEnd());
            // A month-stepping regression keeps year arrows off entirely.
            assert(latest("Previous month") === null && latest("Next month") === null,
                   "year mode must not render month arrows");

            // Any other subtab keeps month granularity (its month's bounds).
            for (const k in reg) delete reg[k];
            created.length = 0;
            NAVSTATE[t.id] = { mode:"month", y:2026, m:5, custom:{start:"",end:""} };
            SUBSTATE[t.id] = { value:"category" };
            renderMonthNav(t, wrap);
            const mb = monthBounds(2026, 5);
            assert(startEnd().join("|") === mb.start + "|" + mb.end, "category month bounds " + startEnd());
            assert(latest("Previous month") !== null && latest("Next month") !== null,
                   "non-year subtab must render month arrows");

            console.log("OK");
            """
        )
    )
    res = _run_node(harness)
    assert res.returncode == 0, res.stderr + res.stdout
