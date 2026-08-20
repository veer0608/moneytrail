"""The certificate as a page, for the reader who will never open a terminal.

``export.py`` already produces the proof: which arithmetic was checked, against
which bytes, and whether it held. It renders as plain text for a terminal and as
a second sheet beside the ledger. Neither travels well to the person who most
needs to read it. An accountant receiving a client's converted statement wants
one page they can glance at, file, and attach -- and a spreadsheet tab is not
that, because the first thing anyone does with a workbook is look at the data
sheet and forget the other one exists.

So this module renders the same :class:`~moneytrail.export.Certificate` objects
as a PDF. It computes nothing. Every number, verdict and caveat on the page
comes from ``certify()``; if this file ever needs to decide whether something
reconciled, the decision is in the wrong place.

Two things on the page are load-bearing and easy to lose in a redesign:

* **The digest, in full.** Sixty-four hex characters are ugly and the temptation
  to abbreviate them is constant. An abbreviated digest cannot be checked, and a
  proof you cannot check is decoration.
* **The failures, with their row numbers.** A NOT RECONCILED page that does not
  say which row broke is worse than no page, because it looks like diligence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from xml.sax.saxutils import escape

from .money import Paise, format_paise

if TYPE_CHECKING:  # pragma: no cover -- avoids importing export at module load
    from .export import Certificate

RUPEE = "\u20b9"

#: Fonts known to carry U+20B9, newest-first per platform. The base-14 Type 1
#: fonts reportlab ships with do not have the glyph at all: Helvetica renders
#: the rupee sign as a blank or a box depending on the reader, which on an
#: Indian statement is the one character that must never be mangled. So a
#: system font is embedded when one can be found, and the presence of the glyph
#: is *verified* after loading rather than assumed from the filename -- font
#: files with the same name differ between Windows releases.
_FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/Nirmala.ttf", "C:/Windows/Fonts/NirmalaB.ttf"),
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
)

_BODY = "moneytrail-body"
_BOLD = "moneytrail-bold"

INK = "#14181f"
MUTED = "#5b6673"
RULE = "#d7dce3"
PASS = "#12673a"
FAIL = "#a3121f"
NOTE = "#8a5b00"


class Typeface:
    """Which fonts the page will actually use, and whether ₹ is safe to draw."""

    def __init__(self, body: str, bold: str, rupee: bool) -> None:
        self.body = body
        self.bold = bold
        self.rupee = rupee

    def money(self, paise: Paise | None) -> str:
        """An amount, in a form the embedded font can definitely draw.

        ``format_paise`` is the single renderer for money in this project and
        stays that way -- the grouping is Indian and belongs in one place. Only
        the currency mark is swapped, and only when the glyph is missing, so a
        fallback page reads ``INR 1,23,456.78`` rather than showing a box where
        the amount should be.
        """
        text = format_paise(paise or 0)
        if self.rupee:
            return text
        return text.replace(RUPEE, "INR ")


_typeface: Typeface | None = None


def typeface() -> Typeface:
    """Register an embeddable Unicode font once, and remember what happened."""
    global _typeface
    if _typeface is None:
        _typeface = _load_typeface()
    return _typeface


def _load_typeface() -> Typeface:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in _FONT_CANDIDATES:
        if not Path(regular).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(_BODY, regular))
            pdfmetrics.registerFont(TTFont(_BOLD, bold if Path(bold).exists() else regular))
        except Exception:
            # A font that will not load is not an error worth failing the whole
            # certificate over -- there is a working fallback below.
            continue
        if not _draws_rupee(_BODY):
            continue
        pdfmetrics.registerFontFamily(_BODY, normal=_BODY, bold=_BOLD, italic=_BODY,
                                      boldItalic=_BOLD)
        return Typeface(_BODY, _BOLD, rupee=True)

    return Typeface("Helvetica", "Helvetica-Bold", rupee=False)


def _draws_rupee(name: str) -> bool:
    """True only if the loaded face actually maps U+20B9 to a glyph."""
    from reportlab.pdfbase import pdfmetrics

    try:
        mapping = getattr(pdfmetrics.getFont(name).face, "charToGlyph", None)
    except Exception:
        return False
    return bool(mapping) and ord(RUPEE) in mapping


def render_pdf(
    certificates: Sequence["Certificate"],
    out: Path,
    *,
    generated: datetime | None = None,
) -> Path:
    """Write the certificate for `certificates` as a PDF, and return the path.

    Raises ``ImportError`` if reportlab is absent -- the caller turns that into
    an install hint, the same way the xlsx export does for openpyxl.
    """
    if not certificates:
        raise ValueError("nothing to certify -- no statements were read")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    out = Path(out)
    face = typeface()
    stamped = (generated or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

    document = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title="moneytrail reconciliation certificate",
        author="moneytrail",
        subject=_verdict(certificates),
    )
    document.build(
        _story(certificates, face, stamped),
        onFirstPage=lambda canvas, doc: _footer(canvas, doc, face),
        onLaterPages=lambda canvas, doc: _footer(canvas, doc, face),
    )
    return out


def _verdict(certificates: Sequence["Certificate"]) -> str:
    return (
        "RECONCILED"
        if all(c.reconciled for c in certificates)
        else "NOT RECONCILED"
    )


def typeset(text: str) -> str:
    """Escape statement text so reportlab renders it rather than parses it.

    Narrations arrive from the file and reach a markup-aware renderer, so
    ``<`` and ``&`` are data here and must stay data.

    Nothing else is done to the prose. The ``--`` that ``export.py`` writes is
    left exactly as it is: this page is one more surface carrying the same
    sentences as the README and the served page, and both of those had their
    em dashes taken out on purpose.
    """
    return escape(text)


def _styles(face: Typeface):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle

    def style(name: str, **kwargs) -> ParagraphStyle:
        kwargs.setdefault("fontName", face.body)
        kwargs.setdefault("textColor", HexColor(INK))
        kwargs.setdefault("leading", kwargs.get("fontSize", 10) * 1.35)
        return ParagraphStyle(name, **kwargs)

    return {
        "title": style("title", fontName=face.bold, fontSize=17, leading=20),
        "meta": style("meta", fontSize=8.5, textColor=HexColor(MUTED)),
        "banner": style("banner", fontName=face.bold, fontSize=14, leading=17,
                        textColor=HexColor("#ffffff")),
        "headline": style("headline", fontSize=9.5),
        "statement": style("statement", fontName=face.bold, fontSize=11, leading=14),
        "label": style("label", fontSize=8.5, textColor=HexColor(MUTED)),
        "value": style("value", fontSize=9),
        # A digest must be shown whole, so it is allowed to break mid-string
        # rather than overflow the column or force the table wider.
        "digest": style("digest", fontSize=8, wordWrap="CJK",
                        textColor=HexColor(MUTED)),
        "amount": style("amount", fontSize=9.5, alignment=2),
        "amountFail": style("amountFail", fontSize=9.5, alignment=2,
                            fontName=face.bold, textColor=HexColor(FAIL)),
        "amountLabel": style("amountLabel", fontSize=9.5, textColor=HexColor(MUTED)),
        "failure": style("failure", fontSize=8.8, textColor=HexColor(FAIL)),
        "caveat": style("caveat", fontSize=8.8, textColor=HexColor(NOTE)),
        "small": style("small", fontSize=8, textColor=HexColor(MUTED)),
    }


def _story(certificates: Sequence["Certificate"], face: Typeface, stamped: str) -> list:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    from .export import headline

    styles = _styles(face)
    passed = all(c.reconciled for c in certificates)
    flow: list = [
        Paragraph("moneytrail reconciliation certificate", styles["title"]),
        Paragraph(
            f"generated {escape(stamped)} &nbsp;·&nbsp; "
            f"{len(certificates)} statement(s) checked",
            styles["meta"],
        ),
        Spacer(1, 7 * mm),
    ]

    banner = Table(
        [[Paragraph(_verdict(certificates), styles["banner"])]],
        colWidths=["100%"],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(PASS if passed else FAIL)),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    flow.append(banner)
    flow.append(Spacer(1, 3 * mm))
    flow.append(Paragraph(typeset(headline(certificates)), styles["headline"]))
    flow.append(Spacer(1, 6 * mm))

    # The closing note travels inside the last block rather than after it. On
    # its own it is two lines, which is exactly the size that lands alone on a
    # second page and makes the document look like it broke.
    note = [
        Spacer(1, 7 * mm),
        Paragraph(
            "Each SHA-256 above is of the exact source bytes that were read. A "
            "file whose digest does not match is not the file this certificate "
            "covers. The checks are arithmetic the institution itself published; "
            "no model, no judgement and no estimate takes part in them.",
            styles["small"],
        ),
    ]

    last = len(certificates) - 1
    for index, certificate in enumerate(certificates):
        if index:
            flow.append(Spacer(1, 6 * mm))
        flow.extend(
            _block(certificate, face, styles, tail=note if index == last else None)
        )

    return flow


def _block(
    certificate: "Certificate", face: Typeface, styles, tail: list | None = None
) -> list:
    """One statement: what it was, what was checked, and how it came out."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

    verdict_colour = PASS if certificate.reconciled else FAIL
    heading = Table(
        [
            [
                Paragraph(escape(Path(certificate.source).name), styles["statement"]),
                Paragraph(
                    f'<font color="{verdict_colour}">{certificate.verdict}</font>',
                    ParagraphRight(styles["statement"]),
                ),
            ]
        ],
        colWidths=["68%", "32%"],
    )
    heading.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.75, HexColor(RULE)),
            ]
        )
    )

    facts = _facts(certificate, styles)
    arithmetic = _arithmetic(certificate, face, styles)

    side_by_side = Table(
        [[facts, arithmetic]], colWidths=["54%", "46%"], style=_bare()
    )

    parts: list = [heading, Spacer(1, 3 * mm), side_by_side]

    if certificate.failures:
        parts.append(Spacer(1, 3 * mm))
        parts.append(
            Paragraph(
                f"{len(certificate.failures)} row(s) flagged -- the export may be "
                f"an incomplete copy of the source:",
                styles["label"],
            )
        )
        for failure in certificate.failures:
            parts.append(Paragraph("• " + typeset(failure), styles["failure"]))

    for caveat in certificate.caveats:
        parts.append(Spacer(1, 1.5 * mm))
        parts.append(Paragraph("note: " + typeset(caveat), styles["caveat"]))

    if tail:
        parts.extend(tail)

    return [KeepTogether(parts)]


def ParagraphRight(base):
    """A right-aligned twin of `base`, so the verdict sits against the margin."""
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle(base.name + "-right", parent=base, alignment=2)


def _bare():
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]
    )


def _facts(certificate: "Certificate", styles) -> object:
    """The left-hand column: which file this is, and what was run against it."""
    from reportlab.platypus import Paragraph, Table

    kind = "card" if certificate.kind == "card" else "bank"
    period = f"{certificate.period_start or '?'} to {certificate.period_end or '?'}"
    how = certificate.date_order + (
        "" if certificate.date_order_observed else ", assumed"
    )
    checks = ", ".join(certificate.checks) if certificate.checks else "none available"

    rows = [
        (kind, certificate.institution or "-"),
        ("account", certificate.account_hint or "-"),
        ("period", period),
        ("transactions", str(certificate.transactions)),
        ("dates read as", how),
        ("checks run", checks),
    ]

    data = [
        [Paragraph(label, styles["label"]), Paragraph(escape(value), styles["value"])]
        for label, value in rows
    ]
    # The digest gets its own full-width row: sixty-four characters do not fit
    # beside a label, and shortening it would make it uncheckable.
    data.append([Paragraph("sha-256", styles["label"]), ""])
    data.append([Paragraph(escape(certificate.digest), styles["digest"]), ""])

    table = Table(data, colWidths=["34%", "66%"], style=_bare())
    table.setStyle(_padded())
    return table


def _padded():
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("SPAN", (0, -1), (-1, -1)),
        ]
    )


def _arithmetic(certificate: "Certificate", face: Typeface, styles) -> object:
    """The right-hand column: the sum the institution itself published.

    This is the whole argument of the product rendered as five lines. A reader
    who trusts nothing else on the page can add these up by hand.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Table, TableStyle

    def line(label: str, sign: str, amount: Paise | None, mark: bool = False):
        amount_style = styles["amountFail"] if mark else styles["amount"]
        return [
            Paragraph(label, styles["amountLabel"]),
            Paragraph(sign, styles["amountLabel"]),
            Paragraph(escape(face.money(amount)), amount_style),
        ]

    if certificate.kind == "bank":
        # The two endpoints are marked only when they actually differ, which is
        # a comparison of two recorded fields rather than a new calculation --
        # the difference itself is already spelled out in the failures below.
        # Marking them on *any* failure would be wrong: a statement can fail on
        # its own stated column totals while opening and closing agree exactly,
        # and colouring a matching pair red would send the reader hunting for a
        # gap that is not there.
        gap = (
            certificate.computed_closing is not None
            and certificate.stated_closing is not None
            and certificate.computed_closing != certificate.stated_closing
        )
        data = [
            line("opening", "", certificate.opening),
            line("credits", "+", certificate.credits),
            line("debits", "−", certificate.debits),
            line("computed", "=", certificate.computed_closing, gap),
            line("statement says", "", certificate.stated_closing, gap),
        ]
        ruled = 3
    else:
        data = [
            line("charged", "", certificate.debits),
            line("paid off", "", certificate.credits),
        ]
        ruled = None

    table = Table(data, colWidths=["44%", "8%", "48%"])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
    ]
    if ruled is not None:
        style.append(("LINEABOVE", (0, ruled), (-1, ruled), 0.75, HexColor(RULE)))
    table.setStyle(TableStyle(style))
    return table


def _footer(canvas, doc, face: Typeface) -> None:
    """A page number and the name of the thing, on every page."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm

    canvas.saveState()
    canvas.setFont(face.body, 7.5)
    canvas.setFillColor(HexColor(MUTED))
    canvas.drawString(
        doc.leftMargin, 11 * mm, "moneytrail reconciliation certificate"
    )
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin, 11 * mm, f"page {doc.page}"
    )
    canvas.restoreState()
