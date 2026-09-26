"""GraphQL enums for the model ``TextChoices``.

Plain ``(str, Enum)`` classes with the exact values the database stores, so an enum argument
can be written straight into a model field. ``tests/test_schema.py`` checks every one against
its ``TextChoices`` so the two cannot drift.
"""

from enum import Enum

import strawberry


@strawberry.enum(description="Lifecycle of a bank consent.")
class ConnectionStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    FAILED = "FAILED"


@strawberry.enum(description="Who a connection reaches its accounts through.")
class Provider(str, Enum):
    ENABLEBANKING = "ENABLEBANKING"
    SCALABLE = "SCALABLE"


@strawberry.enum(description="What an account holds. A DEPOT's balance is its market valuation; its positions are `holdings`.")
class AccountKind(str, Enum):
    CASH = "CASH"
    DEPOT = "DEPOT"
    SAVINGS = "SAVINGS"


@strawberry.enum(description="Where a Scalable link is: waiting for the login code to be approved, then for the second factor.")
class LinkStep(str, Enum):
    DEVICE = "DEVICE"
    MFA = "MFA"
    DONE = "DONE"


@strawberry.enum(description="What went wrong, so a client can offer the fix: CONSENT_EXPIRED → relink, RATE_LIMITED → try again at nextSyncAllowedAt, MFA_REJECTED → log in again, CODE_EXPIRED → get a new code, INVALID_STATE → start over, BANK_UNAVAILABLE → try later. Also every GraphQL error's `extensions.code`.")
class BankErrorCode(str, Enum):
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    MFA_REJECTED = "MFA_REJECTED"
    CODE_EXPIRED = "CODE_EXPIRED"
    INVALID_STATE = "INVALID_STATE"
    BANK_UNAVAILABLE = "BANK_UNAVAILABLE"
    BANK_ERROR = "BANK_ERROR"
    CONNECTION_INACTIVE = "CONNECTION_INACTIVE"
    SYNC_IN_PROGRESS = "SYNC_IN_PROGRESS"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@strawberry.enum(description="A provider's transaction type (Scalable's broker and savings types). OTHER is a type this service does not know yet.")
class TransactionKind(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SAVINGS_PLAN = "SAVINGS_PLAN"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DISTRIBUTION = "DISTRIBUTION"
    INTEREST = "INTEREST"
    FEE = "FEE"
    TAX = "TAX"
    TAX_RETURN = "TAX_RETURN"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    CASH_TRANSFER_IN = "CASH_TRANSFER_IN"
    CASH_TRANSFER_OUT = "CASH_TRANSFER_OUT"
    SWAP_IN = "SWAP_IN"
    SWAP_OUT = "SWAP_OUT"
    CURRENCY_SWITCH_BUY = "CURRENCY_SWITCH_BUY"
    CURRENCY_SWITCH_SELL = "CURRENCY_SWITCH_SELL"
    POCKET_MONEY = "POCKET_MONEY"
    REINVESTMENT = "REINVESTMENT"
    REINVESTMENT_DISTRIBUTION = "REINVESTMENT_DISTRIBUTION"
    REINVESTMENT_POCKET_MONEY = "REINVESTMENT_POCKET_MONEY"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    OTHER = "OTHER"


@strawberry.enum(description="How a started login finishes.")
class AuthFinish(str, Enum):
    REDIRECT = "REDIRECT"  # the provider redirects to redirectUrl with ?code&state; the client calls completeBankLink
    POLL = "POLL"  # the client calls the complete mutation every `interval` seconds until ACTIVE


@strawberry.enum(description="Booking status as reported by the bank.")
class TransactionStatus(str, Enum):
    BOOKED = "BOOK"
    PENDING = "PDNG"
    OTHER = "OTHR"


@strawberry.enum(description="Where a transaction's bank fields came from: a syncer (SYNC), or a file import (IMPORT) — a sync that finds the same booking later takes an IMPORT row over, keeping its category and note.")
class TransactionOrigin(str, Enum):
    SYNC = "SYNC"
    IMPORT = "IMPORT"


@strawberry.enum(description="Who set a transaction's category: a rule, a user (MANUAL — nothing overrides it), or SEMANTIC (similar categorized transactions or a category term agreed; rules and users override it).")
class CategorySource(str, Enum):
    NONE = "NONE"
    RULE = "RULE"
    MANUAL = "MANUAL"
    SEMANTIC = "SEMANTIC"
    IMPORT = "IMPORT"
    MERCHANT = "MERCHANT"


@strawberry.enum(description="Whether a category holds expenses, income, or transfers between own accounts (excluded from stats).")
class CategoryKind(str, Enum):
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"


@strawberry.enum(description="The transaction field a rule matches against.")
class RuleField(str, Enum):
    COUNTERPARTY = "COUNTERPARTY"
    REMITTANCE = "REMITTANCE"
    IBAN = "IBAN"


@strawberry.enum(description="How a rule's pattern is compared (case-insensitive).")
class RuleMatch(str, Enum):
    CONTAINS = "CONTAINS"
    EQUALS = "EQUALS"
    REGEX = "REGEX"


@strawberry.enum(description="Which transactions a rule applies to, by sign.")
class RuleDirection(str, Enum):
    ANY = "ANY"
    OUT = "OUT"
    IN = "IN"


@strawberry.enum(description="Whether a user confirmed or ignored a detected recurring payment. Only CONFIRMED ones feed forecasts by default.")
class RecurringStatus(str, Enum):
    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    IGNORED = "IGNORED"


@strawberry.enum(description="The bucket size of a cashflow series.")
class Granularity(str, Enum):
    MONTH = "MONTH"
    WEEK = "WEEK"


@strawberry.enum(description="Money in or money out.")
class Direction(str, Enum):
    IN = "IN"
    OUT = "OUT"


@strawberry.enum(description="Why a category was suggested.")
class SuggestionReason(str, Enum):
    NEIGHBOURS = "NEIGHBOURS"  # similar transactions the organization categorized
    TERMS = "TERMS"  # a term of the category (its name or a description phrase)
    BOTH = "BOTH"


@strawberry.enum(description="How a transaction got its merchant: a merchant rule (RULE) or an alias (AUTO) — both re-matched when rules or aliases change — or a user (MANUAL, never re-matched).")
class MerchantSource(str, Enum):
    NONE = "NONE"
    AUTO = "AUTO"
    RULE = "RULE"
    MANUAL = "MANUAL"


@strawberry.enum(description="Where a merchant location came from: a store number on a bank line (DISCOVERED, no address yet), a user (MANUAL) or the geocoder (GEOCODED).")
class LocationSource(str, Enum):
    DISCOVERED = "DISCOVERED"
    MANUAL = "MANUAL"
    GEOCODED = "GEOCODED"


@strawberry.enum(description="What a view compares its window with.")
class Comparison(str, Enum):
    NONE = "NONE"
    PREVIOUS_PERIOD = "PREVIOUS_PERIOD"  # the same length, right before
    SAME_PERIOD_LAST_YEAR = "SAME_PERIOD_LAST_YEAR"


@strawberry.enum(description="Which total a change is about.")
class StatMetric(str, Enum):
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    NET = "NET"


@strawberry.enum(description="Where security prices come from: SCALABLE (the organization's Scalable login), TWELVEDATA (API key), YAHOO (unofficial, no key).")
class PriceSource(str, Enum):
    SCALABLE = "SCALABLE"
    TWELVEDATA = "TWELVEDATA"
    YAHOO = "YAHOO"

