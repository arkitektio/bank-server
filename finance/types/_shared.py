from strawberry.scalars import JSON

from finance.scoping import scope_queryset


def build_prescoped_queryset(info, queryset):
    """Limit ``queryset`` to the request's organization.

    Every type backing a Query field must route its ``get_queryset`` through here:
    strawberry_django runs it for single ``x: T = field()`` fetches as well as lists, and
    nothing else in the stack scopes them. Follows ``finance.scoping.organization_path``, so a
    model whose organization sits behind a required FK (a transaction, a balance snapshot) is
    scoped the same way as one that carries the column itself.
    """
    return scope_queryset(queryset, info)


class OrgScoped:
    """Mixin that scopes a type's reads to the request's organization (as mikro's does).

    strawberry_django runs ``get_queryset`` for top-level list fields, single ``x: T = field()``
    fetches and nested relations alike, so listing this as a base class tenant-scopes every
    read of the type. Resolved via MRO.
    """

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset)


DESCRIPTORS_DESCRIPTION = (
    "This object's descriptors, a flat mapping of key to value: the facts about it that an action's port can `require` and a trigger can test "
    "(e.g. `@bank/kind`). The keys are the ones bank declares for this structure, and the values are the ones a signal about the object carries. "
    "Empty for a structure that declares none"
)


def resolve_descriptors(root) -> JSON:  # noqa: ANN001 - the model instance behind any hosted type
    """The descriptors of a hosted object, from its structure's declaration (``bank_server.service``)."""
    from bank_server.service import service  # the declaration imports finance.models

    return service.describe(root)
