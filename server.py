"""Wave Accounting MCP server — READ-ONLY access to your Wave books.

Wraps Wave's public GraphQL API (https://gql.waveapps.com/graphql/public) to
answer the questions that matter about a small business: who owes money, how
overdue it is, whether the invoice was ever even opened, and how revenue is
trending.

Auth is via env var only — never hardcoded:
  WAVE_FULL_ACCESS_TOKEN   Wave personal full-access token. Create one in the
                           Wave Developer Portal (developer.waveapps.com) ->
                           your application -> Full Access Token. Using your own
                           account's token is free even on the Starter plan.
  WAVE_BUSINESS_NAME       Optional. Substring of the business the tools default
                           to. If unset, they use the first non-personal
                           business on the account.

Scope / deliberate limits:
  * READ-ONLY. No invoice send/edit/delete tools — querying is the safe surface.
  * Wave's public API exposes invoices/customers/accounts/estimates but NO
    transactions and NO reports/P&L endpoint. So this server answers AR +
    invoiced revenue; total expenses and net income are not available from the
    Wave API and must come from your bank/card data elsewhere.

GraphQL quirks handled here so callers don't have to:
  * Inline string args are rejected — every string/ID arg goes through
    GraphQL `variables`.
  * `businesses` is a ROOT field, not nested under `user`.
  * Money values come back as comma-formatted strings ("1,050.00").
"""
import os
from datetime import date, datetime

import requests
from mcp.server.fastmcp import FastMCP

WAVE_TOKEN = os.environ["WAVE_FULL_ACCESS_TOKEN"]
WAVE_URL = "https://gql.waveapps.com/graphql/public"
DEFAULT_BUSINESS = os.environ.get("WAVE_BUSINESS_NAME", "")

mcp = FastMCP("wave")

_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {WAVE_TOKEN}",
    "Content-Type": "application/json",
})
_business_cache: dict[str, str] = {}


def _gql(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query and return its `data`, raising on any error."""
    resp = _session.post(
        WAVE_URL, json={"query": query, "variables": variables or {}}, timeout=30
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
        raise RuntimeError(f"Wave API error: {msgs}")
    return body["data"]


def _money(m) -> float:
    """Wave returns money as {"value": "1,050.00"} — strip commas to a float."""
    if not m:
        return 0.0
    v = m.get("value") if isinstance(m, dict) else m
    return round(float(str(v).replace(",", "")), 2)


def _days_overdue(due: str | None) -> int | None:
    """Positive = days past due; negative = days until due; None if unparseable."""
    if not due:
        return None
    try:
        d = datetime.strptime(due, "%Y-%m-%d").date()
        return (date.today() - d).days
    except ValueError:
        return None


def _resolve_business_id(name: str) -> str:
    """Resolve a business name to its Wave id.

    If `name` is given, case-insensitive substring match. If empty, default to
    the first non-personal (then any) non-archived business on the account.
    """
    key = (name or "").lower()
    if key in _business_cache:
        return _business_cache[key]
    data = _gql(
        "query { businesses(page:1, pageSize:50) { edges { node { id name isPersonal isArchived } } } }"
    )
    live = [e["node"] for e in data["businesses"]["edges"] if not e["node"].get("isArchived")]
    if name:
        matches = [n for n in live if name.lower() in (n.get("name") or "").lower()]
    else:
        matches = [n for n in live if not n.get("isPersonal")] or live
    if not matches:
        raise ValueError(
            f"No business matching {name!r}. Available: {[n['name'] for n in live]}"
        )
    bid = matches[0]["id"]
    _business_cache[key] = bid
    return bid


def _all_invoices(business_id: str) -> list[dict]:
    """Fetch every invoice (all pages) for a business."""
    query = """
    query Inv($bid: ID!, $p: Int!) {
      business(id: $bid) {
        invoices(page: $p, pageSize: 100) {
          pageInfo { currentPage totalPages }
          edges { node {
            invoiceNumber status invoiceDate dueDate
            total { value } amountDue { value } amountPaid { value }
            customer { name }
            lastSentAt lastViewedAt viewUrl
          } }
        }
      }
    }
    """
    nodes, page, total_pages = [], 1, 1
    while page <= total_pages:
        data = _gql(query, {"bid": business_id, "p": page})
        inv = data["business"]["invoices"]
        total_pages = inv["pageInfo"]["totalPages"] or 1
        nodes.extend(edge["node"] for edge in inv["edges"])
        page += 1
    return nodes


@mcp.tool()
def wave_list_businesses() -> list[dict]:
    """List the Wave businesses on this account (id, name, personal/archived
    flags, currency). Handy to discover a name to pass as business_name, or to
    confirm which business the tools default to."""
    data = _gql(
        "query { businesses(page:1, pageSize:50) { edges { node { id name isPersonal isArchived currency { code } } } } }"
    )
    return [
        {
            "id": e["node"]["id"],
            "name": e["node"]["name"],
            "is_personal": e["node"].get("isPersonal"),
            "is_archived": e["node"].get("isArchived"),
            "currency": (e["node"].get("currency") or {}).get("code"),
        }
        for e in data["businesses"]["edges"]
    ]


@mcp.tool()
def wave_outstanding_invoices(business_name: str = DEFAULT_BUSINESS) -> dict:
    """Accounts receivable: every invoice with money still owed, aged and
    ranked by due date. The tool for 'who owes me money?'.

    Each invoice reports customer, invoice number, status, invoice/due dates,
    days_overdue (positive = past due), amount_due, total, and crucially
    ever_viewed — Wave tracks whether the customer ever opened the invoice.
    A never-opened, long-overdue invoice usually means it isn't REACHING the
    customer (wrong email / spam / dead address), not that they're refusing to
    pay — a different and faster fix than dunning.

    Returns a summary (total_outstanding, total_overdue, never_opened_overdue,
    counts) plus the per-invoice list sorted soonest-due first.
    """
    bid = _resolve_business_id(business_name)
    outstanding = []
    for n in _all_invoices(bid):
        due = _money(n.get("amountDue"))
        if due <= 0:
            continue
        outstanding.append({
            "customer": (n.get("customer") or {}).get("name"),
            "invoice_number": n.get("invoiceNumber"),
            "status": n.get("status"),
            "invoice_date": n.get("invoiceDate"),
            "due_date": n.get("dueDate"),
            "days_overdue": _days_overdue(n.get("dueDate")),
            "amount_due": due,
            "total": _money(n.get("total")),
            "ever_viewed": n.get("lastViewedAt") is not None,
            "last_sent_at": n.get("lastSentAt"),
            "view_url": n.get("viewUrl"),
        })
    outstanding.sort(key=lambda x: (x["due_date"] or "9999-99-99"))
    overdue = [i for i in outstanding if (i["days_overdue"] or 0) > 0]
    return {
        "as_of": date.today().isoformat(),
        "count_outstanding": len(outstanding),
        "total_outstanding": round(sum(i["amount_due"] for i in outstanding), 2),
        "count_overdue": len(overdue),
        "total_overdue": round(sum(i["amount_due"] for i in overdue), 2),
        "never_opened_overdue": round(
            sum(i["amount_due"] for i in overdue if not i["ever_viewed"]), 2
        ),
        "invoices": outstanding,
    }


@mcp.tool()
def wave_revenue(
    start_date: str = "", end_date: str = "", business_name: str = DEFAULT_BUSINESS
) -> dict:
    """Invoiced revenue (accrual) by month over a date range, from invoice
    dates. start_date/end_date are "YYYY-MM-DD"; omit either for an open range.

    CAVEATS worth stating when you report this number:
      * Accrual, by invoice date — not cash received.
      * Excludes DRAFT/SAVED invoices.
      * Wave's own P&L may run higher because it can include non-invoice income
        this API doesn't expose. Treat this as a close cross-check; Wave's UI is
        authoritative.
      * This is REVENUE only. Expenses/net income are not in the Wave API.
    """
    bid = _resolve_business_id(business_name)
    by_month: dict[str, dict] = {}
    for n in _all_invoices(bid):
        d = n.get("invoiceDate") or ""
        if n.get("status") in ("DRAFT", "SAVED"):
            continue
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        m = d[:7]
        bucket = by_month.setdefault(m, {"month": m, "invoices": 0, "invoiced": 0.0})
        bucket["invoices"] += 1
        bucket["invoiced"] = round(bucket["invoiced"] + _money(n.get("total")), 2)
    months = [by_month[k] for k in sorted(by_month)]
    return {
        "start_date": start_date or None,
        "end_date": end_date or None,
        "total_invoiced": round(sum(m["invoiced"] for m in months), 2),
        "total_invoices": sum(m["invoices"] for m in months),
        "by_month": months,
    }


if __name__ == "__main__":
    mcp.run()
