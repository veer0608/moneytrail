"""The trust strip: whether each statement was read faithfully, first."""

from __future__ import annotations

from moneytrail import parse_statement
from moneytrail.cli import main
from moneytrail.report import FAILED, OK, WEAK, build_tiles, render


def test_a_clean_statement_is_a_green_tile(clean_statement_path):
    tiles = build_tiles([parse_statement(clean_statement_path)])

    assert [tile.status for tiles_ in [tiles] for tile in tiles_] == [OK]
    assert tiles[0].notes == ()


def test_a_broken_statement_is_a_red_tile_carrying_the_reason(dropped_row_path):
    tile = build_tiles([parse_statement(dropped_row_path)])[0]

    assert tile.status == FAILED
    assert tile.notes  # the discrepancies travel with the tile
    assert any("649.00" in note for note in tile.notes)


def test_derived_endpoints_are_amber_not_green(signed_amount_path):
    # It reconciled, but only against figures taken from its own rows.
    tile = build_tiles([parse_statement(signed_amount_path)])[0]

    assert tile.status == WEAK


def test_a_card_with_no_totals_is_amber(tmp_path):
    target = tmp_path / "bare.csv"
    target.write_text(
        "Total Amount Due\n"
        "Date,Transaction Description,Amount\n"
        "03/07/2025,SOME PURCHASE,412.00\n",
        encoding="utf-8",
    )

    assert build_tiles([parse_statement(target)])[0].status == WEAK


def test_the_headline_counts_only_the_green_ones(
    clean_statement_path, dropped_row_path
):
    page = render(
        [parse_statement(clean_statement_path), parse_statement(dropped_row_path)]
    )

    assert "1 of 2 statements reconcile to the paisa" in page


def test_the_page_is_self_contained(patterns_path):
    page = render([parse_statement(patterns_path)])

    assert page.startswith("<!doctype html>")
    # No network of any kind: the privacy claim has to be checkable by reading it.
    for forbidden in ("http://", "https://", "<script", "src=", "@import"):
        assert forbidden not in page, forbidden


def test_narrations_are_escaped(tmp_path):
    target = tmp_path / "nasty.csv"
    target.write_text(
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/04/25,OPENING BALANCE,,,1000.00\n"
        "02/04/25,<script>alert(1)</script> & co,100.00,,900.00\n"
        "03/04/25,CLOSING BALANCE,,,900.00\n",
        encoding="utf-8",
    )

    page = render([parse_statement(target)])

    assert "<script>alert(1)</script>" not in page


def test_open_loops_surface_unrefunded_duplicates(patterns_path):
    page = render([parse_statement(patterns_path)])

    assert "Open loops" in page
    assert "Swiggy charged" in page  # the duplicate nobody refunded
    assert "₹450.00" in page


def test_recurring_shows_active_and_stopped(patterns_path):
    page = render([parse_statement(patterns_path)])

    assert "Netflix" in page and "active" in page
    assert "Spotify" in page and "stopped" in page


class TestCommand:
    def test_writes_a_file(self, patterns_path, tmp_path, capsys):
        out = tmp_path / "report.html"

        assert main(["report", str(patterns_path), "--out", str(out)]) == 0

        assert out.exists()
        assert "<!doctype html>" in out.read_text(encoding="utf-8")
        assert "gitignored" in capsys.readouterr().out

    def test_covers_several_statements_at_once(
        self, clean_statement_path, card_path, tmp_path
    ):
        out = tmp_path / "report.html"
        main(["report", str(clean_statement_path), str(card_path), "--out", str(out)])

        page = out.read_text(encoding="utf-8")
        assert "2 of 2 statements reconcile" in page
