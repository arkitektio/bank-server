"""What went wrong, as a :class:`~finance.models.BankErrorCode`.

One classification serves both places a client sees an error: the ``extensions.code`` of a
GraphQL error (:func:`finance.graphql.errors.translate`) and the ``lastErrorCode`` stored on
accounts and connections. A client maps each code to a fix (relink, try later, log in again, …).
"""

import asyncio
from datetime import datetime

import aiohttp

from finance.models import BankErrorCode


class SyncBudgetExhausted(Exception):
    """The account's daily sync budget is used up (or the provider asked us to wait)."""

    def __init__(self, allowed_at: datetime) -> None:
        super().__init__(f"No sync is allowed before {allowed_at.isoformat()}.")
        self.allowed_at = allowed_at


def code_for(error: BaseException) -> BankErrorCode | None:
    """The code of a known failure; None for anything else (a bug, not a provider answer)."""
    from finance.geocoding import GeocodingDisabled, GeocodingError
    from finance.prices.sources import PriceError
    from finance.providers.errors import LinkError, ProviderError
    from finance.sync import AlreadySyncing, ConnectionInactive

    if isinstance(error, LinkError):
        return error.code
    if isinstance(error, ProviderError):
        return error.code
    if isinstance(error, SyncBudgetExhausted):
        return BankErrorCode.RATE_LIMITED
    if isinstance(error, GeocodingDisabled):
        return BankErrorCode.NOT_CONFIGURED
    if isinstance(error, PriceError):
        return BankErrorCode.BANK_UNAVAILABLE if error.status == 0 or error.status >= 500 else BankErrorCode.BANK_ERROR
    if isinstance(error, GeocodingError):
        return BankErrorCode.BANK_UNAVAILABLE if error.status == 0 or error.status >= 500 else BankErrorCode.BANK_ERROR
    if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError)):
        return BankErrorCode.BANK_UNAVAILABLE
    if isinstance(error, ConnectionInactive):
        return BankErrorCode.CONNECTION_INACTIVE
    if isinstance(error, AlreadySyncing):
        return BankErrorCode.SYNC_IN_PROGRESS
    return None
