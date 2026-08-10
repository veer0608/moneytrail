"""Parser registry. Add a format, register it here, and everything else works."""

from __future__ import annotations

from pathlib import Path

from ..models import Statement
from .base import (
    NoParserFound,
    PasswordRequired,
    StatementParser,
    UnparseableStatement,
)
from .csv_statement import CsvStatementParser
from .pdf_statement import PdfStatementParser
from .xlsx_statement import XlsxStatementParser

#: Order matters only where suffixes overlap -- the spreadsheet parser sniffs
#: magic bytes, so a text file misnamed .xls still falls through to CSV.
PARSERS: list[StatementParser] = [
    XlsxStatementParser(),
    CsvStatementParser(),
    PdfStatementParser(),
]


def register(parser: StatementParser) -> StatementParser:
    PARSERS.append(parser)
    return parser


def supported_suffixes() -> frozenset[str]:
    """Extensions any registered parser might handle."""
    return frozenset(suffix for parser in PARSERS for suffix in parser.suffixes)


def parse_statement(path: str | Path, *, password: str | None = None) -> Statement:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    for parser in PARSERS:
        if parser.sniff(path):
            return parser.parse(path, password=password)
    raise NoParserFound(path)


__all__ = [
    "NoParserFound",
    "PARSERS",
    "PasswordRequired",
    "StatementParser",
    "UnparseableStatement",
    "parse_statement",
    "register",
    "supported_suffixes",
]
