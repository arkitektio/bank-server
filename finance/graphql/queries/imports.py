"""Statement imports by id (lists are plain paginated fields in the schema)."""

import strawberry
from kante.types import Info

from finance import models
from finance.graphql.utils import get_or_404
from finance.types.imports import StatementImport

__all__ = ["statement_import"]


def statement_import(info: Info, id: strawberry.ID) -> StatementImport:
    """A statement import by id."""
    return get_or_404(models.StatementImport, info, id)  # type: ignore[return-value]
