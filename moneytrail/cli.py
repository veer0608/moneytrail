"""``python -m moneytrail check <statement>...``

Exits non-zero if any statement fails to reconcile, so this can gate CI the way
CiteRAG's recall floor does.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .models import Statement
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

    check = subcommands.add_parser(
        "check", help="parse statements and verify they reconcile to the paisa"
    )
    check.add_argument("paths", nargs="+", type=Path)
    check.add_argument(
        "--password",
        help=(
            "password for encrypted PDFs. Prefer leaving this off -- you will be "
            "prompted without echo, which keeps it out of your shell history"
        ),
    )

    check.add_argument(
        "--no-prompt",
        action="store_true",
        help="never ask for a password; report locked files and carry on (for CI)",
    )

    args = parser.parse_args(argv)
    if args.command == "check":
        return _check(args.paths, args.password, prompt=not args.no_prompt)
    return 2


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
