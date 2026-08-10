"""Take a narration apart before trying to understand it.

Bank narrations look like noise but they have a grammar. HDFC writes
``UPI-SWIGGYINSTAMART-SWIGGY@YBL-YESB0000001-435820912-PAYMENT``; ICICI writes
``UPI/ZOMATOLTD/zomato@hdfcbank/512099831``. The delimiter and field order both
vary, but the VPA (``swiggy@ybl``) is unmistakable, and the counterparty is
always beside it.

So rather than pattern-matching whole narrations per bank, this finds the
landmarks -- VPA, IFSC, reference numbers -- and reads the counterparty from
what is left. A new bank format usually needs no new code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Channel(str, Enum):
    UPI = "upi"
    IMPS = "imps"
    NEFT = "neft"
    RTGS = "rtgs"
    ACH = "ach"  # mandates: SIPs, EMIs, utility auto-debits
    ATM = "atm"
    POS = "pos"  # card swipe
    CARD = "card"  # online card payment
    CHEQUE = "cheque"
    INTEREST = "interest"
    CHARGES = "charges"  # the bank paying itself
    UNKNOWN = "unknown"


#: A UPI virtual payment address: swiggy@ybl, 9876543210@paytm.
VPA = re.compile(r"\b([\w.\-]{2,})@([a-z]{2,})\b", re.IGNORECASE)
#: An IFSC code: four letters, a zero, six alphanumerics.
IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$", re.IGNORECASE)
_REFERENCE = re.compile(r"^[A-Z]{0,4}\d{6,}$", re.IGNORECASE)
_SPLIT = re.compile(r"[/\-|]+")

#: Leading keywords that identify the rail. Order matters: longest first, so
#: "UPI" does not shadow a narration that merely mentions it later.
_CHANNEL_KEYWORDS: tuple[tuple[Channel, tuple[str, ...]], ...] = (
    (Channel.UPI, ("upi",)),
    (Channel.IMPS, ("imps",)),
    (Channel.NEFT, ("neft",)),
    (Channel.RTGS, ("rtgs",)),
    (Channel.ACH, ("ach", "nach", "ecs", "si ", "mandate")),
    (Channel.ATM, ("atm", "awb", "nwd", "cash wdl")),
    (Channel.POS, ("pos", "vps", "ipos")),
    (Channel.CARD, ("cc ", "card", "ecom")),
    (Channel.CHEQUE, ("chq", "cheque", "clg")),
    (Channel.INTEREST, ("int.pd", "int pd", "interest", "credit interest")),
    (Channel.CHARGES, ("chrg", "charges", "fee", "gst", "amb ", "sms alert")),
)

#: Segments that are structure, not identity.
_NOISE = frozenset(
    {
        "upi", "imps", "neft", "rtgs", "ach", "nach", "ecs", "pos", "atm", "wdl",
        "awb", "nwd", "chq", "clg", "p2m", "p2a", "dr", "cr", "d", "c", "payment",
        "paymen", "pay", "txn", "ref", "refno", "to", "from", "by", "transfer",
        "trf", "inb", "sent", "recd", "received", "collect", "req", "mandate",
        "si", "ecom", "card", "vps", "ipos", "in", "india", "ltd", "limited",
    }
)


@dataclass(frozen=True)
class Narration:
    raw: str
    channel: Channel
    counterparty: str
    vpa: str | None = None
    handle: str | None = None
    reference: str | None = None

    @property
    def identified(self) -> bool:
        return bool(self.counterparty)


def parse_narration(raw: str) -> Narration:
    text = " ".join(raw.split())
    channel = detect_channel(text)
    segments = [segment.strip() for segment in _SPLIT.split(text) if segment.strip()]

    vpa = handle = None
    vpa_index: int | None = None
    for index, segment in enumerate(segments):
        match = VPA.search(segment)
        if match:
            vpa = match.group(0).lower()
            handle = match.group(1).lower()
            vpa_index = index
            break

    # An IFSC (YESB0000001) also satisfies the reference-number shape, so it has
    # to be ruled out first.
    reference = next(
        (
            segment
            for segment in segments
            if _REFERENCE.match(segment) and not IFSC.match(segment)
        ),
        None,
    )

    return Narration(
        raw=raw,
        channel=channel,
        counterparty=_counterparty(segments, vpa_index),
        vpa=vpa,
        handle=handle,
        reference=reference,
    )


def detect_channel(text: str) -> Channel:
    head = text.lower()[:24]
    for channel, keywords in _CHANNEL_KEYWORDS:
        if any(head.startswith(keyword) or f" {keyword}" in head for keyword in keywords):
            return channel
    return Channel.UNKNOWN


def _counterparty(segments: list[str], vpa_index: int | None) -> str:
    """The best candidate for *who*, given where the VPA sits.

    Banks put the payee immediately before the VPA. If there is no VPA -- an ACH
    mandate, an ATM withdrawal, a salary credit -- fall back to the longest
    segment that is not structural noise, which is reliably the name.
    """
    if vpa_index is not None:
        for index in range(vpa_index - 1, -1, -1):
            if _is_identity(segments[index]):
                return segments[index]

    candidates = [segment for segment in segments if _is_identity(segment)]
    return max(candidates, key=len, default="")


def _is_identity(segment: str) -> bool:
    cleaned = segment.strip()
    if not cleaned or not any(character.isalpha() for character in cleaned):
        return False
    if IFSC.match(cleaned) or _REFERENCE.match(cleaned) or VPA.search(cleaned):
        return False
    words = [word for word in cleaned.lower().split() if word]
    return any(word not in _NOISE for word in words)
