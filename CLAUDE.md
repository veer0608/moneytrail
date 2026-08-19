# moneytrail

A local-first bank-statement ledger that provably balances. README.md covers why
reconciliation comes before categorisation; this file is how to work in it.

## Layout

- `moneytrail/` — the package (`cli.py`, `llm.py`, `export.py`, parsers, reconciliation)
- `tests/` — 364 tests, run from the repo root
- `evals/` — `runner.py` and `questions.yaml` (the golden set), plus saved run JSON
- `statements/` — sample inputs
- `scripts/` — fixture builders

## Commands

Run from the repo root. Python 3.11.

```bash
pip install -e ".[dev]"     # pytest + pdfplumber + openpyxl + reportlab + pyyaml
python -m pytest -q         # 364 tests
moneytrail --help           # console script, defined in pyproject.toml
```

## The dependency rule is a feature, not an accident

`dependencies = []`. The core parses CSV and reconciles with **nothing installed**,
and `llm.py` speaks HTTP over the standard library so `ask --model` works on a bare
install. Everything else is an extra: `pdf`, `xlsx`, `formats`, `evals`, `dev`.

Before adding an import, check which tier it lands in. Moving a dependency into the
core would break the claim the README makes, and that claim is a large part of why
this project reads well.

## What not to break

- **Reconciliation runs before categorisation.** The chain check walks the
  running-balance column and localises a fault to a line number; the totals check
  catches faults the chain cannot see, including rows lost off the end. If a parse
  silently drops a row, every total built on it is wrong and nothing else in the
  product will say so. Do not reorder this.
- **Statements never leave the machine.** No telemetry, no upload, no default that
  sends a statement anywhere. A model is only called when explicitly asked.
- **The deterministic path is gated at 100%.** CI holds the regex parser at 100% on
  the questions it was built for, and never gates on a model.
- **An unverifiable parse is not a pass.** A card statement that prints no totals
  has no discrepancies precisely because nothing could be compared. `export.py`
  requires at least one check to have *run* before it stamps `RECONCILED`, and
  there is a test for it. Treating "no discrepancies" as success would make the
  certificate worthless on exactly the statements that need it most.

## Environment

Machine-wide constraints (PowerShell, no Docker, Groq's invisible daily cap) live in
`~/.claude/CLAUDE.md` and are not repeated here.

## Worth adding

This repo has no `.claude/skills/`, unlike schemablind, citerag and agencydesk. The
eval-running discipline in `schemablind/.claude/skills/run-eval` applies almost
verbatim here and is worth porting.
