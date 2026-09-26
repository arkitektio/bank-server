"""Schema-surface checks: no money as Int/Float, enums in step with the models, no org switch."""

import re

import pytest

from bank_server.schema import schema
from finance import enums, models
from finance.types import imports as import_types

SDL = str(schema)


def test_money_is_never_int_or_float():
    money = re.compile(r"^\s+(amount\w*|total|income|expense|net|budgeted|spent|remaining)\s*:\s*(Int|Float)", re.MULTILINE | re.IGNORECASE)
    assert not money.findall(SDL)


def test_active_organization_is_not_exposed():
    assert "activeOrganization" not in SDL


@pytest.mark.parametrize(
    "gql,choices",
    [
        (enums.ConnectionStatus, models.ConnectionStatus),
        (enums.Provider, models.Provider),
        (enums.AccountKind, models.AccountKind),
        (enums.LinkStep, models.LinkStep),
        (enums.MerchantSource, models.MerchantSource),
        (enums.LocationSource, models.LocationSource),
        (enums.PriceSource, models.PriceSource),
        (enums.BankErrorCode, models.BankErrorCode),
        (enums.TransactionKind, models.TransactionKind),
        (enums.TransactionStatus, models.TransactionStatus),
        (enums.CategorySource, models.CategorySource),
        (enums.TransactionOrigin, models.TransactionOrigin),
        (import_types.ImportSource, models.ImportSource),
        (import_types.ImportStatus, models.ImportStatus),
        (enums.CategoryKind, models.CategoryKind),
        (enums.RuleField, models.RuleField),
        (enums.RuleMatch, models.RuleMatch),
        (enums.RuleDirection, models.RuleDirection),
        (enums.RecurringStatus, models.RecurringStatus),
    ],
)
def test_enums_match_model_choices(gql, choices):
    assert {m.name: m.value for m in gql} == {m.name: m.value for m in choices}
