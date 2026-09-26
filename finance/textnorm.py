"""The text a transaction or a category term is embedded as.

The embedding model is static: a vector is the average of its tokens. Bank lines are mostly
noise around one or two meaningful words — "BILLA DANKT 0421 WIEN" and "BILLA FILIALE 1180"
embed 0.78 apart raw, but both become "billa" (distance 0) once store numbers, dates, card
fragments and boilerplate are gone. The same normalization runs on category terms, so a term
"Billa" and a line from Billa meet exactly.

Pure functions, no Django: migrations import them too.
"""

import re

# Words that say how money moved, not where: they would pull every card payment together.
BOILERPLATE = frozenset(
    {
        # German banking
        "dankt", "danke", "sagt", "filiale", "kartenzahlung", "karte", "bankomat-zahlung", "lastschrift", "einzug",
        "zahlung", "überweisung", "ueberweisung", "dauerauftrag", "auftrag", "gutschrift", "belastung", "ref", "nr",
        "zahlungsreferenz", "verwendungszweck", "empfänger", "auftraggeber", "vom", "von", "an", "für", "fuer", "und",
        # legal forms and web noise
        "gmbh", "ag", "kg", "og", "mbh", "ges", "co", "eu", "e.u", "inc", "ltd", "llc", "www", "com", "at", "de",
        # English card/SEPA noise
        "sepa", "pos", "purchase", "payment", "card", "debit", "credit", "transfer", "the", "and", "of",
        # cities that end up on every card line
        "wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "villach", "wels", "berlin", "münchen",
    }
)

_TOKEN = re.compile(r"[a-zäöüß][a-zäöüß&'.+*-]*")


def normalize(text: str | None) -> str | None:
    """Lower-cased meaningful words; ``None`` when nothing meaningful is left."""
    if not text:
        return None
    words = []
    for token in _TOKEN.findall(text.lower()):
        token = token.strip(".-'*+")
        if len(token) < 2 or any(char.isdigit() for char in token) or token in BOILERPLATE:
            continue
        words.append(token)
    return " ".join(words) or None


def context_words(text: str | None) -> str | None:
    """Lower-cased words without numbers — but *with* city names and legal forms.

    For what a user wrote about a merchant or place (its name, description, city): unlike a bank
    line, there a city is signal ("Wien" tells two stores apart), not card-terminal noise.
    """
    if not text:
        return None
    words = [token.strip(".-'*+") for token in _TOKEN.findall(text.lower())]
    return " ".join(w for w in words if len(w) >= 2 and not any(char.isdigit() for char in w)) or None


def transaction_text(counterparty: str | None, remittance: str | None, kind: str | None, merchant_context: str | None = None) -> str | None:
    """What a transaction is embedded as: its bank line (normalized), then what its merchant and place say."""
    kind_words = kind.replace("_", " ") if kind else None
    line = normalize(" ".join(part for part in (counterparty, remittance, kind_words) if part))
    return " ".join(part for part in (line, merchant_context) if part) or None


def category_terms(name: str, description: str | None, limit: int = 40) -> list[str]:
    """The phrases a category is recognized by: its name and each phrase of its description.

    Descriptions are written as "What it is — term, term, term"; each comma-, semicolon- or
    line-separated phrase (and the part before the dash) becomes one term, so one merchant name
    is not averaged away among twenty others.
    """
    phrases = [name]
    for part in re.split(r"[—\n;,]", description or ""):
        phrases.append(part)
    terms, seen = [], set()
    for phrase in phrases:
        phrase = phrase.strip()
        key = normalize(phrase) or phrase.casefold()
        if not phrase or key in seen:
            continue
        seen.add(key)
        terms.append(phrase[:300])
        if len(terms) >= limit:
            break
    return terms


def term_text(term: str) -> str | None:
    """What a category term is embedded as (normalized like a bank line; the raw term if that empties it)."""
    return normalize(term) or (term.strip().lower() or None)
