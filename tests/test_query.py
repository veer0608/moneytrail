"""Phase 6: questions in English, answers you can check.

The engine is deterministic on purpose. Every assertion here is about a number
computed from rows, not about phrasing a model happened to produce, which is
the difference between an answer and a plausible sentence.
"""

from __future__ import annotations

from datetime import date

import pytest

from dataclasses import fields

from moneytrail import Direction, parse_statement
from moneytrail.cli import main
from moneytrail.query import (
    READS,
    Period,
    Query,
    ask,
    build_ledger,
    find_merchant,
    parse_period,
    parse_question,
    refuse_unknown_fields,
    run,
)


@pytest.fixture
def ledger(patterns_path):
    return build_ledger([parse_statement(patterns_path)])


class TestPeriods:
    def test_a_named_month_resolves_within_the_ledger_year(self, ledger):
        period = parse_period("spend in march", ledger.last_date)

        assert period == Period(date(2025, 3, 1), date(2025, 3, 31), "March 2025")

    def test_an_explicit_year_wins(self, ledger):
        period = parse_period("february 2024", ledger.last_date)

        assert period.start == date(2024, 2, 1)
        assert period.end == date(2024, 2, 29)  # a leap year

    def test_relative_dates_resolve_against_the_ledger_not_today(self, ledger):
        # The statement ends in May 2025, so "last month" is April 2025 no
        # matter when the question is asked. Anchoring to today would make the
        # same question give different answers over time.
        period = parse_period("last month", ledger.last_date)

        assert period.label == "April 2025"

    def test_no_date_means_every_statement(self, ledger):
        assert parse_period("how much on netflix", ledger.last_date) is None


class TestMerchantMatching:
    def test_finds_a_merchant_named_in_the_question(self, ledger):
        assert find_merchant("what did i pay netflix", ledger) == "Netflix"

    def test_the_longest_name_wins(self, patterns_path, clean_statement_path):
        # "swiggy" appears in both; the more specific merchant should win.
        ledger = build_ledger([parse_statement(clean_statement_path)])

        assert find_merchant("how much on swiggy", ledger) == "Swiggy Instamart"

    def test_an_unknown_merchant_matches_nothing(self, ledger):
        assert find_merchant("how much did i spend at tesco", ledger) is None


class TestTotals:
    def test_a_total_for_a_category_in_a_month(self, ledger):
        answer = ask("how much did i spend on rent in march", ledger)

        assert answer.amount == 2_800_000
        assert answer.evidence == 1
        assert "category=rent" in answer.filters
        assert "period=March 2025" in answer.filters

    def test_every_answer_carries_the_rows_behind_it(self, ledger):
        answer = ask("how much did i spend on netflix", ledger)

        assert answer.amount == sum(row.transaction.amount for row in answer.rows)
        assert all(row.match.name == "Netflix" for row in answer.rows)

    def test_income_words_flip_the_direction(self, ledger):
        answer = ask("how much did i receive in march", ledger)

        assert "direction=credit" in answer.filters
        assert all(
            row.transaction.direction is Direction.CREDIT for row in answer.rows
        )

    def test_an_unknown_name_is_refused_not_quietly_dropped(self, ledger):
        # Without this it would drop "tesco" and answer for everything in March
        # -- a confident answer to a question nobody asked.
        answer = ask("how much did i spend at tesco in march", ledger)

        assert answer.amount is None
        assert answer.rows == ()
        assert not answer.understood
        assert "tesco" in answer.headline
        assert any("different question" in caveat for caveat in answer.caveats)

    def test_a_known_category_is_not_mistaken_for_an_unknown_name(self, ledger):
        answer = ask("how much did i spend on rent", ledger)

        assert answer.understood
        assert answer.amount == 14_000_000  # five months of rent

    def test_an_unfiltered_question_still_answers(self, ledger):
        answer = ask("how much did i spend in march", ledger)

        assert answer.understood
        assert answer.amount


class TestOtherIntents:
    def test_counting(self, ledger):
        answer = ask("how many times did i pay netflix", ledger)

        assert answer.evidence == 5
        assert "5 times" in answer.headline

    def test_biggest(self, ledger):
        answer = ask("biggest expenses in february", ledger)

        assert "Housing Rent" in answer.headline
        assert answer.amount == 2_800_000

    def test_refund_arrived(self, ledger):
        answer = ask("did the myntra refund arrive", ledger)

        assert answer.headline.startswith("yes")
        assert answer.amount == 249_900

    def test_a_refund_that_never_came_is_not_claimed_to_exist(self, ledger):
        answer = ask("did the netflix refund arrive", ledger)

        assert "no refund" in answer.headline
        assert any("never issued" in caveat for caveat in answer.caveats)

    def test_subscriptions(self, ledger):
        answer = ask("what subscriptions am i paying", ledger)

        assert "stopped" in answer.headline  # Spotify
        assert "Netflix" in answer.headline

    def test_duplicates(self, ledger):
        answer = ask("was i charged twice", ledger)

        assert "Swiggy" in answer.headline
        assert answer.amount == 45_000  # what is still out
        assert any("candidates, not verdicts" in c for c in answer.caveats)


class TestUnderstanding:
    def test_an_unanswerable_question_says_what_it_can_do(self, ledger):
        answer = ask("what is the meaning of life", ledger)

        assert not answer.understood
        assert "totals" in answer.headline

    def test_it_never_invents_an_amount_when_it_did_not_understand(self, ledger):
        answer = ask("please transfer money to my cousin", ledger)

        assert answer.amount is None
        assert answer.rows == ()


class TestTheSeam:
    """Parsing and execution are separable, which is what lets a model replace
    the first half without getting anywhere near the arithmetic."""

    ASKABLE = [
        "how much did i spend on rent in march",
        "how much did i spend on netflix",
        "how much did i receive in march",
        "how many times did i pay netflix",
        "biggest expenses in february",
        "did the myntra refund arrive",
        "what subscriptions am i paying",
        "was i charged twice",
    ]

    @pytest.mark.parametrize("question", ASKABLE)
    def test_asking_is_exactly_parsing_then_running(self, ledger, question):
        query = parse_question(question, ledger)

        assert ask(question, ledger) == run(query, ledger, question=question)

    def test_a_question_becomes_a_query(self, ledger):
        query = parse_question("how much did i spend on rent in march", ledger)

        assert query == Query(
            "total",
            category="rent",
            period=Period(date(2025, 3, 1), date(2025, 3, 31), "March 2025"),
            direction=Direction.DEBIT,
        )

    def test_a_query_runs_with_no_english_attached_to_it(self, ledger):
        # The eval derives its gold answers this way: no question, just a query
        # and the engine. That is what makes labelling the golden set free.
        answer = run(Query("total", merchant="Netflix"), ledger)

        assert answer.amount == ask("how much did i spend on netflix", ledger).amount
        assert answer.question == ""

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            # "in march" parses to a period, but a subscription sweep reads the
            # whole ledger. Recording the period would imply a filter that is
            # never applied -- and a query that lies about what it did is worse
            # than one that admits it looked everywhere.
            ("what subscriptions am i paying in march", Query("recurring")),
            ("was i charged twice in march", Query("duplicates")),
        ],
    )
    def test_fields_an_intent_never_reads_are_left_unset(
        self, ledger, question, expected
    ):
        assert parse_question(question, ledger) == expected

    def test_the_parser_only_ever_sets_fields_its_intent_reads(self, ledger):
        # Guards the property the golden set is scored on: two queries that
        # would execute identically have to compare equal.
        optional = {field.name for field in fields(Query)} - {"intent"}
        for question in self.ASKABLE + [
            "how much did i spend on my card",
            "biggest food spend in march",
            "how often did i order swiggy",
            "did i get any refunds in april",
        ]:
            query = parse_question(question, ledger)
            for name in optional - set(READS[query.intent]):
                assert getattr(query, name) is None, (
                    f"{question!r} set {name} on a {query.intent} query, which "
                    f"never reads it"
                )

    def test_a_question_it_does_not_take_yields_no_query(self, ledger):
        assert parse_question("what is the meaning of life", ledger) is None

    def test_an_intent_that_does_not_exist_is_declined_not_crashed(self, ledger):
        # A model will eventually hand this in. Nothing it can emit should
        # produce a traceback.
        answer = run(Query("vibes"), ledger)

        assert not answer.understood
        assert answer.amount is None
        assert answer.rows == ()

    def test_a_merchant_the_ledger_never_had_is_refused_not_answered(self, ledger):
        # The text guard cannot catch this: there is no text. A model that
        # invents "Tesco" would otherwise get a confident zero back, which
        # reads as "you spent nothing there" rather than "no such merchant".
        refusal = refuse_unknown_fields("q", ledger, Query("total", merchant="Tesco"))

        assert refusal is not None
        assert not refusal.understood
        assert "Tesco" in refusal.headline

    def test_a_merchant_the_ledger_does_have_passes_the_guard(self, ledger):
        assert refuse_unknown_fields("q", ledger, Query("total", merchant="Netflix")) is None


class TestCommand:
    def test_prints_the_answer_and_its_evidence(self, patterns_path, capsys):
        code = main(["ask", "how much did i spend on rent in march", str(patterns_path)])

        out = capsys.readouterr().out
        assert code == 0
        assert "₹28,000.00" in out
        assert "evidence  1 row" in out  # singular
        assert "ACH D- HOUSING RENT" in out  # the row itself

    def test_an_unanswerable_question_exits_nonzero(self, patterns_path, capsys):
        assert main(["ask", "what is the meaning of life", str(patterns_path)]) == 2

    def test_questions_span_several_statements(
        self, patterns_path, clean_statement_path, capsys
    ):
        main(["ask", "how much did i spend on netflix", str(patterns_path),
              str(clean_statement_path)])

        # Five monthly charges in one statement, one more in the other.
        assert "6 transactions" in capsys.readouterr().out
