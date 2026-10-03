"""bank as the hub's rekuest sees it (vendored ``rekuest_service``): the service, and its HookAgent.

Two declarations, read by rekuest from one manifest and mounted by ``urls.py`` (``*service.urls``):

* the **service** says what exists: the structures bank hosts, the descriptors of their objects,
  and — every save and delete being announced, with no emit in the mutations — the signals it
  emits. Hub-wide; users' triggers are checked against the kinds and descriptor keys declared here,
  and the GraphQL types answer ``descriptors`` from the same declarations (``finance.types``);
* its **agent** says what can be done: the actions rekuest runs here (``finance/scheduled.py``).
  Every organization has the agent and its own schedules, so an action does one organization's
  share of the work.

Transactions arrive in bulk (``bulk_create`` sends no ``post_save``), so a sync is announced once,
by hand, as an UPDATED of its account carrying how many rows it brought (``finance.sync``). Those
counts are facts about the sync, not about the account: they are signal descriptors, and the
account's own ``descriptors`` never list them.
"""

from rekuest_service import Descriptor, HookAgent, Service, organization_of

from finance import models

service = Service("bank", description="Bank accounts, their transactions, categories and budgets.")


# --- Structures ---------------------------------------------------------------------------

ALL = ("CREATED", "UPDATED", "DELETED")
UPSERTED = ("CREATED", "UPDATED")
org = organization_of()

#: What only the per-sync UPDATED of an account carries (see ``finance.sync._signal_sync``).
NEW_TRANSACTIONS = "@bank/new_transactions"
UPDATED_TRANSACTIONS = "@bank/updated_transactions"

service.structure(
    models.BankConnection,
    "@bank/bankconnection",
    kinds=UPSERTED,
    organization=org,
    descriptors=(
        Descriptor("@bank/status", "STRING", "Where the consent is: PENDING, ACTIVE, EXPIRED, REVOKED or FAILED"),
        Descriptor("@bank/provider", "STRING", "The provider the bank is reached through"),
    ),
    describe=lambda connection: {"@bank/status": str(connection.status), "@bank/provider": str(connection.provider)},
    description="One consent at one bank, through which its accounts are synced.",
    signal_description="A bank link was started, completed, expired or revoked.",
)
bank_account = service.structure(
    models.BankAccount,
    "@bank/bankaccount",
    kinds=UPSERTED,
    organization=org,
    descriptors=(
        Descriptor("@bank/kind", "STRING", "What the account holds: CASH, DEPOT or SAVINGS"),
        Descriptor("@bank/currency", "STRING", "Its currency (ISO 4217), empty when unknown"),
    ),
    describe=lambda account: {"@bank/kind": str(account.kind), "@bank/currency": account.currency or ""},
    signal_descriptors=(NEW_TRANSACTIONS, UPDATED_TRANSACTIONS),
    description="A bank account, with its history across relinks.",
    signal_description="A bank account appeared, changed, or was synced (then with the number of new and updated transactions).",
)
service.structure(
    models.StatementImport,
    "@bank/statementimport",
    kinds=UPSERTED,
    organization=org,
    descriptors=(
        Descriptor("@bank/status", "STRING", "Where the import is: PREVIEWED, APPLIED or FAILED"),
        Descriptor("@bank/source", "STRING", "The app or format the statement came from"),
    ),
    describe=lambda statement: {"@bank/status": str(statement.status), "@bank/source": str(statement.source)},
    description="An uploaded statement export and what importing it did.",
    signal_description="A statement import was created or changed status.",
)
service.structure(
    models.RecurringPayment,
    "@bank/recurringpayment",
    kinds=UPSERTED,
    organization=org,
    descriptors=(
        Descriptor("@bank/status", "STRING", "How a user judged it: DETECTED (not reviewed yet), CONFIRMED or IGNORED"),
        Descriptor("@bank/interval_days", "INT", "The days between two payments"),
    ),
    describe=lambda recurring: {"@bank/status": str(recurring.status), "@bank/interval_days": recurring.interval_days},
    description="A payment that repeats at a regular interval on an account.",
    signal_description="A recurring payment was detected or reviewed.",
)
service.structure(
    models.Category,
    "@bank/category",
    kinds=ALL,
    organization=org,
    descriptors=(Descriptor("@bank/kind", "STRING", "Whether its money is an expense, income or a transfer"),),
    describe=lambda category: {"@bank/kind": str(category.kind)},
    description="A spending or income category.",
    signal_description="A category was created, changed or deleted.",
)
service.structure(
    models.Merchant,
    "@bank/merchant",
    kinds=ALL,
    organization=org,
    description="Someone the organization pays or is paid by.",
    signal_description="A merchant was created, changed or deleted.",
)
service.structure(
    models.Budget,
    "@bank/budget",
    kinds=ALL,
    organization=org,
    description="A monthly spending limit for a category.",
    signal_description="A budget was created, changed or deleted.",
)
# Edits by users (categorizing, marking transfers); new transactions are bulk-created and are
# announced per sync on their account instead.
service.structure(
    models.Transaction,
    "@bank/transaction",
    kinds=("UPDATED",),
    organization=organization_of("account.organization"),
    descriptors=(
        Descriptor("@bank/status", "STRING", "Its booking status at the bank: BOOK, PDNG (pending) or OTHR"),
        Descriptor("@bank/kind", "STRING", "The provider's transaction type (BUY, SELL, DEPOSIT, …), empty for a plain bank transaction"),
        Descriptor("@bank/currency", "STRING", "Its currency (ISO 4217), empty when unknown"),
    ),
    describe=lambda transaction: {"@bank/status": str(transaction.status), "@bank/kind": str(transaction.kind or ""), "@bank/currency": transaction.currency or ""},
    description="A booked or pending transaction on an account.",
    signal_description="A transaction was edited (categorized, marked as a transfer, …).",
)


# --- The HookAgent ------------------------------------------------------------------------
# Its actions are registered by ``finance/scheduled.py``, which ``FinanceConfig.ready`` imports.

agent = HookAgent(service)
