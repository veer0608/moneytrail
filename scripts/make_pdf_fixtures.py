"""Generate the synthetic PDF fixtures.

Same seven transactions as ``hdfc_april_2025.csv``, so the tests can assert that
both formats produce byte-identical ledgers. Run from the project root:

    python scripts/make_pdf_fixtures.py

Requires reportlab (``pip install reportlab``). The output is committed, so this
only needs re-running if the fixture data changes.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: Obviously fake -- this unlocks a synthetic fixture and nothing else.
FIXTURE_PASSWORD = "test1234"

PREAMBLE = [
    "HDFC BANK LIMITED",
    "Statement of account",
    "Account No: XXXXXXXX4471",
    "Period: 01/04/2025 to 30/04/2025",
]

HEADER = [
    "Date",
    "Narration",
    "Chq./Ref.No.",
    "Value Dt",
    "Withdrawal Amt.",
    "Deposit Amt.",
    "Closing Balance",
]

ROWS = [
    ["01/04/25", "OPENING BALANCE", "", "", "", "", "45231.60"],
    ["01/04/25", "UPI-SWIGGYINSTAMART-SWIGGY@YBL-YESB0000001-435820912-PAYMENT",
     "435820912", "01/04/25", "412.00", "", "44819.60"],
    ["02/04/25", "SALARY CREDIT ACME TECHNOLOGIES PVT LTD",
     "NEFT0092331", "02/04/25", "", "85000.00", "129819.60"],
    ["05/04/25", "UPI-NETFLIXINDIA-NETFLIX@ICICI-ICIC0000103-508812277-SUBSCRIPTION",
     "508812277", "05/04/25", "649.00", "", "129170.60"],
    ["07/04/25", "UPI-MYNTRADESIGNS-MYNTRA@AXISBANK-UTIB0000441-509912834-ORDER",
     "509912834", "07/04/25", "2499.00", "", "126671.60"],
    ["12/04/25", "ACH D- HOUSING RENT SHOBHA APARTMENTS",
     "ACH99120", "12/04/25", "28000.00", "", "98671.60"],
    ["18/04/25", "UPI-MYNTRADESIGNS-MYNTRA@AXISBANK-UTIB0000441-511220945-REFUND",
     "511220945", "18/04/25", "", "2499.00", "101170.60"],
    ["25/04/25", "ATM WDL BANNERGHATTA RD BLR",
     "ATM77213", "25/04/25", "5000.00", "", "96170.60"],
    ["30/04/25", "CLOSING BALANCE", "", "", "", "", "96170.60"],
]

COLUMN_WIDTHS = [20 * mm, 105 * mm, 25 * mm, 20 * mm, 32 * mm, 32 * mm, 35 * mm]

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=6.5, leading=8)
TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=9, leading=12)


def build(target: Path, *, password: str | None = None) -> None:
    encryption = (
        StandardEncryption(password, ownerPassword=password, strength=128)
        if password
        else None
    )
    document = SimpleDocTemplate(
        str(target),
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        encrypt=encryption,
    )

    table = Table(
        [HEADER] + [[Paragraph(cell, BODY) for cell in row] for row in ROWS],
        colWidths=COLUMN_WIDTHS,
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                # A ruled grid, so the "lines" extraction strategy recovers it exactly.
                ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 6.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
            ]
        )
    )

    document.build(
        [Paragraph(line, TITLE) for line in PREAMBLE] + [Spacer(1, 6 * mm), table]
    )
    print(f"wrote {target}")


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    build(FIXTURES / "hdfc_april_2025.pdf")
    build(FIXTURES / "hdfc_april_2025_locked.pdf", password=FIXTURE_PASSWORD)
