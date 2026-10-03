"""bank as a service of the hub: what exists here (vendored ``rekuest_service``).

Two separate declarations, read by rekuest from the service's manifest (``*service.urls`` in
``urls.py``) and catalogued hub-wide:

* the **structures** bank hosts, and the descriptors of their objects. The GraphQL types answer
  ``descriptors`` from the same declarations (``finance.types``);
* the **signals** it emits: which saves and deletes are announced, with no emit in the mutations.
  Users' triggers are checked against the kinds and descriptor keys declared here.

Hosting announces nothing by itself: a structure with no signal below is hosted silently.

That is all a service is. What can be *done* in this process is not declared here: that is an
agent's to say (``bank_server.hook_agent``), a different thing with its own configuration.

Transactions arrive in bulk (``bulk_create`` sends no ``post_save``), so a sync is announced once,
by hand, as an UPDATED of its account carrying how many rows it brought (``finance.sync``). Those
counts are facts about the sync, not about the account: they are signal descriptors, and the
account's own ``descriptors`` never list them.
"""

from rekuest_service import Descriptor, Service, organization_of

from finance import models

service = Service("bank", description="Bank accounts, their transactions, categories and budgets.")


# --- Structures: what bank hosts ------------------------------------------------------

bankconnection = service.structure(
    models.BankConnection,
    "@bank/bankconnection",
    descriptors=(
        Descriptor("@bank/status", "STRING", "Where the consent is: PENDING, ACTIVE, EXPIRED, REVOKED or FAILED"),
        Descriptor("@bank/provider", "STRING", "The provider the bank is reached through"),
    ),
    describe=lambda connection: {"@bank/status": str(connection.status), "@bank/provider": str(connection.provider)},
    description="One consent at one bank, through which its accounts are synced.",
)
bank_account = service.structure(
    models.BankAccount,
    "@bank/bankaccount",
    descriptors=(
        Descriptor("@bank/kind", "STRING", "What the account holds: CASH, DEPOT or SAVINGS"),
        Descriptor("@bank/currency", "STRING", "Its currency (ISO 4217), empty when unknown"),
    ),
    describe=lambda account: {"@bank/kind": str(account.kind), "@bank/currency": account.currency or ""},
    description="A bank account, with its history across relinks.",
)
statementimport = service.structure(
    models.StatementImport,
    "@bank/statementimport",
    descriptors=(
        Descriptor("@bank/status", "STRING", "Where the import is: PREVIEWED, APPLIED or FAILED"),
        Descriptor("@bank/source", "STRING", "The app or format the statement came from"),
    ),
    describe=lambda statement: {"@bank/status": str(statement.status), "@bank/source": str(statement.source)},
    description="An uploaded statement export and what importing it did.",
)
recurringpayment = service.structure(
    models.RecurringPayment,
    "@bank/recurringpayment",
    descriptors=(
        Descriptor("@bank/status", "STRING", "How a user judged it: DETECTED (not reviewed yet), CONFIRMED or IGNORED"),
        Descriptor("@bank/interval_days", "INT", "The days between two payments"),
    ),
    describe=lambda recurring: {"@bank/status": str(recurring.status), "@bank/interval_days": recurring.interval_days},
    description="A payment that repeats at a regular interval on an account.",
)
category = service.structure(
    models.Category,
    "@bank/category",
    descriptors=(Descriptor("@bank/kind", "STRING", "Whether its money is an expense, income or a transfer"),),
    describe=lambda category: {"@bank/kind": str(category.kind)},
    description="A spending or income category.",
)
merchant = service.structure(
    models.Merchant,
    "@bank/merchant",
    description="Someone the organization pays or is paid by.",
)
budget = service.structure(
    models.Budget,
    "@bank/budget",
    description="A monthly spending limit for a category.",
)
transaction = service.structure(
    models.Transaction,
    "@bank/transaction",
    descriptors=(
        Descriptor("@bank/status", "STRING", "Its booking status at the bank: BOOK, PDNG (pending) or OTHR"),
        Descriptor("@bank/kind", "STRING", "The provider's transaction type (BUY, SELL, DEPOSIT, …), empty for a plain bank transaction"),
        Descriptor("@bank/currency", "STRING", "Its currency (ISO 4217), empty when unknown"),
    ),
    describe=lambda transaction: {"@bank/status": str(transaction.status), "@bank/kind": str(transaction.kind or ""), "@bank/currency": transaction.currency or ""},
    description="A booked or pending transaction on an account.",
)


# --- Signals: what bank announces ------------------------------------------------------

ALL = ("CREATED", "UPDATED", "DELETED")
UPSERTED = ("CREATED", "UPDATED")
org = organization_of()

#: What only the per-sync UPDATED of an account carries (see ``finance.sync._signal_sync``).
NEW_TRANSACTIONS = "@bank/new_transactions"
UPDATED_TRANSACTIONS = "@bank/updated_transactions"

service.model_signal(
    bankconnection,
    kinds=UPSERTED,
    organization=org,
    description="A bank link was started, completed, expired or revoked.",
)
#: Also emitted by hand, once per sync (``finance.sync._signal_sync``).
bank_account_signal = service.model_signal(
    bank_account,
    kinds=UPSERTED,
    organization=org,
    descriptors=(NEW_TRANSACTIONS, UPDATED_TRANSACTIONS),
    description="A bank account appeared, changed, or was synced (then with the number of new and updated transactions).",
)
service.model_signal(
    statementimport,
    kinds=UPSERTED,
    organization=org,
    description="A statement import was created or changed status.",
)
service.model_signal(
    recurringpayment,
    kinds=UPSERTED,
    organization=org,
    description="A recurring payment was detected or reviewed.",
)
service.model_signal(
    category,
    kinds=ALL,
    organization=org,
    description="A category was created, changed or deleted.",
)
service.model_signal(
    merchant,
    kinds=ALL,
    organization=org,
    description="A merchant was created, changed or deleted.",
)
service.model_signal(
    budget,
    kinds=ALL,
    organization=org,
    description="A budget was created, changed or deleted.",
)
# Edits by users (categorizing, marking transfers); new transactions are bulk-created and are
# announced per sync on their account instead.
service.model_signal(
    transaction,
    kinds=("UPDATED",),
    organization=organization_of("account.organization"),
    description="A transaction was edited (categorized, marked as a transfer, …).",
)
