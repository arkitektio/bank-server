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
