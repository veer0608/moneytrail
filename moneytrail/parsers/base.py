"""Parser interface plus the date/column guessing every Indian format needs."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path

from ..models import Statement

#: Seen in the wild across HDFC / ICICI / SBI / Kotak / Axis exports.
DATE_FORMATS = (
    "%d/%m/%y",
    "%d/%m/%Y",
    "%d-%m-%y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %B %Y",
    "%Y-%m-%d",
)

#: A masked account number, e.g. "XXXXXXXX4471" or "****4471". No leading \b --
#: "*" is not a word character, so a boundary would never match the "****" form.
ACCOUNT_HINT = re.compile(r"(?:[Xx]{2,}|\*{2,})\s?\d{3,6}\b")

#: Banks abbreviate freely -- "Closing Balance", "Closing Bal", "Bal C/F".
_OPENING_NARRATIONS = re.compile(
    r"^(opening\s+bal(?:ance)?\.?|bal(?:ance)?\.?\s+b[/\s.]*f|b[/\s.]*f"
    r"|brought\s+forward)\b",
    re.IGNORECASE,
)
_CLOSING_NARRATIONS = re.compile(
    r"^(closing\s+bal(?:ance)?\.?|bal(?:ance)?\.?\s+c[/\s.]*f|c[/\s.]*f"
    r"|carried\s+forward)\b",
    re.IGNORECASE,
)
_SUMMARY_HEADING = re.compile(
    r"^(statement|account|transaction)\s+summary\b|^summary\s*[:\-]",
    re.IGNORECASE,
)


class UnparseableStatement(Exception):
    """The file matched a parser but its contents could not be read."""


class PasswordRequired(UnparseableStatement):
    """The file is encrypted and no usable password was supplied.

    Indian banks email statements locked with something derived from your PAN,
    date of birth or customer ID. The password is never stored or logged by this
    tool -- it is used to open the file and then dropped.
    """

    def __init__(self, path: Path, *, wrong: bool = False) -> None:
        detail = "password is incorrect" if wrong else "password required"
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.wrong = wrong


class NoParserFound(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(f"no parser recognised {path}")
        self.path = path


class StatementParser(ABC):
    name: str
    #: File extensions worth offering to this parser. Used to keep directory
    #: walks from reporting every unrelated file as unreadable.
    suffixes: tuple[str, ...] = ()

    @abstractmethod
    def sniff(self, path: Path) -> bool:
        """Cheap check: could this parser handle the file?"""

    @abstractmethod
    def parse(self, path: Path, *, password: str | None = None) -> Statement:
        """Read the file into a Statement, or raise UnparseableStatement."""


def parse_date(text: str) -> date:
    cleaned = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise UnparseableStatement(f"unrecognised date: {text!r}")


def looks_like_date(text: str) -> bool:
    try:
        parse_date(text)
    except UnparseableStatement:
        return False
    return True


def normalise_header(text: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation."""
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".:")


def is_opening_row(narration: str) -> bool:
    return bool(_OPENING_NARRATIONS.match(narration.strip()))


def is_closing_row(narration: str) -> bool:
    return bool(_CLOSING_NARRATIONS.match(narration.strip()))


def is_summary_heading(text: str) -> bool:
    """"STATEMENT SUMMARY :-" and friends: the ledger has ended."""
    return bool(_SUMMARY_HEADING.match(text.strip()))


def find_account_hint(lines: list[str]) -> str:
    for line in lines:
        match = ACCOUNT_HINT.search(line)
        if match:
            return match.group(0).replace(" ", "")
    return ""
