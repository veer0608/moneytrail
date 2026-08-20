"""The certificate as a page, over HTTP.

The hosted promise is narrow and exact: the bytes exist inside one request and
nothing is kept. The page has to reach the browser without weakening that, so
it rides home inside the response that produced it -- there is no second
request to serve, because there is nothing left to serve it from.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from moneytrail.web import CERTIFICATE_NAME, process

pytest.importorskip("reportlab")


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


def page_text(data: bytes, tmp_path: Path) -> str:
    pdfplumber = pytest.importorskip("pdfplumber")
    out = tmp_path / "downloaded.pdf"
    out.write_bytes(data)
    with pdfplumber.open(out) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


# --- the core --------------------------------------------------------------


def test_the_page_rides_home_with_the_workbook(clean_statement_path):
    result = process([upload(clean_statement_path)])

    assert result.certificate_pdf[:5] == b"%PDF-"
    # And the text certificate is still there: the page is an addition, not a
    # replacement, and anything already reading the text must not break.
    assert result.certificate_text.startswith("moneytrail reconciliation certificate")


def test_the_page_carries_the_same_verdict_as_the_strip(dropped_row_path, tmp_path):
    result = process([upload(dropped_row_path)])

    assert not result.ok
    text = page_text(result.certificate_pdf, tmp_path)
    assert "NOT RECONCILED" in text
    assert "row 10" in text


def test_a_batch_gets_one_page_covering_all_of_it(
    clean_statement_path, dropped_row_path, tmp_path
):
    result = process([upload(clean_statement_path), upload(dropped_row_path)])

    text = page_text(result.certificate_pdf, tmp_path)
    assert Path(clean_statement_path).name in text
    assert Path(dropped_row_path).name in text


def test_nothing_readable_means_no_page(tmp_path):
    """A refused upload must not produce a certificate of anything.

    `process` returns before it certifies when nothing parsed, and this is the
    guard on that: a page rendered from an empty list would be stamped
    RECONCILED over no evidence.
    """
    result = process([("notes.txt", b"this is not a statement")])

    assert result.total == 0
    assert result.certificate_pdf == b""


def test_an_instance_without_the_renderer_still_reconciles(
    clean_statement_path, monkeypatch
):
    """The renderer is an optional extra, so its absence costs the button only.

    Simulated by making the import fail, which is what a `[web]`-only install
    actually does.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "reportlab" or name.startswith("reportlab."):
            raise ImportError("no reportlab here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    result = process([upload(clean_statement_path)])

    assert result.ok
    assert result.data  # the workbook still came back
    assert result.certificate_text  # and so did the proof, as text
    assert result.certificate_pdf == b""


# --- what the page in the browser receives ---------------------------------


def test_export_hands_the_browser_a_pdf_it_can_save(client, clean_statement_path):
    with open(clean_statement_path, "rb") as handle:
        response = client.post("/api/export", files={"files": handle})

    assert response.status_code == 200
    body = response.json()
    assert body["certificate_pdf_name"] == CERTIFICATE_NAME
    assert base64.b64decode(body["certificate_pdf"])[:5] == b"%PDF-"


def test_the_name_is_empty_when_there_is_nothing_to_name(client):
    response = client.post(
        "/api/export", files={"files": ("notes.txt", b"not a statement")}
    )

    body = response.json()
    assert body["certificate_pdf"] == ""
    # The page keys the button off the name, so it must not offer one.
    assert body["certificate_pdf_name"] == ""


# --- the v1 API ------------------------------------------------------------


def test_v1_leaves_the_page_out_unless_it_is_asked_for(client, clean_statement_path):
    with open(clean_statement_path, "rb") as handle:
        response = client.post("/api/v1/reconcile", files={"files": handle})

    assert "certificate_page" not in response.json()


def test_v1_returns_the_page_when_asked(client, clean_statement_path):
    with open(clean_statement_path, "rb") as handle:
        response = client.post(
            "/api/v1/reconcile",
            files={"files": handle},
            data={"include_certificate": "true"},
        )

    page = response.json()["certificate_page"]
    assert page["filename"] == CERTIFICATE_NAME
    assert page["content_type"] == "application/pdf"
    assert base64.b64decode(page["base64"])[:5] == b"%PDF-"


# --- the promise -----------------------------------------------------------


def test_the_page_is_never_behind_the_licence(clean_statement_path, tmp_path):
    """No key, free tier, one statement -- and the proof still arrives whole.

    `licence.py` charges for volume, never for the certificate. A free tier
    without it would be one more silent converter, which is the thing this
    exists not to be.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app
    from moneytrail.licence import Licences

    gate = TestClient(
        create_app(licences=Licences("product", paid_files=25, verify=lambda key: None))
    )
    with open(clean_statement_path, "rb") as handle:
        response = gate.post("/api/export", files={"files": handle})

    body = response.json()
    assert body["licence"]["licensed"] is False
    assert base64.b64decode(body["certificate_pdf"])[:5] == b"%PDF-"


def test_the_served_page_offers_the_download_without_a_second_request():
    """The button saves a Blob built from the response, not a URL to fetch.

    A download endpoint would need the file to still exist somewhere after the
    request that made it, and there is deliberately nowhere for it to be.
    """
    from moneytrail.web import STATIC

    markup = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "save-page" in markup
    assert "certificate_pdf" in markup
    # Same rule the rest of the page is held to: nothing absolute in it.
    assert not re.search(r"https?://", markup)
