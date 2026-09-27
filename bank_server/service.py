"""bank as the hub's rekuest sees it: the actions it offers (``finance/scheduled.py``) and the
signals it emits (vendored ``rekuest_service``).

One ``Service`` declaration, mounted by ``urls.py`` (``*service.urls``) and read by rekuest from
the manifest. Transactions arrive in bulk (``bulk_create`` sends no ``post_save``), so a sync is
announced once, as an UPDATED of its account carrying how many rows it brought.
"""

from rekuest_service import Service, organization_of

from finance import models

service = Service("bank", description="Bank accounts, their transactions, categories and budgets.")

ALL = ("CREATED", "UPDATED", "DELETED")
UPSERTED = ("CREATED", "UPDATED")
org = organization_of()

service.model_signal(
    models.BankConnection, "@bank/bankconnection", kinds=UPSERTED, organization=org,
    descriptors=lambda c: {"@bank/status": str(c.status), "@bank/provider": str(c.provider)},
    descriptor_keys=("@bank/status", "@bank/provider"),
    description="A bank link was started, completed, expired or revoked.",
)
account_signal = service.model_signal(
    models.BankAccount, "@bank/bankaccount", kinds=UPSERTED, organization=org,
    descriptors=lambda a: {"@bank/kind": str(a.kind), "@bank/currency": a.currency or ""},
    # The two counts ride only on the per-sync UPDATED (see ``finance.sync.after_sync``).
    descriptor_keys=("@bank/kind", "@bank/currency", "@bank/new_transactions", "@bank/updated_transactions"),
    description="A bank account appeared, changed, or was synced (then with the number of new and updated transactions).",
)
service.model_signal(
    models.StatementImport, "@bank/statementimport", kinds=UPSERTED, organization=org,
    descriptors=lambda i: {"@bank/status": str(i.status), "@bank/source": str(i.source)},
    descriptor_keys=("@bank/status", "@bank/source"),
    description="A statement import was created or changed status.",
)
service.model_signal(
    models.RecurringPayment, "@bank/recurringpayment", kinds=UPSERTED, organization=org,
    descriptors=lambda r: {"@bank/status": str(r.status), "@bank/interval_days": r.interval_days},
    descriptor_keys=("@bank/status", "@bank/interval_days"),
    description="A recurring payment was detected or reviewed.",
)
service.model_signal(
    models.Category, "@bank/category", kinds=ALL, organization=org,
    descriptors=lambda c: {"@bank/kind": str(c.kind)},
    descriptor_keys=("@bank/kind",),
    description="A category was created, changed or deleted.",
)
service.model_signal(models.Merchant, "@bank/merchant", kinds=ALL, organization=org, description="A merchant was created, changed or deleted.")
service.model_signal(models.Budget, "@bank/budget", kinds=ALL, organization=org, description="A budget was created, changed or deleted.")
# Edits by users (categorizing, marking transfers); new transactions are bulk-created and are
# announced per sync on their account instead.
service.model_signal(
    models.Transaction, "@bank/transaction", kinds=("UPDATED",), organization=organization_of("account.organization"),
    descriptors=lambda t: {"@bank/status": str(t.status), "@bank/kind": str(t.kind), "@bank/currency": t.currency or ""},
    descriptor_keys=("@bank/status", "@bank/kind", "@bank/currency"),
    description="A transaction was edited (categorized, marked as a transfer, …).",
)
