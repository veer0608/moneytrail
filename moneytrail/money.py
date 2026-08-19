"""Money is an integer count of paise.

Floats are banned in this project. ``0.1 + 0.2 != 0.3`` in binary, and the one
promise this tool makes is that a statement reconciles *to the paisa*. A float
pipeline cannot make that promise, so amounts are ``int`` paise everywhere and
are only rendered as rupees at the edge.
"""

from __future__ import annotations

import re

Paise = int

_BLANK = {"", "-", "--", ".", "nil", "n/a", "na", "none"}
#: Runs of these mean "nothing here": bank masking (``*******``), Excel's
#: column-too-narrow marker (``#####``), and ruled divider rows.
_PLACEHOLDER_CHARACTERS = frozenset("*#-._ ")
_CURRENCY_PREFIXES = ("₹", "rs.", "rs", "inr")
_DIGITS = re.compile(r"\d+(\.\d{1,2})?")


def parse_amount(text: str) -> Paise:
    """Parse an Indian-format money string into paise.

    Handles lakh grouping (``1,23,456.78``), currency prefixes, ``Cr``/``Dr``
    suffixes, and parenthesised negatives. Raises ``ValueError`` on anything it
    does not understand -- an amount silently read as zero is far worse than a
    crash, because it still reconciles against a wrong total.
    """
    raw = text
    s = text.strip().lower()

    negative = False

    # Sign and currency arrive in either order. HDFC's card statements mark a
    # payment ``+ ₹ 4,500.00`` -- sign outside -- while a bank statement writes
    # ``₹-500.00`` with the sign inside. Stripping one and then the other in a
    # fixed order leaves whichever came first still attached, and the whole
    # amount then fails to parse, which loses a real transaction. So both are
    # taken off repeatedly until neither is there.
    # At most one sign, whichever side of the currency it sits. Two signs is
    # malformed -- "--5" is not five -- and peeling greedily would quietly turn
    # a corrupt cell into a number, which is the failure this module exists to
    # refuse.
    peeling, signed = True, False
    while peeling:
        peeling = False
        for prefix in _CURRENCY_PREFIXES:
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
                peeling = True
                break
        if not signed:
            if s.startswith("-"):
                negative = not negative
                s = s[1:].strip()
                signed = peeling = True
            elif s.startswith("+"):
                s = s[1:].strip()
                signed = peeling = True

    if s.endswith("cr"):
        s = s[:-2].strip()
    elif s.endswith("dr"):
        # On a balance, "Dr" means overdrawn. On an amount in a debit column it
        # is redundant, and the column already carries the sign.
        s = s[:-2].strip()
        negative = True

    if s.startswith("(") and s.endswith(")"):
        # Parentheses can wrap a currency mark of their own: "(₹1,200.00)".
        negative = not negative
        s = s[1:-1].strip()
        for prefix in _CURRENCY_PREFIXES:
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
                break

    s = s.replace(",", "").replace(" ", "")
    if not _DIGITS.fullmatch(s):
        raise ValueError(f"unparseable amount: {raw!r}")

    rupees, _, frac = s.partition(".")
    paise = int(rupees) * 100 + (int(frac.ljust(2, "0")) if frac else 0)
    return -paise if negative else paise


def parse_optional_amount(text: str | None) -> Paise | None:
    """Like :func:`parse_amount`, but blank/placeholder cells become ``None``.

    Indian statements leave the unused side of the debit/credit pair empty, or
    fill it with a dash. That is absence, not zero.
    """
    if text is None:
        return None
    cleaned = text.strip().lower()
    if cleaned in _BLANK or is_placeholder(cleaned):
        return None
    return parse_amount(text)


def is_placeholder(text: str) -> bool:
    """True for a run of masking characters -- absence, not a number."""
    cleaned = text.strip()
    return bool(cleaned) and set(cleaned) <= _PLACEHOLDER_CHARACTERS


def format_paise(paise: Paise) -> str:
    """Render paise as rupees with lakh/crore grouping: ``1,23,45,678.90``."""
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    body = str(rupees)
    if len(body) > 3:
        head, tail = body[:-3], body[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        body = ",".join(groups + [tail])
    return f"{sign}₹{body}.{remainder:02d}"
