"""Phase 7: a model picks the query, the engine still computes the answer.

Every test here runs against a scripted client. Nothing in this file may touch
a network -- an eval that costs money is a choice someone makes explicitly, not
something a test suite does on their behalf.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from moneytrail import Direction, parse_statement
from moneytrail.cli import main
from moneytrail.interpret import (
    SYSTEM,
    Interpretation,
    ask_model,
    interpret,
    to_query,
    vocabulary,
)
from moneytrail.llm import (
    PRICES,
    USER_AGENT,
    Completion,
    LLMError,
    OpenAICompatibleClient,
    Usage,
    build_client,
    parse_duration,
    load_dotenv,
    price_of,
    resolve,
)
from moneytrail.query import Period, Query, ask, build_ledger, run


@pytest.fixture
def ledger(patterns_path):
    return build_ledger([parse_statement(patterns_path)])


@pytest.fixture
def unlabelled_ledger(tmp_path):
    """One row whose narration is blank, so it resolves to no merchant name."""
    target = tmp_path / "blank.csv"
    target.write_text(
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/04/25,OPENING BALANCE,,,1000.00\n"
        "02/04/25,,100.00,,900.00\n"
        "03/04/25,CLOSING BALANCE,,,900.00\n",
        encoding="utf-8",
    )
    return build_ledger([parse_statement(target)])


class ScriptedClient:
    """Returns whatever it was told to, and remembers what it was asked."""

    def __init__(self, reply: str | Exception, model: str = "test-model") -> None:
        self.model = model
        self._reply = reply
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, json_object: bool = False):
        self.calls.append({"system": system, "user": user, "json": json_object})
        if isinstance(self._reply, Exception):
            raise self._reply
        return Completion(
            text=self._reply,
            usage=Usage(
                model=self.model,
                prompt_tokens=100,
                completion_tokens=20,
                cost_usd=price_of(self.model, 100, 20),
                latency_ms=12.5,
            ),
        )


def replying(payload: dict | str) -> ScriptedClient:
    return ScriptedClient(payload if isinstance(payload, str) else json.dumps(payload))


class TestNoNetworkWithoutAKey:
    @pytest.fixture(autouse=True)
    def unconfigured(self, monkeypatch):
        # Never let a real .env or a real key turn a unit test into a paid call.
        monkeypatch.setattr("moneytrail.llm.load_dotenv", lambda *a, **k: None)
        for name in ("MONEYTRAIL_PROVIDER", "MONEYTRAIL_MODEL"):
            monkeypatch.delenv(name, raising=False)
        for provider in ("GROQ", "OPENAI", "GEMINI", "TOGETHER", "OPENROUTER"):
            monkeypatch.delenv(f"{provider}_API_KEY", raising=False)

    def test_no_key_means_no_client_rather_than_a_broken_one(self):
        assert build_client("groq", "llama-3.3-70b-versatile") is None

    def test_a_provider_that_does_not_exist_is_not_invented(self):
        assert resolve("nonesuch") is None
        assert build_client("nonesuch") is None

    def test_the_command_falls_back_and_says_so(self, patterns_path, capsys):
        code = main(
            ["ask", "how much did i spend on rent in march", str(patterns_path),
             "--model", "llama-3.3-70b-versatile"]
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "no API key configured" in out
        assert "₹28,000.00" in out  # the deterministic answer, unchanged
        assert "built-in parser" in out

    def test_asking_without_the_flag_never_reaches_for_a_model(
        self, patterns_path, capsys
    ):
        main(["ask", "how much did i spend on rent in march", str(patterns_path)])
        out = capsys.readouterr().out

        assert "parsed by the built-in parser" in out
        assert "note:" not in out


class TestReadingDotEnv:
    """Whatever the shell wrote it as, the key has to come out.

    A .env written by PowerShell is UTF-16. Read as UTF-8 it becomes mojibake
    and the key silently does not load -- indistinguishable from having no key,
    which is the worst way for this to fail.
    """

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

    @pytest.mark.parametrize(
        ("encoding", "label"),
        [
            ("utf-8", "plain utf-8"),
            ("utf-8-sig", "utf-8 with a BOM, as Set-Content writes it"),
            ("utf-16", "utf-16, as PowerShell's > writes it"),
        ],
    )
    def test_the_key_loads_however_the_shell_encoded_it(
        self, tmp_path, monkeypatch, encoding, label
    ):
        (tmp_path / ".env").write_text(
            "GROQ_API_KEY=gsk_abc123\n", encoding=encoding
        )

        load_dotenv(tmp_path)

        assert os.environ.get("GROQ_API_KEY") == "gsk_abc123", label

    def test_a_key_already_set_is_not_overwritten(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "from-the-environment")
        (tmp_path / ".env").write_text("GROQ_API_KEY=from-the-file\n", encoding="utf-8")

        load_dotenv(tmp_path)

        assert os.environ["GROQ_API_KEY"] == "from-the-environment"

    def test_no_file_is_not_an_error(self, tmp_path):
        load_dotenv(tmp_path)  # must not raise

        assert "GROQ_API_KEY" not in os.environ

    def test_comments_and_blank_lines_are_skipped(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# a comment\n\nGROQ_API_KEY=gsk_xyz\n", encoding="utf-8"
        )

        load_dotenv(tmp_path)

        assert os.environ.get("GROQ_API_KEY") == "gsk_xyz"

    def test_quotes_around_the_value_are_stripped(self, tmp_path):
        (tmp_path / ".env").write_text('GROQ_API_KEY="gsk_quoted"\n', encoding="utf-8")

        load_dotenv(tmp_path)

        assert os.environ.get("GROQ_API_KEY") == "gsk_quoted"


class TestTheRequestItself:
    """What goes on the wire, checked without going near a wire."""

    def _sent(self, monkeypatch, status=200, body=None):
        """Capture the Request the client would send."""
        import urllib.request
        from contextlib import contextmanager

        captured = {}

        @contextmanager
        def fake_urlopen(request, timeout=None):
            captured["request"] = request

            class Response:
                headers: dict = {}

                def read(self):
                    return json.dumps(
                        body
                        or {
                            "choices": [{"message": {"content": "{}"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            "model": "test",
                        }
                    ).encode()

            yield Response()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleClient(
            base_url="https://example.invalid/v1", api_key="k", model="test"
        )
        client.complete(system="s", user="u", json_object=True)
        return captured["request"]

    def test_the_client_names_itself(self, monkeypatch):
        # urllib's default User-Agent gets a Cloudflare 1010 from Groq, which
        # is a 403 that looks nothing like the auth problem it is not.
        request = self._sent(monkeypatch)

        assert request.get_header("User-agent") == USER_AGENT
        assert "urllib" not in request.get_header("User-agent")

    def test_the_key_travels_as_a_bearer_token(self, monkeypatch):
        assert self._sent(monkeypatch).get_header("Authorization") == "Bearer k"

    def test_nothing_creative_is_asked_for(self, monkeypatch):
        payload = json.loads(self._sent(monkeypatch).data)

        assert payload["temperature"] == 0
        assert payload["response_format"] == {"type": "json_object"}

    def test_the_ledger_is_never_sent(self, monkeypatch, ledger):
        # The one thing this module must never do. The model gets the schema
        # and the vocabulary; it never gets the rows or a single amount.
        import urllib.request
        from contextlib import contextmanager

        captured = {}

        @contextmanager
        def fake_urlopen(request, timeout=None):
            captured["body"] = request.data.decode()

            class Response:
                headers: dict = {}

                def read(self):
                    return json.dumps(
                        {
                            "choices": [{"message": {"content": '{"intent":"total"}'}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                        }
                    ).encode()

            yield Response()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleClient(
            base_url="https://example.invalid/v1", api_key="k", model="test"
        )

        interpret("how much did i spend", ledger, client)

        sent = captured["body"]
        for amount in ("28000", "2800000", "649", "85000"):
            assert amount not in sent, f"an amount reached the model: {amount}"
        for narration in ("ACH D- HOUSING RENT", "UPI-", "NEFT"):
            assert narration not in sent, f"a narration reached the model: {narration}"


class TestPacing:
    """The limit that binds is tokens a minute, and the server publishes it."""

    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("185ms", 0.185),
            ("6.5s", 6.5),
            ("2m30s", 150.0),
            ("1h50m52.8s", 6652.8),
            ("7.66s", 7.66),
            ("", None),
            ("soon", None),
        ],
    )
    def test_the_reset_header_is_understood(self, text, seconds):
        assert parse_duration(text) == seconds

    def _client(self):
        return OpenAICompatibleClient(
            base_url="https://example.invalid/v1", api_key="k", model="test"
        )

    def test_it_waits_when_the_bucket_is_nearly_empty(self, monkeypatch):
        slept = []
        monkeypatch.setattr("moneytrail.llm.time.sleep", slept.append)

        self._client()._pace(
            {"x-ratelimit-remaining-tokens": "200", "x-ratelimit-reset-tokens": "6s"}
        )

        assert slept and slept[0] == pytest.approx(6.2)

    def test_it_does_not_wait_while_there_is_room(self, monkeypatch):
        slept = []
        monkeypatch.setattr("moneytrail.llm.time.sleep", slept.append)

        self._client()._pace(
            {"x-ratelimit-remaining-tokens": "11963", "x-ratelimit-reset-tokens": "185ms"}
        )

        assert slept == []

    def test_a_provider_that_publishes_nothing_is_not_paced(self, monkeypatch):
        slept = []
        monkeypatch.setattr("moneytrail.llm.time.sleep", slept.append)

        self._client()._pace({})

        assert slept == []

    def test_a_long_reset_is_capped_rather_than_hanging_the_run(self, monkeypatch):
        # A daily bucket can report hours. Sleeping that long is not pacing.
        slept = []
        monkeypatch.setattr("moneytrail.llm.time.sleep", slept.append)

        self._client()._pace(
            {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1h50m52.8s"}
        )

        assert slept == [65.0]


class TestCost:
    def test_an_unpriced_model_costs_none_not_nothing(self):
        # A zero here would put a free-looking row in the scorecard for a model
        # nobody priced, which is worse than admitting the gap.
        assert price_of("some-model-nobody-priced", 1000, 1000) is None

    def test_a_priced_model_costs_what_the_table_says(self):
        prompt_rate, completion_rate = PRICES["llama-3.3-70b-versatile"]

        cost = price_of("llama-3.3-70b-versatile", 1_000_000, 1_000_000)

        assert cost == pytest.approx(prompt_rate + completion_rate)

    def test_usage_knows_whether_it_is_priced(self):
        unpriced = Usage("x", 1, 1, None, 1.0)
        priced = Usage("y", 1, 1, 0.5, 1.0)

        assert not unpriced.priced
        assert priced.priced


class TestValidation:
    def test_a_well_formed_query_survives(self, ledger):
        query, reason = to_query(
            {
                "intent": "total",
                "category": "rent",
                "period": {
                    "start": "2025-03-01",
                    "end": "2025-03-31",
                    "label": "March 2025",
                },
                "direction": "debit",
            },
            ledger,
        )

        assert reason is None
        assert query == Query(
            "total",
            category="rent",
            period=Period(date(2025, 3, 1), date(2025, 3, 31), "March 2025"),
            direction=Direction.DEBIT,
        )

    def test_an_invented_merchant_is_refused_not_answered(self, ledger):
        query, reason = to_query({"intent": "total", "merchant": "Tesco"}, ledger)

        assert query is None
        assert "Tesco" in reason

    def test_an_invented_category_is_refused_too(self, ledger):
        query, reason = to_query({"intent": "total", "category": "crypto"}, ledger)

        assert query is None
        assert "crypto" in reason

    def test_an_intent_the_engine_does_not_have_is_refused(self, ledger):
        query, reason = to_query({"intent": "forecast"}, ledger)

        assert query is None
        assert "forecast" in reason

    def test_a_declining_model_is_taken_at_its_word(self, ledger):
        query, reason = to_query(
            {"intent": None, "reason": "no such merchant in the list"}, ledger
        )

        assert query is None
        assert reason == "no such merchant in the list"

    def test_fields_the_intent_never_reads_are_dropped(self, ledger):
        # The engine would ignore these anyway. Dropping them is what keeps the
        # query an honest record of what was measured.
        query, reason = to_query(
            {
                "intent": "recurring",
                "merchant": "Netflix",
                "period": {"start": "2025-03-01", "end": "2025-03-31"},
                "direction": "debit",
            },
            ledger,
        )

        assert reason is None
        assert query == Query("recurring")

    def test_a_backwards_period_is_refused(self, ledger):
        query, reason = to_query(
            {
                "intent": "total",
                "period": {"start": "2025-05-01", "end": "2025-01-01"},
            },
            ledger,
        )

        assert query is None
        assert "ends before it starts" in reason

    def test_a_period_that_is_not_a_date_is_refused(self, ledger):
        query, reason = to_query(
            {"intent": "total", "period": {"start": "March", "end": "April"}}, ledger
        )

        assert query is None
        assert "ISO" in reason

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("debit", Direction.DEBIT),
            ("credit", Direction.CREDIT),
            # Unstated means spending, and is written down rather than left
            # implicit -- see test_an_unstated_direction_is_written_down.
            (None, Direction.DEBIT),
        ],
    )
    def test_directions_map(self, ledger, given, expected):
        query, reason = to_query({"intent": "total", "direction": given}, ledger)

        assert reason is None
        assert query.direction == expected

    def test_a_nonsense_direction_is_refused(self, ledger):
        query, reason = to_query({"intent": "total", "direction": "sideways"}, ledger)

        assert query is None
        assert "sideways" in reason

    def test_an_unstated_direction_is_written_down_as_debit(self, ledger):
        # run() reads an unset direction as debit, so leaving it None would
        # make the query say something other than what the engine did with it.
        query, _ = to_query({"intent": "total", "category": "rent"}, ledger)

        assert query.direction is Direction.DEBIT

    def test_that_default_is_not_invented_for_intents_that_ignore_direction(
        self, ledger
    ):
        # refunds and the pattern sweeps never read direction; filling one in
        # would put a filter in the query that nothing applies.
        for intent in ("refunds", "recurring", "duplicates"):
            query, _ = to_query({"intent": intent}, ledger)

            assert query.direction is None, intent

    def test_a_stated_direction_is_left_alone(self, ledger):
        query, _ = to_query({"intent": "total", "direction": "credit"}, ledger)

        assert query.direction is Direction.CREDIT

    def test_the_default_matches_what_the_engine_actually_does(self, ledger):
        # The point of writing the default down: these must not diverge.
        stated, _ = to_query({"intent": "total", "direction": "debit"}, ledger)
        unstated, _ = to_query({"intent": "total"}, ledger)

        assert run(stated, ledger).amount == run(unstated, ledger).amount

    def test_on_card_false_means_every_account_not_only_the_bank(self, ledger):
        query, _ = to_query({"intent": "total", "on_card": False}, ledger)

        assert query.on_card is None


class TestParsingWhatModelsActuallySend:
    @pytest.mark.parametrize(
        "reply",
        [
            '{"intent": "duplicates"}',
            '```json\n{"intent": "duplicates"}\n```',
            '```\n{"intent": "duplicates"}\n```',
            'Sure! Here is the query:\n{"intent": "duplicates"}\nHope that helps.',
        ],
    )
    def test_a_fence_or_a_preamble_does_not_defeat_it(self, ledger, reply):
        read = interpret("was i charged twice", ledger, ScriptedClient(reply))

        assert read.query == Query("duplicates")

    def test_prose_with_no_json_at_all_is_a_failure_not_a_guess(self, ledger):
        read = interpret("hello", ledger, ScriptedClient("I cannot help with that."))

        assert read.query is None
        assert read.failed
        assert "did not return JSON" in read.reason

    def test_a_dead_api_is_carried_not_raised(self, ledger):
        read = interpret("anything", ledger, ScriptedClient(LLMError("HTTP 401")))

        assert read.failed
        assert not read.ok
        assert "401" in read.reason


class TestThePrompt:
    def test_it_carries_this_ledgers_own_vocabulary(self, ledger):
        text = vocabulary(ledger)

        assert "Netflix" in text
        assert "rent" in text
        assert str(ledger.last_date) in text

    def test_unlabelled_rows_are_not_offered_as_names(self, unlabelled_ledger):
        # "(no narration)" is not something anyone asks about, and listing it
        # invites the model to select it.
        assert "(no narration)" in unlabelled_ledger.merchants  # the fixture bites
        merchant_section = vocabulary(unlabelled_ledger).split("Category names")[0]

        assert "(no narration)" not in merchant_section
        assert "Merchant names (0)" in merchant_section

    def test_the_model_is_told_not_to_do_arithmetic(self):
        assert "never compute" in SYSTEM
        assert "engine does the arithmetic" in SYSTEM

    def test_json_mode_is_requested(self, ledger):
        client = replying({"intent": "recurring"})

        interpret("subscriptions", ledger, client)

        assert client.calls[0]["json"] is True


class TestTheModelNeverTouchesTheNumbers:
    def test_a_model_query_and_a_typed_query_get_the_same_arithmetic(self, ledger):
        typed = ask("how much did i spend on rent in march", ledger)
        modelled = ask_model(
            "how much did i spend on rent in march",
            ledger,
            replying(
                {
                    "intent": "total",
                    "category": "rent",
                    "period": {
                        "start": "2025-03-01",
                        "end": "2025-03-31",
                        "label": "March 2025",
                    },
                    "direction": "debit",
                }
            ),
        )

        assert modelled.amount == typed.amount
        assert modelled.rows == typed.rows

    def test_an_answer_still_carries_the_rows_behind_it(self, ledger):
        answer = ask_model(
            "netflix?", ledger, replying({"intent": "total", "merchant": "Netflix"})
        )

        assert answer.amount == sum(row.transaction.amount for row in answer.rows)
        assert all(row.match.name == "Netflix" for row in answer.rows)

    def test_an_invented_merchant_gets_a_refusal_not_a_confident_zero(self, ledger):
        # The dangerous failure: the engine would answer this with 0, which
        # reads as "you spent nothing there" rather than "no such merchant".
        answer = ask_model(
            "how much at tesco", ledger, replying({"intent": "total", "merchant": "Tesco"})
        )

        assert not answer.understood
        assert answer.amount is None
        assert answer.rows == ()
        assert "Tesco" in answer.headline

    def test_a_refusal_says_it_was_a_refusal_to_measure(self, ledger):
        answer = ask_model(
            "what about tesco",
            ledger,
            replying({"intent": None, "reason": "Tesco is not in the merchant list"}),
        )

        assert not answer.understood
        assert any("refusal to measure" in caveat for caveat in answer.caveats)

    def test_a_model_reaching_past_the_schema_cannot_widen_the_answer(self, ledger):
        # intent=top ignores merchant by construction, so a model cannot use it
        # to quietly turn a ranking into a single-merchant total.
        answer = ask_model(
            "biggest in march",
            ledger,
            replying({"intent": "top", "merchant": "Netflix", "category": None}),
        )

        assert answer.understood
        assert "biggest" in answer.headline

    def test_an_empty_ledger_is_not_worth_a_paid_call(self):
        client = replying({"intent": "total"})

        answer = ask_model("anything", build_ledger([]), client)

        assert not answer.understood
        assert client.calls == []


class TestTheSeamIsTheSameSeam:
    def test_a_model_query_runs_through_the_very_same_engine(self, ledger):
        query = Query("total", merchant="Netflix", direction=Direction.DEBIT)

        read = interpret(
            "netflix", ledger, replying({"intent": "total", "merchant": "Netflix"})
        )

        assert read.query == query
        assert run(read.query, ledger).amount == run(query, ledger).amount

    def test_what_the_call_cost_is_recorded(self, ledger):
        read = interpret("netflix", ledger, replying({"intent": "total"}))

        assert read.usage.prompt_tokens == 100
        assert read.usage.completion_tokens == 20
        assert read.usage.latency_ms == 12.5
        assert read.usage.model == "test-model"

    def test_an_interpretation_with_no_query_is_not_ok(self):
        assert not Interpretation(reason="nope").ok
