"""Which account a provider's account (or an imported one) is.

An account is found, in order, by:

1. its syncer — the provider's stable identity (``backend`` + ``identification_key``);
2. its IBAN (with its currency) — only when exactly one account of the organization has it and
   no live syncer of the same provider feeds it already. This is how a relink adopts an account
   that was only imported, or whose old syncer's identity changed, instead of starting a second
   one — while two accounts one consent reports with the same IBAN (currency pockets) stay two.
   Several matches are ambiguous and adopt nothing;
3. else a new account.
"""

from django.db.models import Q, QuerySet

from finance import models
from finance.models import normalize_iban


def by_iban(organization_id: int, iban: str | None, currency: str | None) -> QuerySet:
    """The organization's accounts with this IBAN (and currency, when given)."""
    normalized = normalize_iban(iban)
    if normalized is None:
        return models.BankAccount.objects.none()
    accounts = models.BankAccount.objects.filter(organization_id=organization_id, iban_normalized=normalized)
    return accounts.filter(currency=currency) if currency else accounts


def adoptable(organization_id: int, iban: str | None, currency: str | None, *, for_connection: models.BankConnection | None = None) -> models.BankAccount | None:
    """The one account with this IBAN and currency; None if there is none or several.

    ``for_connection`` (a link being completed) leaves out accounts a live syncer of the same
    provider already feeds — through an ACTIVE or PENDING connection, or this very one.
    """
    candidates = by_iban(organization_id, iban, currency)
    if for_connection is not None:
        live = Q(connection_id=for_connection.id) | Q(connection__status__in=[models.ConnectionStatus.ACTIVE, models.ConnectionStatus.PENDING])
        fed = models.AccountSyncer.objects.filter(live, backend=for_connection.provider).values("account_id")
        candidates = candidates.exclude(id__in=fed)
    matches = list(candidates[:2])
    return matches[0] if len(matches) == 1 else None


def attach_syncer(
    connection: models.BankConnection,
    identification_key: str,
    remote_id: str,
    *,
    iban: str | None,
    name: str | None,
    currency: str,
    product: str | None,
    kind: str | None = None,
    raw: dict | None = None,
) -> models.AccountSyncer:
    """Point the syncer for this provider identity at ``connection``, finding or creating its account.

    Runs inside the linking transaction. The provider's view of the account (IBAN, name,
    currency, product, kind) overwrites the stored one, as every relink always did.
    """
    organization_id = connection.organization_id
    backend = connection.provider
    syncer = (
        models.AccountSyncer.objects.select_for_update()
        .select_related("account")
        .filter(organization_id=organization_id, backend=backend, identification_key=identification_key)
        .first()
    )
    account = syncer.account if syncer else adoptable(organization_id, iban, currency, for_connection=connection)
    if account is None:
        account = models.BankAccount(organization_id=organization_id)
    account.iban = iban or account.iban
    account.name = name or account.name
    account.currency = currency
    account.product = product or account.product
    if kind is not None:
        account.kind = kind
    account.save()

    if syncer is None:
        syncer = models.AccountSyncer(organization_id=organization_id, backend=backend, identification_key=identification_key)
    syncer.account = account
    syncer.connection = connection
    syncer.remote_id = remote_id
    syncer.raw = raw or {}
    syncer.last_error = syncer.last_error_code = None
    syncer.save()
    return syncer
