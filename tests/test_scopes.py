"""What an app may do here is what its token's scopes say: every root field asks for one of bank's
own (the ``@auth`` directive publishes it, a generated client gathers it), and `bank_all` stands
for all of them."""

import pytest
from kante.context import HttpContext

from arkitekt_service.graphql import scope_gaps
from bank_server.schema import schema
from tests.conftest import _context

pytestmark = pytest.mark.django_db(transaction=True)

MAKE_CATEGORY = 'mutation { createCategory(input: {name: "Scoped"}) { id } }'
CATEGORIES = "query { categories { id } }"


@pytest.fixture
def reader_context(transactional_db) -> HttpContext:
    """The ``test`` user of tenant A, through an app granted only ``bank_read``."""
    return _context("readertest", "1", "static_org")


def test_every_root_field_asks_for_a_scope_bank_declares():
    assert scope_gaps(schema) == []


async def test_an_app_granted_reads_reads_and_writes_nothing(aexecute, reader_context):
    await aexecute(CATEGORIES, context=reader_context)
    refused = await aexecute(MAKE_CATEGORY, context=reader_context, allow_errors=True)

    assert refused.errors[0].extensions["reason"] == "INSUFFICIENT_SCOPE"


async def test_the_all_scope_writes(aexecute):
    made = await aexecute(MAKE_CATEGORY)

    assert made.data["createCategory"]["id"]
