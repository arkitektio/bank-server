"""Sharing a workflow: what a root task created in bank, put in a collaboration as it is now.

A workflow is a root task (koherent's ``Task.root_task_id``). Its tasks call bank with a task token
rekuest signed (``tests.conftest.task_token``, ``rtk`` naming the workflow), and koherent records the
task on the CREATE row of what each request made. A category it created goes in. A provider, a
merchant and a budget it created never do, nor would a connection or a statement import: their
structures name no object for any workflow. Accounts and transactions are synced from a bank or
imported and record no task: they are reported unknown, and nothing of them is put in.
"""

import datetime
import uuid

import pytest
from arkitekt_service.collaborations.models import Collaboration
from kante.context import HttpContext
from strawberry.types import ExecutionResult

from bank_server.schema import schema
from finance import models
from tests.conftest import request_context, task_token

pytestmark = pytest.mark.django_db(transaction=True)

ORG = "static_issuer#static_org"

SHARE = """
mutation ($input: ShareWorkflowInput!) {
  shareWorkflow(input: $input) {
    dryRun
    added { identifier object }
    covered
    skipped { identifier object }
    unknown
  }
}
"""


async def _as(ctx: HttpContext, query: str, **variables: object) -> dict:
    result: ExecutionResult = await schema.execute(
        query, context_value=ctx, variable_values=variables or None
    )
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


@pytest.fixture
async def workflow(authenticated_context: HttpContext) -> dict[str, str]:
    """A workflow of bank's admin (two tasks under one root) that set up a provider, a category,
    a budget on it and a merchant; and a category made by hand."""
    root = f"root-{uuid.uuid4().hex}"
    first = task_token("3", ORG, "3", tsk=f"task-{uuid.uuid4().hex}", rtk=root)
    second = task_token("3", ORG, "3", tsk=f"task-{uuid.uuid4().hex}", rtk=root)
    by_hand = await _as(
        request_context("admin"),
        'mutation { createCategory(input: {name: "By hand"}) { id } }',
    )
    provider = await _as(
        request_context("admin", task=first),
        'mutation { createScalableProvider(input: {name: "Workflow broker"}) { id } }',
    )
    category = await _as(
        request_context("admin", task=first),
        'mutation { createCategory(input: {name: "Workflow category"}) { id } }',
    )
    budget = await _as(
        request_context("admin", task=second),
        'mutation ($c: ID!, $m: Date!) { createBudget(input: {category: $c, amount: "100", startMonth: $m}) { id } }',
        c=category["createCategory"]["id"],
        m=datetime.date(2026, 9, 1).isoformat(),
    )
    merchant = await _as(
        request_context("admin", task=second),
        'mutation { createMerchant(input: {name: "Workflow shop"}) { id } }',
    )
    return {
        "root": root,
        "by_hand": by_hand["createCategory"]["id"],
        "provider": provider["createScalableProvider"]["id"],
        "category": category["createCategory"]["id"],
        "budget": budget["createBudget"]["id"],
        "merchant": merchant["createMerchant"]["id"],
    }


async def test_the_workflows_category_goes_in_and_nothing_that_is_never_shared(
    workflow: dict[str, str],
) -> None:
    # Each of them records the workflow that made it: what keeps them out is their structure.
    for model, pk in [
        (models.BankProvider, workflow["provider"]),
        (models.Budget, workflow["budget"]),
        (models.Merchant, workflow["merchant"]),
    ]:
        assert await model.provenance.model.objects.filter(
            id=pk, task__root_task_id=workflow["root"], history_type="+"
        ).aexists(), model

    key = str(uuid.uuid4())
    dry = (
        await _as(
            request_context("test"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"], "dryRun": True},
        )
    )["shareWorkflow"]
    assert dry["added"] == [
        {"identifier": "@bank/category", "object": workflow["category"]}
    ]
    assert dry["skipped"] == [] and dry["covered"] == 0
    assert sorted(dry["unknown"]) == ["@bank/bankaccount", "@bank/transaction"]
    assert not await Collaboration.objects.filter(key=key).aexists()

    shared = (
        await _as(
            request_context("test"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"]},
        )
    )["shareWorkflow"]
    assert shared["added"] == dry["added"]
    listed = await _as(
        request_context("test"),
        "query ($f: CategoryFilter) { categories(filters: $f) { id } }",
        f={"inCollaboration": key},
    )
    assert listed["categories"] == [{"id": workflow["category"]}]


async def test_lists_filter_by_the_workflow_that_made_them(
    workflow: dict[str, str],
) -> None:
    listed = await _as(
        request_context("test"),
        "query ($r: ID!) { categories(filters: {createdUnder: $r}) { id } budgets(filters: {createdUnder: $r}) { id } merchants(filters: {createdUnder: $r}) { id } bankProviders(filters: {createdUnder: $r}) { id } }",
        r=workflow["root"],
    )
    assert listed == {
        "categories": [{"id": workflow["category"]}],
        # Never shared: no workflow lists them.
        "budgets": [],
        "merchants": [],
        "bankProviders": [],
    }
