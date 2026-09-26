"""Service exceptions as GraphQL errors whose ``extensions.code`` is a :class:`~finance.models.BankErrorCode`.

The classification is :func:`finance.errors.code_for`, the same one that fills ``lastErrorCode``
on accounts and connections, so a client handles both with one table.
"""

from kante.errors import KanteError, ValidationError

from finance import linking
from finance.errors import SyncBudgetExhausted, code_for


def translate(error: Exception) -> Exception:
    """A GraphQL error for a known failure; anything else (a bug) passes through unchanged."""
    if isinstance(error, KanteError):
        return error
    if isinstance(error, linking.LinkError) and error.code is None:
        return ValidationError(str(error))
    code = code_for(error)
    if code is None:
        return error
    extensions = {"nextSyncAllowedAt": error.allowed_at.isoformat()} if isinstance(error, SyncBudgetExhausted) else {}
    return KanteError(str(error), code=str(code), extensions=extensions or None)
