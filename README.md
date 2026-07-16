# wave-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server over Wave
Accounting's public GraphQL API. Ask your books plain-English questions from any
MCP client — "who owes me money?", "how's revenue this year?" — with a focus on
**accounts receivable** and the signal Wave quietly exposes but most tools
ignore: whether the customer ever *opened* the invoice.

FastMCP, env-var auth, no dependencies beyond `mcp` and `requests`.

## Tools

| Tool | What it answers |
|------|-----------------|
| `wave_outstanding_invoices` | AR: every invoice still owed, aged, with `ever_viewed`. A never-opened + overdue invoice usually means it isn't *reaching* the customer (wrong email / spam), not that they won't pay — a faster fix than dunning. Returns summary totals + per-invoice list. |
| `wave_revenue` | Invoiced revenue (accrual) by month over a date range. |
| `wave_list_businesses` | List businesses (id, name, currency) to discover a `business_name`. |

## Scope & limits (deliberate)

- **Read-only.** Querying is the safe surface; invoice send/edit tools are a
  possible v2.
- Wave's public API has **no transactions and no P&L/reports endpoint** — so
  this gives AR + invoiced revenue only. **Total expenses and net income are not
  available from the Wave API** and must come from your bank/card data
  elsewhere.
- Revenue is accrual (by invoice date), excludes DRAFT/SAVED, and may run under
  Wave's own P&L (which can include non-invoice income the API doesn't expose).
  Wave's UI is authoritative; this is a cross-check.

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
