"""bank as a service of the hub: the models and the code behind what its contract says it hosts.

What exists here (the structures, the descriptors of their objects, the signals and their kinds)
is declared once, as data, in ``bank_server.contract`` (``hosts``), so that a hub knows it from the
image. This module only binds it: each structure to its model and to what computes its
descriptors, each signal to the saves and deletes that send it. A structure the contract does not
declare cannot be bound, and one it declares that nothing binds here stops the service at its
start. Each described row stores its descriptors on every save (``stored_descriptors``): the
GraphQL types answer ``descriptors`` from there (``finance.types``), and a list filter's
``requires`` reads it in SQL (``finance.filters``).

Hosting announces nothing by itself: a structure with no signal below is hosted silently.

That is all a service is. What can be *done* in this process is not declared here: that is an
agent's to say (``bank_server.hook_agent``), a different thing with its own configuration.

Transactions arrive in bulk (``bulk_create`` sends no ``post_save``), so a sync is announced once,
by hand, as an UPDATED of its account carrying how many rows it brought (``finance.sync``). Those
counts are facts about the sync, not about the account: they are signal descriptors, and the
account's own ``descriptors`` never list them. A bulk write sends no ``pre_save`` either: it
stamps its rows (``service.stamp``) or restores them after an ``update()``
(:func:`restore_transactions`, :func:`restore_connection`, :func:`restore_categories`). A
restore of transactions announces nothing, so for them the per-sync signal stays the only one; a
connection or category whose descriptors changed is announced as UPDATED, as a save would.
"""

from collections.abc import Iterable

from arkitekt_service.service import Service, organization_of

from bank_server import descriptors
from bank_server.contract import contract

from finance import models

service = Service("bank", hosts=contract.description.hosts, description="Bank accounts, their transactions, categories and budgets.")


# --- Structures: what bank hosts ------------------------------------------------------

bankconnection = service.structure(models.BankConnection, "@bank/bankconnection", describe=descriptors.connection_descriptors)
bankprovider = service.structure(models.BankProvider, "@bank/bankprovider", describe=descriptors.provider_descriptors)
bank_account = service.structure(models.BankAccount, "@bank/bankaccount", describe=descriptors.account_descriptors)
statementimport = service.structure(models.StatementImport, "@bank/statementimport", describe=descriptors.import_descriptors)
recurringpayment = service.structure(models.RecurringPayment, "@bank/recurringpayment", describe=descriptors.recurring_descriptors)
category = service.structure(models.Category, "@bank/category", describe=descriptors.category_descriptors)
merchant = service.structure(models.Merchant, "@bank/merchant", describe=descriptors.merchant_descriptors)
budget = service.structure(models.Budget, "@bank/budget", describe=descriptors.budget_descriptors)
transaction = service.structure(models.Transaction, "@bank/transaction", describe=descriptors.transaction_descriptors)


# --- Signals: what bank announces ------------------------------------------------------

org = organization_of()

service.model_signal(bankconnection, organization=org)
service.model_signal(bankprovider, organization=org)
#: Also emitted by hand, once per sync (``finance.sync._signal_sync``).
bank_account_signal = service.model_signal(bank_account, organization=org)
service.model_signal(statementimport, organization=org)
service.model_signal(recurringpayment, organization=org)
service.model_signal(category, organization=org)
service.model_signal(merchant, organization=org)
service.model_signal(budget, organization=org)
# Edits by users (categorizing, marking transfers); new transactions are bulk-created and are
# announced per sync on their account instead.
service.model_signal(transaction, organization=organization_of("account.organization"))


# --- Restoring what a bulk write changed --------------------------------------------------


def restore_transactions(ids: Iterable[int]) -> int:
    """Store the descriptors of these transactions again, after an ``update()`` of a field they read.

    Quietly: a transaction is announced when a user saves it, and a sync on its account
    (``finance.sync``). Loads only what the describer reads, never the embedding.
    """
    rows = models.Transaction.objects.filter(id__in=list(ids)).only("id", "stored_descriptors", *descriptors.TRANSACTION_FIELDS)
    return service.restore(rows, emit=False)


def restore_connection(connection_id: int) -> int:
    """Store a connection's descriptors again after an ``update()``; a changed one is announced (it expired, failed, was cancelled)."""
    return service.restore(models.BankConnection.objects.filter(id=connection_id))


def restore_categories(ids: Iterable[int]) -> int:
    """Store the descriptors of these categories again after an ``update()``; a changed one is announced."""
    return service.restore(models.Category.objects.filter(id__in=list(ids)))
