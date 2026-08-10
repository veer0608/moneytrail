"""Who was that, and what kind of spending is it?

Resolution runs most-specific first, and every match records *how* it was
reached. "Swiggy Instamart, matched on the narration slug" and "Swiggy, matched
on the VPA handle" are different levels of confidence, and a normaliser that
hides which one it used cannot be debugged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .narration import Channel, Narration, parse_narration
from .segmentation import Vocabulary, segment

UNCATEGORISED = "uncategorised"


@dataclass(frozen=True)
class Merchant:
    name: str
    category: str = UNCATEGORISED


@dataclass(frozen=True)
class MerchantMatch:
    name: str
    category: str
    channel: Channel
    #: lexicon | vpa | prefix | segmented | raw -- how the name was arrived at.
    source: str
    narration: Narration

    @property
    def confident(self) -> bool:
        return self.source in {"lexicon", "vpa", "prefix"}


def _m(name: str, category: str) -> Merchant:
    return Merchant(name, category)


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
    "cred": _m("CRED", "fees"),
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
            return _match(merchant.name, merchant.category, narration, source)

    name = " ".join(word.capitalize() for word in words) if words else narration.raw
    source = "segmented" if words else "raw"
    return _match(name, UNCATEGORISED, narration, source)


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


def _match(name: str, category: str, narration: Narration, source: str) -> MerchantMatch:
    if category == UNCATEGORISED:
        category = _infer_category(narration)
    return MerchantMatch(
        name=name.strip() or narration.raw,
        category=category,
        channel=narration.channel,
        source=source,
        narration=narration,
    )


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
