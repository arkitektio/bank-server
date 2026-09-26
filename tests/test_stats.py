"""Stats, budget status, recurring detection and forecast, on one seeded history."""

import datetime
from decimal import Decimal

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = datetime.date.today()


def _history() -> list[dict]:
    rows = []
    for month in (6, 7, 8, 9):
        rows += [
            tx("-900.00", f"2026-{month:02d}-01", "Hausverwaltung", remittance="Miete"),
            tx("3000.00", f"2026-{month:02d}-02", "ACME GmbH", remittance="Gehalt"),
            tx("-120.00", f"2026-{month:02d}-10", "BILLA"),
            tx(f"-{10 + month}.99", f"2026-{month:02d}-15", "Streamflix"),
        ]
    rows.append(tx("-500.00", "2026-09-20", "Me", iban="AT00SAVINGS"))  # to own savings: a transfer
    return rows


@pytest.fixture
async def seeded(link, aexecute, fakebank):
    fakebank.scenario([account(iban="AT00GIRO", transactions=_history(), balance="4000.00"), account(iban="AT00SAVINGS", name="Savings")])
    connection = await link()
    giro = next(a["id"] for a in connection["accounts"] if a["iban"] == "AT00GIRO")
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": giro})
    groceries = str((await models.Category.objects.aget(name="Groceries")).id)
    await aexecute('mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "billa"}) { id } }', {"c": groceries})
    return {"giro": giro, "groceries": groceries}


async def test_cashflow_per_month_excludes_transfers(seeded, aexecute):
    result = await aexecute('query { cashflow(dateFrom: "2026-06-01", dateTo: "2026-09-30") { periodStart currency income expense net count } }')
    months = result.data["cashflow"]
    assert [m["periodStart"] for m in months] == ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]
    september = months[-1]
    assert Decimal(september["income"]) == Decimal("3000.00")
    assert Decimal(september["expense"]) == Decimal("900.00") + Decimal("120.00") + Decimal("19.99")  # not the 500 transfer
    assert september["count"] == 4

    with_transfers = await aexecute('query { cashflow(dateFrom: "2026-09-01", dateTo: "2026-09-30", includeTransfers: true) { expense } }')
    assert Decimal(with_transfers.data["cashflow"][0]["expense"]) == Decimal("1539.99")


async def test_spending_by_category(seeded, aexecute):
    result = await aexecute('query { spendingByCategory(dateFrom: "2026-06-01", dateTo: "2026-09-30") { category { name } expense income count } }')
    by_name = {(r["category"] or {}).get("name"): r for r in result.data["spendingByCategory"]}
    assert Decimal(by_name["Groceries"]["expense"]) == Decimal("480.00")
    assert by_name["Groceries"]["count"] == 4
    assert Decimal(by_name[None]["income"]) == Decimal("12000.00")


async def test_top_counterparties(seeded, aexecute):
    result = await aexecute('query { topCounterparties(dateFrom: "2026-06-01", limit: 2) { counterparty total count } }')
    assert [r["counterparty"] for r in result.data["topCounterparties"]] == ["Hausverwaltung", "BILLA"]
    assert Decimal(result.data["topCounterparties"][0]["total"]) == Decimal("3600.00")


async def test_balance_history_walks_back_from_the_reported_balance(seeded, aexecute):
    result = await aexecute('query($a: ID!) { balanceHistory(account: $a, dateFrom: "2026-09-19") { date amount reported } }', {"a": seeded["giro"]})
    points = {p["date"]: p for p in result.data["balanceHistory"]}
    assert points[TODAY.isoformat()] == {"date": TODAY.isoformat(), "amount": "4000.00", "reported": True}
    # The 500 transfer on the 20th: the day before it, the balance was 500 higher.
    assert Decimal(points["2026-09-19"]["amount"]) == Decimal("4500.00")
    assert Decimal(points["2026-09-20"]["amount"]) == Decimal("4000.00")


async def test_budget_status_rolls_up_children(seeded, aexecute):
    child = await aexecute('mutation($p: ID!) { createCategory(input: {name: "Snacks", parent: $p}) { id } }', {"p": seeded["groceries"]})
    snack = await models.Transaction.objects.filter(counterparty="BILLA", booking_date=datetime.date(2026, 9, 10)).afirst()
    await aexecute('mutation($id: ID!, $c: ID!) { categorizeTransaction(input: {id: $id, category: $c}) { id } }', {"id": str(snack.id), "c": child.data["createCategory"]["id"]})
    await aexecute('mutation($c: ID!) { createBudget(input: {category: $c, amount: "100", startMonth: "2026-06-15"}) { startMonth } }', {"c": seeded["groceries"]})

    result = await aexecute('query { budgetStatus(month: "2026-09-01") { budgeted spent remaining ratio budget { category { name } } } }')
    [status] = result.data["budgetStatus"]
    assert status["budget"]["category"]["name"] == "Groceries"
    assert Decimal(status["spent"]) == Decimal("120.00")  # counted through the child category
    assert Decimal(status["remaining"]) == Decimal("-20.00")
    assert status["ratio"] == pytest.approx(1.2)


async def test_recurring_detection_and_forecast(seeded, aexecute):
    result = await aexecute("mutation { detectRecurring { id label amount intervalDays nextExpected status } }")
    by_label = {r["label"]: r for r in result.data["detectRecurring"]}
    assert {"Hausverwaltung", "ACME GmbH", "BILLA", "Streamflix"} <= set(by_label)
    rent = by_label["Hausverwaltung"]
    assert rent["intervalDays"] == 30
    assert Decimal(rent["amount"]) == Decimal("-900.00")
    assert rent["status"] == "DETECTED"

    # Nothing confirmed: the forecast is flat.
    flat = await aexecute('query($a: ID!) { forecast(account: $a, horizonDays: 5) { amount } }', {"a": seeded["giro"]})
    assert {p["amount"] for p in flat.data["forecast"]} == {"4000.00"}

    await aexecute('mutation($id: ID!) { setRecurringStatus(input: {id: $id, status: CONFIRMED}) { status } }', {"id": rent["id"]})
    await aexecute('mutation($id: ID!) { setRecurringStatus(input: {id: $id, status: IGNORED}) { status } }', {"id": by_label["Streamflix"]["id"]})

    horizon = 120
    points = (await aexecute('query($a: ID!, $h: Int!) { forecast(account: $a, horizonDays: $h) { date amount } }', {"a": seeded["giro"], "h": horizon})).data["forecast"]
    assert len(points) == horizon + 1
    rent_days = [p["date"] for prev, p in zip(points, points[1:]) if Decimal(p["amount"]) - Decimal(prev["amount"]) == Decimal("-900.00")]
    assert rent_days, points
    assert all((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days == 30 for a, b in zip(rent_days, rent_days[1:]))

    # Ignored stays ignored on re-detection.
    again = await aexecute("mutation { detectRecurring { label } }")
    assert "Streamflix" not in {r["label"] for r in again.data["detectRecurring"]}
    assert (await models.RecurringPayment.objects.aget(label="Streamflix")).status == models.RecurringStatus.IGNORED
