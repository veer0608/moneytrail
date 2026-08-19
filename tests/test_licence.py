"""Who has paid: the gate, and the cases that cost money when they are wrong."""

from __future__ import annotations

import pytest

from moneytrail.licence import (
    FREE_FILES,
    Licences,
    read_gumroad,
)

PAID = 25


def gate(answers=None, **kwargs) -> Licences:
    """A gate whose verification is a dict lookup instead of the network."""
    answers = answers or {"good-key": (True, "")}
    calls: list[str] = []

    def verify(product_id: str, key: str) -> tuple[bool, str]:
        calls.append(key)
        if key not in answers:
            return False, "that key is not valid for this product"
        answer = answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer

    licences = Licences("prod_1", paid_files=PAID, verify=verify, **kwargs)
    licences.calls = calls  # type: ignore[attr-defined]
    return licences


# --- the tiers --------------------------------------------------------------


def test_no_product_configured_unlocks_everything():
    """Self-hosting and the CLI must never meet a paywall.

    The gate exists on the hosted service and nowhere else. Someone who cloned
    this repo owns it, and charging them would be charging for their own CPU.
    """
    licences = Licences(None, paid_files=PAID)

    entitlement = licences.entitlement(None)

    assert licences.selling is False
    assert entitlement.licensed
    assert entitlement.files == PAID


def test_no_key_is_the_free_tier_not_a_refusal():
    entitlement = gate().entitlement("")

    assert entitlement.files == FREE_FILES
    assert not entitlement.licensed
    assert not entitlement.refused  # nothing went wrong; they just have not paid


def test_a_good_key_raises_the_limit():
    entitlement = gate().entitlement("good-key")

    assert entitlement.licensed
    assert entitlement.files == PAID


def test_a_bad_key_falls_back_and_says_why():
    entitlement = gate().entitlement("nonsense")

    assert entitlement.files == FREE_FILES
    assert entitlement.refused
    assert "not valid" in entitlement.problem


def test_whitespace_around_a_key_does_not_break_it():
    assert gate().entitlement("  good-key  ").licensed


# --- the ones that cost money -----------------------------------------------


@pytest.mark.parametrize(
    "purchase, expected",
    [
        ({"refunded": True}, "refunded"),
        ({"disputed": True}, "disputed"),
        ({"chargebacked": True}, "charged back"),
        ({"subscription_cancelled_at": "2026-01-01"}, "cancelled"),
        ({"subscription_failed_at": "2026-01-01"}, "last payment failed"),
    ],
)
def test_a_purchase_that_came_undone_does_not_still_entitle(purchase, expected):
    """Gumroad answers `success: true` for a refunded purchase.

    Reading that as "has paid" leaves anyone who took their money back with
    permanent access, and a cancelled subscription paying for nothing forever.
    This is the single most expensive thing in this module to get wrong.
    """
    good, problem = read_gumroad({"success": True, "purchase": purchase})

    assert not good
    assert expected in problem


def test_a_clean_purchase_entitles():
    good, problem = read_gumroad({"success": True, "purchase": {"refunded": False}})

    assert good
    assert problem == ""


def test_an_unsuccessful_response_is_refused():
    good, _ = read_gumroad({"success": False, "message": "does not exist"})

    assert not good


def test_a_response_with_no_purchase_block_is_still_read():
    """Absence of the block must not crash the request that pays the bills."""
    good, _ = read_gumroad({"success": True})

    assert good


# --- caching ----------------------------------------------------------------


def test_a_verdict_is_reused_rather_than_re_asked(monkeypatch):
    licences = gate(cache_seconds=300.0)

    licences.entitlement("good-key", now=0.0)
    licences.entitlement("good-key", now=10.0)

    assert licences.calls == ["good-key"]  # asked once


def test_the_verdict_is_re_asked_once_it_goes_stale():
    licences = gate(cache_seconds=300.0)

    licences.entitlement("good-key", now=0.0)
    licences.entitlement("good-key", now=301.0)

    assert licences.calls == ["good-key", "good-key"]


def test_idle_keys_are_forgotten():
    licences = gate(cache_seconds=1.0, grace_seconds=10.0)
    for i in range(5000):
        licences.entitlement(f"key-{i}", now=0.0)

    licences.entitlement("good-key", now=1_000.0)

    assert len(licences._seen) < 4096


# --- when Gumroad is down ---------------------------------------------------


def test_a_recently_verified_key_survives_an_outage():
    """A payment processor outage must not lock out someone who paid."""
    answers = {"good-key": (True, "")}
    licences = gate(answers, cache_seconds=300.0, grace_seconds=86_400.0)
    licences.entitlement("good-key", now=0.0)

    answers["good-key"] = OSError("gumroad is down")
    entitlement = licences.entitlement("good-key", now=400.0)

    assert entitlement.licensed
    assert entitlement.files == PAID


def test_an_unknown_key_during_an_outage_is_asked_to_retry_not_told_it_is_bad():
    """These are different sentences and have to stay different.

    "That key is not valid" sends a paying customer to support. "Try again
    shortly" sends them back in a minute. Collapsing the two turns every
    outage into a stream of refund requests.
    """
    licences = gate({"good-key": OSError("gumroad is down")})

    entitlement = licences.entitlement("good-key", now=0.0)

    assert not entitlement.licensed
    assert "try again" in entitlement.problem
    assert "not valid" not in entitlement.problem


def test_the_grace_period_does_expire():
    answers = {"good-key": (True, "")}
    licences = gate(answers, cache_seconds=300.0, grace_seconds=3600.0)
    licences.entitlement("good-key", now=0.0)

    answers["good-key"] = OSError("still down")
    entitlement = licences.entitlement("good-key", now=7200.0)

    assert not entitlement.licensed


def test_an_outage_does_not_upgrade_a_key_that_was_already_refused():
    answers = {}
    licences = gate(answers, cache_seconds=1.0)
    assert not licences.entitlement("bad-key", now=0.0).licensed

    answers["bad-key"] = OSError("down")
    assert not licences.entitlement("bad-key", now=100.0).licensed


# --- the endpoint -----------------------------------------------------------


@pytest.fixture
def paid_client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app

    monkeypatch.setenv("MONEYTRAIL_BUY_URL", "https://example.test/buy")
    return TestClient(create_app(licences=gate()))


def test_pricing_describes_the_tiers(paid_client):
    body = paid_client.get("/api/pricing").json()

    assert body["selling"] is True
    assert body["free_files"] == FREE_FILES
    assert body["paid_files"] == PAID
    assert body["buy_url"] == "https://example.test/buy"


def test_pricing_on_a_self_hosted_instance_sells_nothing():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from moneytrail.api import create_app

    client = TestClient(create_app(licences=Licences(None, paid_files=PAID)))

    assert client.get("/api/pricing").json()["selling"] is False


def test_one_statement_is_free(paid_client, clean_statement_path):
    response = paid_client.post(
        "/api/export",
        files=[("files", ("s.csv", clean_statement_path.read_bytes()))],
    )

    assert response.status_code == 200
    assert response.json()["licence"]["licensed"] is False


def test_a_batch_without_a_key_answers_402_and_points_at_the_shop(
    paid_client, clean_statement_path
):
    """402, not 413. The request is not too big -- it is unpaid."""
    blob = clean_statement_path.read_bytes()

    response = paid_client.post(
        "/api/export",
        files=[("files", ("a.csv", blob)), ("files", ("b.csv", blob))],
    )
    body = response.json()

    assert response.status_code == 402
    assert body["needs_licence"] is True
    assert body["buy_url"] == "https://example.test/buy"


def test_a_batch_with_a_key_goes_through(paid_client, clean_statement_path, july_bank_path):
    response = paid_client.post(
        "/api/export",
        files=[
            ("files", ("a.csv", clean_statement_path.read_bytes())),
            ("files", ("b.csv", july_bank_path.read_bytes())),
        ],
        data={"licence": "good-key"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == 2
    assert body["licence"]["licensed"] is True


def test_a_refused_key_is_reported_even_when_the_upload_worked(
    paid_client, clean_statement_path
):
    """A lapsed key silently falling back to free is how someone stops paying
    without noticing, or keeps paying for nothing."""
    response = paid_client.post(
        "/api/export",
        files=[("files", ("s.csv", clean_statement_path.read_bytes()))],
        data={"licence": "expired-key"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["licence"]["licensed"] is False
    assert body["licence"]["problem"]
