"""Who was that, and what kind of spending is it?

Resolution runs most-specific first, and every match records *how* it was
reached. "Swiggy Instamart, matched on the narration slug" and "Swiggy, matched
on the VPA handle" are different levels of confidence, and a normaliser that
hides which one it used cannot be debugged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from enum import Enum

from .narration import Channel, Narration, parse_narration
from .segmentation import Vocabulary, segment

UNCATEGORISED = "uncategorised"


class Kind(str, Enum):
    """What sort of counterparty this is.

    Measuring merchant coverage over a ledger that is mostly person-to-person
    transfers gives a terrible number for a good reason, and a single "resolved"
    percentage hides that. Classifying the counterparty first makes the metric
    honest: a transfer to a friend is not an unresolved merchant.
    """

    MERCHANT = "merchant"
    PERSON = "person"
    CARD = "card bill"
    CASH = "cash"  # an ATM, i.e. yourself
    BANK = "bank"  # the bank charging or paying you
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Merchant:
    name: str
    category: str = UNCATEGORISED
    kind: Kind = Kind.MERCHANT


@dataclass(frozen=True)
class MerchantMatch:
    name: str
    category: str
    channel: Channel
    #: lexicon | vpa | prefix | segmented | raw -- how the name was arrived at.
    source: str
    narration: Narration
    kind: Kind = Kind.UNKNOWN

    @property
    def confident(self) -> bool:
        """The name is one we recognise, not one we merely made readable."""
        return self.source in {"lexicon", "vpa", "prefix"}

    @property
    def classified(self) -> bool:
        return self.kind is not Kind.UNKNOWN


def _m(name: str, category: str, kind: Kind = Kind.MERCHANT) -> Merchant:
    return Merchant(name, category, kind)


#: Keyed by slug (lowercase alphanumerics only). Longer, more specific keys are
#: matched before shorter ones, so "swiggyinstamart" beats "swiggy".
LEXICON: dict[str, Merchant] = {
    "swiggyinstamart": _m("Swiggy Instamart", "groceries"),
    "swiggy": _m("Swiggy", "food"),
    "zomato": _m("Zomato", "food"),
    "zomatoltd": _m("Zomato", "food"),
    "blinkit": _m("Blinkit", "groceries"),
    "zepto": _m("Zepto", "groceries"),
    "bigbasket": _m("BigBasket", "groceries"),
    "licious": _m("Licious", "groceries"),
    "dmart": _m("DMart", "groceries"),
    "amazon": _m("Amazon", "shopping"),
    "amazonpay": _m("Amazon Pay", "shopping"),
    "flipkart": _m("Flipkart", "shopping"),
    "myntra": _m("Myntra", "shopping"),
    "myntradesigns": _m("Myntra", "shopping"),
    "ajio": _m("Ajio", "shopping"),
    "meesho": _m("Meesho", "shopping"),
    "nykaa": _m("Nykaa", "shopping"),
    "lenskart": _m("Lenskart", "shopping"),
    "decathlon": _m("Decathlon", "shopping"),
    "croma": _m("Croma", "shopping"),
    "netflix": _m("Netflix", "entertainment"),
    "netflixindia": _m("Netflix", "entertainment"),
    "spotify": _m("Spotify", "entertainment"),
    "hotstar": _m("Disney+ Hotstar", "entertainment"),
    "bookmyshow": _m("BookMyShow", "entertainment"),
    "pvr": _m("PVR", "entertainment"),
    "youtube": _m("YouTube", "entertainment"),
    "uber": _m("Uber", "transport"),
    "ola": _m("Ola", "transport"),
    "rapido": _m("Rapido", "transport"),
    "irctc": _m("IRCTC", "travel"),
    "makemytrip": _m("MakeMyTrip", "travel"),
    "goibibo": _m("Goibibo", "travel"),
    "oyo": _m("OYO", "travel"),
    "indianoil": _m("Indian Oil", "fuel"),
    "jio": _m("Jio", "utilities"),
    "airtel": _m("Airtel", "utilities"),
    "vodafone": _m("Vi", "utilities"),
    "bescom": _m("BESCOM", "utilities"),
    "pharmeasy": _m("PharmEasy", "health"),
    "netmeds": _m("Netmeds", "health"),
    "apollo": _m("Apollo", "health"),
    "medplus": _m("MedPlus", "health"),
    "practo": _m("Practo", "health"),
    "cult": _m("Cult.fit", "health"),
    "curefit": _m("Cult.fit", "health"),
    "zerodha": _m("Zerodha", "investments"),
    "groww": _m("Groww", "investments"),
    "upstox": _m("Upstox", "investments"),
    "smallcase": _m("Smallcase", "investments"),
    # CRED is a credit-card bill platform, so these are card repayments, not
    # fees. Getting this wrong put the single largest outflow in the wrong bucket.
    "cred": _m("CRED", "card bill", Kind.CARD),
    "credclub": _m("CRED", "card bill", Kind.CARD),
    "razorpay": _m("Razorpay", "uncategorised"),
    "billdesk": _m("BillDesk", "utilities"),
}

#: When the lexicon has nothing, the words themselves often say enough.
_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("salary", ("salary", "payroll", "stipend", "wages")),
    ("rent", ("rent", "housing", "apartments", "society", "maintenance", "landlord")),
    ("utilities", ("electricity", "bescom", "water", "broadband", "recharge", "postpaid")),
    ("investments", ("mutual", "sip", "nps", "ppf", "elss", "folio")),
    ("health", ("hospital", "clinic", "pharma", "medical", "diagnostics", "labs")),
    ("education", ("school", "college", "tuition", "academy", "institute")),
    ("fuel", ("petrol", "fuel", "petroleum", "hpcl", "bpcl")),
)

#: Some channels categorise themselves.
_CHANNEL_CATEGORIES = {
    Channel.ATM: "cash",
    Channel.CHARGES: "fees",
    Channel.INTEREST: "interest",
    Channel.CHEQUE: "transfer",
}

_NON_SLUG = re.compile(r"[^a-z0-9]")


def slug(text: str) -> str:
    return _NON_SLUG.sub("", text.lower())


def identify(raw: str, vocabulary: Vocabulary | None = None) -> MerchantMatch:
    narration = parse_narration(raw)
    words = segment(narration.counterparty, vocabulary)

    for key, source in _candidates(narration, words):
        merchant = LEXICON.get(key)
        if merchant is not None:
            return _match(
                merchant.name, merchant.category, narration, source, merchant.kind
            )

    name = " ".join(word.capitalize() for word in words) if words else narration.raw
    source = "segmented" if words else "raw"
    return _match(name, UNCATEGORISED, narration, source, _classify(narration))


def _candidates(narration: Narration, words: list[str]) -> list[tuple[str, str]]:
    """Lookup keys, most specific first, paired with the source they represent."""
    keys: list[tuple[str, str]] = []
    counterparty = slug(narration.counterparty)
    if counterparty:
        keys.append((counterparty, "lexicon"))
    if narration.handle:
        keys.append((slug(narration.handle), "vpa"))
    if words:
        keys.append((slug(words[0]), "prefix"))
    return keys


def _match(
    name: str, category: str, narration: Narration, source: str, kind: Kind
) -> MerchantMatch:
    if category == UNCATEGORISED:
        category = _infer_category(narration)
    if category == UNCATEGORISED and kind in _KIND_CATEGORIES:
        category = _KIND_CATEGORIES[kind]
    return MerchantMatch(
        # Never an empty name: an empty string is a substring of every question,
        # so it would match anything asked of the ledger.
        name=name.strip() or narration.raw.strip() or "(no narration)",
        category=category,
        channel=narration.channel,
        source=source,
        narration=narration,
        kind=kind,
    )


#: A masked card number standing in as a counterparty: 435584xxxxxx8918.
_MASKED_CARD = re.compile(r"^\d{4,6}[xX*]{4,}\d{3,4}$")
#: A bare mobile number is a person's UPI handle, not a brand.
_PHONE_HANDLE = re.compile(r"^(\+?91)?[6-9]\d{9}$")
#: If any of these appear, it is an organisation rather than an individual.
_ORGANISATION_WORDS = frozenset(
    {
        "ltd", "limited", "pvt", "private", "inc", "llp", "opc", "co", "corp",
        "corporation", "company", "technologies", "technology", "solutions",
        "services", "enterprises", "industries", "traders", "agencies", "store",
        "stores", "mart", "retail", "foods", "hotel", "hotels", "restaurant",
        "cafe", "bank", "finance", "capital", "securities", "insurance",
        "hospital", "clinic", "pharmacy", "school", "college", "institute",
        "apartments", "society", "association", "builders", "developers",
    }
)

_KIND_CATEGORIES = {
    Kind.CARD: "card bill",
    Kind.PERSON: "transfer",
    Kind.CASH: "cash",
}


def _classify(narration: Narration) -> Kind:
    """What kind of counterparty is this, when the lexicon had nothing?"""
    counterparty = narration.counterparty.strip()

    if _MASKED_CARD.match(counterparty.replace(" ", "")):
        return Kind.CARD
    if narration.channel in {Channel.CHARGES, Channel.INTEREST}:
        return Kind.BANK
    if narration.channel is Channel.ATM:
        # "ATM WDL BANNERGHATTA RD BLR" is five plain words and would otherwise
        # sail straight through the person heuristic below.
        return Kind.CASH
    if narration.handle and _PHONE_HANDLE.match(narration.handle):
        return Kind.PERSON

    words = [word for word in re.split(r"\s+", counterparty.lower()) if word]
    if any(word in _ORGANISATION_WORDS for word in words):
        return Kind.UNKNOWN
    # Two to five plain alphabetic words is what a human name looks like; a
    # brand is normally one token, or carries one of the words above.
    if 2 <= len(words) <= 5 and all(word.isalpha() and len(word) >= 2 for word in words):
        return Kind.PERSON
    return Kind.UNKNOWN


def _infer_category(narration: Narration) -> str:
    haystack = narration.raw.lower()
    for category, keywords in _CATEGORY_HINTS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return _CHANNEL_CATEGORIES.get(narration.channel, UNCATEGORISED)


def build_vocabulary(narrations: list[str]) -> Vocabulary:
    """Seed vocabulary plus whatever these narrations already spell out."""
    vocabulary = Vocabulary()
    vocabulary.learn_all(narrations)
    return vocabulary
