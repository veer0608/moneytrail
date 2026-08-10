from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def clean_statement_path() -> Path:
    return FIXTURES / "hdfc_april_2025.csv"


@pytest.fixture
def dropped_row_path() -> Path:
    """Same statement with the Netflix row (Rs 649.00) deleted.

    This is the realistic parser failure: a row swallowed by a wrapped
    narration or a page break.
    """
    return FIXTURES / "hdfc_april_2025_dropped_row.csv"


@pytest.fixture
def signed_amount_path() -> Path:
    return FIXTURES / "icici_may_2025_signed.csv"


@pytest.fixture
def pdf_path() -> Path:
    """The same statement as `clean_statement_path`, rendered as a ruled PDF."""
    return FIXTURES / "hdfc_april_2025.pdf"


@pytest.fixture
def locked_pdf_path() -> Path:
    return FIXTURES / "hdfc_april_2025_locked.pdf"


@pytest.fixture
def spreadsheet_path() -> Path:
    """The same statement again, as a workbook full of real-world quirks."""
    return FIXTURES / "hdfc_april_2025.xls"


@pytest.fixture
def card_path() -> Path:
    """A credit-card statement: summary box, no running balance, Cr suffixes."""
    return FIXTURES / "hdfc_card_july_2025.csv"


@pytest.fixture
def fixture_password() -> str:
    """Unlocks a synthetic fixture and nothing else."""
    return "test1234"
