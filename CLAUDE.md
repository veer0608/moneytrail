# moneytrail

A local-first bank-statement ledger that provably balances. README.md covers why
reconciliation comes before categorisation; this file is how to work in it.

## Layout

- `moneytrail/` — the package (`cli.py`, `llm.py`, `export.py`, parsers, reconciliation)
- `moneytrail/parsers/words.py` — grid recovery for PDFs that draw no table
- `moneytrail/web.py` — the hosted front-end's core, framework-free; `api.py` is
  the FastAPI layer and `static/index.html` the single page
- `tests/` — 482 tests, run from the repo root
- `evals/` — `runner.py` and `questions.yaml` (the golden set), plus saved run JSON
- `statements/` — sample inputs
- `scripts/` — fixture builders
- `render.yaml` — the deploy, described by the repo rather than by dashboard clicks

## Commands

Run from the repo root. Python 3.11.

```bash
pip install -e ".[dev]"     # pytest + pdfplumber + openpyxl + reportlab + pyyaml
python -m pytest -q         # 482 tests
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
- **The PDF passes run most-informed first, and the order is load-bearing.**
  Ruling, then `words.py`, then pdfplumber's whitespace clustering. That last one
  finds a header often enough to *look* like it worked while splitting "HDFC BANK"
  across two columns and losing the running-balance column — which costs the chain
  check and leaves the statement reporting RECONCILED on half the evidence.
  Succeeding worse is not succeeding sooner. Do not move it earlier.
- **In `words.py`, a row is not a line.** Banks wrap narrations over several lines
  and put the date and the amounts on whichever they like — HDFC prints the date
  first and the money on the next line; ICICI centres the dated line inside the
  narration, so text arrives above *and* below the figures. A row starts at a
  dated line and absorbs its neighbours. Narration printed above a dated line
  attaches to the transaction before it: that misplaces words, never money, and
  the reconciliation gate catches it if that is ever untrue.
- **Thresholds in `words.py` are measured, not assumed.** Word spaces and column
  gaps are two populations that are obvious within a line and not comparable
  between them. On a real HDFC header they are 1.8pt and 12.7pt; a fixed 12.0 sat
  0.7pt from merging two money columns and losing a side of the ledger.
- **Prose about balances is not a balance row.** HDFC prints "Closing balance
  includes funds earmarked for hold" under *every* page. A cross-column label
  only marks an endpoint when the row also carries a figure — otherwise the parse
  stops on page one and silently discards everything after it. In `words.py` the
  same rule drops it entirely rather than passing it on, because a row with no
  date and no figures is indistinguishable from a wrapped narration downstream
  and gets glued onto the last transaction.
- **The CI fixture list is checked by a test.** A hand-maintained list drifts, and
  it did: five fixtures including both word-position PDFs sat unlisted while their
  unit tests passed, so that whole path could have broken with CI green. Adding a
  fixture means naming it in `ci.yml` or exempting it with a reason.
- **Date order is inferred per file, never configured.** `03/04` is undecidable
  alone and usually decidable across a statement: a component above twelve
  cannot be a month, and failing that, statements run forwards. This is the one
  reading the reconciliation gate cannot check — dates play no part in the
  arithmetic, so an American statement read day-first passes every check with
  every date below the twelfth silently wrong. A file that contradicts itself
  raises. A genuinely undecidable one is marked `assumed` on the certificate
  rather than downgraded, because a third of short statements are undecidable
  and colouring them all amber would teach people to ignore amber.
- **Three checks, not two.** Where a bank prints its own column totals (Axis
  labels the row `TRANSACTION TOTAL`) they are parsed into `stated_debits` /
  `stated_credits` and compared. This is not redundant with the other two: when
  both endpoints are derived, a row lost off the *end* takes the closing balance
  with it, and chain and totals both pass on a statement missing a transaction.
  `None` means not stated, never zero.
- **Format differences are normalised, not enumerated.** The alias tables in
  `parsers/table.py` are a losing game on their own: they already carried
  `balance (inr)` with a space, which matched nothing ICICI actually writes.
  Currency suffixes are stripped in `normalise_header` instead. `(Dr)`/`(Cr)`
  must survive that strip -- they name a direction, not a unit.
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
