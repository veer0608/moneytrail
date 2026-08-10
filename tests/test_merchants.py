"""Phase 2: turning narration noise into a name."""

from __future__ import annotations

import pytest

from moneytrail import parse_statement
from moneytrail.merchants import build_vocabulary, identify
from moneytrail.narration import Channel, parse_narration
from moneytrail.segmentation import Vocabulary, segment, weld_ratio

HDFC_SWIGGY = "UPI-SWIGGYINSTAMART-SWIGGY@YBL-YESB0000001-435820912-PAYMENT"
ICICI_ZOMATO = "UPI/ZOMATOLTD/zomato@hdfcbank/512099831"


class TestNarrationStructure:
    def test_finds_the_vpa_and_the_name_beside_it(self):
        parsed = parse_narration(HDFC_SWIGGY)

        assert parsed.channel is Channel.UPI
        assert parsed.vpa == "swiggy@ybl"
        assert parsed.handle == "swiggy"
        assert parsed.counterparty == "SWIGGYINSTAMART"

    def test_works_on_a_different_bank_with_a_different_delimiter(self):
        parsed = parse_narration(ICICI_ZOMATO)

        assert parsed.channel is Channel.UPI
        assert parsed.handle == "zomato"
        assert parsed.counterparty == "ZOMATOLTD"

    def test_ifsc_and_reference_numbers_are_not_mistaken_for_names(self):
        parsed = parse_narration(HDFC_SWIGGY)

        assert "YESB0000001" not in parsed.counterparty
        assert parsed.reference == "435820912"

    @pytest.mark.parametrize(
        ("raw", "channel"),
        [
            ("ACH D- HOUSING RENT SHOBHA APARTMENTS", Channel.ACH),
            ("ATM WDL BANNERGHATTA RD BLR", Channel.ATM),
            ("NEFT/FREELANCE INVOICE 14/512884120", Channel.NEFT),
            ("IMPS/RENT MAY/512210047", Channel.IMPS),
            ("SALARY CREDIT ACME TECHNOLOGIES PVT LTD", Channel.UNKNOWN),
        ],
    )
    def test_detects_the_rail(self, raw, channel):
        assert parse_narration(raw).channel is channel

    def test_falls_back_to_the_longest_meaningful_segment(self):
        parsed = parse_narration("ACH D- HOUSING RENT SHOBHA APARTMENTS")

        assert parsed.vpa is None
        assert parsed.counterparty == "HOUSING RENT SHOBHA APARTMENTS"


class TestSegmentation:
    @pytest.mark.parametrize(
        ("welded", "expected"),
        [
            ("SWIGGYINSTAMART", ["swiggy", "instamart"]),
            ("MYNTRADESIGNS", ["myntra", "designs"]),
            ("NETFLIXINDIA", ["netflix", "india"]),
            ("SwiggyInstamart", ["Swiggy", "Instamart"]),
        ],
    )
    def test_unwelds_merchant_names(self, welded, expected):
        assert segment(welded) == expected

    def test_short_tokens_are_left_alone(self):
        # Splitting "ZOMATO" into "zo"+"mato" would be worse than not trying.
        assert segment("ZOMATO") == ["ZOMATO"]

    def test_a_run_the_vocabulary_cannot_cover_is_left_intact(self):
        # All or nothing: a partial split would invent words.
        assert segment("QWXZPLKJHGFD") == ["QWXZPLKJHGFD"]

    def test_already_spaced_text_survives_untouched(self):
        assert segment("SALARY CREDIT ACME") == ["SALARY", "CREDIT", "ACME"]

    def test_learned_vocabulary_excludes_long_tokens(self):
        # Otherwise the weld itself becomes a "word" and explains itself.
        vocabulary = Vocabulary()
        vocabulary.learn("SWIGGYINSTAMART")

        assert "swiggyinstamart" not in vocabulary
        assert segment("SWIGGYINSTAMART", vocabulary) == ["swiggy", "instamart"]

    def test_weld_ratio_falls_once_names_are_known(self):
        before = weld_ratio(["SWIGGYINSTAMART", "MYNTRADESIGNS", "QWXZPLKJHGFD"])
        assert 0 < before <= 1


class TestIdentification:
    def test_the_specific_name_beats_the_vpa_handle(self):
        match = identify(HDFC_SWIGGY)

        # swiggy@ybl would give "Swiggy"; the narration says Instamart.
        assert match.name == "Swiggy Instamart"
        assert match.category == "groceries"
        assert match.source == "lexicon"
        assert match.confident

    def test_matches_across_bank_formats(self):
        match = identify(ICICI_ZOMATO)

        assert match.name == "Zomato"
        assert match.category == "food"

    def test_unknown_merchants_are_still_made_readable(self):
        match = identify("UPI-KIRANASTOREBLR-kirana@okaxis-UTIB0000441-99912834-PAYMENT")

        assert match.source in {"segmented", "raw"}
        assert not match.confident
        assert match.name  # never empty

    @pytest.mark.parametrize(
        ("raw", "category"),
        [
            ("ACH D- HOUSING RENT SHOBHA APARTMENTS", "rent"),
            ("SALARY CREDIT ACME TECHNOLOGIES PVT LTD", "salary"),
            ("ATM WDL BANNERGHATTA RD BLR", "cash"),
        ],
    )
    def test_categories_fall_back_to_the_words_and_the_rail(self, raw, category):
        assert identify(raw).category == category

    def test_every_fixture_narration_resolves_to_something(self, clean_statement_path):
        statement = parse_statement(clean_statement_path)
        narrations = [t.narration for t in statement.transactions]
        vocabulary = build_vocabulary(narrations)

        matches = [identify(text, vocabulary) for text in narrations]

        assert all(match.name.strip() for match in matches)
        named = {match.name for match in matches}
        assert {"Swiggy Instamart", "Netflix", "Myntra"} <= named
