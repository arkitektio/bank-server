"""What a provider can answer with, in one family.

Every backend raises subclasses of :class:`ProviderError`, each knowing its
:class:`~finance.models.BankErrorCode`. So :func:`finance.errors.code_for` and the sync never
name a provider's classes, and a new kind brings its errors with it.
"""

from datetime import timedelta

from finance.models import BankErrorCode

#: How long a started link may stay PENDING.
PENDING_TTL = timedelta(minutes=30)


class ProviderError(Exception):
    """A provider refused a request, or could not be asked.

    ``status`` is the HTTP status (0: never reached it). ``explicit_code`` is set when the answer
    means something specific to a client; otherwise the code follows from the status.
    """

    status: int = 0
    explicit_code: BankErrorCode | None = None
    #: Seconds the provider asked us to wait, when it said (rate limits).
    retry_after: int | None = None

    @property
    def code(self) -> BankErrorCode:
        """The kind of failure a client acts on."""
        if self.explicit_code is not None:
            return self.explicit_code
        return BankErrorCode.BANK_UNAVAILABLE if self.status == 0 or self.status >= 500 else BankErrorCode.BANK_ERROR


class NotConfigured(ProviderError):
    """There is no usable provider for this: none set up, disabled, or lacking the capability."""

    explicit_code = BankErrorCode.NOT_CONFIGURED


class ConsentGone(ProviderError):
    """The consent or login ran out; the user must link again."""

    explicit_code = BankErrorCode.CONSENT_EXPIRED


class LinkError(Exception):
    """The link cannot be started or completed as asked.

    ``code`` is the :class:`~finance.models.BankErrorCode` a client acts on; None marks invalid
    input (a validation error).
    """

    def __init__(self, message: str, code: BankErrorCode | None = BankErrorCode.INVALID_STATE) -> None:
        super().__init__(message)
        self.code = code
