"""The export: every row, exact amounts, and a certificate bound to the bytes."""

from __future__ import annotations

import csv
import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from moneytrail import Direction, parse_statement
from moneytrail.cli import main
from moneytrail.export import (
    COLUMNS,
    DIGEST_UNAVAILABLE,
    build_rows,
    certify,
    export_csv,
    export_xlsx,
    render_certificates,
    rupees,
    write,
)


def read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig, matching what the exporter wrote: the BOM is what makes Excel
    # open it as UTF-8, and it must not leak into the first column name.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# --- the ledger ------------------------------------------------------------


def test_every_transaction_survives_the_export(clean_statement_path, tmp_path):
    statement = parse_statement(clean_statement_path)
    out = tmp_path / "ledger.csv"

    export_csv([statement], out)

    assert len(read_csv(out)) == len(statement.transactions)


def test_the_header_is_exactly_the_declared_columns(clean_statement_path, tmp_path):
    out = tmp_path / "ledger.csv"
    export_csv([parse_statement(clean_statement_path)], out)

    assert tuple(read_csv(out)[0]) == COLUMNS


def test_debit_and_credit_are_exclusive(clean_statement_path, tmp_path):
    out = tmp_path / "ledger.csv"
    export_csv([parse_statement(clean_statement_path)], out)

    for row in read_csv(out):
        assert bool(row["debit"]) != bool(row["credit"]), row["narration"]


def test_the_columns_sum_to_what_reconciliation_counted(
    clean_statement_path, tmp_path
):
    """The export must not drift from the arithmetic that certified it.

    Summing the spreadsheet is exactly what the person receiving it will do,
    so the sum has to match the certificate printed beside it -- to the paisa,
    which is why these are Decimals and not floats.
    """
    statement = parse_statement(clean_statement_path)
    out = tmp_path / "ledger.csv"
    certificate = export_csv([statement], out)[0]

    rows = read_csv(out)
    debits = sum(Decimal(row["debit"]) for row in rows if row["debit"])
    credits = sum(Decimal(row["credit"]) for row in rows if row["credit"])

    assert debits == rupees(certificate.debits)
    assert credits == rupees(certificate.credits)


def test_amounts_are_exact_not_floating(tmp_path):
    # 20.10 + 0.20 is not 20.30 in binary. It has to be here.
    assert rupees(2010) + rupees(20) == Decimal("20.30")
    assert rupees(-4550) == Decimal("-45.50")


def test_rows_from_several_statements_are_merged_in_date_order(
    july_bank_path, card_path
):
    statements = [parse_statement(card_path), parse_statement(july_bank_path)]

    dates = [row["date"] for row in build_rows(statements)]

    assert dates == sorted(dates)
    assert len({row["source_file"] for row in build_rows(statements)}) == 2


def test_a_card_row_carries_no_running_balance(card_path):
    rows = list(build_rows([parse_statement(card_path)]))

    assert rows
    assert all(row["balance"] is None for row in rows)


def test_the_narration_is_kept_verbatim_beside_the_resolved_name(
    clean_statement_path,
):
    """The raw narration is the audit trail; the merchant name is a convenience.

    Shipping only the tidy name would make the export unauditable against the
    source PDF, which defeats the certificate sitting next to it.
    """
    rows = list(build_rows([parse_statement(clean_statement_path)]))
    netflix = next(row for row in rows if row["merchant"] == "Netflix")

    assert netflix["narration"].startswith("UPI-NETFLIXINDIA")
    assert netflix["category"] == "entertainment"


# --- the certificate -------------------------------------------------------


def test_a_clean_statement_certifies_reconciled(clean_statement_path):
    certificate = certify(parse_statement(clean_statement_path))

    assert certificate.reconciled
    assert certificate.verdict == "RECONCILED"
    assert certificate.checks == ("chain", "totals")
    assert certificate.failures == ()


def test_a_dropped_row_certifies_not_reconciled_and_names_the_row(dropped_row_path):
    certificate = certify(parse_statement(dropped_row_path))

    assert not certificate.reconciled
    assert certificate.verdict == "NOT RECONCILED"
    assert any("649.00" in failure for failure in certificate.failures)


def test_derived_endpoints_reconcile_but_carry_a_caveat(signed_amount_path):
    certificate = certify(parse_statement(signed_amount_path))

    assert certificate.reconciled
    assert certificate.caveats  # weaker than a clean pass, and says so


def test_a_card_with_nothing_to_check_against_is_not_stamped_reconciled(tmp_path):
    """An unverifiable parse must never read as a pass.

    This is the one that matters: a card statement printing no totals has no
    discrepancies precisely because nothing could be compared, and calling
    that RECONCILED would make the certificate worthless on exactly the
    statements that need it most.
    """
    target = tmp_path / "bare.csv"
    target.write_text(
        "Total Amount Due\n"
        "Date,Transaction Description,Amount\n"
        "03/07/2025,SOME PURCHASE,412.00\n",
        encoding="utf-8",
    )

    certificate = certify(parse_statement(target))

    assert certificate.checks == ()
    assert not certificate.reconciled
    assert certificate.caveats


def test_the_digest_is_of_the_source_bytes(clean_statement_path):
    certificate = certify(parse_statement(clean_statement_path))

    assert certificate.digest == hashlib.sha256(
        Path(clean_statement_path).read_bytes()
    ).hexdigest()


def test_changing_a_byte_changes_the_digest(clean_statement_path, tmp_path):
    copy = tmp_path / "copy.csv"
    copy.write_bytes(Path(clean_statement_path).read_bytes() + b"\n")

    assert certify(parse_statement(copy)).digest != certify(
        parse_statement(clean_statement_path)
    ).digest


def test_a_statement_with_no_file_behind_it_still_certifies(clean_statement_path):
    """Never raise on a missing digest -- absence is a state, not an error."""
    from dataclasses import replace

    statement = replace(parse_statement(clean_statement_path), source=Path("gone.csv"))

    assert certify(statement).digest == DIGEST_UNAVAILABLE


def test_the_document_leads_with_the_failure(clean_statement_path, dropped_row_path):
    certificates = [
        certify(parse_statement(clean_statement_path)),
        certify(parse_statement(dropped_row_path)),
    ]

    text = render_certificates(certificates)
    headline = text.splitlines()[3]

    assert headline.startswith("NOT RECONCILED -- 1 of 2")


# --- formats ---------------------------------------------------------------


def test_csv_is_written_with_a_bom_so_excel_reads_utf8(clean_statement_path, tmp_path):
    out = tmp_path / "ledger.csv"
    export_csv([parse_statement(clean_statement_path)], out)

    assert out.read_bytes().startswith(b"\xef\xbb\xbf")


def test_the_workbook_carries_the_certificate_as_a_second_sheet(
    clean_statement_path, tmp_path
):
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "ledger.xlsx"

    export_xlsx([parse_statement(clean_statement_path)], out)
    book = openpyxl.load_workbook(out)

    assert book.sheetnames == ["Ledger", "Certificate"]
    assert "RECONCILED" in "\n".join(
        str(row[0]) for row in book["Certificate"].iter_rows(values_only=True)
    )


def test_workbook_amounts_are_numbers_not_text(clean_statement_path, tmp_path):
    """An accountant sums the column. Text in it silently sums to zero."""
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "ledger.xlsx"

    statement = parse_statement(clean_statement_path)
    export_xlsx([statement], out)
    sheet = openpyxl.load_workbook(out)["Ledger"]

    debit_column = COLUMNS.index("debit") + 1
    values = [
        row[debit_column - 1]
        for row in sheet.iter_rows(min_row=2, values_only=True)
        if row[debit_column - 1] is not None
    ]

    assert values
    assert all(isinstance(value, (int, float, Decimal)) for value in values)
    assert sum(Decimal(str(value)) for value in values) == rupees(
        sum(
            txn.amount
            for txn in statement.transactions
            if txn.direction is Direction.DEBIT
        )
    )


def test_write_dispatches_on_the_suffix(clean_statement_path, tmp_path):
    pytest.importorskip("openpyxl")
    statements = [parse_statement(clean_statement_path)]

    write(statements, tmp_path / "a.csv")
    write(statements, tmp_path / "b.xlsx")

    assert (tmp_path / "a.csv").exists()
    assert (tmp_path / "b.xlsx").exists()


def test_an_unknown_suffix_is_refused_rather_than_guessed(
    clean_statement_path, tmp_path
):
    with pytest.raises(ValueError, match="csv"):
        write([parse_statement(clean_statement_path)], tmp_path / "ledger.pdf")


# --- the command -----------------------------------------------------------


def test_export_exits_zero_and_writes_a_sidecar(clean_statement_path, tmp_path):
    out = tmp_path / "ledger.csv"

    code = main(["export", str(clean_statement_path), "--out", str(out)])

    assert code == 0
    assert out.exists()
    proof = tmp_path / "ledger.csv.certificate.txt"
    assert "RECONCILED" in proof.read_text(encoding="utf-8")


def test_a_failed_statement_is_still_exported_but_exits_one(
    dropped_row_path, tmp_path, capsys
):
    """Withholding the data would send the user back to a silent converter.

    So the file is written, every row is stamped NO, and the exit code carries
    the failure for anything scripting this.
    """
    out = tmp_path / "ledger.csv"

    code = main(["export", str(dropped_row_path), "--out", str(out)])

    assert code == 1
    rows = read_csv(out)
    assert rows
    assert {row["reconciled"] for row in rows} == {"NO"}
    assert "NOT RECONCILED" in capsys.readouterr().out


def test_the_certificate_path_can_be_chosen(clean_statement_path, tmp_path):
    proof = tmp_path / "proof.txt"

    main(
        [
            "export",
            str(clean_statement_path),
            "--out",
            str(tmp_path / "ledger.csv"),
            "--certificate",
            str(proof),
        ]
    )

    assert "sha-256" in proof.read_text(encoding="utf-8")


def test_an_unwritable_format_is_reported_not_raised(clean_statement_path, tmp_path):
    code = main(
        ["export", str(clean_statement_path), "--out", str(tmp_path / "ledger.pdf")]
    )

    assert code == 2
