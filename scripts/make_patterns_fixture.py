"""Generate the five-month fixture the pattern detectors are tested against.

Running balances are computed rather than typed, so the fixture reconciles by
construction and a hand-arithmetic slip cannot quietly invalidate the tests
built on it.

    python scripts/make_patterns_fixture.py

Deliberately contains one of each thing worth finding:

- Netflix monthly for five months, still running
- Spotify monthly for three months, then cancelled
- Amazon charged twice on one day, one of them refunded
- Swiggy charged twice on one day, never refunded
- Myntra charged and refunded twelve days later
- rent and salary, to prove a cadence detector does not only find subscriptions
"""

from __future__ import annotations

import csv
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

OPENING = 1_000_000  # Rs 10,000.00 in paise

NETFLIX = "UPI-NETFLIXINDIA-NETFLIX@ICICI-ICIC0000103-{ref}-SUBSCRIPTION"
SPOTIFY = "UPI-SPOTIFYINDIA-SPOTIFY@YBL-YESB0000001-{ref}-SUBSCRIPTION"
AMAZON = "UPI-AMAZONRETAIL-AMAZON@APL-UTIB0000001-{ref}-ORDER"
AMAZON_REFUND = "UPI-AMAZONRETAIL-AMAZON@APL-UTIB0000001-{ref}-REFUND"
SWIGGY = "UPI-SWIGGY-SWIGGY@YBL-YESB0000001-{ref}-PAYMENT"
MYNTRA = "UPI-MYNTRADESIGNS-MYNTRA@AXISBANK-UTIB0000441-{ref}-ORDER"
MYNTRA_REFUND = "UPI-MYNTRADESIGNS-MYNTRA@AXISBANK-UTIB0000441-{ref}-REFUND"
SALARY = "SALARY CREDIT ACME TECHNOLOGIES PVT LTD"
RENT = "ACH D- HOUSING RENT SHOBHA APARTMENTS"

#: (date, narration, debit paise, credit paise)
ENTRIES = [
    ("01/01/25", SALARY, 0, 8_500_000),
    ("05/01/25", NETFLIX.format(ref="510011001"), 64_900, 0),
    ("07/01/25", SPOTIFY.format(ref="510011002"), 11_900, 0),
    ("12/01/25", RENT, 2_800_000, 0),
    ("01/02/25", SALARY, 0, 8_500_000),
    ("05/02/25", NETFLIX.format(ref="520011001"), 64_900, 0),
    ("07/02/25", SPOTIFY.format(ref="520011002"), 11_900, 0),
    ("10/02/25", AMAZON.format(ref="520011003"), 129_900, 0),
    ("10/02/25", AMAZON.format(ref="520011004"), 129_900, 0),
    ("12/02/25", RENT, 2_800_000, 0),
    ("20/02/25", AMAZON_REFUND.format(ref="520011009"), 0, 129_900),
    ("01/03/25", SALARY, 0, 8_500_000),
    ("05/03/25", NETFLIX.format(ref="530011001"), 64_900, 0),
    ("07/03/25", SPOTIFY.format(ref="530011002"), 11_900, 0),
    ("12/03/25", RENT, 2_800_000, 0),
    ("18/03/25", SWIGGY.format(ref="530011007"), 45_000, 0),
    ("18/03/25", SWIGGY.format(ref="530011008"), 45_000, 0),
    ("01/04/25", SALARY, 0, 8_500_000),
    ("05/04/25", NETFLIX.format(ref="540011001"), 64_900, 0),
    ("12/04/25", RENT, 2_800_000, 0),
    ("15/04/25", MYNTRA.format(ref="540011005"), 249_900, 0),
    ("27/04/25", MYNTRA_REFUND.format(ref="540011006"), 0, 249_900),
    ("01/05/25", SALARY, 0, 8_500_000),
    ("05/05/25", NETFLIX.format(ref="550011001"), 64_900, 0),
    ("12/05/25", RENT, 2_800_000, 0),
]

HEADER = [
    "Date", "Narration", "Chq./Ref.No.", "Value Dt",
    "Withdrawal Amt.", "Deposit Amt.", "Closing Balance",
]


def rupees(paise: int) -> str:
    return f"{paise / 100:.2f}"


def build(target: Path) -> None:
    balance = OPENING
    rows = [["01/01/25", "OPENING BALANCE", "", "", "", "", rupees(balance)]]

    for index, (day, narration, debit, credit) in enumerate(ENTRIES, start=1):
        balance += credit - debit
        rows.append(
            [
                day,
                narration,
                f"REF{index:05d}",
                day,
                rupees(debit) if debit else "",
                rupees(credit) if credit else "",
                rupees(balance),
            ]
        )

    rows.append(["12/05/25", "CLOSING BALANCE", "", "", "", "", rupees(balance)])

    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["HDFC BANK LIMITED"])
        writer.writerow(["Statement of account"])
        writer.writerow(["Account No: XXXXXXXX4471"])
        writer.writerow(["Period: 01/01/2025 to 12/05/2025"])
        writer.writerow([])
        writer.writerow(HEADER)
        writer.writerows(rows)

    print(f"wrote {target} ({len(ENTRIES)} transactions, closing {rupees(balance)})")


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    build(FIXTURES / "hdfc_jan_may_2025.csv")
