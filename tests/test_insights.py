from __future__ import annotations

from moneytrail import parse_statement
from moneytrail.cli import main
from moneytrail.insights import by_category, roll_up


def test_rolls_transactions_up_by_merchant(clean_statement_path):
    rollup = roll_up(parse_statement(clean_statement_path))

    assert rollup.transactions == 7
    names = {entry.name for entry in rollup.entries}
    assert {"Swiggy Instamart", "Netflix", "Myntra"} <= names


def test_a_merchant_appearing_twice_is_one_entry(clean_statement_path):
    # Myntra charged 2,499 and refunded 2,499: one merchant, two transactions,
    # both sides recorded rather than netted away.
    rollup = roll_up(parse_statement(clean_statement_path))
    myntra = next(entry for entry in rollup.entries if entry.name == "Myntra")

    assert myntra.count == 2
    assert myntra.debits == 249_900
    assert myntra.credits == 249_900
    assert myntra.net == 0


def test_entries_are_ordered_by_turnover(clean_statement_path):
    rollup = roll_up(parse_statement(clean_statement_path))
    turnovers = [entry.turnover for entry in rollup.entries]

    assert turnovers == sorted(turnovers, reverse=True)


def test_coverage_counts_only_confident_matches(clean_statement_path):
    rollup = roll_up(parse_statement(clean_statement_path))

    assert rollup.confident == sum(1 for m in rollup.matches if m.confident)
    assert 0.0 <= rollup.coverage <= 1.0
    assert rollup.classified + len(rollup.unclassified) == rollup.transactions


def test_counterparties_are_classified_before_they_are_named(clean_statement_path):
    # Naming and understanding are different things: an ATM withdrawal has no
    # merchant but is not a mystery, and counting it as an unresolved merchant
    # would make the coverage number lie.
    rollup = roll_up(parse_statement(clean_statement_path))

    assert rollup.by_kind["merchant"] == 4  # Swiggy Instamart, Netflix, Myntra x2
    assert rollup.by_kind["cash"] == 1  # the ATM withdrawal
    assert rollup.by_kind["person"] == 0


def test_every_transaction_lands_in_exactly_one_category(clean_statement_path):
    rollup = roll_up(parse_statement(clean_statement_path))

    assert sum(count for _, _, _, count in by_category(rollup)) == rollup.transactions


def test_categories_are_ordered_by_spend(clean_statement_path):
    rows = by_category(roll_up(parse_statement(clean_statement_path)))
    debits = [debit for _, debit, _, _ in rows]

    assert debits == sorted(debits, reverse=True)


class TestCommand:
    def test_prints_a_rollup(self, clean_statement_path, capsys):
        assert main(["merchants", str(clean_statement_path)]) == 0

        out = capsys.readouterr().out
        assert "Swiggy Instamart" in out
        assert "from a known merchant" in out
        assert "counterparties" in out

    def test_top_limits_the_list(self, clean_statement_path, capsys):
        assert main(["merchants", str(clean_statement_path), "--top", "2"]) == 0
        assert "and 4 more" in capsys.readouterr().out

    def test_unmatched_shows_the_raw_narration(self, clean_statement_path, capsys):
        assert main(["merchants", str(clean_statement_path), "--unmatched"]) == 0

        out = capsys.readouterr().out
        assert "unclassified" in out
        # The salary credit: an organisation the lexicon has never heard of.
        assert "ACME TECHNOLOGIES" in out

    def test_a_locked_file_does_not_crash_the_rollup(self, locked_pdf_path, capsys):
        assert main(["merchants", str(locked_pdf_path), "--no-prompt"]) == 1
        assert "LOCKED" in capsys.readouterr().out
