"""Pulling a Scalable account: broker cash, depot or overnight savings.

Unlike a PSD2 bank, Scalable gives every transaction a stable id that survives its whole life
(an order goes PENDING → FILLED → SETTLED, a cancellation stays the same row), so rows are
upserted by ``sc:<id>`` — pending included — and user annotations survive every transition.

* **Cash** (the broker clearing account): the broker's transactions, newest first, paged back
  to the incremental start; balance = ``buyingPower.cashBalance``.
* **Depot**: one :class:`~finance.models.HoldingSnapshot` per ISIN for today (re-syncs the
  same day overwrite it) and a ``VALU`` balance of securities + crypto — cash is on the cash
  account, so it is not counted twice.
* **Savings**: the overnight account's transactions and ``totalAmount``.

Buys, sells, deposits and withdrawals move money between the user's own pots, so they are
flagged ``is_transfer`` and stay out of spending stats; dividends, interest, fees and taxes
count. The flag is the provider's (``kind`` is set), so IBAN-based transfer detection leaves
these rows alone.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from embeddings.models import EMBEDDING_FIELDS
from finance import models
from finance.semantic import embed_rows
from finance.scalable.client import ScalableClient
from finance.scalable.tokens import Session

PAGE_SIZE = 50
MAX_PAGES = 200

BOOKED = {"FILLED", "SETTLED", "CONFIRMED"}
PENDING = {"CREATED", "REQUESTED", "PENDING", "PARTIAL_FILLED", "CANCEL_REQUESTED"}

# Money moving between the user's own pots (cash, depot, savings, other banks): not spending.
TRANSFER_KINDS = {
    "BUY",
    "SELL",
    "DEPOSIT",
    "WITHDRAWAL",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "CASH_TRANSFER_IN",
    "CASH_TRANSFER_OUT",
    "SWAP_IN",
    "SWAP_OUT",
    "CURRENCY_SWITCH_BUY",
    "CURRENCY_SWITCH_SELL",
    "SAVINGS_PLAN",
    "REINVESTMENT",
}

FIELDS = ["booking_date", "value_date", "transaction_date", "amount", "currency", "status", "counterparty", "remittance", "entry_reference", "kind", "isin", "quantity", "raw"]


#: Every part a fetch can bring.
ALL = frozenset(models.ProviderCapability)


@dataclass
class Fetched:
    """One account's fetch, ready to persist."""

    transactions: list[dict] = field(default_factory=list)
    balance: Decimal | None = None
    balance_type: str = "CLBD"
    currency: str = "EUR"
    holdings: list[dict] | None = None


def _decimal(value) -> Decimal | None:  # noqa: ANN001
    return None if value is None else Decimal(str(value))


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def kind_of(tx: dict) -> str:
    """The transaction's :class:`~finance.models.TransactionKind`: BUY/SELL for trades, else Scalable's cash or non-trade type.

    A type this service does not know yet becomes OTHER (``raw`` keeps Scalable's value).
    """
    typename = tx.get("__typename") or ""
    if ("Security" in typename and "NonTrade" not in typename) or "Eltif" in typename:
        value = tx.get("side") or tx.get("securityTransactionType")
    elif "NonTrade" in typename:
        value = tx.get("nonTradeSecurityTransactionType")
        if value not in models.TransactionKind.values:
            value = models.TransactionKind.CORPORATE_ACTION
    else:
        value = tx.get("cashTransactionType")
    return value if value in models.TransactionKind.values else models.TransactionKind.OTHER


def normalize(tx: dict) -> dict:
    """A Scalable transaction summary as model fields. Amounts come signed already."""
    day = (_moment(tx.get("lastEventDateTime")) or timezone.now()).date()
    status = tx.get("status") or ""
    quantity = tx.get("quantity", tx.get("eltifQuantity"))
    return {
        "booking_date": day,
        "value_date": day,
        "transaction_date": day,
        "amount": _decimal(tx.get("amount")) or Decimal("0"),
        "currency": tx.get("currency") or "EUR",
        "status": models.TransactionStatus.BOOKED if status in BOOKED else models.TransactionStatus.PENDING if status in PENDING else models.TransactionStatus.OTHER,
        "counterparty": tx.get("description") or None,
        "remittance": None,
        "entry_reference": tx["id"],
        "kind": kind_of(tx),
        "isin": tx.get("isin") or tx.get("relatedIsin") or None,
        "quantity": _decimal(quantity),
        "raw": tx,
    }


async def _page_back(fetch_page, since: date | None) -> list[dict]:  # noqa: ANN001
    """Every transaction from newest back to ``since`` (all of them if None)."""
    out: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        page = await fetch_page(cursor)
        rows = page.get("transactions") or []
        out.extend(rows)
        cursor = page.get("cursor")
        oldest = _moment(rows[-1].get("lastEventDateTime")) if rows else None
        if not rows or not cursor or (since and oldest and oldest.date() < since):
            break
    return out


async def fetch(syncer: models.AccountSyncer, session: Session, client: ScalableClient, since: date | None, want: frozenset[models.ProviderCapability] = ALL) -> Fetched:
    """What this syncer's pot shows at Scalable, of the parts in ``want`` (nothing else is asked for)."""
    person = session.person_id
    kind = syncer.account.kind
    transactions = models.ProviderCapability.TRANSACTIONS in want
    balances = models.ProviderCapability.BALANCES in want

    async def gql(operation: str, variables: dict) -> dict:
        return await client.graphql(session.key, session.access_token, operation, variables)

    if kind == models.AccountKind.SAVINGS:
        ids = {"accountId": person, "savingsAccountId": syncer.remote_id}

        async def savings_page(cursor: str | None) -> dict:
            data = await gql("OvernightTransactions", {**ids, "input": {"pageSize": PAGE_SIZE, "cursor": cursor}})
            return ((data.get("account") or {}).get("savingsAccount") or {}).get("moreTransactions") or {}

        total = None
        if balances:
            summary = await gql("OvernightSummary", ids)
            total = ((summary.get("account") or {}).get("savingsAccount") or {}).get("totalAmount")
        return Fetched(transactions=await _page_back(savings_page, since) if transactions else [], balance=_decimal(total))

    ids = {"accountId": person, "portfolioId": syncer.remote_id}
    if kind == models.AccountKind.DEPOT:
        invested = items = None
        if balances:
            overview = await gql("BrokerOverview", {**ids, "includeYearToDate": False})
            valuation = ((overview.get("account") or {}).get("brokerPortfolio") or {}).get("valuation") or {}
            invested = (_decimal(valuation.get("securitiesValuation")) or Decimal(0)) + (_decimal(valuation.get("cryptoValuation")) or Decimal(0))
        if models.ProviderCapability.HOLDINGS in want:
            holdings = await gql("BrokerHoldings", {**ids, "includeYearToDate": False, "quoteSource": None})
            items = (((holdings.get("account") or {}).get("brokerPortfolio") or {}).get("inventory") or {}).get("items") or []
        return Fetched(balance=invested, balance_type="VALU", holdings=items)

    async def broker_page(cursor: str | None) -> dict:
        data = await gql("BrokerTransactions", {**ids, "input": {"pageSize": PAGE_SIZE, "cursor": cursor, "includeReinvestmentSubtypes": True}})
        return ((data.get("account") or {}).get("brokerPortfolio") or {}).get("moreTransactions") or {}

    cash = None
    if balances:
        limits = await gql("BrokerLimits", ids)
        cash = ((((limits.get("account") or {}).get("brokerPortfolio") or {}).get("payments") or {}).get("buyingPower") or {}).get("cashBalance")
    return Fetched(transactions=await _page_back(broker_page, since) if transactions else [], balance=_decimal(cash))


def _holding(item: dict) -> dict | None:
    position = ((item.get("inventory") or {}).get("position")) or {}
    quantity = _decimal(position.get("filled")) or Decimal(0)
    if not quantity and not position.get("pending"):
        return None
    performance = item.get("portfolioIsinPerformance") or {}
    quote = item.get("quoteTick") or {}
    return {
        "name": item.get("name") or item["isin"],
        "security_type": item.get("type"),
        "quantity": quantity,
        "fifo_price": _decimal(position.get("fifoPrice")),
        "price": _decimal(quote.get("midPrice")),
        "valuation": (_decimal(performance.get("valuation")) or Decimal(0)).quantize(Decimal("0.01")),
        "currency": performance.get("currency") or quote.get("currency") or "EUR",
        "raw": item,
    }


def persist(syncer_id: int, fetched: Fetched, result) -> None:  # noqa: ANN001 - finance.sync.SyncResult
    """Write one fetch in a single transaction; fills in ``result``."""
    from finance.sync import categorize_rows, mark_synced, take_over_imported

    now = timezone.now()
    today = now.date()
    syncer = models.AccountSyncer.objects.get(id=syncer_id)
    with db_transaction.atomic():
        account = models.BankAccount.objects.select_for_update().get(id=syncer.account_id)

        rows = {f"sc:{tx['id']}": normalize(tx) for tx in fetched.transactions if tx.get("id")}
        existing = {tx.fingerprint: tx for tx in account.transactions.filter(fingerprint__in=list(rows))}
        imported = take_over_imported(account, [(fp, row) for fp, row in rows.items() if fp not in existing])
        to_create, to_update = [], []
        for fingerprint, row in rows.items():
            transfer = row["kind"] in TRANSFER_KINDS
            tx = existing.get(fingerprint) or imported.get(fingerprint)
            if tx is None:
                to_create.append(models.Transaction(account=account, syncer=syncer, fingerprint=fingerprint, is_transfer=transfer, **row))
                continue
            changed = fingerprint in imported or tx.syncer_id != syncer.id or any(getattr(tx, name) != row[name] for name in FIELDS)
            if changed or (not tx.is_transfer_manual and tx.is_transfer != transfer):
                for name in FIELDS:
                    setattr(tx, name, row[name])
                tx.fingerprint, tx.syncer, tx.origin = fingerprint, syncer, models.TransactionOrigin.SYNC
                if not tx.is_transfer_manual:
                    tx.is_transfer = transfer
                tx.updated_at = now
                to_update.append(tx)
        embed_rows(to_create + to_update)
        models.Transaction.objects.bulk_create(to_create)
        models.Transaction.objects.bulk_update(to_update, FIELDS + ["fingerprint", "syncer", "origin", "is_transfer", "updated_at", *EMBEDDING_FIELDS], batch_size=500)
        result.created, result.updated = len(to_create), len(to_update)

        if fetched.balance is not None:
            models.BalanceSnapshot.objects.update_or_create(
                account=account, date=today, balance_type=fetched.balance_type, defaults={"amount": fetched.balance.quantize(Decimal("0.01")), "currency": fetched.currency}
            )
            result.balances = 1

        if fetched.holdings is not None:
            positions = {item["isin"]: holding for item in fetched.holdings if item.get("isin") and (holding := _holding(item))}
            account.holdings.filter(date=today).exclude(isin__in=list(positions)).delete()
            for isin, values in positions.items():
                models.HoldingSnapshot.objects.update_or_create(account=account, date=today, isin=isin, defaults=values)
            result.holdings = len(positions)

        touched = [tx.id for tx in to_create + to_update]
        result.categorized = categorize_rows(account.organization_id, touched)

        mark_synced(syncer, now)
