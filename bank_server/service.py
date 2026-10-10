"""bank as a service of the hub: the models and the code behind what its contract says it hosts.

What exists here (the structures, the descriptors of their objects, the signals and their kinds)
is declared once, as data, in ``bank_server.contract`` (``hosts``), so that a hub knows it from the
image. This module only binds it: each structure to its model and to what computes its
descriptors, each signal to the saves and deletes that send it. A structure the contract does not
declare cannot be bound, and one it declares that nothing binds here stops the service at its
start. The GraphQL types answer ``descriptors`` from the same binding (``finance.types``).

Hosting announces nothing by itself: a structure with no signal below is hosted silently.

That is all a service is. What can be *done* in this process is not declared here: that is an
agent's to say (``bank_server.hook_agent``), a different thing with its own configuration.

Transactions arrive in bulk (``bulk_create`` sends no ``post_save``), so a sync is announced once,
by hand, as an UPDATED of its account carrying how many rows it brought (``finance.sync``). Those
counts are facts about the sync, not about the account: they are signal descriptors, and the
account's own ``descriptors`` never list them.
"""

from arkitekt_service.service import Service, organization_of

from bank_server.contract import contract

from finance import models

service = Service("bank", hosts=contract.description.hosts, description="Bank accounts, their transactions, categories and budgets.")


# --- Structures: what bank hosts ------------------------------------------------------

bankconnection = service.structure(
    models.BankConnection,
    "@bank/bankconnection",
    describe=lambda connection: {"@bank/status": str(connection.status), "@bank/provider": str(connection.provider)},
)
bankprovider = service.structure(
    models.BankProvider,
    "@bank/bankprovider",
    describe=lambda provider: {"@bank/provider": str(provider.kind), "@bank/enabled": provider.enabled},
)
bank_account = service.structure(
    models.BankAccount,
    "@bank/bankaccount",
    describe=lambda account: {"@bank/kind": str(account.kind), "@bank/currency": account.currency or ""},
)
statementimport = service.structure(
    models.StatementImport,
    "@bank/statementimport",
    describe=lambda statement: {"@bank/status": str(statement.status), "@bank/source": str(statement.source)},
)
recurringpayment = service.structure(
    models.RecurringPayment,
    "@bank/recurringpayment",
    describe=lambda recurring: {"@bank/status": str(recurring.status), "@bank/interval_days": recurring.interval_days},
)
category = service.structure(models.Category, "@bank/category", describe=lambda category: {"@bank/kind": str(category.kind)})
merchant = service.structure(models.Merchant, "@bank/merchant")
budget = service.structure(models.Budget, "@bank/budget")
transaction = service.structure(
    models.Transaction,
    "@bank/transaction",
    describe=lambda transaction: {"@bank/status": str(transaction.status), "@bank/kind": str(transaction.kind or ""), "@bank/currency": transaction.currency or ""},
)


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
