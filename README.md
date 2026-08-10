# moneytrail

Turn bank statements into a ledger that **provably adds up**, then ask it the
questions no banking app will answer.

> *Did that Myntra refund from March ever actually arrive?*
> *What am I paying for every month that I stopped using?*
> *Was I charged twice for anything this year?*

Local-first: statements are parsed on your machine and never leave it.

---

## The idea

Every personal-finance tool starts by categorising transactions. That is the
wrong first step. If the parse dropped a row — a wrapped narration, a page
break, a misread decimal — then every category total, every "you spent 23% more
on food" insight, and every answer built on top of it is quietly wrong, and
nothing in the product will ever tell you.

So moneytrail's first component is not a categoriser. It is a **reconciliation
gate**, and it is checked against arithmetic the bank already published:

| check | what it proves |
|---|---|
| `chain` | walking the running-balance column, every row moves the balance by exactly its own amount — localises a fault to a line number |
| `totals` | `opening + credits − debits == closing` — catches faults the chain cannot see, including rows lost off the end |

This is free ground truth. No labelling, no judgement, no model. A statement
either reconciles to the paisa or it does not, and if it does not, the tool says
which row and by how much.

**Money is an integer count of paise, everywhere.** `0.1 + 0.2 != 0.3` in
binary. A float pipeline cannot promise "to the paisa", so there are no floats
in this codebase — amounts are only rendered as rupees at the edge.

## It works

A clean statement:

```
tests/fixtures/hdfc_april_2025.csv
  bank            HDFC
  account         XXXXXXXX4471
  period          2025-04-01 -> 2025-04-25
  transactions    7 (7 carry a running balance)
  opening             ₹45,231.60  (explicit)
  credits     +       ₹87,499.00
  debits      -       ₹36,560.00
  computed            ₹96,170.60
  closing             ₹96,170.60  (explicit)
  RECONCILED to the paisa
```

The same statement with one row lost during parsing:

```
tests/fixtures/hdfc_april_2025_dropped_row.csv
  ...
  computed            ₹96,819.60
  closing             ₹96,170.60  (explicit)
  FAILED -- 2 discrepancy(ies):
    [chain] row 10: expected ₹1,27,320.60, statement says ₹1,26,671.60 (off by -₹649.00)
            -- UPI-MYNTRADESIGNS-MYNTRA@AXISBANK-UTIB0000441-509912834-ORDER
    [totals] statement total: expected ₹96,819.60, statement says ₹96,170.60 (off by -₹649.00)
```

₹649.00 is exactly the missing Netflix charge, and row 10 is the line right
after the gap. A silent data-loss bug becomes a line number.

### Reading a failure

The walk resyncs to the bank's printed balance after each break, so faults stay
local instead of cascading. That gives two distinguishable signatures:

- **one** chain fault → a row is **missing** before that line
- **two adjacent** faults with **equal and opposite** deltas → that row's
  **balance** was misread; the amounts around it are fine

## Usage

```bash
python -m moneytrail check path/to/statement.pdf
python -m moneytrail check path/to/statement.csv
python -m moneytrail check path/to/a/folder

python -m moneytrail merchants statements/
python -m moneytrail review statements/
python -m moneytrail spend statements/
```

Encrypted PDFs prompt for a password without echoing it, so it stays out of
your shell history; the password is used to open the file and then dropped.
`--password` is there for scripts, and `--no-prompt` reports locked files and
carries on, for CI.

Exit code is non-zero if anything fails to reconcile.

```bash
python -m pytest
```

## Status

**Phases 0 and 1 — done.** 102 tests.

- **CSV / TSV / delimited-text** net-banking exports.
- **PDF**, including the password-protected ones banks email you. Ruled tables
  are recovered from the borders; borderless layouts fall back to clustering
  text by position. Scanned PDFs are rejected rather than guessed at — they
  need OCR first.
- **Spreadsheets**, including the very common case of an OOXML workbook shipped
  under an `.xls` name. Magic bytes decide the format, never the extension.
- **Credit-card statements**, detected from content rather than filename.
- The reconciliation gate and the CLI.

### Cards reconcile differently

A card statement has no running balance, so there is no chain to walk. What it
does publish is a summary box, and that box is the ground truth:

| check | what it proves |
|---|---|
| `summary` | `previous − payments + purchases + fees == total due` — the issuer's own arithmetic |
| `rows-debit` / `rows-credit` | the transaction rows this parse recovered sum to the totals the box states — the only check that says the ledger is *complete* |

A statement that prints no totals is reported `UNVERIFIED` rather than passing
quietly. And the sign convention inverts: on a card, `Cr` is money coming off
what you owe, the opposite of a bank statement's credit column — reading the
bank convention there would flip the entire ledger.

Card transactions reuse the same `Transaction` type, so the merchant rollup
works on them unchanged. That matters, because the card statement is where the
merchant spend actually is.

All three formats go through one code path: a parser's only job is to recover a
grid, and `parsers/table.py` turns rows into a statement. The test suite parses
the same statement as CSV, as PDF and as a workbook, and asserts all three
ledgers are identical.

**Verified against a real bank export:** 225 transactions across three months of
an HDFC statement reconcile to the paisa — and against the closing balance the
bank prints in its own summary block, not merely against the rows' internal
consistency. Four defects surfaced in that first contact with real data, none
of which the synthetic fixtures could have found: divider rows drawn in
asterisks, amounts masked with `******`, a two-row summary block at the foot,
and an IFSC code being mistaken for a reference number. Each is now a test.

**Phase 2 — shipped.** `moneytrail merchants` rolls a ledger up by counterparty
and category.

- narrations are taken apart structurally — find the VPA, and the counterparty
  is the segment beside it, which works across banks without per-bank patterns
- welded merchant names are un-welded (`SWIGGYINSTAMART` → Swiggy Instamart) by
  minimum-cost segmentation over a vocabulary, all-or-nothing so a run that
  cannot be fully covered is left intact
- names resolve through a lexicon, and every match records *how* it was reached
  (`lexicon` / `vpa` / `prefix` / `segmented` / `raw`), because a normaliser
  that hides its confidence cannot be debugged

### What measuring it changed

Merchant coverage on the real statement came out at **18%**, and chasing that
number would have been the wrong response. Classifying the *counterparty* first
showed why it was low:

| counterparty | share of a real 225-transaction statement |
|---|---|
| person-to-person transfer | 41% |
| credit-card bill repayment | 18% |
| bank charges and interest | 5% |
| recognised merchant | 4% |
| still unclassified | 32% |

**This is not a merchant-heavy ledger.** A bigger brand lexicon would have moved
almost nothing; the value is in transfers and card bills, which is where phases
3 and 5 now point. Reporting one "resolved" percentage would have hidden that
entirely, so the tool reports the classification breakdown alongside it — a
transfer to a friend is understood, not an unresolved merchant.

The same pass found the largest single outflow filed under the wrong category:
CRED is a card-bill platform, not a fee.

### Where the money actually went

`moneytrail spend` treats a card bill payment as what it is: one event recorded
twice — a debit leaving the bank, and a credit reducing what the card says you
owe. Counting both double-counts the money.

```
  bank outflow                  ₹45,900.00
  repayments matched    -       ₹12,450.00   (1 linked to a card statement)
  card charges          +        ₹8,320.50
  ----------------------------------------
  actually spent                ₹41,770.50
```

Repayments are matched to card-side payments on an exact amount and a date
window. **A repayment with no card statement behind it stays counted**, because
the purchases it settled are not in front of us — removing it would understate
spending, and understating is the more dangerous error. The report names those
repayments rather than burying the assumption. Card payments with no matching
bank debit are reported too: they were settled from an account you did not
supply.

### The questions

`moneytrail review` reports three things, and is careful about which:

```
  recurring charges
    Housing Rent Shobha Apartme… monthly    ₹28,000.00  ₹3,40,666.67 /yr  last 2025-05-12  active
    Netflix                      monthly       ₹649.00      ₹7,896.17 /yr  last 2025-05-05  active
    Spotify                      monthly       ₹119.00      ₹1,447.83 /yr  last 2025-03-07  stopped

  possible duplicate charges
    2025-03-18  Swiggy      ₹450.00 x2  none refunded, ₹450.00 still out
    2025-02-10  Amazon    ₹1,299.00 x2  1 refunded, ₹0.00 still out

  refunds that arrived
    2025-02-20  Amazon    ₹1,299.00  10 days after the 2025-02-10 charge
    2025-04-27  Myntra    ₹2,499.00  12 days after the 2025-04-15 charge
```

- **Cadence is measured, not assumed.** Three charges at irregular gaps are a
  habit, not a subscription, and are not reported as one. Amounts that swing
  wildly disqualify a run too. Monthly rent qualifies — a cadence detector that
  only found streaming services would be missing the expensive half.
- **Duplicates are candidates, not verdicts.** Buying the same coffee twice
  looks identical to being charged twice, so each one is reported with its span
  and how much is still outstanding after any refund, and you judge.
- **Refunds that never arrived cannot be found.** Nothing in a statement records
  that you asked for one. The report says so rather than implying the absence of
  a finding means nothing is owed to you.

### More than one account

Move ₹25,000 between your own accounts and a naive merge records ₹25,000 spent
and ₹25,000 earned. Neither happened. `spend` matches debits on one account to
credits on another and removes them from both sides:

```
  bank outflow                  ₹53,600.00
  transfers to yourself -       ₹25,000.00   (1 moved between your own accounts)
  actually spent                ₹28,600.00

  bank inflow                 ₹1,05,000.00
  less those transfers  -       ₹25,000.00
  actually received             ₹80,000.00
```

Matching only ever pairs *across* accounts — a debit and credit on the same
statement is a refund, not a transfer. And because paying a friend a round sum
on the day someone pays you the same amount would look identical, both
narrations are printed so you can see exactly what was matched to what.

Card repayments and inter-account transfers turned out to be the same
operation — find the credit on another document that this debit produced — so
they share one matcher rather than two that can drift apart.

### The trust strip

```bash
python -m moneytrail report statements/ --open
```

Writes one self-contained HTML file. Deliberately a file, not a server: no
ports, no build step, and no network of any kind — there is a test asserting the
page contains no `http`, no `<script>`, no `src=` and no `@import`, so the "your
data never leaves the machine" claim is checkable by reading the output.

The layout is an argument about what matters:

1. **Was every statement read correctly?** One tile per statement — green if it
   reconciled to the paisa, amber if it reconciled only against figures taken
   from its own rows, red with the row locator and the exact delta if it failed.
   *No other finance tool tells you this*, and every number below it is
   worthless if the answer is no.
2. **Open loops** — duplicate charges never refunded, card bills paid with no
   card statement covering them, card payments no supplied account explains.
3. **Recurring** — active and stopped, with the annual cost.
4. **Where it went** — the category breakdown, last and smallest, because it is
   the part every other app already has.

Generated reports contain everything the statements do, so `*.html` is
gitignored and the command says so each time it runs.

## Roadmap

| phase | what |
|---|---|
| 6 | Natural-language query layer over the ledger, every answer traced back to the rows it came from |
| — | Grow the parser against real statements from other banks; every format so far has broken it in a new way |

### The number to report

Once it runs on real statements, the headline is not an accuracy score — it is:

> reconciles to the paisa on *N* of *M* statement-months across *K* banks,
> with every failure documented.

Anything that does not reconcile is a parser bug with a line number attached, so
the metric is also the bug tracker.

## Design notes

- **Derived endpoints are flagged.** If a statement states neither an opening
  nor a closing balance, both are reconstructed from the rows. The totals check
  still catches rows dropped in the middle, but it can no longer catch a fault
  in the first or last row. `is_tautological()` surfaces that rather than
  letting it pass as a clean reconcile.
- **Unparseable input raises.** An amount silently read as zero is worse than a
  crash, because it still reconciles against a wrong total.
- **Absence is not zero.** Indian statements leave the unused side of the
  debit/credit pair empty; `parse_optional_amount` returns `None`, not `0`.
- **Parsers map by header alias, not column position.** Every bank names its
  columns differently; adding a bank should be a new entry in a tuple, not a new
  branch in the parser.
- **A half-recovered PDF table is rejected.** If no header can be found under
  either extraction strategy the file is refused, because a table missing rows
  would still reconcile — against a wrong total.
- **Failures carry a locator.** Row number for CSV, row *and page* for PDF, so
  a discrepancy in a six-page statement is findable.
- **`.gitignore` blocks `*.pdf`, `*.csv`, `/statements/` and `/data/` by
  default**, with an explicit exception for the synthetic test fixtures. A tool
  that eats bank data must not make it easy to commit any.

## Prior art

B2B APIs (Perfios, Docsumo and similar) parse Indian statements well, but they
are built for lenders doing underwriting, not for you asking questions about
your own money. Consumer apps are cloud-hosted, closed, and usually tied to one
account. Open-source parsers mostly stop at categorisation. The gap this fills
is *local + multi-bank + answers reconciliation questions*.

## Licence

MIT.
