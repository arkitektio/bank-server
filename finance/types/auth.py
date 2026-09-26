"""Narrow ``User`` / ``Organization`` types (as mikro's).

authentikate's own strawberry types expose ``activeOrganization`` — a user's organization
outside the one the request acts in — so they are not used here.
"""

import kante
import strawberry
from authentikate import models as amodels


@kante.django_type(amodels.Organization, description="An organization (tenant). Every bank object belongs to exactly one, and queries only see the current one's data.")
class Organization:
    id: strawberry.ID
    slug: str


@kante.django_type(amodels.User, description="A user account; sub is the stable subject identifier from the identity provider.")
class User:
    id: strawberry.ID
    sub: str
    preferred_username: str
