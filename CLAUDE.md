# moneytrail

A local-first bank-statement ledger that provably balances. README.md covers why
reconciliation comes before categorisation; this file is how to work in it.

## Layout

- `moneytrail/` — the package (`cli.py`, `llm.py`, `export.py`, parsers, reconciliation)
- `moneytrail/web.py` — the hosted front-end's core, framework-free; `api.py` is
  the FastAPI layer and `static/index.html` the single page
- `tests/` — 430 tests, run from the repo root
- `evals/` — `runner.py` and `questions.yaml` (the golden set), plus saved run JSON
- `statements/` — sample inputs
- `scripts/` — fixture builders
- `render.yaml` — the deploy, described by the repo rather than by dashboard clicks

## Commands

Run from the repo root. Python 3.11.

```bash
pip install -e ".[dev]"     # pytest + pdfplumber + openpyxl + reportlab + pyyaml
python -m pytest -q         # 430 tests
moneytrail --help           # console script, defined in pyproject.toml
python -m moneytrail.api    # the hosted front-end on :8000, needs the web extra
```

## The dependency rule is a feature, not an accident

`dependencies = []`. The core parses CSV and reconciles with **nothing installed**,
and `llm.py` speaks HTTP over the standard library so `ask --model` works on a bare
install. Everything else is an extra: `pdf`, `xlsx`, `formats`, `evals`, `web`, `dev`.

`web.py` holds the hosted front-end's reasoning and imports no framework; `api.py`
is the only file that imports FastAPI. Keep it that way — and note that `api.py`
deliberately omits `from __future__ import annotations`, because FastAPI resolves
handler annotations against module globals and postponed evaluation breaks it.

Before adding an import, check which tier it lands in. Moving a dependency into the
core would break the claim the README makes, and that claim is a large part of why
this project reads well.

## What not to break

- **Reconciliation runs before categorisation.** The chain check walks the
  running-balance column and localises a fault to a line number; the totals check
  catches faults the chain cannot see, including rows lost off the end. If a parse
  silently drops a row, every total built on it is wrong and nothing else in the
  product will say so. Do not reorder this.
- **Statements never leave the machine** — in the CLI, absolutely. No telemetry, no
  upload, no default that sends a statement anywhere. A model is only called when
  explicitly asked. The hosted front-end is the one deliberate exception and makes
  the weaker promise it can actually keep: the bytes live inside a single request,
  in a `TemporaryDirectory` removed before the response is written, and there is no
  database, no logging of content, and no second request that could see them. Do
  not add storage, a queue, or a job id — each would break that sentence, and the
  sentence is the reason anyone would trust it over a converter that keeps files.
- **The deterministic path is gated at 100%.** CI holds the regex parser at 100% on
  the questions it was built for, and never gates on a model.
- **The served page contains no absolute URL.** Every outbound link -- the shop,
  the repository -- is handed to it at runtime by `/api/pricing`. Keeping the rule
  mechanical is the point: "this page loads nothing from anywhere else" stays
  checkable by reading the file rather than by trusting whoever added the link.
  There is a test. Do not hardcode a URL to make something simpler.
- **The certificate is never paywalled, and the gate never touches the CLI.**
  `licence.py` charges for volume -- one statement at a time free, batches with a
  key -- because the certificate is the whole argument for the product and a free
  tier without it is one more silent converter. With no `MONEYTRAIL_GUMROAD_PRODUCT_ID`
  set, everything is unlocked: self-hosting this repo must never meet a paywall.
  Gumroad answers `success: true` for a refunded purchase, so `read_gumroad` checks
  the refund, dispute, chargeback and subscription fields separately; treating
  success as "has paid" is the most expensive bug available in that file.
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
