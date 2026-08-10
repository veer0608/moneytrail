"""The CLI is the only surface a user touches. It must never traceback."""

from __future__ import annotations

import re
from pathlib import Path

from moneytrail.cli import main
from moneytrail.parsers import supported_suffixes


def test_clean_statement_exits_zero(clean_statement_path, capsys):
    assert main(["check", str(clean_statement_path)]) == 0
    assert "RECONCILED to the paisa" in capsys.readouterr().out


def test_failing_statement_exits_one(dropped_row_path, capsys):
    assert main(["check", str(dropped_row_path)]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_missing_file_is_reported_not_raised(tmp_path: Path, capsys):
    missing = tmp_path / "not-here.csv"

    assert main(["check", str(missing)]) == 1

    out = capsys.readouterr().out
    assert "NOT FOUND" in out
    assert "Traceback" not in out


def test_unreadable_file_is_reported_not_raised(tmp_path: Path, capsys):
    junk = tmp_path / "junk.csv"
    junk.write_text("this is not a statement\n", encoding="utf-8")

    assert main(["check", str(junk)]) == 1
    assert "UNREADABLE" in capsys.readouterr().out


def test_directory_ignores_files_no_parser_could_read(tmp_path: Path, capsys):
    (tmp_path / "holiday.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "notes.docx").write_bytes(b"PK\x03\x04")

    assert main(["check", str(tmp_path)]) == 2

    out = capsys.readouterr().out
    assert "nothing to check" in out
    assert "holiday.jpg" not in out


def test_directory_checks_every_statement_inside(capsys):
    fixtures = Path(__file__).parent / "fixtures"
    readable = [p for p in fixtures.iterdir() if p.suffix.lower() in supported_suffixes()]

    assert main(["check", str(fixtures)]) == 1  # some fixtures fail on purpose

    out = capsys.readouterr().out
    assert re.search(rf"\d+/{len(readable)} statements reconciled", out)
    assert "hdfc_april_2025.csv" in out
    assert "hdfc_april_2025.pdf" in out


def test_locked_pdf_is_reported_not_raised(locked_pdf_path, capsys):
    # Non-interactive, so there is no prompt to fall back to.
    assert main(["check", str(locked_pdf_path)]) == 1

    out = capsys.readouterr().out
    assert "LOCKED" in out
    assert "Traceback" not in out


def test_no_prompt_never_blocks_on_a_locked_file(locked_pdf_path, capsys):
    assert main(["check", str(locked_pdf_path), "--no-prompt"]) == 1
    assert "LOCKED" in capsys.readouterr().out


def test_password_flag_unlocks_a_pdf(locked_pdf_path, fixture_password, capsys):
    assert main(["check", str(locked_pdf_path), "--password", fixture_password]) == 0
    assert "RECONCILED to the paisa" in capsys.readouterr().out
