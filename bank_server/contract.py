"""What this image answers a hub's installer: ``arkitekt-service <verb>`` (see ``arkitekt_service.contract``).

The installer knows the hub; how this release spells its config is written here, with the
settings it is read by. A key renamed in ``configuration.py`` is renamed in :func:`render` in
the same commit, and no installer has to learn of it.
"""

from __future__ import annotations

from arkitekt_service.contract import JSON, Contract, Description, Descriptor, Facts, Hosts, Job, Needs, Offers, Scope, Signal, Start, Structure, blocks

from bank_server.configuration import Settings

#: What a token may be allowed to do here: defined at the coordination server when the hub enrols.
SCOPES = [
    Scope(key="bank_read", description="Read bank accounts, transactions and budgets"),
    Scope(key="bank_write", description="Link accounts, import statements and edit budgets"),
]

#: What only the per-sync UPDATED of an account carries (see ``finance.sync._signal_sync``): facts
#: about the sync, not about the account, so the account's own descriptors never list them.
NEW_TRANSACTIONS = "@bank/new_transactions"
UPDATED_TRANSACTIONS = "@bank/updated_transactions"

#: Money in or out, from the sign of the amount: OUT when it is negative, IN otherwise (zero
#: included: no money left the account). A category rule's IN and OUT match neither for zero.
_DIRECTION = Descriptor(key="@bank/direction", type="STRING", description="Whether the money comes in or goes out: IN (zero or positive amount) or OUT (negative amount)")
_CURRENCY = Descriptor(key="@bank/currency", type="STRING", description="Its currency (ISO 4217), empty when unknown")

CONNECTION_DESCRIPTORS = [
    Descriptor(key="@bank/status", type="STRING", description="Where the consent is: PENDING, ACTIVE, EXPIRED, REVOKED, FAILED or CANCELLED"),
    Descriptor(key="@bank/provider", type="STRING", description="The provider the bank is reached through"),
    Descriptor(key="@bank/country", type="STRING", description="The bank's ISO country code, empty when none"),
    Descriptor(key="@bank/link_step", type="STRING", description="Where a login is: DEVICE, MFA or DONE, empty for a consent that has no steps"),
    Descriptor(key="@bank/error_code", type="STRING", description="The kind of the last error (a BankErrorCode such as CONSENT_EXPIRED), empty when there was none"),
]
PROVIDER_DESCRIPTORS = [
    Descriptor(key="@bank/provider", type="STRING", description="The kind of provider"),
    Descriptor(key="@bank/enabled", type="BOOL", description="Whether it starts links and syncs"),
    Descriptor(
        key="@bank/capabilities",
        type="LIST",
        description="What is switched on (TRANSACTIONS, BALANCES, HOLDINGS, PRICES, SCHEDULED_SYNC): HOLDINGS for a depot's positions",
    ),
]
ACCOUNT_DESCRIPTORS = [
    Descriptor(key="@bank/kind", type="STRING", description="What the account holds: CASH, DEPOT or SAVINGS"),
    _CURRENCY,
    Descriptor(key="@bank/has_iban", type="BOOL", description="Whether its IBAN is known"),
]
IMPORT_DESCRIPTORS = [
    Descriptor(key="@bank/status", type="STRING", description="Where the import is: PREVIEWED, APPLIED or FAILED"),
    Descriptor(key="@bank/source", type="STRING", description="The app or format the statement came from"),
]
RECURRING_DESCRIPTORS = [
    Descriptor(key="@bank/status", type="STRING", description="How a user judged it: DETECTED (not reviewed yet), CONFIRMED or IGNORED"),
    Descriptor(key="@bank/interval_days", type="INT", description="The days between two payments"),
    _CURRENCY,
    _DIRECTION,
]
CATEGORY_DESCRIPTORS = [
    Descriptor(key="@bank/kind", type="STRING", description="Whether its money is an expense, income or a transfer"),
    Descriptor(key="@bank/custom", type="BOOL", description="Whether the organization made it (true) or it is a base category of the taxonomy (false)"),
]
MERCHANT_DESCRIPTORS = [
    Descriptor(key="@bank/online", type="BOOL", description="Whether it is online only, with no physical stores"),
]
BUDGET_DESCRIPTORS = [_CURRENCY]
TRANSACTION_DESCRIPTORS = [
    Descriptor(key="@bank/status", type="STRING", description="Its booking status at the bank: BOOK, PDNG (pending) or OTHR"),
    Descriptor(key="@bank/kind", type="STRING", description="The provider's transaction type (BUY, SELL, DEPOSIT, …), empty for a plain bank transaction"),
    _CURRENCY,
    _DIRECTION,
    Descriptor(key="@bank/is_transfer", type="BOOL", description="Whether it moves money between the organization's own accounts (left out of stats)"),
    Descriptor(key="@bank/categorized", type="BOOL", description="Whether it has a category"),
    Descriptor(key="@bank/has_merchant", type="BOOL", description="Whether it is linked to a merchant"),
    Descriptor(key="@bank/has_security", type="BOOL", description="Whether it trades or pays out a security (its ISIN is known)"),
]


def _keys(descriptors: list[Descriptor]) -> list[str]:
    return [descriptor.key for descriptor in descriptors]


#: What exists on a hub because this service is there: said here, as data, so the hub knows it from
#: the image. ``service.py`` binds each of these to its model and refuses anything not said here.
HOSTS = Hosts(
    structures=[
        Structure(
            identifier="@bank/bankconnection",
            label="Bank Connection",
            description="One consent at one bank, through which its accounts are synced.",
            descriptors=CONNECTION_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/bankprovider",
            label="Bank Provider",
            description="A provider an organization set up (an Enable Banking application, Scalable Capital), through which banks are linked.",
            descriptors=PROVIDER_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/bankaccount",
            label="Bank Account",
            description="A bank account, with its history across relinks.",
            descriptors=ACCOUNT_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/statementimport",
            label="Statement Import",
            description="An uploaded statement export and what importing it did.",
            descriptors=IMPORT_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/recurringpayment",
            label="Recurring Payment",
            description="A payment that repeats at a regular interval on an account.",
            descriptors=RECURRING_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/category",
            label="Category",
            description="A spending or income category.",
            descriptors=CATEGORY_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/merchant",
            label="Merchant",
            description="Someone the organization pays or is paid by.",
            descriptors=MERCHANT_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/budget",
            label="Budget",
            description="A monthly spending limit for a category.",
            descriptors=BUDGET_DESCRIPTORS,
        ),
        Structure(
            identifier="@bank/transaction",
            label="Transaction",
            description="A booked or pending transaction on an account.",
            descriptors=TRANSACTION_DESCRIPTORS,
        ),
    ],
    signals=[
        Signal(
            identifier="@bank/bankconnection",
            kinds=["CREATED", "UPDATED"],
            descriptors=_keys(CONNECTION_DESCRIPTORS),
            description="A bank link was started, completed, expired, revoked, refused or cancelled.",
        ),
        Signal(
            identifier="@bank/bankprovider",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=_keys(PROVIDER_DESCRIPTORS),
            description="A provider was set up, changed (enabled, its capabilities, its key) or removed.",
        ),
        Signal(
            identifier="@bank/bankaccount",
            kinds=["CREATED", "UPDATED"],
            descriptors=[*_keys(ACCOUNT_DESCRIPTORS), NEW_TRANSACTIONS, UPDATED_TRANSACTIONS],
            description="A bank account appeared, changed, or was synced (then with the number of new and updated transactions).",
        ),
        Signal(
            identifier="@bank/statementimport",
            kinds=["CREATED", "UPDATED"],
            descriptors=_keys(IMPORT_DESCRIPTORS),
            description="A statement import was created or changed status.",
        ),
        Signal(
            identifier="@bank/recurringpayment",
            kinds=["CREATED", "UPDATED"],
            descriptors=_keys(RECURRING_DESCRIPTORS),
            description="A recurring payment was detected or reviewed.",
        ),
        Signal(
            identifier="@bank/category",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=_keys(CATEGORY_DESCRIPTORS),
            description="A category was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/merchant",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=_keys(MERCHANT_DESCRIPTORS),
            description="A merchant was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/budget",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=_keys(BUDGET_DESCRIPTORS),
            description="A budget was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/transaction",
            kinds=["UPDATED"],
            descriptors=_keys(TRANSACTION_DESCRIPTORS),
            description="A transaction was edited (categorized, marked as a transfer, …).",
        ),
    ],
)


def render(facts: Facts) -> dict[str, JSON]:
    """This release's config for the hub ``facts`` describes."""
    document: dict[str, JSON] = blocks.server(facts)
    if facts.storage is not None:
        document["datalayer"] = blocks.datalayer(facts)
    document["instance"] = blocks.instance(facts)
    fernet = facts.secrets.get("fernet")
    if fernet is not None:
        document["encryption"] = {"key_path": fernet}
    hook = blocks.rekuest_hook(facts)
    if hook is not None:
        document["rekuest_hook"] = hook
    return document


contract = Contract(
    description=Description(
        name="bank",
        identifier="live.arkitekt.bank",
        summary="Bank accounts, transactions and budgets.",
        needs=Needs(scopes=SCOPES, storage=["bigfile"], instance_key=True, peers=["rekuest"], secrets=["fernet"]),
        offers=Offers(endpoints={"rekuest_service": "_rekuest/service", "rekuest_hook": "_rekuest/hook"}),
        requires={"rekuest": ">=6"},
        hosts=HOSTS,
    ),
    settings=Settings,
    render=render,
    # How this service is started: there is no script beside it. `arkitekt-service serve`
    # (and `debug`) become these, so they get the container's signals themselves.
    serve=Start(("daphne", "-b", "0.0.0.0", "-p", "80", "--websocket_timeout", "-1", "bank_server.asgi:application")),
    debug=Start(("python", "manage.py", "runserver", "0.0.0.0:80")),
    jobs={
        "ensureadmin": Job(("ensureadmin",), "Create the operator account the config names"),
        "restore_descriptors": Job(("restore_descriptors",), "Store the descriptors of every object that misses one (--all: of every row)"),
    },
    setup=("ensureadmin", "restore_descriptors"),
)
