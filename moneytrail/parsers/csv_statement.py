"""CSV/TSV net-banking exports.

The grid is already a grid; all this does is find where the table starts and
hand the rows to the shared builder.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..models import Statement
from .base import StatementParser, UnparseableStatement, find_account_hint
from .table import RawRow, build_statement, detect_bank, find_header


class CsvStatementParser(StatementParser):
    name = "csv"
    suffixes = (".csv", ".tsv", ".txt")

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() not in self.suffixes:
            return False
        try:
            rows = _read_rows(path)
        except (OSError, UnicodeDecodeError):
            return False
        return find_header(rows) is not None

    def parse(self, path: Path, *, password: str | None = None) -> Statement:
        rows = _read_rows(path)
        found = find_header(rows)
        if found is None:
            raise UnparseableStatement(f"no recognisable header row in {path}")
        header_index, columns = found

        preamble = [" ".join(row) for row in rows[:header_index]]
        # Row numbers are 1-based source lines, so a failure report points at a
        # line the user can open the file to.
        raw = [
            RawRow(number=header_index + 1 + offset, cells=cells)
            for offset, cells in enumerate(rows[header_index + 1 :], start=1)
        ]

        return build_statement(
            source=path,
            bank=detect_bank(preamble, path),
            account_hint=find_account_hint(preamble),
            columns=columns,
            rows=raw,
        )


def _read_rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    # StringIO rather than splitlines() so a narration containing a quoted
    # newline stays one record.
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))
