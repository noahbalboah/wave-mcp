"""Wave Accounting MCP server — READ-ONLY access to your Wave books.

Wraps Wave's public GraphQL API (https://gql.waveapps.com/graphql/public) to
answer the questions that matter about a small business: who owes money, what
just got paid, where the money went (expenses), how revenue is trending, and
what's in the catalog / pipeline.

Auth is via env var only — never hardcoded:
  WAVE_FULL_ACCESS_TOKEN   Wave personal full-access token. Create one in the
                           Wave Developer Portal (developer.waveapps.com) ->
                           your application -> Full Access Token. Using your own
                           account's token is free even on the Starter plan.
  WAVE_BUSINESS_NAME       Optional. Substring of the business the tools default
                           to. If unset, they use the first non-personal
                           business on the account.

Scope / deliberate limits:
  * READ-ONLY. No create/send/edit/delete tools — querying is the safe surface.
  * The public API has NO money-transaction (bank-ledger) endpoint and NO
    date-filterable P&L report. BUT account *balances* ARE exposed, so P&L and
    expense breakdowns are available as CUMULATIVE-TO-DATE figures (not scoped
    to an arbitrary date range). Invoice *payments* are also fully exposed.

GraphQL quirks handled here so callers don't have to:
  * Inline string args are rejected — every string/ID arg goes through
    GraphQL `variables`.
  * `businesses` is a ROOT field, not nested under `user`.
  * Money values come back as comma-formatted strings ("1,050.00"); Decimal
    scalars (balances, unit prices) come back as plain strings ("88121.84").
"""
import os
from datetime import date, datetime, timedelta

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
    """Parse a Wave money/decimal value to float.

    Handles Money objects ({"value": "1,050.00"}), plain Decimal/String scalars
    ("88121.84"), and None. Strips thousands commas.
    """
    if m is None:
        return 0.0
    v = m.get("value") if isinstance(m, dict) else m
    if v is None:
        return 0.0
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


def _all_nodes(business_id: str, field: str, node_body: str, page_size: int = 100) -> list[dict]:
    """Fetch every node across all pages of a `business.<field>` connection.

    `node_body` is the GraphQL selection set for each node (no braces).
    """
    query = f"""
    query P($bid: ID!, $p: Int!) {{
      business(id: $bid) {{
        {field}(page: $p, pageSize: {page_size}) {{
          pageInfo {{ currentPage totalPages }}
          edges {{ node {{ {node_body} }} }}
        }}
      }}
    }}
    """
    nodes, page, total_pages = [], 1, 1
    while page <= total_pages:
        data = _gql(query, {"bid": business_id, "p": page})
        conn = data["business"][field]
        total_pages = conn["pageInfo"]["totalPages"] or 1
        nodes.extend(edge["node"] for edge in conn["edges"])
        page += 1
    return nodes


# Invoice selection sets. Lean version powers AR/revenue; the +payments version
# powers wave_recent_payments (payments are only fetched when actually needed).
_INVOICE_LEAN = """
  invoiceNumber status invoiceDate dueDate
  total { value } amountDue { value } amountPaid { value }
  customer { name }
  lastSentAt lastViewedAt viewUrl
"""
_INVOICE_WITH_PAYMENTS = _INVOICE_LEAN + """
  payments {
    id paymentDate createdAt amount paymentMethod paymentProvider
    memo state account { name }
  }
"""


def _all_invoices(business_id: str, with_payments: bool = False) -> list[dict]:
    """Fetch every invoice (all pages) for a business."""
    body = _INVOICE_WITH_PAYMENTS if with_payments else _INVOICE_LEAN
    return _all_nodes(business_id, "invoices", body, page_size=100)


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
def wave_recent_payments(days: int = 30, business_name: str = DEFAULT_BUSINESS) -> dict:
    """Invoice payments RECEIVED in the last `days` days — 'what just got paid?'.

    Walks invoices and flattens their payment records, newest first. Each
    payment reports customer, invoice_number, amount, payment_date, recorded_at
    (when it hit Wave), method/provider, state, and the deposit account.

    Crucial signal: via_processor. Payments landing in Wave's own
    'Payments by Wave' clearing account are card/bank payments that Wave has
    collected but not yet PAID OUT to your bank — they settle on a ~2 business
    day lag. So total_in_processor is real money in transit that hasn't shown up
    in your bank feed yet. Payments to any other account are already deposited.

    Returns summary (total_paid, count, total_in_processor, total_deposited)
    plus the per-payment list sorted newest-first.
    """
    bid = _resolve_business_id(business_name)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    for n in _all_invoices(bid, with_payments=True):
        for p in (n.get("payments") or []):
            pdate = p.get("paymentDate") or (p.get("createdAt") or "")[:10]
            if not pdate or pdate < cutoff:
                continue
            acct = (p.get("account") or {}).get("name")
            rows.append({
                "customer": (n.get("customer") or {}).get("name"),
                "invoice_number": n.get("invoiceNumber"),
                "amount": _money(p.get("amount")),
                "payment_date": p.get("paymentDate"),
                "recorded_at": p.get("createdAt"),
                "method": p.get("paymentMethod"),
                "provider": p.get("paymentProvider"),
                "state": p.get("state"),
                "memo": p.get("memo"),
                "deposit_account": acct,
                "via_processor": bool(acct and "payments by wave" in acct.lower()),
            })
    rows.sort(key=lambda x: (x["recorded_at"] or x["payment_date"] or ""), reverse=True)
    in_processor = round(sum(r["amount"] for r in rows if r["via_processor"]), 2)
    total = round(sum(r["amount"] for r in rows), 2)
    return {
        "as_of": date.today().isoformat(),
        "window_days": days,
        "since": cutoff,
        "count": len(rows),
        "total_paid": total,
        "total_in_processor": in_processor,
        "total_deposited": round(total - in_processor, 2),
        "payments": rows,
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
      * This is REVENUE only. For expenses/net income use wave_profit_and_loss.
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


def _all_accounts(business_id: str) -> list[dict]:
    """Every ledger account with its current balance and classification."""
    body = """
      name description type { name normalBalanceType } subtype { name }
      balance isArchived
    """
    return _all_nodes(business_id, "accounts", body, page_size=200)


@mcp.tool()
def wave_profit_and_loss(business_name: str = DEFAULT_BUSINESS) -> dict:
    """Profit & loss from ledger account balances: total income, total expenses,
    net, and the per-account breakdown (where the money came from and went).

    IMPORTANT CAVEAT — state this when you report these numbers:
      * These are CUMULATIVE-TO-DATE account balances. Wave's public API has no
        date-filterable P&L report, so this is NOT scoped to a month/quarter —
        it's the running balance of each income/expense account. Cross-check
        revenue timing with wave_revenue (which IS date-filterable, by invoice
        date). Wave's own UI reports are authoritative for a period P&L.
      * Income and expense figures are reported as positive magnitudes; net =
        total_income - total_expenses.

    Returns totals plus income[] and expenses[] line items sorted largest-first
    (zero-balance and archived accounts omitted).
    """
    bid = _resolve_business_id(business_name)
    income, expenses = [], []
    for a in _all_accounts(bid):
        if a.get("isArchived"):
            continue
        bal = _money(a.get("balance"))
        if bal == 0:
            continue
        tname = (a.get("type") or {}).get("name") or ""
        line = {"account": a.get("name"), "subtype": (a.get("subtype") or {}).get("name"), "balance": bal}
        if tname == "Income":
            income.append(line)
        elif tname == "Expenses":
            expenses.append(line)
    income.sort(key=lambda x: x["balance"], reverse=True)
    expenses.sort(key=lambda x: x["balance"], reverse=True)
    total_income = round(sum(i["balance"] for i in income), 2)
    total_expenses = round(sum(e["balance"] for e in expenses), 2)
    return {
        "as_of": date.today().isoformat(),
        "basis": "cumulative-to-date account balances (not date-filterable)",
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net": round(total_income - total_expenses, 2),
        "income": income,
        "expenses": expenses,
    }


@mcp.tool()
def wave_chart_of_accounts(
    include_zero: bool = False,
    account_type: str = "",
    business_name: str = DEFAULT_BUSINESS,
) -> dict:
    """The full chart of accounts with current balances, grouped by type
    (Assets, Liabilities & Credit Cards, Income, Expenses, Equity).

    Defaults to nonzero, non-archived accounts — Wave auto-creates a large pile
    of empty per-invoice 'Accounts Receivable' sub-accounts, so include_zero
    dumps a lot of noise. Set account_type to filter to one group (case-
    insensitive substring, e.g. 'expenses', 'liab').

    Balances are cumulative-to-date (see wave_profit_and_loss caveat). Returns
    per-group account lists (sorted largest-balance-first) plus a group total.
    """
    bid = _resolve_business_id(business_name)
    groups: dict[str, list] = {}
    for a in _all_accounts(bid):
        if a.get("isArchived"):
            continue
        bal = _money(a.get("balance"))
        if not include_zero and bal == 0:
            continue
        tname = (a.get("type") or {}).get("name") or "Other"
        if account_type and account_type.lower() not in tname.lower():
            continue
        groups.setdefault(tname, []).append({
            "account": a.get("name"),
            "subtype": (a.get("subtype") or {}).get("name"),
            "balance": bal,
        })
    out = []
    for tname in sorted(groups):
        rows = sorted(groups[tname], key=lambda x: abs(x["balance"]), reverse=True)
        out.append({
            "type": tname,
            "count": len(rows),
            "total": round(sum(r["balance"] for r in rows), 2),
            "accounts": rows,
        })
    return {"as_of": date.today().isoformat(), "groups": out}


@mcp.tool()
def wave_customers(
    include_archived: bool = False, business_name: str = DEFAULT_BUSINESS
) -> dict:
    """Customer list with per-customer accounts receivable (outstanding and
    overdue amounts) and contact info. Sorted by outstanding balance, largest
    first. The fast answer to 'who owes me money?' at the customer level (use
    wave_outstanding_invoices for the invoice-by-invoice / ever-viewed detail).
    """
    bid = _resolve_business_id(business_name)
    body = """
      name email phone mobile website isArchived
      outstandingAmount { value } overdueAmount { value }
    """
    rows = []
    for n in _all_nodes(bid, "customers", body, page_size=200):
        if n.get("isArchived") and not include_archived:
            continue
        rows.append({
            "name": n.get("name"),
            "email": n.get("email") or None,
            "phone": n.get("phone") or n.get("mobile") or None,
            "website": n.get("website") or None,
            "outstanding": _money(n.get("outstandingAmount")),
            "overdue": _money(n.get("overdueAmount")),
            "is_archived": n.get("isArchived"),
        })
    rows.sort(key=lambda x: x["outstanding"], reverse=True)
    return {
        "count": len(rows),
        "total_outstanding": round(sum(r["outstanding"] for r in rows), 2),
        "total_overdue": round(sum(r["overdue"] for r in rows), 2),
        "customers": rows,
    }


@mcp.tool()
def wave_vendors(
    include_archived: bool = False, business_name: str = DEFAULT_BUSINESS
) -> dict:
    """Vendor list with contact info (name, email, phone, website). Vendors are
    who you buy from / pay bills to."""
    bid = _resolve_business_id(business_name)
    body = "name email phone mobile website isArchived"
    rows = []
    for n in _all_nodes(bid, "vendors", body, page_size=200):
        if n.get("isArchived") and not include_archived:
            continue
        rows.append({
            "name": n.get("name"),
            "email": n.get("email") or None,
            "phone": n.get("phone") or n.get("mobile") or None,
            "website": n.get("website") or None,
            "is_archived": n.get("isArchived"),
        })
    rows.sort(key=lambda x: (x["name"] or "").lower())
    return {"count": len(rows), "vendors": rows}


@mcp.tool()
def wave_products(
    include_archived: bool = False, business_name: str = DEFAULT_BUSINESS
) -> dict:
    """Product / service catalog with unit prices — your price list. Each item
    reports name, unit_price, whether it's sold and/or bought, and description.
    Sorted by unit_price, highest first."""
    bid = _resolve_business_id(business_name)
    body = "name description unitPrice isSold isBought isArchived"
    rows = []
    for n in _all_nodes(bid, "products", body, page_size=100):
        if n.get("isArchived") and not include_archived:
            continue
        rows.append({
            "name": n.get("name"),
            "unit_price": _money(n.get("unitPrice")),
            "is_sold": n.get("isSold"),
            "is_bought": n.get("isBought"),
            "description": n.get("description") or None,
            "is_archived": n.get("isArchived"),
        })
    rows.sort(key=lambda x: x["unit_price"], reverse=True)
    return {"count": len(rows), "products": rows}


@mcp.tool()
def wave_estimates(status: str = "", business_name: str = DEFAULT_BUSINESS) -> dict:
    """Estimates / quotes — the sales pipeline. Each reports estimate number,
    status (DRAFT/SENT/VIEWED/ACCEPTED/CONVERTED/etc.), estimate/expiry dates,
    total, amount due, deposit status, and customer. Optional `status` filter
    (case-insensitive). Sorted newest-first by estimate date."""
    bid = _resolve_business_id(business_name)
    body = """
      estimateNumber status estimateDate dueDate
      total { value } amountDue { value } amountPaid { value }
      depositStatus customer { name }
    """
    rows = []
    for n in _all_nodes(bid, "estimates", body, page_size=100):
        if status and (n.get("status") or "").lower() != status.lower():
            continue
        rows.append({
            "estimate_number": n.get("estimateNumber"),
            "status": n.get("status"),
            "customer": (n.get("customer") or {}).get("name"),
            "estimate_date": n.get("estimateDate"),
            "expiry_date": n.get("dueDate"),
            "total": _money(n.get("total")),
            "amount_due": _money(n.get("amountDue")),
            "deposit_status": n.get("depositStatus"),
        })
    rows.sort(key=lambda x: (x["estimate_date"] or ""), reverse=True)
    return {
        "count": len(rows),
        "total_value": round(sum(r["total"] for r in rows), 2),
        "estimates": rows,
    }


if __name__ == "__main__":
    mcp.run()
