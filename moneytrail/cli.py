"""``python -m moneytrail check <statement>...``

Exits non-zero if any statement fails to reconcile, so this can gate CI the way
CiteRAG's recall floor does.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import webbrowser
from pathlib import Path

from .insights import by_category, roll_up
from .linking import find_transfers, link_card_repayments, summarise_spend
from .models import CardStatement, Statement
from .money import format_paise
from .patterns import find_duplicates, find_recurring, find_refunds
from .query import ask, build_ledger, plural
from .parsers import (
    NoParserFound,
    PasswordRequired,
    UnparseableStatement,
    parse_statement,
    supported_suffixes,
)
from .reconcile import is_tautological, reconcile, reconcile_card
from .report import render

MAX_PASSWORD_ATTEMPTS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moneytrail")
    subcommands = parser.add_subparsers(dest="command", required=True)

    credentials = argparse.ArgumentParser(add_help=False)
    credentials.add_argument(
        "--password",
        help=(
            "password for encrypted PDFs. Prefer leaving this off -- you will be "
            "prompted without echo, which keeps it out of your shell history"
        ),
    )
    credentials.add_argument(
        "--no-prompt",
        action="store_true",
        help="never ask for a password; report locked files and carry on (for CI)",
    )

    common = argparse.ArgumentParser(add_help=False, parents=[credentials])
    common.add_argument("paths", nargs="+", type=Path)

    # `ask` puts the question first, so it needs paths after rather than before.
    ask_command = subcommands.add_parser(
        "ask",
        parents=[credentials],
        help='ask the ledger a question, e.g. "how much did I spend on food in March"',
    )
    ask_command.add_argument("question")
    ask_command.add_argument("paths", nargs="+", type=Path)

    subcommands.add_parser(
        "check",
        parents=[common],
        help="parse statements and verify they reconcile to the paisa",
    )
    merchants = subcommands.add_parser(
        "merchants",
        parents=[common],
        help="who you actually paid, rolled up by merchant and category",
    )
    merchants.add_argument(
        "--top", type=int, default=15, help="how many merchants to list (default 15)"
    )
    merchants.add_argument(
        "--unmatched",
        action="store_true",
        help="list the narrations that did not resolve confidently",
    )
    subcommands.add_parser(
        "review",
        parents=[common],
        help="recurring charges, possible duplicates, and refunds that arrived",
    )
    report = subcommands.add_parser(
        "report",
        parents=[common],
        help="write a self-contained HTML page: what reconciled, what is still open",
    )
    report.add_argument(
        "--out",
        type=Path,
        default=Path("moneytrail-report.html"),
        help="where to write the page (default moneytrail-report.html)",
    )
    report.add_argument(
        "--open", action="store_true", dest="open_it", help="open it when done"
    )
    subcommands.add_parser(
        "spend",
        parents=[common],
        help=(
            "what actually left your control, with card bill repayments matched "
            "to the card purchases they settle"
        ),
    )

    args = parser.parse_args(argv)
    prompt = not args.no_prompt
    if args.command == "check":
        return _check(args.paths, args.password, prompt=prompt)
    if args.command == "merchants":
        return _merchants(
            args.paths,
            args.password,
            prompt=prompt,
            top=args.top,
            unmatched=args.unmatched,
        )
    if args.command == "review":
        return _review(args.paths, args.password, prompt=prompt)
    if args.command == "report":
        return _report(
            args.paths, args.password, prompt=prompt, out=args.out, open_it=args.open_it
        )
    if args.command == "spend":
        return _spend(args.paths, args.password, prompt=prompt)
    if args.command == "ask":
        return _ask(args.question, args.paths, args.password, prompt=prompt)
    return 2


def _ask(
    question: str,
    paths: list[Path],
    password: str | None = None,
    *,
    prompt: bool = True,
    show: int = 10,
) -> int:
    statements, failures = _load_all(paths, password, prompt=prompt)
    if statements is None:
        return 2
    if not statements:
        return 1

    answer = ask(question, build_ledger(statements))

    print(f"> {answer.question}")
    print()
    print(f"  {answer.headline}")
    print()
    if answer.filters:
        print(f"  filters   {', '.join(answer.filters)}")
    if answer.rows:
        # The rows are the point: an answer you cannot check is worth nothing.
        print(f"  evidence  {plural(answer.evidence, 'row')}")
        for row in answer.rows[:show]:
            txn = row.transaction
            print(
                f"    {txn.date}  {format_paise(txn.amount):>13}  "
                f"{_clip(row.match.name, 22):<22} {_clip(txn.narration, 44)}"
            )
        if answer.evidence > show:
            print(f"    ... and {answer.evidence - show} more")
    for caveat in answer.caveats:
        print(f"  note: {caveat}")

    if failures:
        return 1
    return 0 if answer.understood else 2


def _load_all(
    paths: list[Path], password: str | None, *, prompt: bool
) -> tuple[list | None, int]:
    """Load every statement in `paths`; None means there was nothing to read."""
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to read -- no {listed} files found in those paths")
        return None, 0

    statements = []
    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
        else:
            statements.append(statement)
    return statements, failures


def _report(
    paths: list[Path],
    password: str | None = None,
    *,
    prompt: bool = True,
    out: Path = Path("moneytrail-report.html"),
    open_it: bool = False,
) -> int:
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to read -- no {listed} files found in those paths")
        return 2

    statements = []
    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
        else:
            statements.append(statement)

    if not statements:
        return 1

    out.write_text(render(statements), encoding="utf-8")
    print(f"wrote {out.resolve()} from {len(statements)} statement(s)")
    print("  it contains your financial data -- *.html is gitignored for that reason")
    if open_it:
        webbrowser.open(out.resolve().as_uri())

    return 1 if failures else 0


def _review(paths: list[Path], password: str | None = None, *, prompt: bool = True) -> int:
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to read -- no {listed} files found in those paths")
        return 2

    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
            continue
        print(_review_report(statement))
        print()

    return 1 if failures else 0


def _review_report(statement: Statement | CardStatement) -> str:
    lines = [str(statement.source), ""]

    recurring = find_recurring(statement)
    lines.append("  recurring charges")
    if not recurring:
        lines.append("    none found -- a cadence needs at least three charges")
    for item in recurring:
        status = "active" if item.active else "stopped"
        lines.append(
            f"    {_clip(item.merchant, 28):<28} {item.cadence:<11}"
            f"{format_paise(item.typical_amount):>13}"
            f"{format_paise(item.annualised):>15} /yr   "
            f"last {item.last_seen}  {status}"
        )

    lines.append("")
    duplicates = find_duplicates(statement)
    lines.append("  possible duplicate charges")
    if not duplicates:
        lines.append("    none found")
    for dup in duplicates:
        when = (
            f"{dup.charges[0].date}"
            if dup.span_days == 0
            else f"{dup.charges[0].date} to {dup.charges[-1].date}"
        )
        settled = (
            "none refunded"
            if not dup.refunded
            else f"{dup.refunded} refunded"
        )
        lines.append(
            f"    {when}  {_clip(dup.merchant, 24):<24} "
            f"{format_paise(dup.amount):>12} x{dup.count}  "
            f"{settled}, {format_paise(dup.exposure)} still out"
        )

    lines.append("")
    refunds = find_refunds(statement)
    lines.append("  refunds that arrived")
    if not refunds:
        lines.append("    none found")
    for loop in refunds:
        lines.append(
            f"    {loop.refund.date}  {_clip(loop.merchant, 24):<24} "
            f"{format_paise(loop.amount):>12}  "
            f"{loop.lag_days} days after the {loop.charge.date} charge"
        )

    lines.append("")
    lines.append(
        "  note: a refund you were owed but never received cannot be detected -- "
        "nothing in a statement records that you asked for one"
    )
    return "\n".join(lines)


def _spend(paths: list[Path], password: str | None = None, *, prompt: bool = True) -> int:
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to read -- no {listed} files found in those paths")
        return 2

    banks: list[Statement] = []
    cards: list[CardStatement] = []
    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
        elif isinstance(statement, CardStatement):
            cards.append(statement)
        else:
            banks.append(statement)

    if not banks and not cards:
        return 1

    linkage = link_card_repayments(banks, cards)
    transfers = find_transfers(banks)
    spend = summarise_spend(banks, cards, linkage, transfers)

    print(f"{len(banks)} bank statement(s), {len(cards)} card statement(s)")
    print()
    print(f"  bank outflow            {format_paise(spend.bank_outflow):>16}")
    print(
        f"  repayments matched    - {format_paise(spend.matched_repayments):>16}"
        f"   ({len(linkage.matched)} linked to a card statement)"
    )
    print(
        f"  transfers to yourself - {format_paise(spend.internal_transfers):>16}"
        f"   ({len(transfers)} moved between your own accounts)"
    )
    print(f"  card charges          + {format_paise(spend.card_charges):>16}")
    print("  " + "-" * 40)
    print(f"  actually spent          {format_paise(spend.true_outflow):>16}")
    print()
    print(f"  bank inflow             {format_paise(spend.bank_inflow):>16}")
    print(f"  less those transfers  - {format_paise(spend.internal_transfers):>16}")
    print("  " + "-" * 40)
    print(f"  actually received       {format_paise(spend.true_inflow):>16}")
    print()

    if transfers:
        print(
            "  transfers between your own accounts (both narrations shown -- an "
            "exact-amount coincidence would look the same):"
        )
        for transfer in transfers:
            out_txn, in_txn = transfer.out_transaction, transfer.in_transaction
            print(
                f"    {out_txn.date}  {format_paise(transfer.amount):>13}  "
                f"{transfer.out_source.name} -> {transfer.in_source.name}"
                f"{'' if transfer.lag_days == 0 else f' (+{transfer.lag_days}d)'}"
            )
            print(f"        out: {_clip(out_txn.narration, 66)}")
            print(f"         in: {_clip(in_txn.narration, 66)}")
        print()

    if linkage.unmatched:
        print(
            f"  {len(linkage.unmatched)} repayment(s) totalling "
            f"{format_paise(spend.unmatched_repayments)} have no card statement "
            f"behind them, so they stay counted -- the purchases they settled are "
            f"not in front of us:"
        )
        for link in linkage.unmatched:
            txn = link.bank_transaction
            print(
                f"    {txn.date}  {format_paise(txn.amount):>13}  "
                f"{_clip(txn.narration, 58)}"
            )
        print()

    if linkage.orphan_card_payments:
        total = sum(txn.amount for _, txn in linkage.orphan_card_payments)
        print(
            f"  {len(linkage.orphan_card_payments)} card payment(s) totalling "
            f"{format_paise(total)} have no matching bank debit -- settled from an "
            f"account that was not supplied"
        )
        print()

    if spend.complete and cards:
        print("  every card repayment is accounted for by a card statement")

    return 1 if failures else 0


def _merchants(
    paths: list[Path],
    password: str | None = None,
    *,
    prompt: bool = True,
    top: int = 15,
    unmatched: bool = False,
) -> int:
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to read -- no {listed} files found in those paths")
        return 2

    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
            continue
        print(_merchant_report(statement, top=top, unmatched=unmatched))
        print()

    return 1 if failures else 0


def _merchant_report(
    statement: Statement | CardStatement, *, top: int, unmatched: bool
) -> str:
    rollup = roll_up(statement)
    lines = [f"{statement.source}  --  {rollup.transactions} transactions", ""]

    heading = f"  {'merchant':<26} {'category':<14} {'out':>14} {'in':>14}   n"
    lines.append(heading)
    lines.append("  " + "-" * (len(heading) - 2))
    for entry in rollup.entries[:top]:
        lines.append(
            f"  {_clip(entry.name, 26):<26} {_clip(entry.category, 14):<14} "
            f"{_amount(entry.debits):>14} {_amount(entry.credits):>14} "
            f"{entry.count:>3}"
        )
    if len(rollup.entries) > top:
        lines.append(f"  ... and {len(rollup.entries) - top} more")

    lines.append("")
    heading = f"  {'category':<26} {'out':>14} {'in':>14}   n"
    lines.append(heading)
    lines.append("  " + "-" * (len(heading) - 2))
    for category, debits, credits, count in by_category(rollup):
        lines.append(
            f"  {_clip(category, 26):<26} {_amount(debits):>14} "
            f"{_amount(credits):>14} {count:>3}"
        )

    kinds = ", ".join(f"{kind} {count}" for kind, count in rollup.by_kind.most_common())
    sources = ", ".join(
        f"{source} {count}" for source, count in rollup.by_source.most_common()
    )
    lines.append("")
    lines.append(f"  counterparties  {kinds}")
    lines.append(
        f"  named           {rollup.confident}/{rollup.transactions} "
        f"({rollup.coverage:.1%}) from a known merchant -- {sources}"
    )

    remaining = rollup.unclassified
    if remaining and not unmatched:
        lines.append(
            f"  {len(remaining)} unclassified -- rerun with --unmatched to see them"
        )
    elif remaining:
        lines.append(f"  {len(remaining)} unclassified:")
        seen: set[str] = set()
        for match in remaining:
            if match.name in seen:
                continue
            seen.add(match.name)
            lines.append(f"    {_clip(match.name, 34):<34} {match.narration.raw}")

    return "\n".join(lines)


def _amount(paise: int) -> str:
    return format_paise(paise) if paise else "-"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _check(
    paths: list[Path], password: str | None = None, *, prompt: bool = True
) -> int:
    targets = _expand(paths)
    if not targets:
        listed = ", ".join(sorted(supported_suffixes()))
        print(f"nothing to check -- no {listed} files found in those paths")
        return 2

    failures = 0
    for path in targets:
        statement = _load(path, password, prompt=prompt)
        if statement is None:
            failures += 1
            continue

        if isinstance(statement, CardStatement):
            result = reconcile_card(statement)
            print(result.report())
        else:
            result = reconcile(statement)
            print(result.report())
            if is_tautological(statement):
                print(
                    "  note: both endpoints were derived from the rows, so the first "
                    "and last row are not independently checked"
                )
        print()
        if not result.ok:
            failures += 1

    print(f"{len(targets) - failures}/{len(targets)} statements reconciled")
    return 1 if failures else 0


def _load(
    path: Path, password: str | None, *, prompt: bool = True, attempt: int = 1
) -> Statement | CardStatement | None:
    """Parse one file, reporting any failure rather than raising it."""
    try:
        return parse_statement(path, password=password)
    except FileNotFoundError:
        print(f"{path}\n  NOT FOUND -- no file at that path\n")
    except PasswordRequired as exc:
        if prompt and attempt < MAX_PASSWORD_ATTEMPTS:
            entered = _ask_password(path, wrong=exc.wrong)
            if entered is not None:
                return _load(path, entered, prompt=prompt, attempt=attempt + 1)
        print(f"{path}\n  LOCKED -- {exc}\n")
    except (NoParserFound, UnparseableStatement, ValueError) as exc:
        # ValueError is the backstop: parsing is driven entirely by file
        # contents, and no input should ever produce a traceback.
        print(f"{path}\n  UNREADABLE -- {exc}\n")
    return None


def _ask_password(path: Path, *, wrong: bool) -> str | None:
    """Read a password without echoing it. It is never stored or logged."""
    if not _interactive():
        return None
    print(f"{'Wrong password. ' if wrong else ''}{path.name} is encrypted.")
    sys.stdout.flush()  # the prompt must be visible before we block on input
    try:
        entered = getpass.getpass("  password (blank to skip): ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return entered or None


def _interactive() -> bool:
    """True only when there is plausibly a person at the keyboard.

    On Windows getpass reads the console directly rather than stdin, so a
    process that has a console attached but nobody watching will block forever
    even though stdin was redirected. Requiring stdout to be a terminal as well
    is what separates "someone is running this" from "output is going to a
    file or a pipe".
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # closed or replaced streams
        return False


def _expand(paths: list[Path]) -> list[Path]:
    """A directory contributes only files a parser might handle.

    A path named explicitly on the command line is always attempted, whatever
    its extension -- if the user pointed at it, they want an answer about it.
    """
    suffixes = supported_suffixes()
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix.lower() in suffixes
                )
            )
        else:
            expanded.append(path)
    return expanded


if __name__ == "__main__":
    sys.exit(main())
