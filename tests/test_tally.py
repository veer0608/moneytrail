"""The Tally export: one balanced voucher per row, and the client's books untouched."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

from moneytrail import Direction, parse_statement
from moneytrail.cli import main
from moneytrail.export import write
from moneytrail.models import Statement, Transaction
from moneytrail.tally import (
    SUSPENSE_GROUP,
    SUSPENSE_LEDGER,
    default_bank_ledger,
    export_tally,
)


def vouchers(path: Path) -> list[ET.Element]:
    return ET.parse(path).getroot().findall(".//VOUCHER")


def entries(voucher: ET.Element) -> list[tuple[str, bool, Decimal]]:
    return [
        (
            entry.findtext("LEDGERNAME"),
            entry.findtext("ISDEEMEDPOSITIVE") == "Yes",
            Decimal(entry.findtext("AMOUNT")),
        )
        for entry in voucher.findall("ALLLEDGERENTRIES.LIST")
    ]


def statement(*transactions: Transaction, bank: str = "HDFC") -> Statement:
    return Statement(
        source=Path("synthetic.csv"),
        bank=bank,
        account_hint="XXXX4471",
        opening_balance=100_000,
        closing_balance=100_000
        + sum(txn.signed for txn in transactions),
        transactions=transactions,
    )


def txn(row: int, direction: Direction, amount: int, narration: str = "x") -> Transaction:
    return Transaction(
        row=row,
        date=date(2025, 4, row),
        narration=narration,
        direction=direction,
        amount=amount,
    )


# --- the vouchers ------------------------------------------------------------


def test_every_transaction_becomes_one_voucher(clean_statement_path, tmp_path):
    parsed = parse_statement(clean_statement_path)
    out = tmp_path / "vouchers.xml"

    export_tally([parsed], out)

    assert len(vouchers(out)) == len(parsed.transactions)


def test_every_voucher_balances_to_zero(clean_statement_path, tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([parse_statement(clean_statement_path)], out)

    for voucher in vouchers(out):
        assert sum(amount for _, _, amount in entries(voucher)) == 0


def test_a_withdrawal_is_a_payment_out_of_the_bank(tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 41_200))], out, bank_ledger="HDFC Bank A/c")

    (voucher,) = vouchers(out)
    assert voucher.get("VCHTYPE") == "Payment"
    assert entries(voucher) == [
        (SUSPENSE_LEDGER, True, Decimal("-412.00")),
        ("HDFC Bank A/c", False, Decimal("412.00")),
    ]


def test_a_deposit_is_a_receipt_into_the_bank(tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(2, Direction.CREDIT, 8_500_000))], out, bank_ledger="HDFC Bank A/c")

    (voucher,) = vouchers(out)
    assert voucher.get("VCHTYPE") == "Receipt"
    assert entries(voucher) == [
        ("HDFC Bank A/c", True, Decimal("-85000.00")),
        (SUSPENSE_LEDGER, False, Decimal("85000.00")),
    ]


def test_amounts_are_exact_to_the_paisa(tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 123_456_789))], out)

    (voucher,) = vouchers(out)
    assert entries(voucher)[1][2] == Decimal("1234567.89")


def test_dates_are_written_the_way_tally_reads_them(tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(7, Direction.DEBIT, 100))], out)

    assert vouchers(out)[0].findtext("DATE") == "20250407"


def test_the_narration_survives_xml_escaping(tmp_path):
    narration = "NEFT <ACME & SONS> \"Q1\" 50% PAYMENT"
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 100, narration))], out)

    assert vouchers(out)[0].findtext("NARRATION") == narration


def test_a_control_character_cannot_break_the_file(tmp_path):
    # Tally rejects a whole import over one byte XML cannot carry.
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 100, "UPI\x0bSWIGGY\x00"))], out)

    assert vouchers(out)[0].findtext("NARRATION") == "UPISWIGGY"


# --- the ledgers -----------------------------------------------------------


def test_the_suspense_ledger_is_created_under_suspense(tmp_path):
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 100))], out)

    (ledger,) = ET.parse(out).getroot().findall(".//LEDGER")
    assert ledger.get("NAME") == SUSPENSE_LEDGER
    assert ledger.findtext("PARENT") == SUSPENSE_GROUP


def test_the_bank_ledger_is_never_created(tmp_path):
    # It already exists in the client's company with an opening balance, and
    # a master under the same name could alter it.
    out = tmp_path / "vouchers.xml"
    export_tally([statement(txn(1, Direction.DEBIT, 100))], out, bank_ledger="HDFC Bank A/c")

    names = [ledger.get("NAME") for ledger in ET.parse(out).getroot().iter("LEDGER")]
    assert names == [SUSPENSE_LEDGER]


def test_a_named_bank_ledger_is_used_for_every_statement(tmp_path):
    out = tmp_path / "vouchers.xml"
    first = statement(txn(1, Direction.DEBIT, 100), bank="HDFC")
    second = statement(txn(2, Direction.CREDIT, 100), bank="ICICI")

    export_tally([first, second], out, bank_ledger="Client Bank")

    for voucher in vouchers(out):
        assert "Client Bank" in [name for name, _, _ in entries(voucher)]


def test_without_a_name_each_statement_guesses_its_own(tmp_path):
    out = tmp_path / "vouchers.xml"
    first = statement(txn(1, Direction.DEBIT, 100), bank="HDFC")
    second = statement(txn(2, Direction.CREDIT, 100), bank="ICICI")

    export_tally([first, second], out)

    banks = [entries(v)[1][0] if v.get("VCHTYPE") == "Payment" else entries(v)[0][0]
             for v in vouchers(out)]
    assert banks == ["HDFC Bank", "ICICI Bank"]


def test_the_guess_does_not_say_bank_twice():
    assert default_bank_ledger(statement(bank="State Bank Of India")) == "State Bank Of India"
    assert default_bank_ledger(statement(bank="Kotak Mahindra Bank")) == "Kotak Mahindra Bank"
    assert default_bank_ledger(statement(bank="unknown")) == "Bank Account"


# --- the certificate travels with it ---------------------------------------


def test_an_unreconciled_statement_is_still_written_and_says_so(dropped_row_path, tmp_path):
    out = tmp_path / "vouchers.xml"

    certificates = export_tally([parse_statement(dropped_row_path)], out)

    assert not certificates[0].reconciled
    text = out.read_text(encoding="utf-8")
    assert "NOT RECONCILED" in text
    ET.parse(out)  # the comment holding it is still well-formed XML


def test_write_routes_xml_to_tally(clean_statement_path, tmp_path):
    out = tmp_path / "ledger.xml"

    write([parse_statement(clean_statement_path)], out, bank_ledger="HDFC Bank A/c")

    assert ET.parse(out).getroot().tag == "ENVELOPE"


def test_the_cli_writes_a_tally_file(clean_statement_path, tmp_path, capsys):
    out = tmp_path / "vouchers.xml"

    code = main(
        ["export", str(clean_statement_path), "--out", str(out), "--bank-ledger", "HDFC Bank A/c"]
    )

    assert code == 0
    assert vouchers(out)
    assert "Import > Transactions" in capsys.readouterr().out
