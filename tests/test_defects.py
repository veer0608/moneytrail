"""Regressions for defects found by hunting rather than by writing a feature.

Each of these was live in a passing 198-test suite, which is the point: green
tests prove the cases you thought of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from moneytrail import Direction, parse_statement
from moneytrail.insights import roll_up
from moneytrail.query import ask, build_ledger, find_merchant, parse_period


@pytest.fixture
def ledger(patterns_path):
    return build_ledger([parse_statement(patterns_path)])


@pytest.fixture
def blank_narration(tmp_path: Path) -> Path:
    target = tmp_path / "blank.csv"
    target.write_text(
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/04/25,OPENING BALANCE,,,1000.00\n"
        "02/04/25,,100.00,,900.00\n"
        "03/04/25,CLOSING BALANCE,,,900.00\n",
        encoding="utf-8",
    )
    return target


def test_a_blank_narration_never_becomes_an_empty_merchant_name(blank_narration):
    # An empty string is a substring of every question, so a merchant named ""
    # silently matched anything the ledger was asked.
    ledger = build_ledger([parse_statement(blank_narration)])

    assert "" not in ledger.merchants
    assert ledger.merchants == {"(no narration)"}


def test_an_unlabelled_row_is_not_matched_to_a_question(blank_narration):
    ledger = build_ledger([parse_statement(blank_narration)])

    assert find_merchant("how much did i spend on swiggy", ledger) is None


def test_biggest_also_refuses_an_unknown_name(ledger):
    # _total and _count refused; _top did not, and answered for everything.
    answer = ask("biggest expenses at tesco in march", ledger)

    assert not answer.understood
    assert "tesco" in answer.headline


def test_biggest_shows_evidence_for_every_merchant_it_names(ledger):
    answer = ask("biggest expenses in february", ledger)

    named = {part.strip().rsplit(" ", 1)[0] for part in answer.headline.split(": ", 1)[1].split(";")}
    covered = {row.match.name for row in answer.rows}

    assert named <= covered, "the headline claims more than the evidence supports"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("how much may i have spent on rent", None),
        ("how much did i spend on rent in may", "May 2025"),
        ("how much did i spend on rent in may 2025", "May 2025"),
        ("what did i spend during march", "March 2025"),
        ("how much in february", "February 2025"),
    ],
)
def test_month_words_that_are_also_english_words(ledger, question, expected):
    period = parse_period(question, ledger.last_date)

    assert (period.label if period else None) == expected


def test_an_unknown_name_is_caught_without_a_preposition(ledger):
    # The guard used to look only after "at"/"to"/"from", so this sailed through
    # and answered "25 times for transactions".
    answer = ask("how many times did i pay flipkart", ledger)

    assert not answer.understood
    assert "flipkart" in answer.headline


def test_a_month_range_stays_a_range(ledger):
    # Taking the first month named would answer January alone.
    period = parse_period("what did i spend from january to march", ledger.last_date)

    assert period.start.month == 1
    assert period.end.month == 3
    assert period.label == "January to March 2025"


@pytest.mark.parametrize(
    "question",
    [
        "what did i pay to netflix",
        "what am i paying netflix",
        "how much did i pay netflix",
    ],
)
def test_pay_and_paying_are_understood_not_just_paid(ledger, question):
    answer = ask(question, ledger)

    assert answer.understood
    assert answer.amount


def test_asking_about_the_card_scopes_to_the_card(patterns_path, card_path):
    both = build_ledger([parse_statement(patterns_path), parse_statement(card_path)])

    answer = ask("how much did i spend on my card", both)

    assert answer.amount == 832_050  # the card statement alone
    assert all(row.on_card for row in answer.rows)
    assert "source=card statements only" in answer.filters


def test_the_word_card_is_ignored_when_no_card_is_loaded(ledger):
    answer = ask("how much did i spend on my card", ledger)

    assert answer.understood
    assert not any("card" in f for f in answer.filters)


def test_counting_one_thing_says_time_not_times(ledger):
    answer = ask("how many times did i pay amazon", ledger)

    assert "1 time " in answer.headline or answer.headline.startswith("2 times")


def test_the_spending_count_excludes_refunds(patterns_path):
    # Amazon was charged twice and refunded once: two occasions of spending,
    # three transactions.
    rollup = roll_up(parse_statement(patterns_path))
    amazon = next(entry for entry in rollup.entries if entry.name == "Amazon")

    assert amazon.count == 3
    assert amazon.debit_count == 2
    assert amazon.debit_count == sum(
        1
        for txn, match in zip(
            parse_statement(patterns_path).transactions, rollup.matches
        )
        if match.name == "Amazon" and txn.direction is Direction.DEBIT
    )
