"""Categories and categorization rules."""

from decimal import Decimal
from typing import Optional

import strawberry
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from kante.errors import ValidationError
from kante.types import Info

from finance import enums, merchants, models, rules, semantic, taxonomy, types
from finance.graphql.utils import get_many, get_or_404
from finance.scoping import for_org

__all__ = [
    "CreateCategoryInput",
    "UpdateCategoryInput",
    "CreateCategoryRuleInput",
    "UpdateCategoryRuleInput",
    "create_category",
    "update_category",
    "delete_category",
    "seed_default_categories",
    "sync_base_categories",
    "restore_base_category",
    "CategoryDeletion",
    "create_category_rule",
    "update_category_rule",
    "delete_category_rule",
    "reapply_rules",
]


@strawberry.input(description="A new category. Under a parent it takes the parent's kind (a subtree is one kind).")
class CreateCategoryInput:
    name: str
    parent: Optional[strawberry.ID] = None
    color: Optional[str] = None
    kind: enums.CategoryKind = strawberry.field(default=enums.CategoryKind.EXPENSE, description="For a top-level category; a child always takes its parent's kind.")
    description: str = strawberry.field(default="", description="What belongs here, in words bank lines use (merchants, keywords; commas separate terms). Drives suggestions.")
    hidden: bool = False


@strawberry.input(description="Changes to a category; omitted fields stay as they are.")
class UpdateCategoryInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    parent: Optional[strawberry.ID] = strawberry.field(default=strawberry.UNSET, description="The new parent, or null to make it top-level. Moving takes the new root's kind.")
    color: Optional[str] = strawberry.UNSET
    kind: Optional[enums.CategoryKind] = strawberry.field(default=strawberry.UNSET, description="Only for a top-level category; the whole subtree follows.")
    description: Optional[str] = strawberry.UNSET
    hidden: Optional[bool] = strawberry.UNSET


def _save_category(category: models.Category) -> models.Category:
    try:
        with db_transaction.atomic():
            category.save()
    except IntegrityError:
        raise ValidationError(f"A category named {category.name!r} already exists there.")
    return category


def _descendant_ids(category: models.Category) -> list[int]:
    from finance.budgets import descendants

    return sorted(descendants(category.organization_id)[category.id])


def create_category(info: Info, input: CreateCategoryInput) -> types.Category:
    """Create a category, optionally under a parent (whose kind it takes)."""
    parent = get_or_404(models.Category, info, input.parent) if input.parent else None
    kind = parent.root_kind() if parent else input.kind.value
    category = models.Category(organization=info.context.request.organization, name=input.name.strip(), parent=parent, color=input.color, kind=kind, description=input.description.strip(), hidden=input.hidden)
    return _save_category(category)  # type: ignore[return-value]


def update_category(info: Info, input: UpdateCategoryInput) -> types.Category:
    """Rename, describe, hide, move or recolor a category, or change a top-level category's kind (its subtree follows)."""
    category = get_or_404(models.Category, info, input.id)
    if input.name is not strawberry.UNSET and input.name is not None:
        category.name = input.name.strip()
    if input.color is not strawberry.UNSET:
        category.color = input.color
    if input.description is not strawberry.UNSET:
        category.description = (input.description or "").strip()
    if input.hidden is not strawberry.UNSET and input.hidden is not None:
        category.hidden = input.hidden
    if input.parent is not strawberry.UNSET:
        parent = get_or_404(models.Category, info, input.parent) if input.parent else None
        ancestor = parent
        while ancestor is not None:
            if ancestor.id == category.id:
                raise ValidationError("A category cannot be moved under itself or one of its children.")
            ancestor = ancestor.parent
        category.parent = parent
    if input.kind is not strawberry.UNSET and input.kind is not None:
        if category.parent_id is not None and input.kind.value != category.parent.root_kind():
            raise ValidationError("A child category has its root's kind; change the kind of the top-level category instead.")
        category.kind = input.kind.value
    if category.parent_id is not None:
        category.kind = category.parent.root_kind()
    with db_transaction.atomic():
        _save_category(category)
        # A subtree is one kind: children follow a moved category or a changed root.
        models.Category.objects.filter(id__in=_descendant_ids(category)).exclude(kind=category.kind).update(kind=category.kind)
    return category  # type: ignore[return-value]


@strawberry.type(description="What deleting a category does (or did): counted over the category and its children.")
class CategoryDeletion:
    categories: int = strawberry.field(description="The category and its descendants.")
    transactions: int = strawberry.field(description="Transactions in them: reassigned, or handed back to rules and suggestions.")
    rules: int = strawberry.field(description="Rules assigning them (deleted with them).")
    budgets: int = strawberry.field(description="Budgets on them (deleted with them).")
    dismissed_base_keys: list[str] = strawberry.field(description="Base categories among them; they are not re-added by syncBaseCategories until restored.")


def delete_category(info: Info, id: strawberry.ID, reassign_to: Optional[strawberry.ID] = None, dry_run: bool = False) -> CategoryDeletion:
    """Delete a category with its children, rules and budgets.

    Its transactions move to `reassignTo` (as if categorized by hand) or, without it, go back to
    uncategorized and get the rules and suggestions again. A base category is remembered as
    dismissed. `dryRun` only reports what would happen.
    """
    category = get_or_404(models.Category, info, id)
    ids = _descendant_ids(category)
    target = get_or_404(models.Category, info, reassign_to) if reassign_to else None
    if target is not None and target.id in ids:
        raise ValidationError("Cannot reassign to the category being deleted or one of its children.")
    affected = models.Transaction.objects.filter(category_id__in=ids)
    keys = sorted(models.Category.objects.filter(id__in=ids, key__isnull=False).values_list("key", flat=True))
    report = CategoryDeletion(
        categories=len(ids),
        transactions=affected.count(),
        rules=models.CategoryRule.objects.filter(category_id__in=ids).count(),
        budgets=models.Budget.objects.filter(category_id__in=ids).count(),
        dismissed_base_keys=keys,
    )
    if dry_run:
        return report
    organization_id = info.context.request.organization.id
    with db_transaction.atomic():
        tx_ids = list(affected.values_list("id", flat=True))
        if target is not None:
            affected.update(category=target, category_source=models.CategorySource.MANUAL, updated_at=timezone.now())
        else:
            affected.update(category=None, category_source=models.CategorySource.NONE, updated_at=timezone.now())
        models.DismissedBaseCategory.objects.bulk_create([models.DismissedBaseCategory(organization_id=organization_id, key=key) for key in keys], ignore_conflicts=True)
        category.delete()
        if target is None and tx_ids:
            rules.apply_rules(organization_id, models.Transaction.objects.filter(id__in=tx_ids))
            semantic.auto_assign(organization_id, tx_ids)
    return report


def seed_default_categories(info: Info) -> list[types.Category]:
    """Add the base categories this organization lacks (alias of `syncBaseCategories`, returning all categories)."""
    taxonomy.seed_base_categories(info.context.request.organization.id)
    return list(for_org(models.Category, info).order_by("name"))  # type: ignore[return-value]


def sync_base_categories(info: Info) -> list[types.Category]:
    """Add base categories this organization does not have yet (new ones from an upgrade); returns the created ones.

    Never changes an existing category, and never re-adds one the organization deleted.
    """
    return taxonomy.seed_base_categories(info.context.request.organization.id)  # type: ignore[return-value]


def restore_base_category(info: Info, key: str) -> list[types.Category]:
    """Bring back a deleted base category (and its base children); returns the created ones."""
    if key not in taxonomy.NODES:
        raise ValidationError(f"{key!r} is not a base category key.")
    organization_id = info.context.request.organization.id
    subtree = {k for k in taxonomy.NODES if k == key or k.startswith(f"{key}.")}
    models.DismissedBaseCategory.objects.filter(organization_id=organization_id, key__in=subtree).delete()
    return taxonomy.seed_base_categories(organization_id, keys=subtree)  # type: ignore[return-value]


@strawberry.input(description="A new categorization rule.")
class CreateCategoryRuleInput:
    category: strawberry.ID
    field: enums.RuleField
    pattern: str
    match: enums.RuleMatch = enums.RuleMatch.CONTAINS
    direction: enums.RuleDirection = enums.RuleDirection.ANY
    priority: int = strawberry.field(default=100, description="Lower runs first; the first matching rule wins.")
    amount_min: Optional[Decimal] = None
    amount_max: Optional[Decimal] = None
    active: bool = True
    apply: bool = strawberry.field(default=True, description="Re-categorize existing transactions right away (never manually categorized ones).")


@strawberry.input(description="Changes to a rule; omitted fields stay as they are.")
class UpdateCategoryRuleInput:
    id: strawberry.ID
    category: Optional[strawberry.ID] = strawberry.UNSET
    field: Optional[enums.RuleField] = strawberry.UNSET
    pattern: Optional[str] = strawberry.UNSET
    match: Optional[enums.RuleMatch] = strawberry.UNSET
    direction: Optional[enums.RuleDirection] = strawberry.UNSET
    priority: Optional[int] = strawberry.UNSET
    amount_min: Optional[Decimal] = strawberry.UNSET
    amount_max: Optional[Decimal] = strawberry.UNSET
    active: Optional[bool] = strawberry.UNSET
    apply: bool = True


def _validate(rule: models.CategoryRule) -> None:
    try:
        rules.validate_rule(rule.match, rule.pattern)
    except DjangoValidationError as error:
        raise ValidationError(" ".join(error.messages))
    if rule.amount_min is not None and rule.amount_max is not None and rule.amount_min > rule.amount_max:
        raise ValidationError("amountMin must not exceed amountMax.")


def _apply_all(info: Info) -> int:
    """After a rule changed: the whole pipeline, so rows a rule let go fall back to their merchant's category or a suggestion."""
    from finance.sync import categorize_rows

    organization_id = info.context.request.organization.id
    with db_transaction.atomic():
        return categorize_rows(organization_id, list(for_org(models.Transaction, info).values_list("id", flat=True)))


def create_category_rule(info: Info, input: CreateCategoryRuleInput) -> types.CategoryRule:
    """Create a rule that categorizes matching transactions on every sync."""
    rule = models.CategoryRule(
        organization=info.context.request.organization,
        category=get_or_404(models.Category, info, input.category),
        field=input.field.value,
        pattern=input.pattern,
        match=input.match.value,
        direction=input.direction.value,
        priority=input.priority,
        amount_min=input.amount_min,
        amount_max=input.amount_max,
        active=input.active,
    )
    _validate(rule)
    rule.save()
    if input.apply:
        _apply_all(info)
    return rule  # type: ignore[return-value]


def update_category_rule(info: Info, input: UpdateCategoryRuleInput) -> types.CategoryRule:
    """Change a rule."""
    rule = get_or_404(models.CategoryRule, info, input.id)
    if input.category is not strawberry.UNSET and input.category is not None:
        rule.category = get_or_404(models.Category, info, input.category)
    for name in ("field", "match", "direction"):
        value = getattr(input, name)
        if value is not strawberry.UNSET and value is not None:
            setattr(rule, name, value.value)
    for name in ("pattern", "priority", "active"):
        value = getattr(input, name)
        if value is not strawberry.UNSET and value is not None:
            setattr(rule, name, value)
    for name in ("amount_min", "amount_max"):
        value = getattr(input, name)
        if value is not strawberry.UNSET:
            setattr(rule, name, value)
    _validate(rule)
    rule.save()
    if input.apply:
        _apply_all(info)
    return rule  # type: ignore[return-value]


def delete_category_rule(info: Info, id: strawberry.ID, apply: bool = True) -> strawberry.ID:
    """Delete a rule. With ``apply``, what it alone categorized goes back to uncategorized."""
    get_or_404(models.CategoryRule, info, id).delete()
    if apply:
        _apply_all(info)
    return id


def reapply_rules(info: Info, accounts: Optional[list[strawberry.ID]] = None, semantic_assign: bool = True) -> int:
    """Re-categorize existing transactions (never manually categorized ones); returns how many changed.

    Merchants are re-matched, then rules run, then merchants' default categories. With
    `semanticAssign`, earlier semantic guesses are recomputed too and every transaction still
    uncategorized gets its confident suggestion (source SEMANTIC).
    """
    organization_id = info.context.request.organization.id
    transactions = for_org(models.Transaction, info)
    if accounts:
        transactions = transactions.filter(account__in=get_many(models.BankAccount, info, accounts))
    candidates = transactions.exclude(category_source=models.CategorySource.MANUAL)
    before = dict((pk, (category, source)) for pk, category, source in candidates.values_list("id", "category_id", "category_source"))
    if semantic_assign:
        semantic.reset_semantic(organization_id, transactions)
    ids = list(transactions.values_list("id", flat=True))
    merchants.match(organization_id, ids)
    rules.apply_rules(organization_id, transactions)
    merchants.categorize_by_merchant(organization_id, ids)
    if semantic_assign:
        pending = list(transactions.filter(category_source=models.CategorySource.NONE).values_list("id", flat=True))
        semantic.auto_assign(organization_id, pending)
    after = candidates.values_list("id", "category_id", "category_source")
    return sum(1 for pk, category, source in after if before.get(pk) != (category, source))
