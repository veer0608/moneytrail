"""Generate an *unruled* PDF statement -- a table with no lines drawn round it.

The committed `hdfc_april_2025.pdf` is ruled, because reportlab drew borders on
it, and pdfplumber recovers a ruled table almost for free. Real bank statements
frequently draw no ruling at all: the columns exist only in where the words sit
on the page. Three third-party sample PDFs -- ICICI savings, ICICI credit card
and an HDFC savings statement, from separate projects -- all report zero lines,
zero rects and zero edges, and `extract_table()` returns None on every one.

So every PDF this project had parsed successfully was one it generated itself,
in the one layout that is easiest to read. These two fixtures exist to make
that visible and keep it visible, and they fail differently on purpose.

`hdfc_april_2025_unruled.pdf` is the ruled fixture with the grid removed and
nothing else changed. It still parses -- but the running-balance column does
not survive, so the chain check is lost and only the totals check runs. A
quieter failure than an error, and a worse one: the statement still reports
RECONCILED, on half the evidence.

`icici_july_2026_wrapped.pdf` is the one that actually defeats it, and is the
honest reproduction of the real ICICI layout: the narration wraps onto lines
above and below the line carrying the date and the amounts, so a row is no
longer a line. Column-splitting cannot fix that at any threshold. Rows have to
be assembled by finding the dated line and absorbing its neighbours.

    python scripts/make_unruled_pdf_fixture.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT = FIXTURES / "hdfc_april_2025_unruled.pdf"

PREAMBLE = [
    "HDFC BANK LIMITED",
    "Statement of account",
    "Account No: XXXXXXXX4471",
    "Period: 01/04/2025 to 30/04/2025",
]

HEADER = ["Date", "Narration", "Chq./Ref.No.", "Value Dt",
          "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]

ROWS = [
    ["01/04/25", "OPENING BALANCE", "", "", "", "", "45231.60"],
    ["01/04/25", "UPI-SWIGGYINSTAMART-SWIGGY@YBL", "435820912", "01/04/25", "412.00", "", "44819.60"],
    ["02/04/25", "SALARY CREDIT ACME TECHNOLOGIES", "NEFT0092331", "02/04/25", "", "85000.00", "129819.60"],
    ["05/04/25", "UPI-NETFLIXINDIA-NETFLIX@ICICI", "508812277", "05/04/25", "649.00", "", "129170.60"],
    ["07/04/25", "UPI-MYNTRADESIGNS-MYNTRA@AXIS", "509912834", "07/04/25", "2499.00", "", "126671.60"],
    ["12/04/25", "ACH D- HOUSING RENT SHOBHA APTS", "ACH99120", "12/04/25", "28000.00", "", "98671.60"],
    ["18/04/25", "UPI-MYNTRADESIGNS-MYNTRA@AXIS", "511220945", "18/04/25", "", "2499.00", "101170.60"],
    ["25/04/25", "ATM WDL BANNERGHATTA RD BLR", "ATM77213", "25/04/25", "5000.00", "", "96170.60"],
    ["30/04/25", "CLOSING BALANCE", "", "", "", "", "96170.60"],
]

#: Column left edges, in points. The columns are real -- they are simply not
#: drawn. Everything a reader needs is here; none of it is a line.
COLUMNS = (40, 100, 300, 380, 450, 540, 640)


WRAPPED = FIXTURES / "icici_july_2026_wrapped.pdf"

#: The structure that actually defeats the parser, taken from a real ICICI
#: savings layout: the narration wraps onto lines *above and below* the line
#: carrying the date and the amounts. A row is no longer a line, and no amount
#: of column-splitting fixes that -- rows have to be assembled by finding the
#: dated line and absorbing its neighbours.
WRAPPED_PREAMBLE = [
    "ICICI Bank Limited",
    "Statement of Transactions in Savings Account XXXXXXXX0136 in INR",
    "for the period July 01, 2026 - July 31, 2026",
]
WRAPPED_HEADER = ["DATE", "MODE", "PARTICULARS", "DEPOSITS", "WITHDRAWALS", "BALANCE"]
WRAPPED_COLUMNS = (40, 110, 170, 470, 560, 660)

#: ``(before, dated_line_cells, after)`` -- `before` and `after` are narration
#: fragments on their own lines, exactly as the real statement prints them.
WRAPPED_ROWS = [
    ([], ["01-07-2026", "B/F", "", "", "", "335678.35"], []),
    (
        ["Amazon Pay Groceries", "UPI/Amazon Pay/amazonpaygroce/You are pa/AXIS"],
        ["01-07-2026", "", "", "", "691.00", "334987.35"],
        ["BANK/000000000000/APL0000000000000000000/"],
    ),
    (
        ["Salary Testcorp Pvt Ltd"],
        ["05-07-2026", "", "NEFT-TESTCORP-000000000", "125000.00", "", "459987.35"],
        [],
    ),
    (
        ["Test Restaurant", "UPI/TEST RESTAURANT/testrest.000000/Payment By/HDFC"],
        ["06-07-2026", "", "", "", "1260.00", "458727.35"],
        ["BANK/000000000000/ICI0000000000000000000/"],
    ),
]


def build_wrapped() -> None:
    page = landscape(A4)
    pdf = canvas.Canvas(str(WRAPPED), pagesize=page)
    y = page[1] - 40

    pdf.setFont("Helvetica", 9)
    for line in WRAPPED_PREAMBLE:
        pdf.drawString(40, y, line)
        y -= 13
    y -= 10

    pdf.setFont("Helvetica-Bold", 8)
    for x, cell in zip(WRAPPED_COLUMNS, WRAPPED_HEADER):
        pdf.drawString(x, y, cell)
    y -= 15

    pdf.setFont("Helvetica", 8)
    for before, cells, after in WRAPPED_ROWS:
        for line in before:
            pdf.drawString(WRAPPED_COLUMNS[2], y, line)
            y -= 11
        for x, cell in zip(WRAPPED_COLUMNS, cells):
            if cell:
                pdf.drawString(x, y, cell)
        y -= 11
        for line in after:
            pdf.drawString(WRAPPED_COLUMNS[2], y, line)
            y -= 11

    pdf.save()
    print(f"wrote {WRAPPED}")


def build() -> None:
    page = landscape(A4)
    pdf = canvas.Canvas(str(OUT), pagesize=page)
    y = page[1] - 40

    pdf.setFont("Helvetica", 9)
    for line in PREAMBLE:
        pdf.drawString(40, y, line)
        y -= 13
    y -= 10

    pdf.setFont("Helvetica-Bold", 8)
    for x, cell in zip(COLUMNS, HEADER):
        pdf.drawString(x, y, cell)
    y -= 15

    pdf.setFont("Helvetica", 8)
    for row in ROWS:
        for x, cell in zip(COLUMNS, row):
            if cell:
                pdf.drawString(x, y, cell)
        y -= 13

    pdf.save()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
    build_wrapped()
