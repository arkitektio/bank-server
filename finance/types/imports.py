"""GraphQL types for statement imports (a Finanzguru export) and their category mappings."""

import datetime
from enum import Enum
from typing import List, Optional

import kante
import strawberry
import strawberry_django
from strawberry.scalars import JSON

from finance import models
from finance.types import BankAccount, Category, OrgScoped
from finance.types._shared import DESCRIPTORS_DESCRIPTION, resolve_descriptors
from finance.types.auth import User


@strawberry.enum(description="Which app or format a statement import came from.")
class ImportSource(str, Enum):
    FINANZGURU = "FINANZGURU"


@strawberry.enum(description="Where a statement import is: PREVIEWED (nothing written yet), APPLIED, or FAILED (the file could not be read; see `error`).")
class ImportStatus(str, Enum):
    PREVIEWED = "PREVIEWED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


@strawberry.enum(description="How a source account's target was decided: IBAN (the organization's one account with its IBAN), IMPORTED (created by an earlier import), NEW (a new account will be created), AMBIGUOUS (several accounts have the IBAN — choose one when applying), CHOSEN (the caller chose it), SKIPPED.")
class ImportTargetKind(str, Enum):
    IBAN = "IBAN"
    IMPORTED = "IMPORTED"
    NEW = "NEW"
    AMBIGUOUS = "AMBIGUOUS"
    CHOSEN = "CHOSEN"
    SKIPPED = "SKIPPED"


@strawberry.type(description="One account of the imported file (a Finanzguru `Referenzkonto`) and where its rows go.")
class ImportAccountPlan:
    reference: str = strawberry.field(description="The account as the file names it (an IBAN, or e.g. a card or PayPal account).")
    name: Optional[str] = strawberry.field(description="The file's name for it (`Name Referenzkonto`).")
    currency: str
    iban: Optional[str] = strawberry.field(description="Its IBAN, if the reference is one.")
    how: ImportTargetKind
    account: Optional[BankAccount] = strawberry.field(description="The account the rows go to; null for a new account (or when ambiguous or skipped).")
    candidates: List[BankAccount] = strawberry.field(description="AMBIGUOUS only: the accounts that have its IBAN.")
    rows: int
    first_date: datetime.date
    last_date: datetime.date
    already_imported: int = strawberry.field(description="Rows an earlier import brought in already (they are refreshed, not duplicated).")
    matched: int = strawberry.field(description="Rows that are the same booking as a synced row: that row gets the import's category and note instead of a duplicate.")
    new: int = strawberry.field(description="Rows that become new transactions.")


@strawberry.type(description="An imported category pair no category is mapped to (yet): its rows stay uncategorized, for rules and suggestions.")
class UnmappedImportCategory:
    main: str
    sub: str
    rows: int


def _accounts(report: dict, key: str = "accounts") -> list[ImportAccountPlan]:
    ids = {e["account_id"] for e in report.get(key) or [] if e.get("account_id")} | {c for e in report.get(key) or [] for c in e.get("candidates") or []}
    accounts = {a.id: a for a in models.BankAccount.objects.filter(id__in=ids)}
    return [
        ImportAccountPlan(
            reference=e["reference"],
            name=e.get("name"),
            currency=e["currency"],
            iban=e.get("iban"),
            how=ImportTargetKind(e["how"]),
            account=accounts.get(e.get("account_id")),  # type: ignore[arg-type]
            candidates=[accounts[c] for c in e.get("candidates") or [] if c in accounts],  # type: ignore[misc]
            rows=e["rows"],
            first_date=datetime.date.fromisoformat(e["first_date"]),
            last_date=datetime.date.fromisoformat(e["last_date"]),
            already_imported=e.get("already_imported", 0),
            matched=e.get("matched", 0),
            new=e.get("new", 0),
        )
        for e in report.get(key) or []
    ]


@kante.django_type(models.StatementImport, pagination=True, description="An uploaded statement export: previewed (nothing written), then applied into the accounts. Applying again is idempotent.")
class StatementImport(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION)
    source: ImportSource
    status: ImportStatus
    file_name: Optional[str]
    error: Optional[str] = strawberry_django.field(description="FAILED only: why the file could not be read.")
    creator: Optional[User]
    created_at: datetime.datetime
    applied_at: Optional[datetime.datetime]
    report: JSON = strawberry_django.field(description="Everything the preview (and the last apply) found, as stored.")

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):  # noqa: ANN001, ANN206
        return super().get_queryset(queryset, info, **kwargs).order_by("-created_at", "-id")

    @strawberry_django.field(description="Each account of the file and where its rows go.")
    def accounts(self) -> List[ImportAccountPlan]:
        return _accounts(self.report or {})  # type: ignore[arg-type]

    @strawberry_django.field(description="Category pairs of the file without a mapped category; `setImportCategoryMappings` maps them.")
    def unmapped_categories(self) -> List[UnmappedImportCategory]:
        return [UnmappedImportCategory(main=u["main"], sub=u["sub"], rows=u["rows"]) for u in (self.report or {}).get("unmapped_categories") or []]  # type: ignore[union-attr]

    @strawberry_django.field(description="What was odd about the file (skipped lines, split parts that do not add up, unknown columns).")
    def warnings(self) -> List[str]:
        return list((self.report or {}).get("warnings") or [])  # type: ignore[union-attr]

    @strawberry_django.field(description="Bookings in the file (split parts folded into their booking).")
    def rows(self) -> int:
        return int((self.report or {}).get("rows") or 0)  # type: ignore[union-attr]

    @strawberry_django.field(description="How many transactions carry this import (new ones and enriched synced ones).")
    def transactions_count(self) -> int:
        return self.transactions.count()  # type: ignore[attr-defined]


@kante.django_type(models.ImportCategoryMapping, pagination=True, description="Which category an imported app's (main, sub) category means. Proposed on first sight, editable; every import uses it.")
class ImportCategoryMapping(OrgScoped):
    id: strawberry.ID
    source: ImportSource
    main: str
    sub: str
    category: Optional[Category] = strawberry_django.field(description="The category rows with this pair get; null leaves them to rules and suggestions.")
    proposed: bool = strawberry_django.field(description="Still the automatic proposal (false once a user set it).")
    created_at: datetime.datetime
