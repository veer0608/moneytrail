"""Phase 3a: a card bill payment is one event recorded on two statements."""

from __future__ import annotations

import pytest

from moneytrail import parse_statement, reconcile
from moneytrail.cli import main
from moneytrail.linking import (
    card_repayments,
    find_transfers,
    link_card_repayments,
    merge,
    summarise_spend,
)


@pytest.fixture
def bank(july_bank_path):
    return parse_statement(july_bank_path)


@pytest.fixture
def card(card_path):
    return parse_statement(card_path)


def test_the_july_bank_fixture_reconciles(bank):
    assert reconcile(bank).ok


def test_card_repayments_are_picked_out_of_the_bank_ledger(bank):
    found = card_repayments(bank)

    # The CRED payment and the one addressed to a masked card number. Rent,
    # groceries and the salary credit are not repayments.
    assert sorted(txn.amount for txn in found) == [500_000, 1_245_000]


def test_a_repayment_is_matched_to_the_payment_it_settles(bank, card):
    linkage = link_card_repayments([bank], [card])
    matched = linkage.matched

    assert len(matched) == 1
    assert matched[0].amount == 1_245_000
    assert matched[0].lag_days == 0
    assert matched[0].card_source == card.source


def test_a_repayment_with_no_card_statement_stays_unmatched(bank, card):
    linkage = link_card_repayments([bank], [card])

    assert [link.amount for link in linkage.unmatched] == [500_000]
    assert linkage.unmatched_total == 500_000


def test_a_card_payment_with_no_bank_debit_is_reported(bank, card):
    # Drop the bank side entirely: the card still says it was paid, and that
    # means it was settled from an account we were not given.
    linkage = link_card_repayments([], [card])

    assert len(linkage.orphan_card_payments) == 1
    assert linkage.orphan_card_payments[0][1].amount == 1_245_000


def test_amounts_must_match_to_the_paisa(bank, card, tmp_path):
    lines = card.source.read_text(encoding="utf-8").replace("12450.00 Cr", "12450.01 Cr")
    altered = tmp_path / "card.csv"
    altered.write_text(lines, encoding="utf-8")

    linkage = link_card_repayments([bank], [parse_statement(altered)])

    assert linkage.matched == ()


def test_a_payment_outside_the_window_is_not_matched(bank, card):
    linkage = link_card_repayments([bank], [card], window=(5, 10))

    assert linkage.matched == ()
    assert len(linkage.unmatched) == 2


def test_one_payment_cannot_settle_two_repayments(bank, card):
    # Both bank statements contain the same CRED debit; only one card payment
    # exists, so exactly one link may be made.
    linkage = link_card_repayments([bank, bank], [card])

    assert len(linkage.matched) == 1
    assert len(linkage.unmatched) == 3


class TestSpend:
    def test_matched_repayments_are_removed_and_card_charges_added(self, bank, card):
        spend = summarise_spend([bank], [card])

        assert spend.bank_outflow == 4_590_000  # 12,450 + 5,000 + 28,000 + 450
        assert spend.matched_repayments == 1_245_000
        assert spend.card_charges == 832_050  # purchases + finance charge
        assert spend.true_outflow == 4_590_000 - 1_245_000 + 832_050

    def test_unmatched_repayments_are_left_in_the_total(self, bank, card):
        # Subtracting a repayment whose purchases we cannot see would
        # understate spending, which is the more dangerous error.
        spend = summarise_spend([bank], [card])

        assert spend.unmatched_repayments == 500_000
        assert not spend.complete
        assert spend.true_outflow > spend.bank_outflow - spend.matched_repayments

    def test_with_no_cards_at_all_the_total_is_just_the_bank(self, bank):
        spend = summarise_spend([bank], [])

        assert spend.true_outflow == spend.bank_outflow
        assert spend.matched_repayments == 0


class TestTransfers:
    @pytest.fixture
    def accounts(self, hdfc_aug_path, icici_aug_path):
        return [parse_statement(hdfc_aug_path), parse_statement(icici_aug_path)]

    def test_both_statements_reconcile(self, accounts):
        assert all(reconcile(statement).ok for statement in accounts)

    def test_money_moving_between_your_own_accounts_is_found(self, accounts):
        transfers = find_transfers(accounts)

        assert len(transfers) == 1
        assert transfers[0].amount == 2_500_000
        assert transfers[0].lag_days == 0
        assert transfers[0].out_source != transfers[0].in_source

    def test_a_debit_and_credit_on_the_same_account_is_not_a_transfer(self, accounts):
        # Otherwise a refund would be read as moving money to yourself.
        assert find_transfers([accounts[0]]) == []

    def test_a_credit_outside_the_window_is_not_a_transfer(self, accounts):
        assert find_transfers(accounts, window=(2, 3)) == []

    def test_it_is_removed_from_both_sides(self, accounts):
        spend = summarise_spend(accounts, [])

        assert spend.bank_outflow == 5_360_000
        assert spend.bank_inflow == 10_500_000
        assert spend.internal_transfers == 2_500_000
        # What was really spent, and what was really earned: the salary alone.
        assert spend.true_outflow == 2_860_000
        assert spend.true_inflow == 8_000_000

    def test_without_the_other_account_nothing_is_removed(self, accounts):
        spend = summarise_spend([accounts[0]], [])

        assert spend.internal_transfers == 0
        assert spend.true_outflow == spend.bank_outflow


class TestMerge:
    def test_orders_across_accounts_and_tags_each_row(
        self, hdfc_aug_path, icici_aug_path
    ):
        entries = merge([parse_statement(hdfc_aug_path), parse_statement(icici_aug_path)])

        dates = [entry.transaction.date for entry in entries]
        assert dates == sorted(dates)
        assert {entry.account for entry in entries} == {
            "HDFC XXXXXXXX4471",
            "ICICI ****8820",
        }


class TestCommand:
    def test_reports_transfers_with_both_narrations(
        self, hdfc_aug_path, icici_aug_path, capsys
    ):
        assert main(["spend", str(hdfc_aug_path), str(icici_aug_path)]) == 0

        out = capsys.readouterr().out
        assert "transfers to yourself" in out
        assert "actually received" in out
        assert "SELF TRANSFER TO ICICI" in out  # the outgoing narration
        assert "VEER ARORA HDFC" in out  # and the incoming one

    def test_reports_the_adjusted_total(self, july_bank_path, card_path, capsys):
        assert main(["spend", str(july_bank_path), str(card_path)]) == 0

        out = capsys.readouterr().out
        assert "actually spent" in out
        assert "no card statement behind them" in out

    def test_a_bank_statement_alone_still_works(self, july_bank_path, capsys):
        assert main(["spend", str(july_bank_path)]) == 0
        assert "1 bank statement(s), 0 card statement(s)" in capsys.readouterr().out
