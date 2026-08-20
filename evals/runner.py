"""``python -m evals.runner`` -- what each parser gets right, and what it cost.

Two accuracies, reported separately because they answer different questions.

  query accuracy   did the parser ask for the same thing the gold query asks
                   for, field by field? This is the honest one.
  answer accuracy  did the number come out the same? Weaker -- a wrong query
                   can land on the right total by luck -- but it is the one a
                   person actually feels.

The deterministic parser is a row in this table like any model, at $0. On the
questions it was built for it will win, and saying so plainly is worth more
than a table arranged to flatter the expensive option.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

import yaml

from moneytrail import parse_statement
from moneytrail.interpret import interpret
from moneytrail.llm import LLMClient, QuotaExhausted, Usage, build_client
from moneytrail.models import Direction
from moneytrail.query import (
    Answer,
    Ledger,
    Period,
    Query,
    build_ledger,
    parse_question,
    refuse_unknown_fields,
    refuse_unknown_words,
    run,
)

HERE = Path(__file__).parent
REPO = HERE.parent
QUESTIONS = HERE / "questions.yaml"
SETS = ("deterministic", "model-only", "beyond-schema")
BASELINE = "built-in regex parser"
SPLITS = ("dev", "test")

#: Share of questions held back. Nothing tuned against dev may be reported on
#: test without saying so.
TEST_SHARE = 40


def split_of(ask: str) -> str:
    """Which half a question belongs to, derived from the question itself.

    A prompt improved by staring at the questions it got wrong, then scored on
    those same questions, reports a number fitted to its own answer key. So the
    split is a hash of the text rather than a field anyone can edit: whoever is
    tuning the prompt does not get to choose what they are graded on, and the
    assignment is reproducible by anyone who doubts it.
    """
    digest = hashlib.sha256(ask.strip().lower().encode()).hexdigest()
    return "test" if int(digest[:8], 16) % 100 < TEST_SHARE else "dev"


# --- the golden set --------------------------------------------------------


@dataclass(frozen=True)
class Item:
    ask: str
    which: str
    expected: Query | None
    why: str = ""

    @property
    def split(self) -> str:
        return split_of(self.ask)


def load(path: Path = QUESTIONS) -> tuple[Ledger, list[Item]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    statements = [parse_statement(REPO / p) for p in raw["ledger"]]
    ledger = build_ledger(statements)

    items = []
    for entry in raw["questions"]:
        which = entry.get("set", "deterministic")
        if which not in SETS:
            raise ValueError(f"{entry['ask']!r}: unknown set {which!r}")
        items.append(
            Item(
                ask=entry["ask"],
                which=which,
                expected=_query(entry.get("query"), entry["ask"]),
                why=entry.get("why", ""),
            )
        )
    return ledger, items


def _query(spec: dict | None, ask: str) -> Query | None:
    if spec is None:
        return None
    direction = spec.get("direction")
    return Query(
        intent=spec["intent"],
        merchant=spec.get("merchant"),
        category=spec.get("category"),
        period=_period(spec.get("period")),
        direction=Direction(direction) if direction else None,
        on_card=spec.get("on_card"),
    )


def _period(spec: dict | None) -> Period | None:
    if spec is None:
        return None
    start, end = spec["start"], spec["end"]
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    end = end if isinstance(end, date) else date.fromisoformat(str(end))
    return Period(start, end, spec.get("label") or f"{start} to {end}")


# --- comparing -------------------------------------------------------------


def same_query(left: Query | None, right: Query | None) -> bool:
    """Do these two queries measure the same thing?

    A period's label is prose for the headline and is deliberately not
    compared: "March 2025" and "1 to 31 March" select identical rows, and
    marking one wrong would be scoring the phrasing rather than the query.
    """
    if left is None or right is None:
        return left is None and right is None
    if left.intent != right.intent:
        return False
    same_fields = (left.merchant, left.category, left.direction, left.on_card) == (
        right.merchant,
        right.category,
        right.direction,
        right.on_card,
    )
    return same_fields and _same_period(left.period, right.period)


def _same_period(left: Period | None, right: Period | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return (left.start, left.end) == (right.start, right.end)


def answer_for(query: Query | None, ledger: Ledger, question: str = "") -> Answer:
    """The gold answer is whatever the engine says. That is the whole trick."""
    if query is None:
        return Answer(question, "refused", understood=False)
    return run(query, ledger, question=question)


# --- parsers ---------------------------------------------------------------


@dataclass
class Attempt:
    query: Query | None = None
    usage: Usage | None = None
    note: str = ""


Parser = Callable[[str, Ledger], Attempt]


def deterministic_parser(question: str, ledger: Ledger) -> Attempt:
    """Exactly what `query.ask` does, stopped one step before the arithmetic."""
    text = question.lower().strip()
    query = parse_question(text, ledger)
    if query is None:
        return Attempt(note="not a question it takes")
    if refuse_unknown_words(question, ledger, query, text) is not None:
        return Attempt(note="refused an unknown name")
    return Attempt(query=query)


def model_parser(client: LLMClient) -> Parser:
    def parse(question: str, ledger: Ledger) -> Attempt:
        read = interpret(question, ledger, client)
        if read.query is None:
            return Attempt(usage=read.usage, note=read.reason or "declined")
        if refuse_unknown_fields(question, ledger, read.query) is not None:
            return Attempt(usage=read.usage, note="named something not in the ledger")
        return Attempt(query=read.query, usage=read.usage)

    return parse


# --- running ---------------------------------------------------------------


@dataclass
class Result:
    item: Item
    produced: Query | None
    amount: int | None
    expected_amount: int | None
    usage: Usage | None
    note: str

    @property
    def query_ok(self) -> bool:
        return same_query(self.item.expected, self.produced)

    @property
    def answer_ok(self) -> bool:
        return self.amount == self.expected_amount

    @property
    def refused(self) -> bool:
        return self.produced is None


@dataclass
class Scorecard:
    parser: str
    results: list[Result] = field(default_factory=list)
    #: Set when the run stopped early. Such a card is reported as incomplete
    #: and never scored: the questions it never reached would otherwise count
    #: as answers it got wrong.
    abandoned: str = ""

    @property
    def complete(self) -> bool:
        return not self.abandoned

    def within(self, which: str) -> list[Result]:
        return [r for r in self.results if r.item.which == which]

    def in_split(self, split: str) -> list[Result]:
        return [r for r in self.results if r.item.split == split]

    def summary(self, which: str | None = None, split: str | None = None) -> dict:
        rows = self.results if which is None else self.within(which)
        if split is not None:
            rows = [r for r in rows if r.item.split == split]
        if not rows:
            return {}
        latencies = [r.usage.latency_ms for r in rows if r.usage]
        costs = [r.usage.cost_usd for r in rows if r.usage and r.usage.priced]
        unpriced = any(r.usage and not r.usage.priced for r in rows)
        return {
            "n": len(rows),
            "query_accuracy": sum(r.query_ok for r in rows) / len(rows),
            "answer_accuracy": sum(r.answer_ok for r in rows) / len(rows),
            "refusal_rate": sum(r.refused for r in rows) / len(rows),
            "cost_per_question": (sum(costs) / len(rows)) if costs else (None if unpriced else 0.0),
            "p50_latency_ms": statistics.median(latencies) if latencies else 0.0,
            "total_cost": sum(costs) if costs else (None if unpriced else 0.0),
        }


def score(
    parser_name: str,
    parse: Parser,
    ledger: Ledger,
    items: Sequence[Item],
    *,
    delay: float = 0.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> Scorecard:
    """Run every question through one parser.

    `delay` paces the calls. Firing them as fast as the loop can manage earns a
    stream of 429s and exponential backoff, which is both slower than pacing
    and unpredictable -- the run spends its time asleep and cannot say how much
    longer it needs.
    """
    card = Scorecard(parser=parser_name)
    for index, item in enumerate(items, 1):
        if delay and index > 1:
            time.sleep(delay)
        expected = answer_for(item.expected, ledger, item.ask)
        try:
            attempt = parse(item.ask, ledger)
        except QuotaExhausted as exc:
            card.abandoned = (
                f"stopped after {index - 1} of {len(items)} questions: {exc}"
            )
            return card
        produced = answer_for(attempt.query, ledger, item.ask)
        card.results.append(
            Result(
                item=item,
                produced=attempt.query,
                amount=produced.amount,
                expected_amount=expected.amount,
                usage=attempt.usage,
                note=attempt.note,
            )
        )
        if on_progress:
            on_progress(index, len(items))
    return card


# --- replaying a saved run -------------------------------------------------


@dataclass(frozen=True)
class ReplayItem:
    """Enough of an Item to group a saved result by set and by split."""

    ask: str
    which: str

    @property
    def split(self) -> str:
        return split_of(self.ask)


@dataclass(frozen=True)
class ReplayResult:
    """A result read back from disk, with its verdicts already decided."""

    item: ReplayItem
    query_ok: bool
    answer_ok: bool
    refused: bool
    usage: Usage | None


def replay(paths: Sequence[Path]) -> list[Scorecard]:
    """Rebuild scorecards from saved runs.

    Each model affords about one pass through the golden set a day on a free
    tier, so regenerating the published table cannot mean re-running it. The
    table is still produced by the scorer rather than typed out -- just from
    the recorded results instead of fresh calls.
    """
    cards: dict[str, Scorecard] = {}
    for path in paths:
        for name, saved in json.loads(path.read_text(encoding="utf-8")).items():
            card = cards.setdefault(name, Scorecard(parser=name))
            for row in saved["results"]:
                usage = None
                if row.get("model"):
                    usage = Usage(
                        model=row["model"],
                        prompt_tokens=row.get("prompt_tokens", 0),
                        completion_tokens=row.get("completion_tokens", 0),
                        cost_usd=row.get("cost_usd"),
                        latency_ms=row.get("latency_ms", 0.0),
                    )
                card.results.append(
                    ReplayResult(
                        item=ReplayItem(ask=row["ask"], which=row["set"]),
                        query_ok=row["query_ok"],
                        answer_ok=row["answer_ok"],
                        refused=row["refused"],
                        usage=usage,
                    )
                )
    return list(cards.values())


# --- reporting -------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "unpriced"
    if value == 0:
        return "$0"
    return f"${value:.6f}".rstrip("0").rstrip(".")


def table(cards: Sequence[Scorecard], which: str) -> str:
    head = (
        f"  {'parser':<32}{'query acc':>11}{'answer acc':>12}"
        f"{'refused':>10}{'$/question':>14}{'p50':>10}"
    )
    lines = [head, "  " + "-" * (len(head) - 2)]
    for card in cards:
        summary = card.summary(which)
        if not summary:
            continue
        lines.append(
            f"  {card.parser[:32]:<32}"
            f"{_pct(summary['query_accuracy']):>11}"
            f"{_pct(summary['answer_accuracy']):>12}"
            f"{_pct(summary['refusal_rate']):>10}"
            f"{_money(summary['cost_per_question']):>14}"
            f"{summary['p50_latency_ms']:>7.0f} ms"
        )
    return "\n".join(lines)


def report(cards: Sequence[Scorecard], ledger: Ledger, items: Sequence[Item]) -> str:
    dates = [row.transaction.date for row in ledger.rows]
    counts = {which: sum(i.which == which for i in items) for which in SETS}
    out = [
        "moneytrail -- query front-end eval",
        f"{len(ledger.rows)} rows, {min(dates)} to {max(dates)}, "
        f"{len(ledger.merchants)} merchants",
        f"{len(items)} questions: "
        + ", ".join(f"{n} {which}" for which, n in counts.items() if n),
        f"split: {sum(i.split == 'dev' for i in items)} dev, "
        f"{sum(i.split == 'test' for i in items)} test "
        f"(assigned by hash of the question, so tuning cannot pick its own marking)",
        "",
    ]
    headings = {
        "deterministic": (
            "deterministic-covered -- questions the regex parser was built for"
        ),
        "model-only": (
            "model-only -- one query expresses these, no regex parses them"
        ),
        "beyond-schema": (
            "beyond-schema -- no query expresses these; refusing is the right answer"
        ),
    }
    scored = [card for card in cards if card.complete]
    for which in SETS:
        if not counts.get(which):
            continue
        out += [f"{headings[which]} ({counts[which]} questions)", table(scored, which), ""]

    out += ["overall", table(scored, None), ""]

    for card in cards:
        if not card.complete:
            out += [
                f"  {card.parser}: NOT SCORED -- {card.abandoned}",
                "    the questions it never reached would count as answers it got "
                "wrong, so it gets no row rather than a misleading one",
                "",
            ]
    for card in cards:
        total = card.summary()
        if total and total["total_cost"]:
            out.append(f"  {card.parser}: {_money(total['total_cost'])} for the run")
    return "\n".join(out)


MARKERS = ("<!-- SCORECARD -->", "<!-- /SCORECARD -->")
LABELS = {
    "deterministic": "deterministic-covered",
    "model-only": "model-only",
    "beyond-schema": "beyond-schema",
}


def markdown(cards: Sequence[Scorecard], items: Sequence[Item]) -> str:
    """The same table, for the README.

    Generated rather than typed. A number in a README that nobody can
    regenerate is a number nobody should believe.
    """
    counts = {which: sum(i.which == which for i in items) for which in SETS}
    cards = [card for card in cards if card.complete]  # never publish a partial row
    blocks = []
    for which in (*SETS, None):
        label = which or "overall"
        if which and not counts.get(which):
            continue
        heading = (
            f"**{LABELS[which]}** ({counts[which]} questions)"
            if which
            else f"**overall** ({len(items)} questions)"
        )
        rows = [
            heading,
            "",
            "| parser | query acc | answer acc | refused | $/question | p50 |",
            "|---|---|---|---|---|---|",
        ]
        for card in cards:
            summary = card.summary(which)
            if not summary:
                continue
            rows.append(
                f"| {card.parser} | {_pct(summary['query_accuracy'])} "
                f"| {_pct(summary['answer_accuracy'])} "
                f"| {_pct(summary['refusal_rate'])} "
                f"| {_money(summary['cost_per_question'])} "
                f"| {summary['p50_latency_ms']:.0f} ms |"
            )
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def splice(readme: Path, table: str) -> bool:
    """Replace whatever sits between the scorecard markers."""
    start, end = MARKERS
    text = readme.read_text(encoding="utf-8")
    if start not in text or end not in text:
        return False
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    readme.write_text(f"{head}{start}\n\n{table}\n\n{end}{tail}", encoding="utf-8")
    return True


def failures(card: Scorecard, which: str | None = None) -> str:
    rows = [r for r in (card.within(which) if which else card.results) if not r.query_ok]
    if not rows:
        return f"  {card.parser}: nothing missed"
    lines = [f"  {card.parser}: {len(rows)} missed"]
    for result in rows:
        lines.append(f"    Q  {result.item.ask}")
        lines.append(f"    want {_show(result.item.expected)}")
        lines.append(f"    got  {_show(result.produced)}{_because(result.note)}")
        lines.append("")
    return "\n".join(lines)


def _show(query: Query | None) -> str:
    if query is None:
        return "(refuse)"
    parts = [query.intent]
    for name in ("merchant", "category", "direction", "on_card"):
        value = getattr(query, name)
        if value is not None:
            parts.append(f"{name}={getattr(value, 'value', value)}")
    if query.period:
        parts.append(f"period={query.period.start}..{query.period.end}")
    return " ".join(parts)


def _because(note: str) -> str:
    return f"   [{note}]" if note else ""


# --- entry point -----------------------------------------------------------


def build_parsers(specs: Iterable[str]) -> list[tuple[str, Parser]]:
    parsers: list[tuple[str, Parser]] = []
    for spec in specs:
        if spec in ("deterministic", "baseline", "regex"):
            parsers.append((BASELINE, deterministic_parser))
            continue
        provider, _, model = spec.rpartition(":")
        client = build_client(provider or None, model or None)
        if client is None:
            print(
                f"  skipping {spec!r}: no API key configured for it",
                file=sys.stderr,
            )
            continue
        parsers.append((client.model, model_parser(client)))
    return parsers


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m evals.runner")
    parser.add_argument(
        "--models",
        default="deterministic",
        help=(
            "comma-separated. 'deterministic' is the built-in parser; anything "
            "else is a model, optionally 'provider:model' "
            "(e.g. groq:llama-3.3-70b-versatile)"
        ),
    )
    parser.add_argument("--set", choices=SETS, help="score only one question set")
    parser.add_argument(
        "--split",
        choices=SPLITS,
        help=(
            "score only one half. Tune a prompt against dev; report test, "
            "which nothing was tuned against"
        ),
    )
    parser.add_argument(
        "--limit", type=int, help="first N questions only -- for a cheap smoke run"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help=(
            # Doubled on purpose: argparse expands help through `help % params`
            # to fill in things like %(default)s, so a lone % is read as a
            # format spec. "100% query" made `--help` itself raise.
            "CI mode: the built-in parser must stay at 100%% query accuracy on "
            "the questions it covers. Never gates on a model"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.1,
        help=(
            "seconds between model calls. The free tiers allow about 30 a "
            "minute, and pacing under that is faster than being throttled "
            "into exponential backoff (default: %(default)s)"
        ),
    )
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--json", type=Path, help="write the raw results here")
    parser.add_argument(
        "--from-json",
        type=Path,
        nargs="+",
        help=(
            "rebuild the table from saved runs instead of calling anything. "
            "One pass through the golden set is about a day's free-tier budget "
            "per model, so republishing must not mean re-running"
        ),
    )
    parser.add_argument(
        "--update-readme",
        action="store_true",
        help="write this scorecard into README.md between the scorecard markers",
    )
    args = parser.parse_args(argv)

    ledger, items = load()
    if args.set:
        items = [i for i in items if i.which == args.set]
    if args.split:
        items = [i for i in items if i.split == args.split]
    if args.limit:
        items = items[: args.limit]

    if args.gate:
        covered = [i for i in items if i.which == "deterministic"]
        card = score(BASELINE, deterministic_parser, ledger, covered)
        summary = card.summary("deterministic")
        accuracy = summary["query_accuracy"]
        print(
            f"built-in parser: {_pct(accuracy)} query accuracy on "
            f"{summary['n']} questions it covers"
        )
        if accuracy < 1.0:
            print()
            print(failures(card, "deterministic"))
            print("the built-in parser regressed on questions it used to handle")
            return 1
        return 0

    if args.from_json:
        cards = replay(args.from_json)
        scored = {r.item.ask for card in cards for r in card.results}
        items = [i for i in items if i.ask in scored]
        print()
        print(report(cards, ledger, items))
        if args.update_readme:
            readme = REPO / "README.md"
            if not splice(readme, markdown(cards, items)):
                print(f"{readme.name} has no scorecard markers", file=sys.stderr)
                return 2
            print(f"\nwrote the scorecard into {readme.name}")
        return 0

    specs = [s.strip() for s in args.models.split(",") if s.strip()]
    parsers = build_parsers(specs)
    if not parsers:
        print("no parsers to run", file=sys.stderr)
        return 2

    cards = []
    for name, parse in parsers:
        started = time.perf_counter()

        def progress(done: int, total: int, _name=name, _started=started) -> None:
            if done % 10 and done != total:
                return
            spent = time.perf_counter() - _started
            left = (spent / done) * (total - done)
            print(
                f"    {_name}: {done}/{total}  {spent:.0f}s spent, ~{left:.0f}s left",
                file=sys.stderr,
                flush=True,
            )

        cards.append(
            score(
                name,
                parse,
                ledger,
                items,
                # The baseline makes no calls, so pacing it would only waste time.
                delay=0.0 if name == BASELINE else args.delay,
                on_progress=None if name == BASELINE else progress,
            )
        )
        print(f"  ran {name} in {time.perf_counter() - started:.1f}s", file=sys.stderr, flush=True)

    print()
    print(report(cards, ledger, items))

    if args.show_failures:
        print("missed questions")
        for card in cards:
            print(failures(card, args.set))

    if args.update_readme:
        readme = REPO / "README.md"
        if splice(readme, markdown(cards, items)):
            print(f"wrote the scorecard into {readme.name}")
        else:
            print(
                f"{readme.name} has no scorecard markers to write between",
                file=sys.stderr,
            )
            return 2

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    card.parser: {
                        "overall": card.summary(),
                        "by_set": {w: card.summary(w) for w in SETS},
                        "results": [
                            {
                                "ask": r.item.ask,
                                "set": r.item.which,
                                "expected": _show(r.item.expected),
                                "produced": _show(r.produced),
                                "query_ok": r.query_ok,
                                "answer_ok": r.answer_ok,
                                "refused": r.refused,
                                "note": r.note,
                                # Recorded so a correction to the price table
                                # never means paying for the run again.
                                "model": r.usage.model if r.usage else "",
                                "prompt_tokens": r.usage.prompt_tokens if r.usage else 0,
                                "completion_tokens": (
                                    r.usage.completion_tokens if r.usage else 0
                                ),
                                "cost_usd": r.usage.cost_usd if r.usage else 0.0,
                                "latency_ms": round(r.usage.latency_ms, 1) if r.usage else 0.0,
                            }
                            for r in card.results
                        ],
                    }
                    for card in cards
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
