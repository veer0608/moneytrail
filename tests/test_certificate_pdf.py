"""The certificate as a page: the same proof, for someone who will not read a terminal.

These tests read the rendered PDF back rather than trusting that it was
written. A file that opens is not the claim being made -- the claim is that the
verdict, the failing row numbers and the full digest all survive onto the page,
because a certificate that quietly loses one of those is worse than none.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

import pytest

from moneytrail import parse_statement
from moneytrail.certificate import Typeface, render_pdf, typeset
from moneytrail.cli import main
from moneytrail.export import certify
from moneytrail.models import CardStatement, CardSummary, Direction, Transaction

pdfplumber = pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")


def page_text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def certificates_for(*paths: Path) -> list:
    return [certify(parse_statement(path)) for path in paths]


# --- the file itself -------------------------------------------------------


def test_writes_something_a_reader_will_open(clean_statement_path, tmp_path):
    out = render_pdf(certificates_for(clean_statement_path), tmp_path / "cert.pdf")

    assert out.exists()
    assert out.read_bytes()[:5] == b"%PDF-"


def test_nothing_to_certify_is_refused(tmp_path):
    # Rendering an empty certificate would produce a page stamped RECONCILED
    # with nothing on it, which is the most misleading document this codebase
    # could emit.
    with pytest.raises(ValueError):
        render_pdf([], tmp_path / "cert.pdf")


# --- the verdict -----------------------------------------------------------


def test_a_clean_statement_reads_as_reconciled(clean_statement_path, tmp_path):
    out = render_pdf(certificates_for(clean_statement_path), tmp_path / "cert.pdf")

    text = page_text(out)
    assert "RECONCILED" in text
    assert "NOT RECONCILED" not in text


def test_a_dropped_row_is_named_on_the_page(dropped_row_path, tmp_path):
    out = render_pdf(certificates_for(dropped_row_path), tmp_path / "cert.pdf")

    text = page_text(out)
    assert "NOT RECONCILED" in text
    # The row number is the whole point: a page that says "does not add up"
    # without saying where is diligence theatre.
    assert "row 10" in text
    assert "649.00" in text


def test_the_page_agrees_with_the_certificate_it_renders(
    clean_statement_path, dropped_row_path, card_path, tmp_path
):
    certificates = certificates_for(clean_statement_path, dropped_row_path, card_path)
    out = render_pdf(certificates, tmp_path / "cert.pdf")

    text = page_text(out)
    for certificate in certificates:
        assert Path(certificate.source).name in text
    assert "NOT RECONCILED" in text


def test_an_unverifiable_card_is_never_stamped_reconciled(tmp_path):
    """A card that prints no totals has nothing to check, so it cannot pass.

    Guarded here as well as in the export because this is the page a client
    actually receives, and "no discrepancies found" is exactly how a silent
    converter would word a parse it could not verify.
    """
    statement = CardStatement(
        source=tmp_path / "no_totals.csv",
        issuer="HDFC",
        account_hint="XXXX1234",
        summary=CardSummary(),
        transactions=(
            Transaction(
                row=2,
                date=date(2025, 7, 4),
                narration="SWIGGY",
                direction=Direction.DEBIT,
                amount=45000,
            ),
        ),
    )
    certificate = certify(statement)
    assert not certificate.reconciled

    out = render_pdf([certificate], tmp_path / "cert.pdf")
    assert "NOT RECONCILED" in page_text(out)


# --- the digest ------------------------------------------------------------


def test_the_digest_lands_on_the_page_in_full(clean_statement_path, tmp_path):
    out = render_pdf(certificates_for(clean_statement_path), tmp_path / "cert.pdf")

    digest = hashlib.sha256(Path(clean_statement_path).read_bytes()).hexdigest()
    # It wraps mid-string by design, so the whitespace comes out before the
    # comparison. An abbreviated digest cannot be checked against anything.
    assert digest in re.sub(r"\s+", "", page_text(out))


# --- money on the page -----------------------------------------------------


def test_the_fallback_never_swallows_the_currency():
    """Without an embeddable Unicode font the amount still says what it is.

    reportlab's built-in Helvetica has no glyph at U+20B9, so drawing the rupee
    sign with it produces a blank or a box depending on the reader. On an
    Indian statement that is the one character that must not go missing, so the
    fallback spells it instead of dropping it.
    """
    fallback = Typeface("Helvetica", "Helvetica-Bold", rupee=False)
    assert fallback.money(12345678) == "INR 1,23,456.78"

    embedded = Typeface("whatever", "whatever-bold", rupee=True)
    assert embedded.money(12345678) == "₹1,23,456.78"


def test_amounts_reach_the_page(clean_statement_path, tmp_path):
    out = render_pdf(certificates_for(clean_statement_path), tmp_path / "cert.pdf")

    text = page_text(out)
    for amount in ("45,231.60", "87,499.00", "36,560.00", "96,170.60"):
        assert amount in text


# --- prose -----------------------------------------------------------------


def test_statement_text_reaches_the_page_as_text():
    # Narrations arrive from the file and reach a markup-aware renderer, so
    # they are data, not markup.
    assert typeset("PAY <b>&</b> GO") == "PAY &lt;b&gt;&amp;&lt;/b&gt; GO"
    # And the prose is left alone: the em dashes came out of the README and
    # the served page on purpose, so the certificate does not reintroduce them.
    assert typeset("row 4 -- NETFLIX") == "row 4 -- NETFLIX"


# --- the CLI ---------------------------------------------------------------


def test_check_writes_the_certificate_as_a_page(clean_statement_path, tmp_path):
    out = tmp_path / "cert.pdf"
    code = main(["check", str(clean_statement_path), "--certificate", str(out)])

    assert code == 0
    assert out.exists()
    assert "RECONCILED" in page_text(out)


def test_check_still_exits_non_zero_when_it_did_not_add_up(dropped_row_path, tmp_path):
    out = tmp_path / "cert.pdf"
    code = main(["check", str(dropped_row_path), "--certificate", str(out)])

    # The page is written either way -- withholding it helps nobody -- but the
    # exit code still has to carry the failure for anything scripting this.
    assert code == 1
    assert "NOT RECONCILED" in page_text(out)


def test_check_without_the_flag_writes_nothing(clean_statement_path, tmp_path):
    main(["check", str(clean_statement_path)])
    assert list(tmp_path.iterdir()) == []


def test_the_suffix_picks_the_form(clean_statement_path, tmp_path):
    text_out = tmp_path / "cert.txt"
    main(["check", str(clean_statement_path), "--certificate", str(text_out)])

    assert text_out.read_text(encoding="utf-8").startswith(
        "moneytrail reconciliation certificate"
    )


def test_export_can_take_its_certificate_as_a_page(clean_statement_path, tmp_path):
    ledger = tmp_path / "ledger.csv"
    proof = tmp_path / "proof.pdf"
    code = main(
        [
            "export",
            str(clean_statement_path),
            "--out",
            str(ledger),
            "--certificate",
            str(proof),
        ]
    )

    assert code == 0
    assert ledger.exists()
    assert "RECONCILED" in page_text(proof)
    # The text sidecar is not written as well: one certificate was asked for.
    assert not (tmp_path / "ledger.csv.certificate.txt").exists()
