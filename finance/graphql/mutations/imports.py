"""Importing statement exports (Finanzguru): preview an uploaded file, map its categories, apply it.

The file reaches the server through the datalayer: ``requestBigfileUpload`` → S3 PUT with the
grant → ``finishBigfileUpload`` → ``createFinanzguruImport(file: <store id>)``.
"""

from typing import Optional

import strawberry
from django.conf import settings
from kante.errors import KanteError, ValidationError
from kante.types import Info

from datalayer import models as dl_models
from datalayer.datalayer import get_current_datalayer
from finance import models
from finance.graphql.utils import get_or_404
from finance.imports import apply as importing
from finance.types.imports import ImportCategoryMapping, StatementImport

__all__ = [
    "CreateFinanzguruImportInput",
    "ImportAccountChoice",
    "ApplyStatementImportInput",
    "ImportCategoryMappingInput",
    "create_finanzguru_import",
    "apply_statement_import",
    "set_import_category_mappings",
]


@strawberry.input(description="Preview an uploaded Finanzguru export.")
class CreateFinanzguruImportInput:
    file: strawberry.ID = strawberry.field(description="The uploaded file's store (`finishBigfileUpload`'s id).")


@strawberry.input(description="Where one account of the file goes, overriding the preview's choice.")
class ImportAccountChoice:
    reference: str = strawberry.field(description="The account as the file names it (the preview's `accounts.reference`).")
    account: Optional[strawberry.ID] = strawberry.field(default=None, description="Land its rows in this account; null creates a new account.")
    skip: bool = strawberry.field(default=False, description="Leave this account out of the import.")


@strawberry.input(description="Apply a previewed import.")
class ApplyStatementImportInput:
    id: strawberry.ID
    accounts: Optional[list[ImportAccountChoice]] = strawberry.field(default=None, description="Choices for accounts of the file; the others go where the preview said (an AMBIGUOUS one needs a choice).")


@strawberry.input(description="Which category an imported (main, sub) category means.")
class ImportCategoryMappingInput:
    main: str
    sub: str = ""
    category: Optional[strawberry.ID] = strawberry.field(default=None, description="The category; null leaves such rows to rules and suggestions.")


def _content(statement_import: models.StatementImport) -> bytes:
    if statement_import.file is None:
        raise ValidationError("This import has no uploaded file (it was made by the management command); import the file again.")
    store = statement_import.file
    return get_current_datalayer().read_object(store.bucket, store.key)


def create_finanzguru_import(info: Info, input: CreateFinanzguruImportInput) -> StatementImport:
    """Read an uploaded Finanzguru export and preview it: which account each of its accounts lands in, how many rows are new or already there, which categories need a mapping. Nothing is written into the accounts until `applyStatementImport`."""
    if not getattr(settings, "DATALAYER", None):
        raise KanteError("This server has no S3 storage configured for uploads.", code=str(models.BankErrorCode.NOT_CONFIGURED))
    store = get_or_404(dl_models.BigFileStore, info, input.file)
    if not store.populated:
        store.fill_info(get_current_datalayer())
    request = info.context.request
    statement_import = models.StatementImport.objects.create(
        organization=request.organization, creator=request.user, source=models.ImportSource.FINANZGURU, file=store, file_name=store.original_file_name
    )
    return importing.preview(statement_import, _content(statement_import))  # type: ignore[return-value]


def apply_statement_import(info: Info, input: ApplyStatementImportInput) -> StatementImport:
    """Write a previewed import into the accounts. Idempotent: applying again refreshes the rows it brought in, and never duplicates a booking a sync already has."""
    statement_import = get_or_404(models.StatementImport, info, input.id)
    if statement_import.status == models.ImportStatus.FAILED:
        raise ValidationError(f"This import failed to read: {statement_import.error}")
    choices = {c.reference: {"account": get_or_404(models.BankAccount, info, c.account).id if c.account else None, "skip": c.skip} for c in input.accounts or []}
    try:
        return importing.apply(statement_import.id, _content(statement_import), choices)  # type: ignore[return-value]
    except importing.StatementImportError as error:
        raise ValidationError(str(error)) from error


def set_import_category_mappings(info: Info, input: list[ImportCategoryMappingInput]) -> list[ImportCategoryMapping]:
    """Set which category imported category pairs mean (a user's choice: no longer a proposal). Applies to later imports and to re-applying one."""
    organization = info.context.request.organization
    out = []
    for item in input:
        category = get_or_404(models.Category, info, item.category) if item.category else None
        mapping, _ = models.ImportCategoryMapping.objects.update_or_create(
            organization=organization, source=models.ImportSource.FINANZGURU, main=item.main[:200], sub=item.sub[:200], defaults={"category": category, "proposed": False}
        )
        out.append(mapping)
    return out  # type: ignore[return-value]
