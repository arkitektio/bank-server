"""Importing a Finanzguru export: parsing, preview, apply, and merging with synced history.

Files go through the real upload path (a scoped STS grant into the stack's RustFS); bank syncs go
through fakebank. The export is built here with openpyxl from Finanzguru's documented columns.
"""

import asyncio
import io
import threading
import uuid
from decimal import Decimal

import pytest
from channels.db import database_sync_to_async
from openpyxl import Workbook

from finance import models
from finance.imports.finanzguru import COLUMNS, ExportError, parse
from finance.sync import sync_account

from .conftest import account, tx

# --- Building exports ---------------------------------------------------------------------------


def iban() -> str:
    return f"DE{uuid.uuid4().int % 10**20:020d}"


def fg(
    booking_id: str,
    reference: str,
    day: str,
    amount: str,
    party: str | None = None,
    *,
    main: str = "",
    sub: str = "",
    party_iban: str | None = None,
    transfer: bool = False,
    note: str | None = None,
    original: str | None = None,
    balance: str | None = None,
    purpose: str | None = None,
    name: str = "Girokonto",
    currency: str = "EUR",
) -> dict:
    """One export row (``day`` as dd.mm.yyyy, ``amount`` signed)."""
    return {
        "Buchungstag": day,
        "Referenzkonto": reference,
        "Name Referenzkonto": name,
        "Betrag": float(amount),
        "Kontostand": float(balance) if balance is not None else None,
        "Waehrung": currency,
        "Beguenstigter/Auftraggeber": party,
        "IBAN Beguenstigter/Auftraggeber": party_iban,
        "Verwendungszweck": purpose,
        "Analyse-Hauptkategorie": main,
        "Analyse-Unterkategorie": sub,
        "Analyse-Umbuchung": "ja" if transfer else "nein",
        "Analyse-Umsatzart": "Lastschrift",
        "Buchungs-ID": booking_id,
        "Referenz-Original-ID": original,
        "Split-Typ": "Split" if original else None,
        "Notiz": note,
    }


def xlsx(rows: list[dict]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append([row.get(column) for column in COLUMNS])
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def csv_export(rows: list[dict]) -> bytes:
    def cell(value) -> str:  # noqa: ANN001
        if isinstance(value, float):
            return f"{value:.2f}".replace(".", ",")
        return "" if value is None else str(value)

    lines = [";".join(COLUMNS)] + [";".join(cell(row.get(c)) for c in COLUMNS) for row in rows]
    return "\n".join(lines).encode("utf-8-sig")


# --- Parsing (no database) ----------------------------------------------------------------------


def test_parse_reads_xlsx_and_german_csv_alike():
    account_iban = iban()
    rows = [fg("1", account_iban, "03.09.2026", "-1234.56", "Hofer", main="Lebenshaltung", sub="Lebensmittel", balance="100.00")]

    for content in (xlsx(rows), csv_export(rows)):
        parsed = parse(content)
        (row,) = parsed.rows
        assert (row.amount, row.currency, row.booking_date.isoformat()) == (Decimal("-1234.56"), "EUR", "2026-09-03")
        assert (row.main, row.sub, row.transfer, row.balance) == ("Lebenshaltung", "Lebensmittel", False, Decimal("100.00"))
        assert row.extra["kind"] == "Lastschrift"


def test_parse_keeps_only_ibans_as_counterparty_iban():
    parsed = parse(xlsx([fg("1", iban(), "01.09.2026", "-5", "PayPal", party_iban="someone@example.com"), fg("2", iban(), "01.09.2026", "-5", "Bank", party_iban="DE89 3704 0044 0532 0130 00")]))

    assert parsed.rows[0].counterparty_iban is None and parsed.rows[0].extra["counterparty_account"] == "someone@example.com"
    assert parsed.rows[1].counterparty_iban == "DE89370400440532013000"


def test_parse_folds_split_parts_into_their_booking():
    ref = iban()
    parsed = parse(
        xlsx(
            [
                fg("100", ref, "02.09.2026", "-50", "Supermarkt"),
                fg("101", ref, "02.09.2026", "-30", "Supermarkt", main="Lebenshaltung", sub="Lebensmittel", original="100"),
                fg("102", ref, "02.09.2026", "-20", "Supermarkt", main="Lebenshaltung", sub="Drogerie", original="100"),
                # Only the parts of booking 200 are in the file.
                fg("201", ref, "04.09.2026", "-7", "Kiosk", main="Freizeit", sub="Hobby", original="200"),
                fg("202", ref, "04.09.2026", "-3", "Kiosk", main="Freizeit", sub="Hobby", original="200"),
            ]
        )
    )

    by_id = {row.booking_id: row for row in parsed.rows}
    assert set(by_id) == {"100", "200"} and parsed.splits_folded == 4
    assert by_id["100"].amount == Decimal("-50.00") and (by_id["100"].main, by_id["100"].sub) == ("Lebenshaltung", "Lebensmittel")  # the largest part
    assert [p["booking_id"] for p in by_id["100"].extra["splits"]] == ["101", "102"]
    assert by_id["200"].amount == Decimal("-10.00") and by_id["200"].sub == "Hobby"


def test_parse_refuses_a_file_without_the_header():
    with pytest.raises(ExportError):
        parse(b"Datum;Betrag\n01.01.2026;5\n")


def test_parse_skips_repeated_booking_ids_with_a_warning():
    ref = iban()
    parsed = parse(xlsx([fg("1", ref, "01.09.2026", "-5", "A"), fg("1", ref, "01.09.2026", "-5", "A")]))

    assert len(parsed.rows) == 1 and "appears twice" in parsed.warnings[0]


# --- Through the API ----------------------------------------------------------------------------

CREATE = """
mutation($file: ID!) { createFinanzguruImport(input: {file: $file}) {
  id status error rows warnings transactionsCount
  accounts { reference name currency iban how account { id } candidates { id } rows alreadyImported matched new }
  unmappedCategories { main sub rows }
} }
"""
APPLY = """
mutation($id: ID!, $accounts: [ImportAccountChoice!]) { applyStatementImport(input: {id: $id, accounts: $accounts}) {
  id status transactionsCount accounts { reference how account { id } alreadyImported matched new }
} }
"""
MAP = "mutation($input: [ImportCategoryMappingInput!]!) { setImportCategoryMappings(input: $input) { main sub proposed category { id } } }"
MAPPINGS = "query { importCategoryMappings { main sub proposed category { key } } }"
SYNC = "mutation Sync($id: ID!) { syncAccount(id: $id) { created updated } }"


@pytest.fixture
def import_file(aexecute, upload):
    """Upload an export and preview it; returns the preview."""

    async def _import(rows: list[dict], context=None) -> dict:  # noqa: ANN001
        store = await upload(xlsx(rows), name="finanzguru.xlsx", context=context)
        return (await aexecute(CREATE, {"file": store}, context=context)).data["createFinanzguruImport"]

    return _import


async def _rows(account_id: int) -> list[models.Transaction]:
    return [t async for t in models.Transaction.objects.filter(account_id=account_id).select_related("category").order_by("booking_date", "id")]


async def test_preview_writes_nothing_and_proposes_mappings(import_file, aexecute):
    dead, card = iban(), "4111 **** **** 1111"
    preview = await import_file(
        [
            fg("1", dead, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel"),
            fg("2", dead, "02.09.2026", "-800", "Hausverwaltung", main="Wohnen", sub="Miete"),
            fg("3", card, "03.09.2026", "-9.99", "Something", main="Sonstiges", sub="Kuriositäten", name="Kreditkarte"),
        ]
    )

    assert preview["status"] == "PREVIEWED" and preview["rows"] == 3 and preview["transactionsCount"] == 0
    by_ref = {a["reference"]: a for a in preview["accounts"]}
    assert by_ref[dead] | {"account": None} == {**by_ref[dead], "how": "NEW", "iban": dead, "new": 2, "account": None}
    assert by_ref[card]["iban"] is None and by_ref[card]["name"] == "Kreditkarte"
    assert await models.Transaction.objects.acount() == 0
    mappings = {(m["main"], m["sub"]): m for m in (await aexecute(MAPPINGS)).data["importCategoryMappings"]}
    assert mappings[("Lebenshaltung", "Lebensmittel")]["category"] == {"key": "food.groceries"}
    assert mappings[("Wohnen", "Miete")]["category"] == {"key": "housing.rent"}
    assert all(m["proposed"] for m in mappings.values())


async def test_apply_creates_import_only_accounts_and_is_idempotent(import_file, aexecute):
    dead = iban()
    rows = [
        fg("1", dead, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel", note="Wocheneinkauf", balance="987.70"),
        fg("2", dead, "03.09.2026", "2500", "ACME GmbH", main="Einnahmen", sub="Gehalt", balance="3487.70"),
    ]
    preview = await import_file(rows)

    applied = (await aexecute(APPLY, {"id": preview["id"]})).data["applyStatementImport"]

    assert applied["status"] == "APPLIED" and applied["transactionsCount"] == 2
    account_id = int(applied["accounts"][0]["account"]["id"])
    created = await models.BankAccount.objects.aget(id=account_id)
    assert (created.iban_normalized, created.name, await created.syncers.acount()) == (dead, "Girokonto", 0)
    stored = await _rows(account_id)
    assert [(t.origin, t.category_source, t.category.key, t.import_ref) for t in stored] == [
        ("IMPORT", "IMPORT", "food.groceries", "fg:1"),
        ("IMPORT", "IMPORT", "income.salary", "fg:2"),
    ]
    assert stored[0].note == "Wocheneinkauf"
    assert {(b.date.isoformat(), b.balance_type, b.amount) async for b in created.balances.all()} == {("2026-09-01", "IMPT", Decimal("987.70")), ("2026-09-03", "IMPT", Decimal("3487.70"))}

    # A user annotates, then the same file is imported again: nothing is duplicated, the user's choices stay.
    groceries = stored[0]
    await aexecute("mutation($id: ID!) { setTransactionNote(input: {id: $id, note: \"mine\"}) { id } }", {"id": groceries.id})
    again = await import_file(rows)
    assert again["accounts"][0]["how"] == "IBAN" and again["accounts"][0]["alreadyImported"] == 2
    await aexecute(APPLY, {"id": again["id"]})

    assert len(await _rows(account_id)) == 2
    assert (await models.Transaction.objects.aget(id=groceries.id)).note == "mine"


async def test_import_into_a_synced_account_enriches_the_synced_rows(link, aexecute, fakebank, import_file):
    """The same bookings as the bank has — other text, a day off — become annotations, not duplicates."""
    giro = iban()
    fakebank.scenario([account(iban=giro, transactions=[tx("-12.30", "2026-09-01", "HOFER DANKT 0815"), tx("-60.00", "2026-09-02", "SHELL 123")])])
    acc_id = int((await link())["accounts"][0]["id"])
    await aexecute(SYNC, {"id": acc_id})

    preview = await import_file(
        [
            fg("1", giro, "02.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel", note="Einkauf"),
            fg("2", giro, "02.09.2026", "-60", "Shell", main="Mobilität", sub="Tanken"),
            fg("3", giro, "15.08.2026", "-5", "Before the bank's history", main="Lebenshaltung", sub="Lebensmittel"),
        ]
    )
    assert preview["accounts"][0] | {"account": None} == {**preview["accounts"][0], "how": "IBAN", "matched": 2, "new": 1, "account": None}
    assert preview["accounts"][0]["account"] == {"id": str(acc_id)}

    await aexecute(APPLY, {"id": preview["id"]})

    rows = await _rows(acc_id)
    assert len(rows) == 3
    by_ref = {t.import_ref: t for t in rows}
    hofer, shell, older = by_ref["fg:1"], by_ref["fg:2"], by_ref["fg:3"]
    assert (hofer.origin, hofer.counterparty, hofer.category.key, hofer.category_source, hofer.note) == ("SYNC", "HOFER DANKT 0815", "food.groceries", "IMPORT", "Einkauf")
    assert (shell.origin, shell.category.key) == ("SYNC", "transport.fuel")
    assert older.origin == "IMPORT"


async def test_a_later_sync_takes_over_imported_rows(link, aexecute, fakebank, import_file):
    """Import a dead account's history, then link the bank: the account is adopted and synced rows replace imported ones."""
    giro = iban()
    preview = await import_file(
        [
            fg("1", giro, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel", note="Einkauf"),
            fg("2", giro, "20.08.2026", "-7.00", "Old", main="Freizeit", sub="Hobby"),
        ]
    )
    applied = (await aexecute(APPLY, {"id": preview["id"]})).data["applyStatementImport"]
    imported_account = int(applied["accounts"][0]["account"]["id"])
    fakebank.scenario([account(iban=giro, transactions=[tx("-12.30", "2026-09-02", "HOFER DANKT 0815"), tx("-3.00", "2026-09-03", "Bakery")])])

    connection = await link()
    assert [a["id"] for a in connection["accounts"]] == [str(imported_account)]
    synced = (await aexecute(SYNC, {"id": imported_account})).data["syncAccount"]

    assert synced == {"created": 1, "updated": 1}
    rows = {t.import_ref or t.counterparty: t for t in await _rows(imported_account)}
    assert len(rows) == 3
    hofer = rows["fg:1"]
    assert (hofer.origin, hofer.syncer_id is not None, hofer.counterparty, hofer.booking_date.isoformat()) == ("SYNC", True, "HOFER DANKT 0815", "2026-09-02")
    assert (hofer.category.key, hofer.category_source, hofer.note, hofer.import_raw["main"]) == ("food.groceries", "IMPORT", "Einkauf", "Lebenshaltung")
    assert rows["fg:2"].origin == "IMPORT"  # older than the bank's history: stays imported
    assert rows["Bakery"].origin == "SYNC"


async def test_splits_are_one_booking(import_file, aexecute):
    ref = iban()
    preview = await import_file(
        [
            fg("100", ref, "02.09.2026", "-50", "Supermarkt"),
            fg("101", ref, "02.09.2026", "-30", "Supermarkt", main="Lebenshaltung", sub="Lebensmittel", original="100"),
            fg("102", ref, "02.09.2026", "-20", "Supermarkt", main="Lebenshaltung", sub="Drogerie", original="100"),
        ]
    )
    applied = (await aexecute(APPLY, {"id": preview["id"]})).data["applyStatementImport"]

    (row,) = await _rows(int(applied["accounts"][0]["account"]["id"]))
    assert row.amount == Decimal("-50.00") and len(row.import_raw["splits"]) == 2


async def test_umbuchung_is_kept_and_rules_leave_imported_categories_alone(import_file, aexecute):
    ref, savings = iban(), iban()
    await aexecute('mutation { seedDefaultCategories { id } }')
    other = await models.Category.objects.aget(key="leisure.hobbies")
    await aexecute(
        'mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, match: CONTAINS, pattern: "hofer"}) { id } }', {"c": other.id}
    )
    preview = await import_file(
        [
            fg("1", ref, "01.09.2026", "-100", "Me", party_iban=savings, transfer=True, main="Umbuchung", sub="Umbuchung"),
            fg("2", ref, "02.09.2026", "-12", "Hofer", main="Lebenshaltung", sub="Lebensmittel"),
        ]
    )
    applied = (await aexecute(APPLY, {"id": preview["id"]})).data["applyStatementImport"]
    account_id = int(applied["accounts"][0]["account"]["id"])

    await aexecute("mutation { reapplyRules }")  # rules and transfer detection run again over everything
    transfer, hofer = await _rows(account_id)
    assert (transfer.is_transfer, transfer.is_transfer_manual, transfer.import_raw["transfer_pinned"]) == (True, True, True)
    assert (hofer.category.key, hofer.category_source) == ("food.groceries", "IMPORT")


async def test_ambiguous_iban_needs_a_choice(import_file, aexecute, authenticated_context):
    shared = iban()
    org = authenticated_context.request.organization
    pockets = [await models.BankAccount.objects.acreate(organization=org, iban=shared, currency="EUR", name=n) for n in ("A", "B")]
    preview = await import_file([fg("1", shared, "01.09.2026", "-5", "X")])

    assert preview["accounts"][0]["how"] == "AMBIGUOUS"
    assert sorted(c["id"] for c in preview["accounts"][0]["candidates"]) == sorted(str(p.id) for p in pockets)
    refused = await aexecute(APPLY, {"id": preview["id"]}, allow_errors=True)
    assert "choose" in refused.errors[0].message

    chosen = (await aexecute(APPLY, {"id": preview["id"], "accounts": [{"reference": shared, "account": pockets[1].id}]})).data["applyStatementImport"]
    assert chosen["accounts"][0]["how"] == "CHOSEN"
    assert await models.Transaction.objects.filter(account=pockets[1]).acount() == 1


async def test_editing_a_mapping_recategorizes_on_reapply(import_file, aexecute):
    ref = iban()
    preview = await import_file([fg("1", ref, "01.09.2026", "-5", "Kiosk", main="Sonstiges", sub="Kuriositäten")])
    assert preview["unmappedCategories"] == [{"main": "Sonstiges", "sub": "Kuriositäten", "rows": 1}]
    await aexecute(APPLY, {"id": preview["id"]})
    hobbies = await models.Category.objects.aget(key="leisure.hobbies")

    mapped = (await aexecute(MAP, {"input": [{"main": "Sonstiges", "sub": "Kuriositäten", "category": hobbies.id}]})).data["setImportCategoryMappings"]
    assert mapped == [{"main": "Sonstiges", "sub": "Kuriositäten", "proposed": False, "category": {"id": str(hobbies.id)}}]
    await aexecute(APPLY, {"id": preview["id"]})

    row = await models.Transaction.objects.select_related("category").aget(import_ref="fg:1")
    assert (row.category.key, row.category_source) == ("leisure.hobbies", "IMPORT")


async def test_other_organizations_cannot_import_my_upload(upload, aexecute, other_org_context):
    store = await upload(xlsx([fg("1", iban(), "01.09.2026", "-5", "X")]))

    result = await aexecute(CREATE, {"file": store}, context=other_org_context, allow_errors=True)

    assert result.errors and result.errors[0].extensions["code"] == "NOT_FOUND"


def _in_thread(fn):  # noqa: ANN001, ANN202
    outcome: dict = {}

    def run() -> None:
        from django.db import connection

        try:
            outcome["value"] = fn()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


async def test_an_import_landing_during_a_sync_is_taken_over_not_duplicated(link, aexecute, fakebank, import_file):
    """The sync is parked mid-fetch (no lock held); the import writes; the sync's write then pairs with the imported rows."""
    from finance.imports import apply as importing

    giro = iban()
    fakebank.scenario([account(iban=giro, transactions=[tx("-12.30", "2026-09-02", "HOFER"), tx("-4.00", "2026-09-03", "Bakery")])])
    acc_id = int((await link())["accounts"][0]["id"])
    preview = await import_file([fg("1", giro, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel", note="Einkauf")])
    content = xlsx([fg("1", giro, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel", note="Einkauf")])

    fakebank.hold()
    syncing, sync_outcome = _in_thread(lambda: asyncio.run(sync_account(acc_id)))
    deadline = asyncio.get_running_loop().time() + 10
    while fakebank.held() < 1:
        assert asyncio.get_running_loop().time() < deadline, "the sync never reached the bank"
        await asyncio.sleep(0.02)
    await database_sync_to_async(importing.apply)(int(preview["id"]), content)
    fakebank.release()
    syncing.join()

    assert "error" not in sync_outcome, sync_outcome
    rows = await _rows(acc_id)
    assert len(rows) == 2
    hofer = next(t for t in rows if t.import_ref == "fg:1")
    assert (hofer.origin, hofer.note, hofer.category_source) == ("SYNC", "Einkauf", "IMPORT")


async def test_the_command_previews_and_applies_a_local_file(tmp_path, authenticated_context):
    from django.core.management import call_command

    ref = iban()
    path = tmp_path / "export.csv"
    path.write_bytes(csv_export([fg("1", ref, "01.09.2026", "-5", "Kiosk", main="Lebenshaltung", sub="Lebensmittel")]))
    org = authenticated_context.request.organization

    await database_sync_to_async(call_command)("import_finanzguru", str(path), "--org", org.slug)
    assert await models.Transaction.objects.acount() == 0
    await database_sync_to_async(call_command)("import_finanzguru", str(path), "--org", str(org.id), "--apply")
    assert await models.Transaction.objects.filter(import_ref="fg:1", account__organization=org).acount() == 1
