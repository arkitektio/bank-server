"""Finanzguru's category names → base-taxonomy keys, for the first proposal of a mapping.

Only a starting point: a pair not listed here is proposed by similarity (the category terms know
the German words too), and every proposal stays editable (``setImportCategoryMappings``).
Matched on normalized names — case, umlauts and ``&``/``und`` do not matter — first the
subcategory, then the main category.
"""

import re
import unicodedata

# Subcategory (or a main category without subcategories) → base key.
BY_NAME: dict[str, str] = {
    # Wohnen
    "miete": "housing.rent",
    "wohnen": "housing",
    "nebenkosten": "housing.utilities",
    "strom": "housing.utilities",
    "gas": "housing.utilities",
    "heizung": "housing.utilities",
    "wasser": "housing.utilities",
    "energie": "housing.utilities",
    "internet": "housing.internet_phone",
    "handy": "housing.internet_phone",
    "mobilfunk": "housing.internet_phone",
    "telefon": "housing.internet_phone",
    "internet telefon": "housing.internet_phone",
    "rundfunkbeitrag": "subscriptions.media",
    "gez": "subscriptions.media",
    "moebel": "housing.maintenance",
    "einrichtung": "housing.maintenance",
    "haushalt": "housing.maintenance",
    "baumarkt": "housing.maintenance",
    "garten": "housing.maintenance",
    # Lebenshaltung
    "lebensmittel": "food.groceries",
    "supermarkt": "food.groceries",
    "lebenshaltung": "food",
    "restaurant": "food.eating_out",
    "restaurants": "food.eating_out",
    "restaurants cafes": "food.eating_out",
    "essen gehen": "food.eating_out",
    "cafe": "food.eating_out",
    "bar": "food.eating_out",
    "lieferdienst": "food.delivery",
    "essenslieferung": "food.delivery",
    "drogerie": "health.pharmacy",
    "apotheke": "health.pharmacy",
    "koerperpflege": "family.personal_care",
    "friseur": "family.personal_care",
    "haustier": "family.pets",
    "haustiere": "family.pets",
    # Mobilität
    "mobilitaet": "transport",
    "tanken": "transport.fuel",
    "tankstelle": "transport.fuel",
    "laden": "transport.fuel",
    "oepnv": "transport.public",
    "oeffentliche verkehrsmittel": "transport.public",
    "bahn": "transport.public",
    "nahverkehr": "transport.public",
    "auto": "transport.car",
    "kfz": "transport.car",
    "parken": "transport.car",
    "werkstatt": "transport.car",
    "kfz steuer": "fees.taxes",
    "taxi": "transport.taxi",
    "carsharing": "transport.taxi",
    "fahrrad": "transport",
    # Gesundheit
    "gesundheit": "health",
    "arzt": "health.doctor",
    "aerzte": "health.doctor",
    "zahnarzt": "health.doctor",
    "krankenhaus": "health.doctor",
    "sport": "health.fitness",
    "fitness": "health.fitness",
    "fitnessstudio": "health.fitness",
    # Versicherungen
    "versicherungen": "insurance",
    "versicherung": "insurance",
    "krankenversicherung": "insurance.health",
    "zahnzusatzversicherung": "insurance.health",
    "haftpflichtversicherung": "insurance.home",
    "hausratversicherung": "insurance.home",
    "rechtsschutzversicherung": "insurance.home",
    "kfz versicherung": "insurance.vehicle",
    "autoversicherung": "insurance.vehicle",
    "lebensversicherung": "insurance.life",
    "berufsunfaehigkeitsversicherung": "insurance.life",
    "rentenversicherung": "insurance.life",
    "altersvorsorge": "insurance.life",
    # Shopping
    "shopping": "shopping",
    "einkaeufe": "shopping",
    "kleidung": "shopping.clothing",
    "bekleidung": "shopping.clothing",
    "schuhe": "shopping.clothing",
    "elektronik": "shopping.electronics",
    "online shopping": "shopping.online",
    "onlineshopping": "shopping.online",
    "versandhandel": "shopping.online",
    # Freizeit
    "freizeit": "leisure",
    "unterhaltung": "leisure.entertainment",
    "kino": "leisure.entertainment",
    "veranstaltungen": "leisure.entertainment",
    "hobby": "leisure.hobbies",
    "hobbys": "leisure.hobbies",
    "buecher": "leisure.hobbies",
    "spiele": "leisure.hobbies",
    "urlaub": "leisure.travel",
    "reisen": "leisure.travel",
    "hotel": "leisure.travel",
    "fluege": "leisure.travel",
    # Verträge / Abos
    "vertraege": "subscriptions",
    "abonnements": "subscriptions",
    "abos": "subscriptions",
    "streaming": "subscriptions.streaming",
    "musik": "subscriptions.streaming",
    "video": "subscriptions.streaming",
    "software": "subscriptions.software",
    "zeitung": "subscriptions.media",
    "zeitschriften": "subscriptions.media",
    # Bildung, Familie
    "bildung": "education",
    "weiterbildung": "education.courses",
    "studium": "education.courses",
    "schule": "education.school",
    "kinder": "family.kids",
    "kinderbetreuung": "family.kids",
    "kita": "family.kids",
    "geschenke": "family.gifts",
    "spenden": "family.gifts",
    # Finanzen
    "bankgebuehren": "fees.bank",
    "gebuehren": "fees.bank",
    "kontofuehrung": "fees.bank",
    "steuern": "fees.taxes",
    "steuern abgaben": "fees.taxes",
    "kredit": "fees.loans",
    "kredite": "fees.loans",
    "zinsen": "fees.loans",
    "bargeld": "cash.atm",
    "geldautomat": "cash.atm",
    "bargeldabhebung": "cash.atm",
    # Einnahmen
    "einnahmen": "income",
    "gehalt": "income.salary",
    "lohn": "income.salary",
    "gehalt lohn": "income.salary",
    "kapitalertraege": "income.investment",
    "dividenden": "income.investment",
    "erstattungen": "income.refunds",
    "rueckerstattung": "income.refunds",
    "kindergeld": "income.benefits",
    "rente": "income.benefits",
    "sozialleistungen": "income.benefits",
    "sonstige einnahmen": "income.other",
    # Umbuchungen, Sparen
    "umbuchung": "transfers.own",
    "umbuchungen": "transfers.own",
    "sparen": "transfers.savings",
    "sparen anlegen": "transfers.investing",
    "geldanlage": "transfers.investing",
    "depot": "transfers.investing",
    "wertpapiere": "transfers.investing",
}

_NON_WORD = re.compile(r"[^a-z0-9]+")
_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def normalize(name: str) -> str:
    """Lower-cased, umlauts folded, ``&``/``und``/punctuation dropped: ``Restaurants & Cafés`` → ``restaurants cafes``."""
    folded = name.casefold().translate(_FOLD)
    folded = "".join(c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c))
    words = [w for w in _NON_WORD.split(folded) if w and w != "und"]
    return " ".join(words)


def base_key(main: str, sub: str) -> str | None:
    """The base-taxonomy key a Finanzguru (main, sub) pair most likely means, if the table knows it."""
    for name in (sub, main):
        if name and (key := BY_NAME.get(normalize(name))):
            return key
    return None
