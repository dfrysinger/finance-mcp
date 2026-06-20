# finance-mcp

Local, privacy-first access to your bank and credit-card transactions for
Copilot, backed by [SimpleFIN](https://www.simplefin.org/). It pulls transactions
from your institutions into a normalized on-disk cache and exposes them to Copilot
as an MCP server. Nothing leaves your machine except the single HTTPS call to
SimpleFIN — no third-party SaaS sees your data, and no fake/sample data is ever
shown.

## Why SimpleFIN

All of these institutions are supported by SimpleFIN (verified against its live
institution search): Target credit card, Nordstrom Card Services, Amazon Chase
card (via Chase Bank), Fidelity credit card, Fidelity NetBenefits (via Fidelity
Investments), Charles Schwab, and Cyprus Credit Union. SimpleFIN costs ~$15/yr
flat with no per-account billing, and you hold the access token.

## Security model

The SimpleFIN **access URL embeds Basic-Auth credentials** that can read your
transactions. It is therefore stored **outside** this project directory (this repo
may live in a synced folder like Dropbox):

- Access URL + transaction cache live in `~/.finance-mcp/` (dir `0700`, files `0600`).
- Override the location with `FINANCE_MCP_HOME`.
- The access URL may instead be supplied via the `SIMPLEFIN_ACCESS_URL` env var
  (takes precedence over the saved file), so it never has to touch disk.

## Setup

1. Get a SimpleFIN **setup token**: sign up at
   <https://bridge.simplefin.org/> and generate one (it is a base64 string).

2. Claim it (one-time — the token dies after a successful claim):

   ```bash
   uv run finance-mcp claim            # prompts for the token
   # or: uv run finance-mcp claim <SETUP_TOKEN>
   ```

   The resulting access URL is saved to `~/.finance-mcp/access_url` (mode 0600).

3. Pull your transactions into the cache:

   ```bash
   uv run finance-mcp sync --days 120
   ```

## CLI

```bash
uv run finance-mcp accounts                          # balances per account
uv run finance-mcp transactions --search grocery     # search the full archive
uv run finance-mcp transactions --start 2026-01-01 --account <id> --json
uv run finance-mcp summary --group-by month          # inflow/outflow aggregation
uv run finance-mcp summary                            # defaults to group-by category, excludes transfers
uv run finance-mcp networth                          # net-worth total per snapshot date
uv run finance-mcp stats                             # archive size + date coverage
uv run finance-mcp categorize                        # seed default rules + show coverage
uv run finance-mcp uncategorized                     # top still-uncategorized merchants
uv run finance-mcp rules list                        # show categorization rules
uv run finance-mcp rules add --pattern "trader joe" --category Groceries
uv run finance-mcp rules rm --rule-id <id>           # remove a rule
uv run finance-mcp set-category <txn_id> Travel      # pin one transaction's category
uv run finance-mcp sync --days 120                    # refresh from SimpleFIN
```

SimpleFIN caps a request at 90 days and expects <=24 requests/day, so `sync`
chunks long ranges into <=89-day windows and you should rely on the archive for
day-to-day queries rather than re-syncing constantly. Any SimpleFIN warnings or
errors (`errors`/`errlist`) are always surfaced.

## Local archive (multi-year history)

Every `sync` does two things locally, both in `~/.finance-mcp/` (mode `0600`):

- updates `cache.json` — the latest normalized snapshot, and
- folds the data into `archive.db`, a **SQLite** database that is the durable,
  searchable, multi-year history.

The archive is **append-only**: transactions are upserted by their stable
SimpleFIN id (a pending charge is later promoted to posted without duplicating),
`first_seen` is preserved, and nothing is ever deleted — so a transaction stays
in the archive even after it ages out of SimpleFIN's rolling window. Each sync
also records a **balance snapshot** per account, which is what powers
`networth` / `net_worth_history` trends over time.

All read commands and MCP tools serve from `archive.db` (falling back to
`cache.json` only before the first sync on this version). You can also query it
with any SQLite tool:

```bash
sqlite3 ~/.finance-mcp/archive.db \
  "SELECT posted, amount, description FROM transactions ORDER BY posted_ts DESC LIMIT 10;"
```


## MCP server (use it from Copilot)

The server runs over stdio and exposes these tools:

| Tool | Network? | Purpose |
|------|----------|---------|
| `list_accounts` | no | accounts + balances + institution |
| `account_balances` | no | just balances and as-of dates |
| `get_transactions` | no | filter by date / account / search / amount (includes category) |
| `spending_summary` | no | inflow/outflow grouped by category, account, org, or month |
| `categorization_status` | no | category coverage + spend-by-category breakdown |
| `list_category_rules` | no | the active substring → category rules |
| `add_category_rule` | no | add a rule (optionally flag matches as transfers) |
| `remove_category_rule` | no | delete a rule by id |
| `set_transaction_category` | no | pin one transaction's category (survives sync) |
| `net_worth_history` | no | total balance per snapshot date (net-worth trend) |
| `archive_stats` | no | archive size + earliest/latest transaction |
| `sync_now` | **yes** | refresh the cache + archive from SimpleFIN |

### Install for others (no clone needed)

With [uv](https://docs.astral.sh/uv/) installed, you can claim/sync and run the
server straight from this repo — `uvx` fetches and builds it on demand:

```bash
# one-time claim + first sync
uvx --from git+https://github.com/dfrysinger/finance-mcp finance-mcp claim
uvx --from git+https://github.com/dfrysinger/finance-mcp finance-mcp sync --days 120
```

Then register it with Copilot CLI by adding this to `~/.copilot/mcp-config.json`
under `mcpServers` (or run `/mcp` in Copilot to manage):

```json
{
  "mcpServers": {
    "finance": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dfrysinger/finance-mcp",
        "finance-mcp-server"
      ],
      "tools": ["*"]
    }
  }
}
```

### Local checkout alternative

If you cloned the repo and prefer running from the working tree:

```json
{
  "mcpServers": {
    "finance": {
      "command": "uv",
      "args": ["--directory", "<ABSOLUTE_PATH_TO>/finance-mcp", "run", "finance-mcp-server"],
      "tools": ["*"]
    }
  }
}
```

`get_transactions`/`spending_summary` serve the durable archive (`archive.db`),
which `sync` (CLI) or `sync_now` keeps up to date.

## Notes / limitations

- SimpleFIN does not provide spending **categories**, so categories are assigned
  locally by a rule engine (case-insensitive substring match on description/payee)
  plus per-transaction manual overrides — nothing is guessed from outside your data.
  Internal movements (transfers, card payments, P2P) are flagged so honest spend
  totals exclude them; pass `--include-transfers` / `include_transfers=true` to count them.
- Amounts are signed: negative = money out. Use `max_amount=0` for spending-only,
  `min_amount=0` for income-only.

## Development

```bash
uv run pytest -q
```

Verified end-to-end against SimpleFIN's public demo dataset (claim → fetch →
normalize → cache → query, plus an MCP stdio round-trip).
