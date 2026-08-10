"""Put the spaces back into ``SWIGGYINSTAMART``.

Merchant names arrive welded, in caps, with no separators. CiteRAG solved the
same problem in 10-K text by learning a vocabulary from the corpus, because 90%
of that text was correctly spaced. That trick does not transfer: here almost
*every* merchant token is welded, so the corpus cannot teach the splitter.

Two sources of vocabulary instead -- a seed list of brand and commerce words,
plus whatever the user's own narrations do spell out with spaces (bank
boilerplate like "SALARY CREDIT ACME TECHNOLOGIES PVT LTD" is a free lesson in
the vocabulary of their statements).

Two safeguards, both carried over from CiteRAG because both were learned the
hard way:

- **All or nothing.** If a run cannot be fully covered by the vocabulary, it is
  left welded. A partial split invents words.
- **Long tokens never enter the learned vocabulary.** Otherwise a weld gets
  memorised as a word, and the splitter starts explaining welds with welds.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

#: Below this length, a welded token is not worth the risk of a bad split.
MIN_WELD_LENGTH = 8
#: A learned word longer than this is probably itself a weld.
MAX_LEARNED_LENGTH = 12

_CASE_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z]{2})(?=[A-Z][a-z])")
_ALPHA_RUN = re.compile(r"[A-Za-z]{2,}")

SEED_WORDS: tuple[str, ...] = (
    # Commonest first -- position in this tuple is the frequency prior.
    "the", "and", "pay", "india", "ltd", "pvt", "limited", "private", "bank",
    "card", "cash", "rent", "fee", "bill", "shop", "store", "mart", "market",
    "super", "food", "foods", "cafe", "hotel", "restaurant", "kitchen", "juice",
    "medical", "pharma", "pharmacy", "hospital", "clinic", "labs", "diagnostics",
    "fuel", "petrol", "petroleum", "energy", "power", "gas", "electric",
    "telecom", "mobile", "recharge", "broadband", "fibre", "internet",
    "travel", "trip", "tours", "cab", "cabs", "auto", "metro", "railway", "irctc",
    "air", "airlines", "airways", "flight", "hotels", "stay", "rooms",
    "insurance", "assurance", "finance", "financial", "capital", "securities",
    "mutual", "fund", "funds", "investment", "investments", "broking", "trade",
    "technologies", "technology", "systems", "solutions", "services", "service",
    "enterprises", "industries", "corporation", "company", "group", "holdings",
    "retail", "commerce", "digital", "online", "global", "national", "international",
    "designs", "design", "fashion", "style", "wear", "apparel", "footwear",
    "beauty", "salon", "spa", "fitness", "gym", "sports", "games", "gaming",
    "books", "education", "academy", "institute", "school", "college", "learning",
    "grocery", "groceries", "fresh", "daily", "farm", "dairy", "milk", "water",
    "instamart", "genie", "express", "delivery", "logistics", "courier", "post",
    "subscription", "prime", "plus", "premium", "annual", "monthly", "renewal",
    "salary", "credit", "debit", "interest", "charges", "refund", "reversal",
    "housing", "apartments", "society", "maintenance", "association", "residency",
    "electricity", "board", "corporation", "municipal", "water", "sewage",
    "medicine", "wellness", "care", "life", "health", "secure",
    # Brands seen constantly in Indian UPI narrations.
    "swiggy", "zomato", "blinkit", "zepto", "dunzo", "bigbasket", "licious",
    "amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "snapdeal",
    "netflix", "spotify", "hotstar", "disney", "prime", "youtube", "jiosaavn",
    "uber", "ola", "rapido", "namma", "yatra", "makemytrip", "goibibo", "oyo",
    "paytm", "phonepe", "gpay", "googlepay", "cred", "razorpay", "billdesk",
    "jio", "airtel", "vodafone", "idea", "bsnl", "tata", "reliance", "adani",
    "dmart", "reliancefresh", "spencers", "more", "star", "lulu", "metro",
    "decathlon", "croma", "vijay", "sales", "lenskart", "firstcry", "pharmeasy",
    "apollo", "medplus", "netmeds", "practo", "cult", "curefit",
    "zerodha", "groww", "upstox", "smallcase", "kuvera", "coin",
    "bookmyshow", "pvr", "inox", "cinemas", "multiplex",
    "indianoil", "bharat", "hindustan", "shell", "nayara",
    "acme", "corp", "inc", "co", "llp", "opc",
)


class Vocabulary:
    """Words the splitter is allowed to produce, with a frequency prior."""

    def __init__(self, words: Iterable[str] = SEED_WORDS) -> None:
        self._cost: dict[str, float] = {}
        self._longest = 0
        for rank, word in enumerate(words):
            self._add(word, rank)

    def _add(self, word: str, rank: int) -> None:
        cleaned = word.strip().lower()
        if len(cleaned) < 2 or not cleaned.isalpha():
            return
        # Zipf: cost grows with rank, so common words win ties.
        cost = math.log((rank + 1) * 12.0)
        if cost < self._cost.get(cleaned, math.inf):
            self._cost[cleaned] = cost
        self._longest = max(self._longest, len(cleaned))

    def learn(self, text: str) -> None:
        """Harvest already-spaced words from the user's own narrations."""
        for word in _ALPHA_RUN.findall(text):
            if len(word) <= MAX_LEARNED_LENGTH:
                self._add(word, len(self._cost))

    def learn_all(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.learn(text)

    def cost(self, word: str) -> float | None:
        return self._cost.get(word)

    @property
    def longest(self) -> int:
        return self._longest

    def __contains__(self, word: object) -> bool:
        return isinstance(word, str) and word.lower() in self._cost

    def __len__(self) -> int:
        return len(self._cost)


def segment(text: str, vocabulary: Vocabulary | None = None) -> list[str]:
    """Split welded runs in ``text`` into words. Unrecoverable runs stay whole."""
    vocab = vocabulary if vocabulary is not None else Vocabulary()
    words: list[str] = []
    for chunk in text.split():
        for piece in _CASE_BOUNDARY.split(chunk):
            if not piece:
                continue
            words.extend(_split_run(piece, vocab))
    return words


def _split_run(piece: str, vocab: Vocabulary) -> list[str]:
    if len(piece) < MIN_WELD_LENGTH or not piece.isalpha():
        return [piece]
    if piece.lower() in vocab:
        return [piece]
    found = _best_split(piece.lower(), vocab)
    # All or nothing: an unparseable run is left exactly as it came in.
    return found if found else [piece]


def _best_split(lowered: str, vocab: Vocabulary) -> list[str]:
    """Minimum-cost cover of the string by vocabulary words, or [] if none."""
    length = len(lowered)
    best = [math.inf] * (length + 1)
    back = [0] * (length + 1)
    best[0] = 0.0

    window = max(vocab.longest, 1)
    for end in range(1, length + 1):
        for start in range(max(0, end - window), end):
            if best[start] == math.inf:
                continue
            cost = vocab.cost(lowered[start:end])
            if cost is None:
                continue
            total = best[start] + cost
            if total < best[end]:
                best[end] = total
                back[end] = start

    if best[length] == math.inf:
        return []

    pieces: list[str] = []
    cursor = length
    while cursor > 0:
        pieces.append(lowered[back[cursor] : cursor])
        cursor = back[cursor]
    pieces.reverse()
    # A single-word "split" is not a split.
    return pieces if len(pieces) > 1 else []


def weld_ratio(texts: Sequence[str], vocabulary: Vocabulary | None = None) -> float:
    """Share of alphabetic tokens that are long and unknown -- i.e. still welded.

    A progress metric, the way CiteRAG tracked 10.2% -> 3.7%.
    """
    vocab = vocabulary if vocabulary is not None else Vocabulary()
    total = welded = 0
    for text in texts:
        for token in _ALPHA_RUN.findall(text):
            total += 1
            if len(token) >= MIN_WELD_LENGTH and token.lower() not in vocab:
                welded += 1
    return welded / total if total else 0.0
