"""Categorization rules: the first active rule (by priority) that matches sets the category.

A rule never touches a transaction a user categorized by hand (``category_source=MANUAL``), and
overrides a semantic guess (``SEMANTIC``): an explicit rule is a stronger signal than
similarity. A row a rule categorized earlier that no rule matches any more goes back to
uncategorized (and semantic assignment may pick it up again).
"""

import re
from functools import lru_cache

from django.core.exceptions import ValidationError
from django.db.models import QuerySet

from finance import models

def seed_default_categories(organization_id: int) -> int:
    """Create the base categories the organization does not have yet; returns how many (see :mod:`finance.taxonomy`)."""
    from finance.taxonomy import seed_base_categories

    return len(seed_base_categories(organization_id))


@lru_cache(maxsize=1024)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def validate_rule(match: str, pattern: str) -> None:
    """Refuse an empty pattern or a regular expression that does not compile."""
    if not pattern.strip():
        raise ValidationError("A rule needs a non-empty pattern.")
    if match == models.RuleMatch.REGEX:
        try:
            _compiled(pattern)
        except re.error as error:
            raise ValidationError(f"Invalid regular expression: {error}") from error


def _field_value(rule: models.CategoryRule, tx: models.Transaction) -> str:
    if rule.field == models.RuleField.COUNTERPARTY:
        return tx.counterparty or ""
    if rule.field == models.RuleField.REMITTANCE:
        return tx.remittance or ""
    return (tx.counterparty_iban or "").replace(" ", "")


def matches(rule: models.CategoryRule, tx: models.Transaction) -> bool:
    """Whether ``rule`` applies to ``tx``."""
    if rule.direction == models.RuleDirection.OUT and tx.amount >= 0:
        return False
    if rule.direction == models.RuleDirection.IN and tx.amount <= 0:
        return False
    magnitude = abs(tx.amount)
    if rule.amount_min is not None and magnitude < rule.amount_min:
        return False
    if rule.amount_max is not None and magnitude > rule.amount_max:
        return False
    value = _field_value(rule, tx)
    pattern = rule.pattern.replace(" ", "") if rule.field == models.RuleField.IBAN else rule.pattern
    if rule.match == models.RuleMatch.EQUALS:
        return value.casefold() == pattern.casefold()
    if rule.match == models.RuleMatch.CONTAINS:
        return pattern.casefold() in value.casefold()
    return _compiled(pattern).search(value) is not None


def apply_rules(organization_id: int, transactions: QuerySet) -> int:
    """Categorize ``transactions`` by the organization's rules; returns how many rows changed."""
    rules = list(models.CategoryRule.objects.filter(organization_id=organization_id, active=True).order_by("priority", "id"))
    changed = []
    # A user's choice and an imported statement's category are never a rule's to change.
    for tx in transactions.exclude(category_source__in=[models.CategorySource.MANUAL, models.CategorySource.IMPORT]).defer("embedding"):
        rule = next((rule for rule in rules if matches(rule, tx)), None)
        if rule is not None:
            category_id, source = rule.category_id, models.CategorySource.RULE
        elif tx.category_source == models.CategorySource.RULE:
            category_id, source = None, models.CategorySource.NONE
        else:
            continue
        if tx.category_id != category_id or tx.category_source != source:
            tx.category_id, tx.category_source = category_id, source
            changed.append(tx)
    models.Transaction.objects.bulk_update(changed, ["category", "category_source"], batch_size=500)
    return len(changed)
