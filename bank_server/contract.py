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

#: What exists on a hub because this service is there: said here, as data, so the hub knows it from
#: the image. ``service.py`` binds each of these to its model and refuses anything not said here.
HOSTS = Hosts(
    structures=[
        Structure(
            identifier="@bank/bankconnection",
            label="Bank Connection",
            description="One consent at one bank, through which its accounts are synced.",
            descriptors=[
                Descriptor(key="@bank/status", type="STRING", description="Where the consent is: PENDING, ACTIVE, EXPIRED, REVOKED or FAILED"),
                Descriptor(key="@bank/provider", type="STRING", description="The provider the bank is reached through"),
            ],
        ),
        Structure(
            identifier="@bank/bankprovider",
            label="Bank Provider",
            description="A provider an organization set up (an Enable Banking application, Scalable Capital), through which banks are linked.",
            descriptors=[
                Descriptor(key="@bank/provider", type="STRING", description="The kind of provider"),
                Descriptor(key="@bank/enabled", type="BOOL", description="Whether it starts links and syncs"),
            ],
        ),
        Structure(
            identifier="@bank/bankaccount",
            label="Bank Account",
            description="A bank account, with its history across relinks.",
            descriptors=[
                Descriptor(key="@bank/kind", type="STRING", description="What the account holds: CASH, DEPOT or SAVINGS"),
                Descriptor(key="@bank/currency", type="STRING", description="Its currency (ISO 4217), empty when unknown"),
            ],
        ),
        Structure(
            identifier="@bank/statementimport",
            label="Statement Import",
            description="An uploaded statement export and what importing it did.",
            descriptors=[
                Descriptor(key="@bank/status", type="STRING", description="Where the import is: PREVIEWED, APPLIED or FAILED"),
                Descriptor(key="@bank/source", type="STRING", description="The app or format the statement came from"),
            ],
        ),
        Structure(
            identifier="@bank/recurringpayment",
            label="Recurring Payment",
            description="A payment that repeats at a regular interval on an account.",
            descriptors=[
                Descriptor(key="@bank/status", type="STRING", description="How a user judged it: DETECTED (not reviewed yet), CONFIRMED or IGNORED"),
                Descriptor(key="@bank/interval_days", type="INT", description="The days between two payments"),
            ],
        ),
        Structure(
            identifier="@bank/category",
            label="Category",
            description="A spending or income category.",
            descriptors=[
                Descriptor(key="@bank/kind", type="STRING", description="Whether its money is an expense, income or a transfer"),
            ],
        ),
        Structure(
            identifier="@bank/merchant",
            label="Merchant",
            description="Someone the organization pays or is paid by.",
        ),
        Structure(
            identifier="@bank/budget",
            label="Budget",
            description="A monthly spending limit for a category.",
        ),
        Structure(
            identifier="@bank/transaction",
            label="Transaction",
            description="A booked or pending transaction on an account.",
            descriptors=[
                Descriptor(key="@bank/status", type="STRING", description="Its booking status at the bank: BOOK, PDNG (pending) or OTHR"),
                Descriptor(key="@bank/kind", type="STRING", description="The provider's transaction type (BUY, SELL, DEPOSIT, …), empty for a plain bank transaction"),
                Descriptor(key="@bank/currency", type="STRING", description="Its currency (ISO 4217), empty when unknown"),
            ],
        ),
    ],
    signals=[
        Signal(
            identifier="@bank/bankconnection",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@bank/status", "@bank/provider"],
            description="A bank link was started, completed, expired or revoked.",
        ),
        Signal(
            identifier="@bank/bankprovider",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=["@bank/provider", "@bank/enabled"],
            description="A provider was set up, changed (enabled, its capabilities, its key) or removed.",
        ),
        Signal(
            identifier="@bank/bankaccount",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@bank/kind", "@bank/currency", NEW_TRANSACTIONS, UPDATED_TRANSACTIONS],
            description="A bank account appeared, changed, or was synced (then with the number of new and updated transactions).",
        ),
        Signal(
            identifier="@bank/statementimport",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@bank/status", "@bank/source"],
            description="A statement import was created or changed status.",
        ),
        Signal(
            identifier="@bank/recurringpayment",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@bank/status", "@bank/interval_days"],
            description="A recurring payment was detected or reviewed.",
        ),
        Signal(
            identifier="@bank/category",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=["@bank/kind"],
            description="A category was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/merchant",
            kinds=["CREATED", "UPDATED", "DELETED"],
            description="A merchant was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/budget",
            kinds=["CREATED", "UPDATED", "DELETED"],
            description="A budget was created, changed or deleted.",
        ),
        Signal(
            identifier="@bank/transaction",
            kinds=["UPDATED"],
            descriptors=["@bank/status", "@bank/kind", "@bank/currency"],
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
    },
    setup=("ensureadmin",),
)
