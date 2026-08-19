"""The hosted front-end: drop a statement in, get a certified ledger back.

This is the one place the local-first rule bends, and it is worth being exact
about how far.

The CLI's promise is absolute: statements never leave the machine. A hosted
service cannot make that promise, and pretending otherwise would be the
dishonest option. So it makes the strongest promise it can actually keep --
the uploaded bytes exist only inside a single request, in a temporary
directory removed before the response is written, and nothing is stored,
logged, or queued. There is no database. A second request cannot see the first
one's file because there is nothing left to see.

Why bend at all: accountants are the people who need the certificate, and they
will not `pip install` anything. A promise nobody can reach is worth less than
a slightly weaker one they can.

The parsers take paths rather than bytes -- pdfplumber wants a file, and the
spreadsheet parser sniffs magic bytes off disk -- so the upload is spooled to
a `TemporaryDirectory` whose context manager guarantees removal even when a
parse raises. That is a real disk write, and the copy in the UI says so
instead of claiming the bytes never land.

The whole request/response core here is framework-free on purpose: `process()`
takes filenames and bytes and returns a dataclass, so it can be tested without
a client and moved off FastAPI without rewriting the reasoning.
"""

from __future__ import annotations

import base64
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence

from .export import Certificate, certify, render_certificates, write as write_export
from .models import CardStatement, Statement
from .money import format_paise
from .parsers import (
    NoParserFound,
    PasswordRequired,
    UnparseableStatement,
    parse_statement,
    supported_suffixes,
)
from .report import FAILED, OK, WEAK

#: A bank statement is a few hundred kilobytes. Ten megabytes is generous for a
#: scanned PDF and still small enough that a public endpoint cannot be made to
#: hold much memory.
MAX_FILE_BYTES = 10 * 1024 * 1024
#: A year of monthly statements across a couple of accounts, with room to spare.
MAX_FILES = 25
#: The whole request, not one file. Checked from Content-Length before anything
#: is read, so an oversized upload is refused rather than buffered.
MAX_BODY_BYTES = 30 * 1024 * 1024

FORMATS = {
    "xlsx": (
        "ledger.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "csv": ("ledger.csv", "text/csv"),
}

STATIC = Path(__file__).parent / "static"


@dataclass(frozen=True)
class Rejected:
    """A file that never became a statement, and why."""

    filename: str
    reason: str
    needs_password: bool = False


@dataclass(frozen=True)
class Tile:
    """One statement's verdict, shaped for the trust strip.

    Mirrors `report.Tile` rather than reusing it: the HTML report renders
    server-side and this crosses a JSON boundary, so it carries the numbers
    already formatted and the status as a string the page can style on.
    """

    filename: str
    status: str
    institution: str
    account: str
    period: str
    transactions: int
    digest: str
    checks: tuple[str, ...]
    lines: tuple[tuple[str, str], ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    tiles: tuple[Tile, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    certificate_text: str = ""
    filename: str = ""
    content_type: str = ""
    data: bytes = b""
    _certificates: tuple[Certificate, ...] = field(default=(), repr=False)

    @property
    def reconciled(self) -> int:
        return sum(1 for c in self._certificates if c.reconciled)

    @property
    def total(self) -> int:
        return len(self._certificates)

    @property
    def ok(self) -> bool:
        """Every file read, and every statement reconciled."""
        return bool(self.total) and self.reconciled == self.total and not self.rejected

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "reconciled": self.reconciled,
            "total": self.total,
            "tiles": [
                {
                    "filename": tile.filename,
                    "status": tile.status,
                    "institution": tile.institution,
                    "account": tile.account,
                    "period": tile.period,
                    "transactions": tile.transactions,
                    "digest": tile.digest,
                    "checks": list(tile.checks),
                    "lines": [list(pair) for pair in tile.lines],
                    "notes": list(tile.notes),
                }
                for tile in self.tiles
            ],
            "rejected": [
                {
                    "filename": item.filename,
                    "reason": item.reason,
                    "needs_password": item.needs_password,
                }
                for item in self.rejected
            ],
            "certificate": self.certificate_text,
            "filename": self.filename,
            "content_type": self.content_type,
            # The file rides home inside the JSON so the browser can hand it
            # over as a Blob. That keeps the server stateless: there is no
            # second request to serve, so there is nothing to keep between
            # them. A few hundred rows is tens of kilobytes.
            "data": base64.b64encode(self.data).decode("ascii"),
        }


class RateLimit:
    """A per-client request budget, held in memory, stdlib only.

    Parsing a PDF is real CPU work, and this runs on a small shared instance.
    Without a budget one loop pins it and everyone else gets nothing.

    Deliberately not distributed and deliberately not a dependency. A limiter
    that needs Redis to start is a limiter that turns a quiet afternoon into an
    outage, and if this ever runs on more than one instance the budget becomes
    per-instance -- a weaker limit, not a broken one.

    It is a speed bump, not a security control, and worth being plain about
    why: the key comes from a proxy header a determined caller can vary. It
    exists to stop accidental and casual abuse from monopolising the CPU. Stop
    anything more determined at the edge, where the real client address is.
    """

    def __init__(self, allowance: int, per_seconds: float) -> None:
        self.allowance = allowance
        self.per_seconds = per_seconds
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, *, now: float | None = None) -> bool:
        """True if this call is within budget, and counts it. False to refuse."""
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.per_seconds

        window = self._hits.setdefault(key, deque())
        while window and window[0] <= cutoff:
            window.popleft()

        # Callers who have gone quiet must not accumulate: without this the
        # dict is a slow memory leak keyed by every address ever seen.
        if len(self._hits) > 4096:
            self._forget_idle(cutoff)

        if len(window) >= self.allowance:
            return False
        window.append(moment)
        return True

    def _forget_idle(self, cutoff: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def client_key(forwarded_for: str | None, peer: str | None) -> str:
    """Whose budget this request spends.

    Behind a proxy the socket address is the proxy's, so the forwarded header
    is used when present -- first entry, which is the original client on every
    platform this is meant to run on. Spoofable, and the docstring on
    `RateLimit` says so; the fallback is the peer address.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"


def safe_name(raw: str) -> str:
    """The filename with every trace of a path removed.

    An upload's filename is attacker-controlled, and it is used to build a path
    inside the scratch directory. Both separators are stripped, not just the
    host platform's -- a Windows server must still refuse a POSIX traversal.
    """
    name = PureWindowsPath(PurePosixPath(raw).name).name
    name = name.replace("\x00", "").strip().lstrip(".")
    cleaned = "".join(ch for ch in name if ch.isprintable())
    return cleaned or "statement"


def _tile(certificate: Certificate, statement: Statement | CardStatement) -> Tile:
    if not certificate.reconciled:
        status = FAILED
    elif certificate.caveats:
        # It added up, but only against figures taken from its own rows, or
        # against nothing at all. Green here would overstate it.
        status = WEAK
    else:
        status = OK

    lines: list[tuple[str, str]] = []
    if certificate.kind == "bank":
        lines = [
            ("opening", format_paise(certificate.opening or 0)),
            ("credits", format_paise(certificate.credits or 0)),
            ("debits", format_paise(certificate.debits or 0)),
            ("computed closing", format_paise(certificate.computed_closing or 0)),
            ("statement closing", format_paise(certificate.stated_closing or 0)),
        ]
    else:
        lines = [
            ("charged", format_paise(certificate.debits or 0)),
            ("paid off", format_paise(certificate.credits or 0)),
        ]

    period = "-"
    if certificate.period_start or certificate.period_end:
        period = f"{certificate.period_start or '?'} to {certificate.period_end or '?'}"

    return Tile(
        filename=Path(certificate.source).name,
        status=status,
        institution=certificate.institution or "-",
        account=certificate.account_hint or "-",
        period=period,
        transactions=certificate.transactions,
        digest=certificate.digest,
        checks=certificate.checks,
        lines=tuple(lines),
        notes=certificate.failures + certificate.caveats,
    )


def process(
    uploads: Sequence[tuple[str, bytes]],
    *,
    password: str | None = None,
    fmt: str = "xlsx",
) -> Result:
    """Parse, reconcile and export uploaded statements in one pass.

    Never raises for bad input: an unreadable file becomes a `Rejected` beside
    the ones that worked, because a batch of twelve statements should not be
    lost to one locked PDF.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r} -- use xlsx or csv")
    if len(uploads) > MAX_FILES:
        raise ValueError(f"too many files at once -- the limit is {MAX_FILES}")

    statements: list[Statement | CardStatement] = []
    rejected: list[Rejected] = []
    allowed = supported_suffixes()

    with tempfile.TemporaryDirectory(prefix="moneytrail-") as scratch:
        root = Path(scratch)
        for index, (raw_name, blob) in enumerate(uploads):
            name = safe_name(raw_name)
            if not blob:
                rejected.append(Rejected(name, "the file is empty"))
                continue
            if len(blob) > MAX_FILE_BYTES:
                rejected.append(
                    Rejected(name, f"larger than {MAX_FILE_BYTES // (1024 * 1024)}MB")
                )
                continue
            if Path(name).suffix.lower() not in allowed:
                listed = ", ".join(sorted(allowed))
                rejected.append(Rejected(name, f"not a format I read ({listed})"))
                continue

            # One directory per file so two uploads sharing a name cannot
            # overwrite each other, and so the certificate can still show the
            # name the user recognises rather than an index.
            holder = root / str(index)
            holder.mkdir()
            target = holder / name
            target.write_bytes(blob)

            try:
                statements.append(parse_statement(target, password=password))
            except PasswordRequired as error:
                reason = (
                    "the password did not open it"
                    if error.wrong
                    else "it is password-protected"
                )
                rejected.append(Rejected(name, reason, needs_password=True))
            except (UnparseableStatement, NoParserFound) as error:
                rejected.append(Rejected(name, _reason(error)))

        if not statements:
            return Result(rejected=tuple(rejected))

        certificates = [certify(statement) for statement in statements]
        tiles = tuple(
            _tile(certificate, statement)
            for certificate, statement in zip(certificates, statements)
        )

        wanted, content_type = FORMATS[fmt]
        out = root / wanted
        write_export(statements, out)
        data = out.read_bytes()

    # Outside the `with`: the scratch directory and every uploaded byte in it
    # are gone by the time this returns, and only the export survives, in
    # memory, on its way to the response.
    return Result(
        tiles=tiles,
        rejected=tuple(rejected),
        certificate_text=render_certificates(certificates),
        filename=wanted,
        content_type=content_type,
        data=data,
        _certificates=tuple(certificates),
    )


def _reason(error: Exception) -> str:
    """The message without the scratch path, which means nothing to the user."""
    text = str(error)
    _, separator, tail = text.partition(": ")
    return (tail if separator else text) or "it could not be read"
