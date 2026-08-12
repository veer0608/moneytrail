"""The golden set has to be trustworthy before anything scored against it is.

The property that makes this cheap is that no expected number is ever written
down: gold answers come from running the gold query. These tests guard that,
and guard the scoring from flattering anybody.
"""

from __future__ import annotations

from datetime import date

import pytest

from evals.runner import (
    BASELINE,
    LABELS,
    MARKERS,
    REPO,
    SETS,
    Item,
    Scorecard,
    answer_for,
    deterministic_parser,
    load,
    main,
    markdown,
    same_query,
    score,
    splice,
)
from moneytrail.models import Direction
from moneytrail.query import Period, Query, run


@pytest.fixture(scope="module")
def golden():
    return load()


class TestTheGoldenSet:
    def test_it_loads(self, golden):
        ledger, items = golden

        assert len(items) >= 60, "the brief asked for about sixty questions"
        assert ledger.rows

    def test_every_question_is_in_a_known_set(self, golden):
        _, items = golden

        assert {item.which for item in items} <= set(SETS)

    def test_every_set_has_questions_in_it(self, golden):
        _, items = golden

        for which in SETS:
            assert any(item.which == which for item in items), which

    def test_no_question_appears_twice(self, golden):
        _, items = golden
        asked = [item.ask for item in items]

        assert len(asked) == len(set(asked))

    def test_every_gold_query_names_something_the_ledger_has(self, golden):
        # A gold query naming a merchant that is not there would make the gold
        # answer a confident zero, and every parser would be scored against it.
        ledger, items = golden
        for item in items:
            if item.expected is None:
                continue
            if item.expected.merchant:
                assert item.expected.merchant in ledger.merchants, item.ask
            if item.expected.category:
                assert item.expected.category in ledger.categories, item.ask

    def test_every_gold_query_executes(self, golden):
        ledger, items = golden
        for item in items:
            if item.expected is None:
                continue
            assert run(item.expected, ledger).understood, item.ask

    def test_a_refusal_is_gold_for_the_beyond_schema_set(self, golden):
        _, items = golden
        beyond = [item for item in items if item.which == "beyond-schema"]

        assert beyond
        assert all(item.expected is None for item in beyond)


class TestGoldAnswersComeFromTheEngine:
    def test_the_expected_answer_is_computed_not_written_down(self, golden):
        ledger, _ = golden
        query = Query("total", merchant="Netflix")

        assert answer_for(query, ledger).amount == run(query, ledger).amount

    def test_a_refusal_has_no_amount_to_match_against(self, golden):
        ledger, _ = golden

        assert answer_for(None, ledger).amount is None

    def test_changing_the_ledger_changes_the_gold(self, golden, patterns_path):
        # The gold cannot go stale, because there is no stored number to go
        # stale. This is the property that makes labelling free.
        from moneytrail import parse_statement
        from moneytrail.query import build_ledger

        full, _ = golden
        smaller = build_ledger([parse_statement(patterns_path)])
        query = Query("total", direction=Direction.DEBIT)

        assert answer_for(query, full).amount != answer_for(query, smaller).amount


class TestComparingQueries:
    def test_identical_queries_match(self):
        assert same_query(Query("total", merchant="Netflix"), Query("total", merchant="Netflix"))

    def test_a_different_intent_does_not_match(self):
        assert not same_query(Query("total"), Query("count"))

    def test_a_different_filter_does_not_match(self):
        assert not same_query(
            Query("total", merchant="Netflix"), Query("total", merchant="Amazon")
        )

    def test_a_different_direction_does_not_match(self):
        assert not same_query(
            Query("total", direction=Direction.DEBIT),
            Query("total", direction=Direction.CREDIT),
        )

    def test_two_refusals_match(self):
        assert same_query(None, None)

    def test_a_refusal_and_an_answer_do_not(self):
        assert not same_query(None, Query("total"))
        assert not same_query(Query("total"), None)

    def test_the_same_dates_under_a_different_name_still_match(self):
        # Scoring the label would be scoring the phrasing. "March 2025" and
        # "1 to 31 March" select exactly the same rows.
        march = Period(date(2025, 3, 1), date(2025, 3, 31), "March 2025")
        same = Period(date(2025, 3, 1), date(2025, 3, 31), "1 to 31 March")

        assert same_query(Query("total", period=march), Query("total", period=same))

    def test_different_dates_do_not_match_however_they_are_labelled(self):
        march = Period(date(2025, 3, 1), date(2025, 3, 31), "March 2025")
        april = Period(date(2025, 4, 1), date(2025, 4, 30), "March 2025")

        assert not same_query(Query("total", period=march), Query("total", period=april))


class TestTheBaseline:
    def test_it_is_perfect_on_the_questions_it_covers(self, golden):
        # The same thing CI gates on. If this fails the parser regressed, or a
        # question was filed under the wrong set.
        ledger, items = golden
        covered = [item for item in items if item.which == "deterministic"]

        card = score(BASELINE, deterministic_parser, ledger, covered)

        assert card.summary("deterministic")["query_accuracy"] == 1.0

    def test_it_scores_nothing_on_the_questions_it_cannot_parse(self, golden):
        # If this ever rises, the model-only set has stopped being model-only
        # and the comparison has quietly lost its point.
        ledger, items = golden
        model_only = [item for item in items if item.which == "model-only"]

        card = score(BASELINE, deterministic_parser, ledger, model_only)

        assert card.summary("model-only")["query_accuracy"] == 0.0

    def test_it_costs_nothing_and_takes_no_time(self, golden):
        ledger, items = golden

        summary = score(BASELINE, deterministic_parser, ledger, items).summary()

        assert summary["cost_per_question"] == 0.0
        assert summary["p50_latency_ms"] == 0.0

    def test_it_over_answers_some_questions_no_query_can_express(self, golden):
        # Recorded because it is a real finding, not a bug to paper over: the
        # regex reads "subscription" and answers, where the honest reply is
        # that one query cannot say it.
        ledger, items = golden
        beyond = [item for item in items if item.which == "beyond-schema"]

        card = score(BASELINE, deterministic_parser, ledger, beyond)

        assert card.summary("beyond-schema")["query_accuracy"] < 1.0


class TestScoring:
    def test_a_wrong_query_that_lands_on_the_right_number_is_marked_honestly(
        self, golden
    ):
        # Answer accuracy is the weaker metric on purpose, and the pair has to
        # be able to disagree or reporting both would be pointless.
        ledger, _ = golden
        item = Item(ask="q", which="deterministic", expected=Query("total", category="rent"))

        card = score("stub", lambda q, l: _attempt(Query("total", category="rent")), ledger, [item])
        result = card.results[0]

        assert result.query_ok and result.answer_ok

    def test_refusals_are_counted(self, golden):
        ledger, _ = golden
        items = [
            Item(ask="a", which="deterministic", expected=None),
            Item(ask="b", which="deterministic", expected=Query("recurring")),
        ]

        card = score("stub", lambda q, l: _attempt(None), ledger, items)

        assert card.summary()["refusal_rate"] == 1.0
        assert card.summary()["query_accuracy"] == 0.5

    def test_an_empty_scorecard_summarises_to_nothing(self):
        assert Scorecard(parser="none").summary() == {}


class TestThePublishedTable:
    def test_it_is_generated_from_the_run_not_typed(self, golden):
        # A number in a README nobody can regenerate is a number nobody should
        # believe, so the published table comes out of the scorer itself.
        ledger, items = golden
        card = score(BASELINE, deterministic_parser, ledger, items)

        table = markdown([card], items)

        assert "| parser | query acc | answer acc | refused | $/question | p50 |" in table
        assert "100.0%" in table
        assert BASELINE in table
        for which in SETS:
            assert LABELS[which] in table

    def test_the_readme_still_has_somewhere_to_put_it(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")

        for marker in MARKERS:
            assert marker in text

    def test_the_readme_carries_the_current_baseline_numbers(self, golden):
        # Guards against the table being edited by hand, or going stale when
        # the engine changes underneath it.
        #
        # Only the baseline row is checked, and deliberately so: a model row
        # cannot be regenerated without paying for it, so a test that demanded
        # one would either cost money on every run or quietly stop being run.
        # The baseline is free, and it is the row CI already depends on.
        ledger, items = golden
        card = score(BASELINE, deterministic_parser, ledger, items)
        start, end = MARKERS
        published = (
            (REPO / "README.md")
            .read_text(encoding="utf-8")
            .split(start, 1)[1]
            .split(end, 1)[0]
        )
        rows = [
            line
            for line in markdown([card], items).splitlines()
            if line.startswith(f"| {BASELINE} |")
        ]

        assert rows, "the generated table has no baseline row to compare"
        for row in rows:
            assert row in published, f"README no longer matches the scorer:\n{row}"

    def test_splicing_leaves_the_rest_of_the_file_alone(self, tmp_path):
        start, end = MARKERS
        target = tmp_path / "README.md"
        target.write_text(f"before\n{start}\nold\n{end}\nafter\n", encoding="utf-8")

        assert splice(target, "new")

        text = target.read_text(encoding="utf-8")
        assert text.startswith("before\n")
        assert text.endswith("\nafter\n")
        assert "new" in text and "old" not in text

    def test_a_file_with_no_markers_is_not_written_to(self, tmp_path):
        target = tmp_path / "README.md"
        target.write_text("nothing to replace", encoding="utf-8")

        assert not splice(target, "new")
        assert target.read_text(encoding="utf-8") == "nothing to replace"


class TestTheGate:
    def test_it_passes_today(self, capsys):
        assert main(["--gate"]) == 0
        assert "100.0%" in capsys.readouterr().out

    def test_it_never_reaches_for_a_model(self, capsys, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("the gate must not make a paid call")

        monkeypatch.setattr("evals.runner.build_client", forbidden)

        assert main(["--gate"]) == 0

    def test_the_default_run_is_the_baseline_alone(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "evals.runner.build_client",
            lambda *a, **k: pytest.fail("no model was asked for"),
        )

        assert main([]) == 0
        assert BASELINE in capsys.readouterr().out


def _attempt(query):
    from evals.runner import Attempt

    return Attempt(query=query)
