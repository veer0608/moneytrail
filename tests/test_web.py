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


# --- facing the internet ----------------------------------------------------


def test_a_budget_refuses_once_it_is_spent():
    from moneytrail.web import RateLimit

    limit = RateLimit(allowance=3, per_seconds=60.0)

    assert [limit.check("1.2.3.4", now=0.0) for _ in range(4)] == [
        True, True, True, False
    ]


def test_the_budget_refills_as_the_window_slides():
    from moneytrail.web import RateLimit

    limit = RateLimit(allowance=2, per_seconds=60.0)
    limit.check("1.2.3.4", now=0.0)
    limit.check("1.2.3.4", now=1.0)

    assert not limit.check("1.2.3.4", now=30.0)
    assert limit.check("1.2.3.4", now=61.0)  # the first hit has aged out


def test_one_caller_cannot_spend_anothers_budget():
    from moneytrail.web import RateLimit

    limit = RateLimit(allowance=1, per_seconds=60.0)

    assert limit.check("1.2.3.4", now=0.0)
    assert limit.check("5.6.7.8", now=0.0)
    assert not limit.check("1.2.3.4", now=0.0)


def test_idle_callers_are_forgotten_rather_than_accumulated():
    """Otherwise the limiter is a slow leak keyed by every address ever seen."""
    from moneytrail.web import RateLimit

    limit = RateLimit(allowance=1, per_seconds=1.0)
    for i in range(5000):
        limit.check(f"10.0.{i // 256}.{i % 256}", now=0.0)

    limit.check("1.2.3.4", now=10_000.0)

    assert len(limit._hits) < 4096


def test_the_forwarded_client_is_preferred_over_the_proxy():
    from moneytrail.web import client_key

    assert client_key("203.0.113.9, 10.0.0.1", "10.0.0.1") == "203.0.113.9"
    assert client_key(None, "10.0.0.1") == "10.0.0.1"
    assert client_key("", None) == "unknown"


def test_the_endpoint_answers_429_once_the_budget_is_spent(clean_statement_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app
    from moneytrail.web import RateLimit

    client = TestClient(create_app(limit=RateLimit(allowance=1, per_seconds=60.0)))
    blob = clean_statement_path.read_bytes()
    files = [("files", ("s.csv", blob))]

    assert client.post("/api/export", files=files).status_code == 200
    assert client.post("/api/export", files=files).status_code == 429


def test_an_oversized_body_is_refused_before_it_is_read(client):
    """Declared, not sent: the point is that nothing buffers it."""
    from moneytrail.web import MAX_BODY_BYTES

    response = client.post(
        "/api/export",
        content=b"x" * 32,
        headers={
            "content-type": "multipart/form-data; boundary=x",
            "content-length": str(MAX_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413


def test_health_is_never_rate_limited(client):
    """The platform pings this constantly; a 429 would read as a dead service."""
    assert all(client.get("/health").status_code == 200 for _ in range(30))


# --- the landing page -------------------------------------------------------


def test_the_sample_is_a_statement_that_does_not_reconcile(client):
    """The demo has to fail, or it demonstrates nothing.

    A visitor will not upload their own bank statement to prove the point, so
    the page proves it with a file of its own -- and that file must be one the
    reconciler actually catches, not a plausible-looking prop.
    """
    from moneytrail.web import process

    body = client.get("/api/sample").json()
    result = process([(body["filename"], body["content"].encode())])

    assert result.total == 1
    assert result.tiles[0].status == FAILED
    assert any("649.00" in note for note in result.tiles[0].notes)


def test_the_sample_looks_plausible_until_it_is_checked(client):
    """Its own closing balance agrees with its own last row.

    That is what makes the demo honest: nothing about the file looks wrong
    until the totals are checked against the opening balance.
    """
    body = client.get("/api/sample").json()
    lines = [l for l in body["content"].splitlines() if l.strip()]

    assert lines[-1].endswith("96170.60")  # stated close
    assert "96170.60" in lines[-2]  # and the last row's running balance


def test_pricing_carries_what_the_page_cannot_hardcode(client, monkeypatch):
    body = client.get("/api/pricing").json()

    assert "price" in body and "period" in body
    assert body["source_url"].startswith("https://")


def test_no_price_is_shown_rather_than_an_invented_one(client, monkeypatch):
    monkeypatch.delenv("MONEYTRAIL_PRICE", raising=False)

    assert client.get("/api/pricing").json()["price"] == ""


def test_the_page_still_contains_no_absolute_url(client):
    """Every outbound link is handed over at runtime instead.

    Keeping this mechanical is the point: "loads nothing from anywhere else"
    stays checkable by reading the file, rather than by trusting that whoever
    added a link thought about it.
    """
    body = client.get("/").text

    assert "http://" not in body
    assert "https://" not in body
    assert "src=" not in body
    assert "@import" not in body


# --- the v1 API -------------------------------------------------------------


@pytest.fixture
def keyed_client():
    """A client whose gate accepts one key, so auth can be exercised."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app
    from moneytrail.licence import Licences

    def verify(product_id, key):
        return (key == "GOOD", "" if key == "GOOD" else "that key is not valid")

    return TestClient(create_app(licences=Licences("p", paid_files=25, verify=verify)))


def post_v1(client, path, headers=None, **kwargs):
    return client.post("/api/v1/reconcile", headers=headers or {}, **kwargs)


def test_amounts_cross_the_api_as_integer_paise(client, clean_statement_path):
    """Never a float, never a formatted string.

    A caller handed "₹1,234.00" has to strip a currency mark and parse a
    decimal, and will eventually do it with a float -- which is the one thing
    this project's promise cannot survive. The unit is named in the response so
    nobody has to guess which it is.
    """
    body = post_v1(
        client, "/api/v1/reconcile",
        files=[("files", ("s.csv", clean_statement_path.read_bytes()))],
    ).json()

    assert body["amounts_in"] == "paise"
    assert body["currency"] == "INR"
    row = body["transactions"][0]
    assert isinstance(row["amount"], int)
    assert isinstance(body["statements"][0]["totals"]["credits"], int)


def test_the_api_shape_is_structured_not_prose(client, dropped_row_path):
    """A pipeline branches on `reconciled`; it should not have to read English."""
    response = post_v1(
        client, "/api/v1/reconcile",
        files=[("files", ("s.csv", dropped_row_path.read_bytes()))],
    )
    body = response.json()

    assert response.status_code == 200
    assert body["reconciled"] is False
    assert body["statements"][0]["reconciled"] is False
    assert body["statements"][0]["failures"]
    assert body["counts"]["transactions"] == len(body["transactions"])


def test_dates_are_iso(client, clean_statement_path):
    body = post_v1(
        client, "/api/v1/reconcile",
        files=[("files", ("s.csv", clean_statement_path.read_bytes()))],
    ).json()

    assert body["transactions"][0]["date"].count("-") == 2
    assert body["statements"][0]["period"]["start"].count("-") == 2


def test_the_workbook_is_not_sent_unless_it_is_asked_for(client, clean_statement_path):
    """Most callers want rows. Shipping a base64 workbook to all of them is
    bytes nobody reads."""
    blob = clean_statement_path.read_bytes()

    without = post_v1(client, "/api/v1/reconcile", files=[("files", ("s.csv", blob))]).json()
    with_it = post_v1(
        client, "/api/v1/reconcile",
        files=[("files", ("s.csv", blob))],
        data={"include_workbook": "true"},
    ).json()

    assert "workbook" not in without
    assert with_it["workbook"]["filename"] == "ledger.xlsx"
    assert with_it["workbook"]["base64"]


def test_a_bearer_token_unlocks_a_batch(keyed_client, clean_statement_path, july_bank_path):
    files = [
        ("files", ("a.csv", clean_statement_path.read_bytes())),
        ("files", ("b.csv", july_bank_path.read_bytes())),
    ]

    refused = post_v1(keyed_client, "/api/v1/reconcile", files=files)
    allowed = post_v1(
        keyed_client, "/api/v1/reconcile",
        headers={"Authorization": "Bearer GOOD"},
        files=files,
    )

    assert refused.status_code == 402
    assert allowed.status_code == 200
    assert allowed.json()["licence"]["licensed"] is True


def test_the_header_is_read_case_insensitively(keyed_client, clean_statement_path):
    """`Bearer`, `bearer`, and the plain header all reach the same gate."""
    blob = clean_statement_path.read_bytes()

    for headers in (
        {"Authorization": "bearer GOOD"},
        {"Authorization": "BEARER GOOD"},
        {"X-Moneytrail-Key": "GOOD"},
    ):
        body = post_v1(
            keyed_client, "/api/v1/reconcile", headers=headers,
            files=[("files", ("s.csv", blob))],
        ).json()
        assert body["licence"]["licensed"] is True, headers


def test_the_api_is_documented(client):
    """An API without documentation is not an API.

    The interactive docs are off at the product surface and on under /api/v1,
    which is the whole reversal: the page at / stays what a person meets first.
    """
    schema = client.get("/api/v1/openapi.json")

    assert schema.status_code == 200
    assert "/api/v1/reconcile" in schema.json()["paths"]
    assert client.get("/api/v1/docs").status_code == 200
