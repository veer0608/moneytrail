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
from moneytrail.llm import LLMClient, Usage, build_client
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


# --- the golden set --------------------------------------------------------


@dataclass(frozen=True)
class Item:
    ask: str
    which: str
    expected: Query | None
    why: str = ""


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

    def within(self, which: str) -> list[Result]:
        return [r for r in self.results if r.item.which == which]

    def summary(self, which: str | None = None) -> dict:
        rows = self.results if which is None else self.within(which)
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


def score(parser_name: str, parse: Parser, ledger: Ledger, items: Sequence[Item]) -> Scorecard:
    card = Scorecard(parser=parser_name)
    for item in items:
        expected = answer_for(item.expected, ledger, item.ask)
        attempt = parse(item.ask, ledger)
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
    return card


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
    for which in SETS:
        if not counts.get(which):
            continue
        out += [f"{headings[which]} ({counts[which]} questions)", table(cards, which), ""]

    out += ["overall", table(cards, None), ""]
    for card in cards:
        total = card.summary()
        if total and total["total_cost"]:
            out.append(f"  {card.parser}: {_money(total['total_cost'])} for the run")
    return "\n".join(out)


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
        "--limit", type=int, help="first N questions only -- for a cheap smoke run"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help=(
            "CI mode: the built-in parser must stay at 100% query accuracy on "
            "the questions it covers. Never gates on a model"
        ),
    )
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--json", type=Path, help="write the raw results here")
    args = parser.parse_args(argv)

    ledger, items = load()
    if args.set:
        items = [i for i in items if i.which == args.set]
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

    specs = [s.strip() for s in args.models.split(",") if s.strip()]
    parsers = build_parsers(specs)
    if not parsers:
        print("no parsers to run", file=sys.stderr)
        return 2

    cards = []
    for name, parse in parsers:
        started = time.perf_counter()
        cards.append(score(name, parse, ledger, items))
        print(f"  ran {name} in {time.perf_counter() - started:.1f}s", file=sys.stderr)

    print()
    print(report(cards, ledger, items))

    if args.show_failures:
        print("missed questions")
        for card in cards:
            print(failures(card, args.set))

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
