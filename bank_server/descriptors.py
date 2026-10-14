"""What bank's objects say about themselves: the describers ``bank_server.service`` binds.

Each is stored on the row whenever it is saved (``stored_descriptors``), so a port's ``requires``
is answered by one indexed query and a signal carries what a picker filters on. Everything here
is read off the row itself, a relation only by its key (``category_id``): a transaction is
described before it is inserted (``service.stamp`` ahead of a ``bulk_create``), and a bulk path
that loads its rows with ``only()`` loads :data:`TRANSACTION_FIELDS` with them.

A provider's secret and settings are never described.
"""

from decimal import Decimal

from finance import models

Descriptors = dict[str, int | str | bool | list[str]]

#: The transaction fields its describer reads: a bulk path that defers fields loads these.
TRANSACTION_FIELDS = ("amount", "currency", "status", "kind", "is_transfer", "category", "merchant", "isin")


def direction(amount: Decimal) -> str:
    """OUT for a negative amount, IN otherwise: a zero amount moved no money out."""
    return "OUT" if amount < 0 else "IN"


def connection_descriptors(connection: models.BankConnection) -> Descriptors:
    """A connection's descriptors: where its consent or login is, where its bank is, and what last went wrong."""
    return {
        "@bank/status": str(connection.status),
        "@bank/provider": str(connection.provider),
        "@bank/country": connection.aspsp_country or "",
        "@bank/link_step": str(connection.link_step or ""),
        "@bank/error_code": str(connection.last_error_code or ""),
    }


def provider_descriptors(provider: models.BankProvider) -> Descriptors:
    """A provider's descriptors: its kind, whether it is on, and what is switched on (never its credentials)."""
    return {
        "@bank/provider": str(provider.kind),
        "@bank/enabled": provider.enabled,
        "@bank/capabilities": sorted(str(capability) for capability in provider.capabilities or []),
    }


def account_descriptors(account: models.BankAccount) -> Descriptors:
    """An account's descriptors: what it holds, in which currency, and whether its IBAN is known."""
    return {"@bank/kind": str(account.kind), "@bank/currency": account.currency or "", "@bank/has_iban": bool(account.iban)}


def import_descriptors(statement: models.StatementImport) -> Descriptors:
    """A statement import's descriptors: where it is, and what it came from."""
    return {"@bank/status": str(statement.status), "@bank/source": str(statement.source)}


def recurring_descriptors(recurring: models.RecurringPayment) -> Descriptors:
    """A recurring payment's descriptors: how it was judged, its rhythm, and its money.

    The direction is read from the typical amount, as a transaction's is: detection groups by
    direction and skips zero, so it is the one its ``key`` starts with.
    """
    return {
        "@bank/status": str(recurring.status),
        "@bank/interval_days": recurring.interval_days,
        "@bank/currency": recurring.currency or "",
        "@bank/direction": direction(recurring.amount),
    }


def category_descriptors(category: models.Category) -> Descriptors:
    """A category's descriptors: its kind, and whether the organization made it (a base category has a taxonomy key)."""
    return {"@bank/kind": str(category.kind), "@bank/custom": category.key is None}


def merchant_descriptors(merchant: models.Merchant) -> Descriptors:
    """A merchant's descriptors: whether it is online only."""
    return {"@bank/online": merchant.online}


def budget_descriptors(budget: models.Budget) -> Descriptors:
    """A budget's descriptors: the currency it counts."""
    return {"@bank/currency": budget.currency or ""}


def transaction_descriptors(transaction: models.Transaction) -> Descriptors:
    """A transaction's descriptors: its booking, its money, and what the organization made of it."""
    return {
        "@bank/status": str(transaction.status),
        "@bank/kind": str(transaction.kind or ""),
        "@bank/currency": transaction.currency or "",
        "@bank/direction": direction(transaction.amount),
        "@bank/is_transfer": transaction.is_transfer,
        "@bank/categorized": transaction.category_id is not None,
        "@bank/has_merchant": transaction.merchant_id is not None,
        "@bank/has_security": bool(transaction.isin),
    }
