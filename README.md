# wave-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server over Wave
Accounting's public GraphQL API. Ask your books plain-English questions from any
MCP client — "who owes me money?", "what just got paid?", "where did the money
go?", "how's revenue this year?" — with a focus on **accounts receivable**, the
signal Wave quietly exposes but most tools ignore (whether the customer ever
*opened* the invoice), and **payments received** (including money still in
Wave's payout pipeline, not yet in your bank).

FastMCP, env-var auth, no dependencies beyond `mcp` and `requests`.

## Tools

| Tool | What it answers |
|------|-----------------|
| `wave_outstanding_invoices` | AR: every invoice still owed, aged, with `ever_viewed`. A never-opened + overdue invoice usually means it isn't *reaching* the customer (wrong email / spam), not that they won't pay — a faster fix than dunning. Returns summary totals + per-invoice list. |
| `wave_recent_payments` | Payments received in the last N days, newest first. Flags `via_processor` — money Wave has collected but not yet paid out to your bank (~2-day lag), so `total_in_processor` is real cash in transit that hasn't hit your bank feed. |
| `wave_revenue` | Invoiced revenue (accrual) by month over a date range. |
| `wave_profit_and_loss` | Total income, total expenses, net, and per-account breakdown — from ledger account balances. **Cumulative-to-date, not date-filterable** (see limits). |
| `wave_chart_of_accounts` | Full chart of accounts with balances, grouped by type. Defaults to nonzero/non-archived (Wave auto-creates a big pile of empty per-invoice AR sub-accounts). |
| `wave_customers` | Customer list with per-customer outstanding/overdue AR + contact info, largest balance first. |
| `wave_vendors` | Vendor list (who you buy from) with contact info. |
| `wave_products` | Product/service catalog with unit prices — your price list. |
| `wave_estimates` | Estimates/quotes (sales pipeline) with status, totals, deposit status. |
| `wave_list_businesses` | List businesses (id, name, currency) to discover a `business_name`. |
| `wave_create_expense` | **The one write tool.** Books a business expense (increase an expense account, funded from an account — defaults to owner equity for personally-paid purchases). Dry-run by default; `confirm=True` to post. Deterministic idempotency key prevents duplicates. |

## Scope & limits (deliberate)

- **Read-only except one guarded write** (`wave_create_expense`): dry-run
  default, explicit `confirm=True` to post, deterministic `externalId` so an
  identical call can't duplicate. Every other tool only queries. Money
  transactions can't be read back via the API, so verify writes in Wave's UI.
- Wave's public API has **no money-transaction (bank-ledger) endpoint and no
  date-filterable P&L report.** But account *balances* and invoice *payments*
  ARE exposed, so `wave_profit_and_loss` / `wave_chart_of_accounts` give real
  expense and net-income figures — as **cumulative-to-date balances**, not
  scoped to an arbitrary date range. For period-accurate P&L, Wave's own UI
  reports are authoritative.
- Revenue (`wave_revenue`) is accrual (by invoice date), excludes DRAFT/SAVED,
  and is the one figure here that IS date-filterable. Use it to cross-check the
  timing that the cumulative balances can't express.

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export WAVE_FULL_ACCESS_TOKEN=...   # or via your secret manager
./.venv/bin/python server.py
```

Get `WAVE_FULL_ACCESS_TOKEN` from the [Wave Developer
Portal](https://developer.waveapps.com) → your application → **Full Access
Token**. Using your own account's token is free even on the Starter plan.

### Register with an MCP client

Example stdio entry (Claude Code / Claude Desktop `mcpServers`):

```json
{
  "wave": {
    "command": "/path/to/wave-mcp/.venv/bin/python",
    "args": ["/path/to/wave-mcp/server.py"],
    "env": { "WAVE_FULL_ACCESS_TOKEN": "your-token" }
  }
}
```

Prefer a secret manager over inlining the token — e.g. wrap the command with
`doppler run -- ...` and keep `WAVE_FULL_ACCESS_TOKEN` in Doppler. Set
`WAVE_BUSINESS_NAME` to pick a default business (otherwise the first
non-personal business is used).

## License

MIT © 2026 Noah Weir
