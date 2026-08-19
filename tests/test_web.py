"""The hosted front-end: what it promises, and what it refuses."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from moneytrail.report import FAILED, OK, WEAK
from moneytrail.web import (
    MAX_FILE_BYTES,
    MAX_FILES,
    process,
    safe_name,
)


def upload(path) -> tuple[str, bytes]:
    path = Path(path)
    return path.name, path.read_bytes()


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app

    return TestClient(create_app())


# --- the core, without a framework anywhere near it ------------------------


def test_a_clean_statement_comes_back_green_with_a_file(clean_statement_path):
    result = process([upload(clean_statement_path)])

    assert result.ok
    assert result.total == 1
    assert [tile.status for tile in result.tiles] == [OK]
    assert result.data  # the workbook rode home with the verdict
    assert result.filename == "ledger.xlsx"


def test_a_dropped_row_comes_back_red_and_still_carries_the_file(dropped_row_path):
    """The export is not withheld on failure.

    Refusing to hand over the data would send the user straight back to a
    converter that cannot tell them anything is wrong, which is the outcome
    this whole tool exists to prevent.
    """
    result = process([upload(dropped_row_path)])

    assert not result.ok
    assert result.tiles[0].status == FAILED
    assert any("649.00" in note for note in result.tiles[0].notes)
    assert result.data


def test_derived_endpoints_are_amber_not_green(signed_amount_path):
    result = process([upload(signed_amount_path)])

    assert result.tiles[0].status == WEAK
    assert result.tiles[0].notes  # the qualification travels with the tile
    assert result.reconciled == 1


def test_the_tile_carries_the_digest_of_what_was_read(clean_statement_path):
    import hashlib

    result = process([upload(clean_statement_path)])
    expected = hashlib.sha256(Path(clean_statement_path).read_bytes()).hexdigest()

    assert result.tiles[0].digest == expected


def test_a_locked_pdf_is_rejected_with_a_flag_the_page_can_act_on(locked_pdf_path):
    pytest.importorskip("pdfplumber")

    result = process([upload(locked_pdf_path)])

    assert result.total == 0
    assert result.rejected[0].needs_password


def test_the_right_password_opens_it(locked_pdf_path, fixture_password):
    pytest.importorskip("pdfplumber")

    result = process([upload(locked_pdf_path)], password=fixture_password)

    assert result.total == 1
    assert result.rejected == ()


def test_one_bad_file_does_not_lose_the_good_ones(clean_statement_path, tmp_path):
    """A batch of statements must not be lost to a single unreadable file."""
    junk = tmp_path / "notes.csv"
    junk.write_text("this is not a bank statement at all\n", encoding="utf-8")

    result = process([upload(clean_statement_path), upload(junk)])

    assert result.total == 1
    assert len(result.rejected) == 1
    assert not result.ok  # a skipped file still means the batch is not clean


def test_an_unsupported_format_is_named_not_guessed(tmp_path):
    target = tmp_path / "statement.docx"
    target.write_bytes(b"PK\x03\x04not really")

    rejected = process([upload(target)]).rejected[0]

    assert "not a format I read" in rejected.reason


def test_an_empty_file_is_rejected_before_it_reaches_a_parser(tmp_path):
    target = tmp_path / "empty.csv"
    target.write_bytes(b"")

    assert "empty" in process([upload(target)]).rejected[0].reason


def test_an_oversized_file_is_refused(tmp_path):
    result = process([("huge.csv", b"x" * (MAX_FILE_BYTES + 1))])

    assert "MB" in result.rejected[0].reason


def test_too_many_files_at_once_is_refused(clean_statement_path):
    one = upload(clean_statement_path)

    with pytest.raises(ValueError, match="too many"):
        process([one] * (MAX_FILES + 1))


def test_csv_is_offered_as_well_as_excel(clean_statement_path):
    result = process([upload(clean_statement_path)], fmt="csv")

    assert result.filename == "ledger.csv"
    assert result.data.startswith(b"\xef\xbb\xbf")  # still opens right in Excel


def test_an_unknown_format_is_refused(clean_statement_path):
    with pytest.raises(ValueError, match="xlsx or csv"):
        process([upload(clean_statement_path)], fmt="pdf")


def test_two_files_sharing_a_name_do_not_overwrite_each_other(
    clean_statement_path, july_bank_path
):
    """Uploads land in per-file directories, so a collision is impossible."""
    _, first = upload(clean_statement_path)
    _, second = upload(july_bank_path)

    result = process([("statement.csv", first), ("statement.csv", second)])

    assert result.total == 2


# --- the promise ------------------------------------------------------------


def test_nothing_survives_the_request(clean_statement_path, monkeypatch):
    """The scratch directory is gone by the time the result is returned.

    This is the whole privacy claim, so it is asserted rather than trusted:
    every temporary directory created during the call must no longer exist.
    """
    import tempfile

    created: list[str] = []
    real = tempfile.TemporaryDirectory

    class Watched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Watched)

    process([upload(clean_statement_path)])

    assert created
    assert not any(Path(name).exists() for name in created)


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/absolute/path/statement.csv",
        "C:\\Users\\someone\\statement.csv",
    ],
)
def test_an_upload_cannot_escape_the_scratch_directory(raw):
    """The filename is attacker-controlled and is used to build a path.

    Both separators are stripped regardless of host platform: a Windows server
    must still refuse a POSIX traversal, and the reverse.
    """
    name = safe_name(raw)

    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")


def test_a_nameless_upload_still_gets_a_name():
    assert safe_name("") == "statement"
    assert safe_name("...") == "statement"


# --- the endpoint -----------------------------------------------------------


def test_the_page_is_served_and_is_self_contained(client):
    """No CDN, no analytics, no external anything -- checkable by reading it.

    The offline HTML report holds the same line. A privacy claim on a page
    that phones out to a font host is not a privacy claim.
    """
    body = client.get("/").text

    assert "<title>moneytrail" in body
    for forbidden in ("http://", "https://", "src=", "@import"):
        assert forbidden not in body


def test_health_answers(client):
    assert client.get("/health").json() == {"ok": True}


def test_posting_a_statement_returns_the_verdict_and_the_workbook(
    client, clean_statement_path
):
    response = client.post(
        "/api/export",
        files=[("files", (clean_statement_path.name, clean_statement_path.read_bytes()))],
        data={"fmt": "xlsx"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["reconciled"] == payload["total"] == 1
    assert payload["tiles"][0]["status"] == OK
    assert "RECONCILED" in payload["certificate"]
    # Real bytes, not a placeholder: a workbook is a zip.
    assert base64.b64decode(payload["data"]).startswith(b"PK")


def test_a_failed_statement_answers_200_with_a_red_tile(client, dropped_row_path):
    """Not an HTTP error. The request worked; the statement did not add up."""
    response = client.post(
        "/api/export",
        files=[("files", (dropped_row_path.name, dropped_row_path.read_bytes()))],
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["tiles"][0]["status"] == FAILED


def test_nothing_readable_answers_422(client, tmp_path):
    response = client.post(
        "/api/export", files=[("files", ("notes.docx", b"not a statement"))]
    )

    assert response.status_code == 422
    assert response.json()["rejected"][0]["filename"] == "notes.docx"


def test_too_many_files_answers_413(client, clean_statement_path):
    blob = clean_statement_path.read_bytes()
    files = [("files", (f"s{i}.csv", blob)) for i in range(MAX_FILES + 1)]

    assert client.post("/api/export", files=files).status_code == 413


def test_an_unknown_format_answers_400(client, clean_statement_path):
    response = client.post(
        "/api/export",
        files=[("files", ("s.csv", clean_statement_path.read_bytes()))],
        data={"fmt": "docx"},
    )

    assert response.status_code == 400
    assert "error" in response.json()
