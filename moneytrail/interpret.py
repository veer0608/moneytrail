"""Let a model decide what was asked. It still does not decide the answer.

The model is handed the query schema and this ledger's actual vocabulary, and
returns JSON naming what to measure. That JSON is validated into a `Query` and
executed by `query.run`, which is the same code path the regex parser feeds.
So the worst a wrong model can do is measure the wrong thing -- visibly, with
the filters printed and the rows attached -- rather than report a number that
was never computed from anything.

Two things are deliberately not delegated. The arithmetic, obviously. And the
vocabulary: a model that invents a merchant gets refused by the same guard
that refuses an unknown name typed by a person, because a query for a merchant
this ledger never had returns a confident zero, and a confident zero reads
exactly like "you spent nothing there".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from .llm import Completion, LLMClient, LLMError, Usage
from .models import Direction
from .query import (
    CANNOT_ANSWER,
    INTENTS,
    READS,
    Answer,
    Ledger,
    Period,
    Query,
    refuse_unknown_fields,
    run,
)

SYSTEM = """\
You translate questions about a personal bank ledger into a JSON query object.

You never answer the question and you never compute, estimate or guess an
amount. A separate engine does the arithmetic against the actual rows. Your
only job is to say what should be measured.

Reply with ONE JSON object and nothing else -- no prose, no code fence.

  {"intent": ..., "merchant": ..., "category": ...,
   "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "..."},
   "direction": ..., "on_card": ...}

intent is exactly one of:
  total       how much was spent or received
  count       how many transactions there were
  top         which merchants were the biggest, ranked
  refunds     refunds that actually arrived
  recurring   subscriptions and regularly repeating charges
  duplicates  the same charge appearing more than once

Every other field is optional. Use null when it does not apply.
  merchant    EXACTLY one name from the merchant list, or null
  category    EXACTLY one name from the category list, or null
  period      a date range with an inclusive start and end, or null for all time
  direction   "debit" for money going out, "credit" for money coming in, or null
  on_card     true to count only credit-card rows, or null for every account

Set only the fields the chosen intent reads. Everything else must be null:
  total       merchant, category, period, direction, on_card
  count       merchant, category, period, direction
  top         category, period, direction
  refunds     merchant, period
  recurring   nothing -- it surveys the whole ledger
  duplicates  nothing -- it surveys the whole ledger

Rules that matter more than being helpful:
- Use only names that appear in the lists below, spelled exactly as given.
- If the question names a merchant, shop or category that is not in the lists,
  do not substitute the nearest one. Reply {"intent": null, "reason": "<what
  is missing>"}. Answering about a different merchant is worse than not
  answering.
- If the question is not about this ledger at all, reply the same way.
- Relative dates ("last month", "this year") resolve against the ledger's last
  transaction date given below, never against the real today.
- "spent" is direction "debit". "received", "earned", "paid me", "income" and
  "credited" are direction "credit".
"""


@dataclass(frozen=True)
class Interpretation:
    """What the model made of a question, and what asking cost."""

    query: Query | None = None
    usage: Usage | None = None
    raw: str = ""
    #: Why there is no query. A refusal is a legitimate outcome, not a failure.
    reason: str | None = None
    refused: bool = False
    failed: bool = False

    @property
    def ok(self) -> bool:
        return self.query is not None


def vocabulary(ledger: Ledger, *, limit: int = 400) -> str:
    """The ledger's own words, which is the only vocabulary the model may use.

    Unlabelled rows are excluded: "(no narration)" is not a name anyone would
    ask about, and offering it invites the model to select it.
    """
    merchants = sorted(name for name in ledger.merchants if not name.startswith("("))
    categories = sorted(ledger.categories)
    dates = [row.transaction.date for row in ledger.rows]
    span = f"{min(dates)} to {max(dates)}" if dates else "empty"

    lines = [
        f"This ledger covers {span}.",
        f"Its last transaction date is {ledger.last_date}; resolve relative dates "
        f"against that.",
        "",
        f"Merchant names ({len(merchants)}):",
    ]
    shown = merchants[:limit]
    lines += [f"  {name}" for name in shown]
    if len(merchants) > limit:
        lines.append(f"  ... and {len(merchants) - limit} more not listed")
    lines += ["", f"Category names ({len(categories)}):"]
    lines += [f"  {name}" for name in sorted(categories)]
    return "\n".join(lines)


def interpret(question: str, ledger: Ledger, client: LLMClient) -> Interpretation:
    """Ask the model what the question means. Never raises for a bad answer."""
    user = f"{vocabulary(ledger)}\n\nQuestion: {question.strip()}\n"
    try:
        completion: Completion = client.complete(
            system=SYSTEM, user=user, json_object=True
        )
    except LLMError as exc:
        return Interpretation(reason=str(exc), failed=True)

    payload = _loads(completion.text)
    if payload is None:
        return Interpretation(
            usage=completion.usage,
            raw=completion.text,
            reason="the model did not return JSON",
            failed=True,
        )

    query, reason = to_query(payload, ledger)
    if query is None:
        return Interpretation(
            usage=completion.usage, raw=completion.text, reason=reason, refused=True
        )
    return Interpretation(query=query, usage=completion.usage, raw=completion.text)


def _loads(text: str) -> dict | None:
    """Parse JSON, tolerating the code fence models add when asked not to."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate[3:]
        if candidate.lstrip().lower().startswith("json"):
            candidate = candidate.lstrip()[4:]
    candidate = candidate.strip()
    if not candidate.startswith("{"):
        # Some models narrate first. Take the outermost object if there is one.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def to_query(payload: dict, ledger: Ledger) -> tuple[Query | None, str | None]:
    """Validate JSON into a canonical `Query`, or explain why it is not one.

    Fields the chosen intent never reads are dropped rather than rejected: the
    engine would ignore them anyway, and a query that carries a filter it does
    not apply is a query that lies about what it measured.
    """
    intent = payload.get("intent")
    if intent is None:
        stated = str(payload.get("reason") or "").strip()
        return None, stated or "the model declined to answer"
    if not isinstance(intent, str) or intent.lower() not in INTENTS:
        return None, f"not an intent this engine has: {intent!r}"
    intent = intent.lower()
    reads = READS[intent]

    merchant = _named(payload.get("merchant"))
    category = _named(payload.get("category"))
    if merchant is not None and merchant not in ledger.merchants:
        return None, f'no merchant called "{merchant}" in these statements'
    if category is not None and category not in ledger.categories:
        return None, f'no category called "{category}" in these statements'

    period, bad = _period(payload.get("period"))
    if bad is not None:
        return None, bad

    direction, bad = _direction(payload.get("direction"))
    if bad is not None:
        return None, bad

    on_card = payload.get("on_card")
    on_card = True if on_card is True else None  # false and null both mean "all rows"

    chosen = {
        "merchant": merchant,
        "category": category,
        "period": period,
        "direction": direction,
        "on_card": on_card,
    }
    return Query(intent, **{k: v for k, v in chosen.items() if k in reads}), None


def _named(value) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    return value.strip() or None


def _period(value) -> tuple[Period | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, f"period should be an object, got {type(value).__name__}"
    try:
        start = date.fromisoformat(str(value["start"]))
        end = date.fromisoformat(str(value["end"]))
    except (KeyError, ValueError):
        return None, f"period needs an ISO start and end, got {value!r}"
    if end < start:
        return None, f"period ends before it starts: {start} to {end}"
    label = str(value.get("label") or "").strip() or f"{start} to {end}"
    return Period(start, end, label), None


def _direction(value) -> tuple[Direction | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip().lower()
    if text in ("debit", "out", "spent"):
        return Direction.DEBIT, None
    if text in ("credit", "in", "received"):
        return Direction.CREDIT, None
    return None, f"not a direction: {value!r}"


def ask_model(question: str, ledger: Ledger, client: LLMClient) -> Answer:
    """The model-parsed counterpart to `query.ask`, with the same guarantees."""
    if not ledger.rows:
        return Answer(question, "no statements loaded", understood=False)

    read = interpret(question, ledger, client)
    if read.query is None:
        headline = read.reason or CANNOT_ANSWER
        return Answer(
            question,
            headline,
            caveats=(
                "the model was asked only to name what to measure, so this is a "
                "refusal to measure rather than a failed calculation",
            )
            if read.refused
            else (),
            understood=False,
        )

    # The same guard a typed question meets, for the same reason.
    refusal = refuse_unknown_fields(question, ledger, read.query)
    if refusal is not None:
        return refusal
    return run(read.query, ledger, question=question)
