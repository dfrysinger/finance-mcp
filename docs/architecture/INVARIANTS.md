# Architecture invariants — enforcement register

Canonical source of enforcement truth for finance-mcp. Each row is an invariant
the codebase must hold, paired with the deterministic check that enforces it in
CI. Design docs reference these `INV-*` IDs rather than re-stating the rule, so
the rule and its enforcement can't drift apart.

Checks are deterministic (never an LLM judge). A check is either **structural**
(build/parse-time shape) or **behavioral** (runtime output). Every check runs in
the `pytest -q` CI job.

## Web UI (`src/finance_mcp/webui.py`)

See design: [`docs/design/webui-date-navigation.md`](../design/webui-date-navigation.md).

| ID | Invariant | Class | Enforcing check |
|---|---|---|---|
| INV-WEBUI-001 | Month-scoped tabs Transactions and Spending declare `range:` and do not render `start`/`end` as manual filters; Burn-down declares `month:`. (Subscriptions/Allocation are audit-window tabs and keep explicit date filters.) | structural | `tests/test_webui_design_guards.py::test_month_scoped_tabs_use_navigator_not_manual_date_filters` |
| INV-WEBUI-002 | Spending exposes `group_by` as `subtabs`, not a `select` dropdown | structural | `tests/test_webui_design_guards.py::test_spending_group_by_is_subtabs_not_dropdown` |
| INV-WEBUI-003 | Last tab is persisted/restored via `localStorage` key `fmcp.lastTab` | structural | `tests/test_webui_design_guards.py::test_last_tab_is_persisted_to_localstorage` |
| INV-WEBUI-004 | Embedded UI script is syntactically valid JavaScript | structural | `tests/test_webui_design_guards.py::test_embedded_script_is_valid_javascript` |
| INV-WEBUI-005 | Date helpers compute correct month bounds and wrap year boundaries | behavioral | `tests/test_webui_design_guards.py::test_date_helpers_compute_correct_bounds_and_year_wrap` |
| INV-WEBUI-006 | When Spending's active subtab groups by month, its navigator steps by year and scopes to whole-calendar-year bounds (`YYYY-01-01`..`YYYY-12-31`) | behavioral | `tests/test_webui_design_guards.py::test_year_navigator_wiring_resolves_year_bounds_for_month_subtab` |
| INV-WEBUI-007 | Spending's `group_by` subtabs list `envelope` first, so Envelope is the leftmost subtab and the default grouping when no prior selection is stored | structural | `tests/test_webui_design_guards.py::test_spending_defaults_to_envelope_grouping` |
| INV-WEBUI-008 | The Subscriptions "missing expected charges" table reuses the tracked-row Mark control, resolving each missing row to its tracked bill by name | structural | `tests/test_webui_design_guards.py::test_missing_charges_reuse_tracked_mark_control` |
| INV-WEBUI-009 | A `bool` filter renders as a checkbox that applies instantly on change (`onchange` = `load`); a tab whose only manual filter is a toggle renders no Load button | behavioral | `tests/test_webui_design_guards.py::test_bool_filters_are_instant_checkboxes` |
| INV-WEBUI-010 | The Red-flags view groups resolved deficits under a "Made good" section keyed on the `cleared` severity, and keeps the red table filtered to `red` severity so the banner/red count excludes made-good items | structural | `tests/test_webui_design_guards.py::test_redflags_groups_made_good_separately_from_red` |
| INV-WEBUI-011 | SimpleFIN connection problems (`errors`/`errlist`) are surfaced: `/api/accounts` is fetched on load and an always-visible banner + Accounts-tab badge reflect the combined error count, so a bank needing re-auth is loud from any tab | structural | `tests/test_webui_design_guards.py::test_connection_errors_surface_from_any_tab` |

## Queries (`src/finance_mcp/queries.py`)

| ID | Invariant | Class | Enforcing check |
|---|---|---|---|
| INV-QUERIES-001 | A bare `YYYY-MM-DD` `end_date` is inclusive through the end of that day (a noon-stamped last-day transaction is returned) | behavioral | `tests/test_queries.py::test_bare_end_date_is_inclusive_through_end_of_day` |
