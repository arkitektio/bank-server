"""The base categories every organization starts with — a two-level tree it may reshape freely.

Each node has a stable ``key`` (``food.groceries``). Seeding is idempotent *by key*: it only ever
creates keys an organization does not have yet, so renames, colors, re-parenting and hiding
survive every upgrade, and a base category the organization deleted is remembered
(:class:`~finance.models.DismissedBaseCategory`) and not brought back.

Names are English. Descriptions are bilingual and full of the words that actually appear on
bank lines here (merchant names, German terms): they are embedded (``finance.semantic``), so a
never-seen "HOFER DANKT" lands near Groceries before anyone categorized a single Hofer receipt.
A child's kind is always its root's (see :func:`finance.models.Category.root_kind`).
"""

from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from finance import models

EXPENSE, INCOME, TRANSFER = models.CategoryKind.EXPENSE, models.CategoryKind.INCOME, models.CategoryKind.TRANSFER


@dataclass(frozen=True)
class Node:
    key: str
    name: str
    description: str
    kind: str = EXPENSE
    color: str | None = None
    children: tuple["Node", ...] = field(default_factory=tuple)


def _leaf(key: str, name: str, description: str) -> Node:
    return Node(key=key, name=name, description=description)


BASE: tuple[Node, ...] = (
    Node("housing", "Housing", "Home and living costs — Wohnen, Haushalt, Wohnung", EXPENSE, "#8b5cf6", (
        _leaf("housing.rent", "Rent", "Rent and housing costs, landlord, property management — Miete, Mietzahlung, Betriebskosten, Hausverwaltung, Genossenschaft"),
        _leaf("housing.utilities", "Utilities", "Electricity, gas, heating, water — Strom, Gas, Heizung, Fernwärme, Wasser, Wien Energie, Verbund, EVN, Energie AG"),
        _leaf("housing.internet_phone", "Internet & Phone", "Internet, mobile and landline contracts — Internet, Handy, Mobilfunk, Festnetz, A1, Magenta, Drei, HoT, spusu, yesss"),
        _leaf("housing.maintenance", "Home & Furniture", "Furniture, repairs, DIY and hardware stores — Möbel, Reparatur, Handwerker, Baumarkt, IKEA, OBI, Hornbach, Bauhaus, XXXLutz, Leiner"),
    )),
    Node("food", "Food", "Food and drink — Essen, Trinken, Lebensmittel", EXPENSE, "#16a34a", (
        _leaf("food.groceries", "Groceries", "Supermarkets and grocery stores — Supermarkt, Lebensmittel, Einkauf, Billa, Spar, Eurospar, Interspar, Hofer, Lidl, Penny, MPreis, Merkur"),
        _leaf("food.eating_out", "Eating Out", "Restaurants, cafés, bars, bakeries — Restaurant, Gasthaus, Wirtshaus, Café, Kaffeehaus, Bar, Lokal, Bäckerei, Mittagessen, McDonald's, Burger King"),
        _leaf("food.delivery", "Food Delivery", "Food delivered to the door — Lieferservice, Essenslieferung, Lieferando, foodora, Wolt, Mjam"),
    )),
    Node("transport", "Transport", "Getting around — Mobilität, Verkehr", EXPENSE, "#0ea5e9", (
        _leaf("transport.public", "Public Transport", "Trains, buses, trams, subway, tickets — Öffis, Fahrschein, Jahreskarte, Klimaticket, Wiener Linien, ÖBB, Westbahn, Postbus"),
        _leaf("transport.fuel", "Fuel & Charging", "Petrol stations and EV charging — Tankstelle, Tanken, Benzin, Diesel, Laden, Ladestation, OMV, BP, Shell, Eni, Jet, Turmöl"),
        _leaf("transport.car", "Car", "Car running costs, parking, service, tolls — Auto, Parken, Parkgarage, Kurzparkschein, Werkstatt, Service, Pickerl, Vignette, ÖAMTC, ARBÖ, ASFINAG"),
        _leaf("transport.taxi", "Taxi & Rides", "Taxi, ride hailing, car and scooter sharing — Taxi, Uber, Bolt, Free Now, Share Now, Lime, Tier, Citybike"),
    )),
    Node("health", "Health", "Health and body — Gesundheit", EXPENSE, "#ef4444", (
        _leaf("health.pharmacy", "Pharmacy & Drugstore", "Pharmacy, drugstore, medicine, toiletries — Apotheke, Drogerie, Medikamente, dm, Bipa, Müller"),
        _leaf("health.doctor", "Doctor & Care", "Doctors, dentists, therapy, hospital — Arzt, Ärztin, Zahnarzt, Ordination, Wahlarzt, Therapie, Physiotherapie, Spital, Krankenhaus"),
        _leaf("health.fitness", "Fitness & Sports", "Gym and sports — Fitnessstudio, Fitness, Sport, Sportverein, McFit, FitInn, John Harris, Kletterhalle"),
    )),
    Node("insurance", "Insurance", "Insurance premiums — Versicherung, Prämie, Uniqa, Generali, Allianz, Wiener Städtische, Donau, Merkur Versicherung", EXPENSE, "#64748b", (
        _leaf("insurance.health", "Health Insurance", "Health and supplementary insurance — Krankenversicherung, Zusatzversicherung, Sonderklasse, ÖGK, SVS, BVAEB"),
        _leaf("insurance.home", "Home & Liability", "Household, liability and legal insurance — Haushaltsversicherung, Eigenheim, Haftpflicht, Rechtsschutz"),
        _leaf("insurance.vehicle", "Vehicle Insurance", "Car and vehicle insurance — KFZ-Versicherung, Kasko, Haftpflicht Auto"),
        _leaf("insurance.life", "Life & Pension", "Life insurance and private pensions — Lebensversicherung, Pensionsvorsorge, Zukunftsvorsorge, Ablebensversicherung"),
    )),
    Node("shopping", "Shopping", "Things bought — Einkaufen", EXPENSE, "#f59e0b", (
        _leaf("shopping.clothing", "Clothing & Shoes", "Clothes, shoes, fashion — Kleidung, Schuhe, Mode, H&M, Zara, C&A, Peek & Cloppenburg, Zalando, Deichmann, Humanic"),
        _leaf("shopping.electronics", "Electronics", "Electronics and devices — Elektronik, Handy, Computer, MediaMarkt, Saturn, Apple, Cyberport, e-tec"),
        _leaf("shopping.online", "Online Shopping", "Online orders and marketplaces — Online-Bestellung, Versandhandel, Amazon, eBay, willhaben, Temu, AliExpress"),
    )),
    Node("leisure", "Leisure & Travel", "Free time and holidays — Freizeit, Urlaub", EXPENSE, "#ec4899", (
        _leaf("leisure.entertainment", "Entertainment", "Cinema, concerts, theatre, events — Kino, Konzert, Theater, Museum, Veranstaltung, Tickets, oeticket, Eventim"),
        _leaf("leisure.hobbies", "Hobbies & Books", "Books, games, music, hobbies — Bücher, Spiele, Musik, Hobby, Thalia, Morawa, Steam, PlayStation, Nintendo"),
        _leaf("leisure.travel", "Travel & Holidays", "Trips, hotels, flights — Urlaub, Reise, Hotel, Unterkunft, Flug, Booking.com, Airbnb, Expedia, Austrian Airlines, Ryanair, Wizz Air"),
    )),
    Node("subscriptions", "Subscriptions", "Recurring subscriptions — Abo, Abonnement, Mitgliedschaft", EXPENSE, "#a855f7", (
        _leaf("subscriptions.streaming", "Streaming", "Video and music streaming — Streaming, Netflix, Spotify, Disney+, Amazon Prime, YouTube Premium, Apple Music, DAZN"),
        _leaf("subscriptions.software", "Software & Cloud", "Software, apps and cloud services — Software, App, Cloud, Google, Microsoft, Adobe, GitHub, iCloud, Dropbox, OpenAI, Anthropic"),
        _leaf("subscriptions.media", "News & Media", "Newspapers, magazines, broadcasting fee — Zeitung, Magazin, Der Standard, Die Presse, Kurier, Falter, ORF-Beitrag, GIS"),
    )),
    Node("education", "Education", "Learning — Bildung, Ausbildung", EXPENSE, "#14b8a6", (
        _leaf("education.courses", "Courses & Tuition", "Courses, tuition, university — Kurs, Studiengebühr, Studienbeitrag, Universität, Weiterbildung, Sprachkurs, WIFI, BFI"),
        _leaf("education.school", "School", "School costs and tutoring — Schule, Schulbedarf, Schulveranstaltung, Nachhilfe"),
    )),
    Node("family", "Personal & Family", "Personal life and family — Familie, Persönliches", EXPENSE, "#f97316", (
        _leaf("family.kids", "Kids & Childcare", "Children and childcare — Kinder, Kindergarten, Hort, Tagesmutter, Babysitter, Spielzeug, Smyths"),
        _leaf("family.gifts", "Gifts & Donations", "Presents and charity — Geschenk, Spende, Spenden, Caritas, Rotes Kreuz, Volkshilfe, Ärzte ohne Grenzen"),
        _leaf("family.personal_care", "Personal Care", "Hairdresser, cosmetics, wellness — Friseur, Kosmetik, Wellness, Massage, Nagelstudio"),
        _leaf("family.pets", "Pets", "Pets and vets — Haustier, Tierarzt, Tierfutter, Fressnapf, Das Futterhaus"),
    )),
    Node("fees", "Finance & Fees", "Money costs — Finanzen, Gebühren", EXPENSE, "#78716c", (
        _leaf("fees.bank", "Bank Fees", "Account and card fees — Kontoführung, Kontoführungsgebühr, Spesen, Entgelt, Kartengebühr, Bankgebühr, fee"),
        _leaf("fees.taxes", "Taxes", "Taxes paid — Steuer, Finanzamt, Einkommensteuer, Kapitalertragsteuer, KESt, Grundsteuer, tax"),
        _leaf("fees.loans", "Loans & Interest", "Loan repayments and debit interest — Kredit, Darlehen, Kreditrate, Sollzinsen, Überziehungszinsen, Leasing"),
    )),
    Node("cash", "Cash", "Cash — Bargeld", EXPENSE, "#94a3b8", (
        _leaf("cash.atm", "ATM Withdrawal", "Cash withdrawals — Bargeld, Bargeldbehebung, Behebung, Bankomat, Geldautomat, ATM"),
    )),
    Node("income", "Income", "Money coming in — Einnahmen, Einkommen", INCOME, "#22c55e", (
        _leaf("income.salary", "Salary", "Wages and salary — Gehalt, Lohn, Bezug, Bezüge, Gehaltszahlung, Arbeitgeber, Payroll, salary"),
        _leaf("income.investment", "Investment Income", "Dividends, distributions, interest received — Dividende, Ausschüttung, Zinsen, Zinsgutschrift, Habenzinsen, Kapitalertrag, distribution, interest"),
        _leaf("income.refunds", "Refunds", "Money returned — Rückerstattung, Gutschrift, Retoure, Rückzahlung, Erstattung, Steuerausgleich, refund"),
        _leaf("income.benefits", "Benefits & Pension", "Public benefits and pensions — Familienbeihilfe, Kinderbetreuungsgeld, AMS, Arbeitslosengeld, Pension, Förderung, Klimabonus"),
        _leaf("income.other", "Other Income", "Other money received — Verkauf, willhaben Verkauf, Nebenverdienst, Honorar, Überweisung erhalten"),
    )),
    Node("transfers", "Transfers & Investing", "Money moved between own accounts and into savings or investments; left out of spending", TRANSFER, "#6b7280", (
        _leaf("transfers.own", "Own Accounts", "Transfers between own accounts — Übertrag, Umbuchung, eigenes Konto, Kontoübertrag, internal transfer"),
        _leaf("transfers.savings", "Savings", "Money put aside — Sparen, Sparkonto, Tagesgeld, Sparbuch, Bausparen, savings"),
        _leaf("transfers.investing", "Investing", "Deposits into a broker and security trades — Depot, Wertpapiere, ETF, Aktien, Sparplan, Broker, Scalable Capital, Trade Republic, flatex, deposit, buy, sell"),
    )),
)

#: The flat defaults bank seeded before the taxonomy, and the base key each one becomes.
LEGACY_KEYS: dict[str, str] = {
    "Groceries": "food.groceries",
    "Rent & Housing": "housing",
    "Utilities": "housing.utilities",
    "Transport": "transport",
    "Eating Out": "food.eating_out",
    "Shopping": "shopping",
    "Health": "health",
    "Insurance": "insurance",
    "Subscriptions": "subscriptions",
    "Leisure": "leisure",
    "Cash": "cash",
    "Salary": "income.salary",
    "Other Income": "income.other",
    "Transfer": "transfers",
}


def walk(nodes: tuple[Node, ...] = BASE, parent: Node | None = None):  # noqa: ANN201
    """Every node with its parent, parents first."""
    for node in nodes:
        yield node, parent
        yield from walk(node.children, node)


NODES: dict[str, Node] = {node.key: node for node, _ in walk()}
ROOT_OF: dict[str, str] = {}
for _node, _parent in walk():
    ROOT_OF[_node.key] = ROOT_OF[_parent.key] if _parent else _node.key


def seed_base_categories(organization_id: int, keys: set[str] | None = None) -> list[models.Category]:
    """Create the base categories the organization does not have yet; returns the created ones.

    Idempotent by key and never touches an existing row. A key the organization dismissed (by
    deleting its category) is skipped. A key whose parent was deleted or moved still lands
    under whatever category now holds the parent key, or at the top level when there is none.
    An existing category with the same name at the same place is adopted (it gets the key)
    instead of duplicated. ``keys`` limits seeding to some keys (and their parents).
    """
    created: list[models.Category] = []
    with transaction.atomic():
        existing = {c.key: c for c in models.Category.objects.filter(organization_id=organization_id, key__isnull=False)}
        dismissed = set(models.DismissedBaseCategory.objects.filter(organization_id=organization_id).values_list("key", flat=True))
        wanted = None
        if keys is not None:
            wanted = set(keys) | {parent.key for node, parent in walk() if node.key in keys and parent}
        for node, parent in walk():
            if node.key in existing or node.key in dismissed or (wanted is not None and node.key not in wanted):
                continue
            parent_category = existing.get(parent.key) if parent else None
            if parent and parent_category is None:
                continue  # the parent was dismissed: its children are not forced back at the top level
            kind = parent_category.root_kind() if parent_category else node.kind
            same_place = models.Category.objects.filter(organization_id=organization_id, parent=parent_category, name=node.name, key__isnull=True).first()
            if same_place is not None:
                same_place.key = node.key
                if not same_place.description:
                    same_place.description = node.description
                same_place.save(update_fields=["key", "description"])
                existing[node.key] = same_place
                continue
            category = models.Category(
                organization_id=organization_id,
                key=node.key,
                name=node.name,
                description=node.description,
                kind=kind,
                color=node.color,
                parent=parent_category,
            )
            try:
                with transaction.atomic():
                    category.save()
            except IntegrityError:
                continue  # a concurrent seed created it first
            existing[node.key] = category
            created.append(category)
    return created
