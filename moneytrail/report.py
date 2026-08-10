"""Render the ledger as a single self-contained HTML page.

Deliberately a file, not a server. No ports, no build step, no network: the
page is one document you can open with a double-click, which is the only
delivery mechanism consistent with a tool whose whole promise is that your
statements never leave the machine.

The layout is an argument about what matters. The trust strip comes first --
whether each statement was read faithfully -- because every number underneath
it is worthless if the answer is no, and no other finance tool will tell you.
Open loops come next, because they are money. The category breakdown comes
last and smallest, because it is the part everybody else already has.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html import escape
from typing import Sequence

from .insights import by_category, roll_up
from .linking import find_transfers, link_card_repayments, summarise_spend
from .models import CardStatement, Statement
from .money import Paise, format_paise
from .patterns import find_duplicates, find_recurring
from .reconcile import is_tautological, reconcile, reconcile_card

OK = "ok"
WEAK = "weak"
FAILED = "failed"


@dataclass(frozen=True)
class Tile:
    title: str
    period: str
    detail: str
    status: str
    notes: tuple[str, ...] = ()


def build_tiles(statements: Sequence[Statement | CardStatement]) -> list[Tile]:
    tiles: list[Tile] = []
    for statement in statements:
        period = f"{statement.period_start or '?'} to {statement.period_end or '?'}"
        count = len(statement.transactions)

        if isinstance(statement, CardStatement):
            result = reconcile_card(statement)
            if not result.ok:
                status, detail = FAILED, f"{len(result.discrepancies)} discrepancy(ies)"
            elif not result.verified:
                status, detail = WEAK, "no totals published to check the rows against"
            else:
                status, detail = OK, "reconciled to the paisa"
            tiles.append(
                Tile(
                    title=f"{statement.issuer} card {statement.account_hint}".strip(),
                    period=period,
                    detail=f"{count} transactions — {detail}",
                    status=status,
                    notes=tuple(d.describe() for d in result.discrepancies),
                )
            )
            continue

        result = reconcile(statement)
        if not result.ok:
            status, detail = FAILED, f"{len(result.discrepancies)} discrepancy(ies)"
        elif is_tautological(statement):
            status, detail = WEAK, "reconciled, but both endpoints came from the rows"
        else:
            status, detail = OK, "reconciled to the paisa"
        tiles.append(
            Tile(
                title=f"{statement.bank} {statement.account_hint}".strip(),
                period=period,
                detail=f"{count} transactions — {detail}",
                status=status,
                notes=tuple(d.describe() for d in result.discrepancies),
            )
        )
    return tiles


def render(statements: Sequence[Statement | CardStatement]) -> str:
    tiles = build_tiles(statements)
    banks = [s for s in statements if not isinstance(s, CardStatement)]
    cards = [s for s in statements if isinstance(s, CardStatement)]

    sections = [
        _trust_strip(tiles),
        _open_loops(statements, banks, cards),
        _recurring(statements),
        _categories(statements),
    ]
    body = "\n".join(section for section in sections if section)
    return _PAGE.replace("{{BODY}}", body)


def _trust_strip(tiles: Sequence[Tile]) -> str:
    clean = sum(1 for tile in tiles if tile.status == OK)
    headline = f"{clean} of {len(tiles)} statements reconcile to the paisa"

    cards = []
    for tile in tiles:
        notes = "".join(f"<li>{escape(note)}</li>" for note in tile.notes)
        cards.append(
            f'<article class="tile {tile.status}">'
            f"<h3>{escape(tile.title)}</h3>"
            f'<p class="period">{escape(tile.period)}</p>'
            f"<p>{escape(tile.detail)}</p>"
            + (f"<ul>{notes}</ul>" if notes else "")
            + "</article>"
        )

    return (
        '<section><h2>Was every statement read correctly?</h2>'
        f'<p class="headline">{escape(headline)}</p>'
        f'<div class="tiles">{"".join(cards)}</div>'
        "<p class=\"aside\">Checked against arithmetic the bank already published, "
        "not against a judgement. Everything below is only as good as this.</p>"
        "</section>"
    )


def _open_loops(
    statements: Sequence[Statement | CardStatement],
    banks: Sequence[Statement],
    cards: Sequence[CardStatement],
) -> str:
    rows: list[tuple[str, str, str]] = []

    for statement in statements:
        for dup in find_duplicates(statement):
            if dup.exposure:
                rows.append(
                    (
                        str(dup.charges[0].date),
                        f"{dup.merchant} charged {format_paise(dup.amount)} "
                        f"{dup.count} times",
                        format_paise(dup.exposure),
                    )
                )

    if banks:
        linkage = link_card_repayments(banks, cards)
        for link in linkage.unmatched:
            rows.append(
                (
                    str(link.bank_transaction.date),
                    "Card bill paid, but no card statement covers it — "
                    "those purchases are unaccounted for",
                    format_paise(link.amount),
                )
            )
        for source, payment in linkage.orphan_card_payments:
            rows.append(
                (
                    str(payment.date),
                    f"{source.name} says this was paid, but no supplied "
                    f"account shows it leaving",
                    format_paise(payment.amount),
                )
            )

    if not rows:
        return (
            "<section><h2>Open loops</h2>"
            '<p class="aside">Nothing outstanding. Note that a refund you were '
            "owed but never chased cannot be detected — nothing in a statement "
            "records that you asked.</p></section>"
        )

    rows.sort()
    body = "".join(
        f"<tr><td>{escape(when)}</td><td>{escape(what)}</td>"
        f'<td class="num">{escape(amount)}</td></tr>'
        for when, what, amount in rows
    )
    return (
        "<section><h2>Open loops</h2>"
        '<div class="scroll"><table><thead><tr><th>when</th><th>what</th>'
        '<th class="num">still out</th>'
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        '<p class="aside">Duplicates are candidates, not verdicts — buying the '
        "same thing twice looks identical to being charged twice.</p></section>"
    )


def _recurring(statements: Sequence[Statement | CardStatement]) -> str:
    items = [item for statement in statements for item in find_recurring(statement)]
    if not items:
        return ""
    items.sort(key=lambda item: -item.annualised)

    body = "".join(
        f"<tr><td>{escape(item.merchant)}</td><td>{escape(item.cadence)}</td>"
        f'<td class="num">{escape(format_paise(item.typical_amount))}</td>'
        f'<td class="num">{escape(format_paise(item.annualised))}</td>'
        f"<td>{escape(str(item.last_seen))}</td>"
        f'<td class="{"ok" if item.active else "weak"}">'
        f'{"active" if item.active else "stopped"}</td></tr>'
        for item in items
    )
    return (
        "<section><h2>Recurring</h2>"
        '<div class="scroll"><table><thead><tr><th>merchant</th><th>cadence</th>'
        '<th class="num">each</th><th class="num">a year</th>'
        "<th>last seen</th><th>status</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        '<p class="aside">Cadence is measured, not assumed: irregular gaps are a '
        "habit, not a subscription.</p></section>"
    )


def _categories(statements: Sequence[Statement | CardStatement]) -> str:
    debits: Counter = Counter()
    counts: Counter = Counter()
    for statement in statements:
        for category, out, _, count in by_category(roll_up(statement)):
            debits[category] += out
            counts[category] += count
    if not debits:
        return ""

    # Salary and inbound transfers have a category but no outflow; listing them
    # here at 0% is noise in a table about where money went.
    spent = [(name, amount) for name, amount in debits.most_common() if amount]
    if not spent:
        return ""

    total = sum(amount for _, amount in spent)
    body = "".join(
        f"<tr><td>{escape(category)}</td>"
        f'<td class="num">{escape(format_paise(amount))}</td>'
        f'<td class="num">{amount * 100 // total}%</td>'
        f'<td class="num">{counts[category]}</td></tr>'
        for category, amount in spent
    )
    return (
        "<section><h2>Where it went</h2>"
        '<div class="scroll"><table><thead><tr><th>category</th>'
        '<th class="num">out</th>'
        '<th class="num">share</th><th class="num">n</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>moneytrail</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1c1b19; --muted: #6c6a66; --line: #e3e1dd;
  --panel: #ffffff; --ok: #1f7a4d; --weak: #9a6b12; --failed: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16150f; --fg: #ecebe6; --muted: #9d9a92; --line: #302e28;
    --panel: #1e1d17; --ok: #57c98b; --weak: #d6a53c; --failed: #f2827a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 .75rem; letter-spacing: -.01em; }
h3 { font-size: .95rem; margin: 0 0 .2rem; }
.sub { color: var(--muted); margin: 0 0 1rem; }
.headline { font-size: 1.15rem; font-weight: 600; margin: 0 0 1rem; }
.aside { color: var(--muted); font-size: .85rem; margin: .6rem 0 0; }
.tiles { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr)); }
.tile {
  background: var(--panel); border: 1px solid var(--line); border-radius: .5rem;
  padding: .8rem .9rem; border-left-width: 4px;
}
.tile.ok { border-left-color: var(--ok); }
.tile.weak { border-left-color: var(--weak); }
.tile.failed { border-left-color: var(--failed); }
.tile p { margin: .15rem 0; font-size: .87rem; }
.tile .period { color: var(--muted); font-variant-numeric: tabular-nums; }
.tile ul { margin: .5rem 0 0; padding-left: 1.1rem; font-size: .82rem; color: var(--failed); }
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: .8rem; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.ok { color: var(--ok); } td.weak { color: var(--muted); }
.scroll { overflow-x: auto; }
</style></head>
<body><main>
<h1>moneytrail</h1>
<p class="sub">Generated on this machine. Nothing here was sent anywhere.</p>
{{BODY}}
</main></body></html>
"""
