"""``python -m moneytrail check <statement>...``

Exits non-zero if any statement fails to reconcile, so this can gate CI the way
CiteRAG's recall floor does.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .insights import by_category, roll_up
from .models import Statement
from .money import format_paise
from .parsers import (
    NoParserFound,
    PasswordRequired,
    UnparseableStatement,
    parse_statement,
    supported_suffixes,
)
from .reconcile import is_tautological, reconcile

MAX_PASSWORD_ATTEMPTS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moneytrail")
    subcommands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("paths", nargs="+", type=Path)
    common.add_argument(
        "--password",
        help=(
            "password for encrypted PDFs. Prefer leaving this off -- you will be "
            "prompted without echo, which keeps it out of your shell history"
        ),
    )
    common.add_argument(
        "--no-prompt",
        action="store_true",
        help="never ask for a password; report locked files and carry on (for CI)",
    )

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
    return 2


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


def _merchant_report(statement: Statement, *, top: int, unmatched: bool) -> str:
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

        result = reconcile(statement)
        print(result.report())
        if is_tautological(statement):
            print(
                "  note: both endpoints were derived from the rows, so the first and "
                "last row are not independently checked"
            )
        print()
        if not result.ok:
            failures += 1

    print(f"{len(targets) - failures}/{len(targets)} statements reconciled")
    return 1 if failures else 0


def _load(
    path: Path, password: str | None, *, prompt: bool = True, attempt: int = 1
) -> Statement | None:
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
