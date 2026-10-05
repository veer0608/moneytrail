"""The ledger as Tally vouchers, so a CA can import it instead of typing it.

Every CA in India keeps the books in Tally, and a spreadsheet is one step short
of where the data has to end up. This writes Tally's own XML import format:
one Payment voucher per withdrawal, one Receipt voucher per deposit, each
posted between the client's bank ledger and a suspense ledger. Reclassifying
suspense entries to the right expense or party ledger is the accountant's
judgement, and the step they already do by hand; guessing it here would put
wrong entries into somebody's books with nothing to say they were guessed.

Two rules hold this together:

- **The bank ledger is never created or altered, only named.** It already
  exists in the client's company, with an opening balance and a history.
  Importing a master under the same name can alter it, and an import that
  quietly rewrites a client's bank ledger is the worst thing this file could
  do. If the name does not match, Tally refuses the vouchers and says which
  ledger it could not find: loud, and nothing in the books has changed.
- **The suspense ledger is created, under a name nobody already uses**, so
  creating it can never collide with a ledger the accountant made.

Stdlib only. Like CSV, this is a format the core should write with nothing
installed.

Built from Tally's documented import schema, and not yet imported into a real
Tally company. Until it has been, treat a first import as a test on a copy of
the company.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

from .export import Certificate, certify, render_certificates, rupees
from .models import CardStatement, Direction, Statement, Transaction

#: Distinctive on purpose: a ledger that already exists under this name would
#: be altered by the import, and "Suspense" alone is a name accountants use.
SUSPENSE_LEDGER = "Bank Statement Suspense"
#: Tally's built-in group for exactly this: entries awaiting classification.
SUSPENSE_GROUP = "Suspense A/c"

#: XML 1.0 cannot carry most control characters at all, escaped or not, and a
#: PDF narration occasionally arrives with one in it. Tally rejects the whole
#: file over a single bad byte, so they are removed rather than escaped.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def default_bank_ledger(statement: Statement | CardStatement) -> str:
    """A guess at the bank ledger's name, used only when none was given.

    It will often be wrong -- accountants name ledgers after the account,
    ``HDFC Bank A/c 4471`` -- and that is why the name can be passed in.
    A wrong guess fails loudly in Tally rather than posting anywhere.
    """
    if isinstance(statement, CardStatement):
        issuer = statement.issuer if statement.issuer != "unknown" else ""
        return f"{issuer} Credit Card".strip()
    bank = statement.bank if statement.bank != "unknown" else ""
    if not bank:
        return "Bank Account"
    # "State Bank Of India" names a bank mid-string; only a bare "HDFC" needs it.
    return bank if re.search(r"\bbank\b", bank, re.IGNORECASE) else f"{bank} Bank"


def export_tally(
    statements: Sequence[Statement | CardStatement],
    out: Path,
    *,
    bank_ledger: str | None = None,
    suspense_ledger: str = SUSPENSE_LEDGER,
) -> list[Certificate]:
    """Write a Tally import file and return the certificates.

    ``bank_ledger`` names the ledger every statement posts against. Left out,
    each statement guesses its own from the bank's name. One name for several
    statements is right for a year of one account and wrong for two banks in
    one batch; the accountant knows which they uploaded and this does not.

    An unreconciled statement is still written, as every export here is, and
    the certificate rides at the top of the file as a comment. Unlike a
    workbook, nobody reads an XML file before importing it, so the CLI's exit
    code and the page's red tile are what actually stand between a broken
    parse and a client's books.
    """
    out = Path(out)
    named = (bank_ledger or "").strip()
    suspense = suspense_ledger.strip() or SUSPENSE_LEDGER
    certificates = [certify(statement) for statement in statements]

    envelope = ET.Element("ENVELOPE")
    envelope.append(ET.Comment(_comment_safe(render_certificates(certificates))))
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"
    importing = ET.SubElement(ET.SubElement(envelope, "BODY"), "IMPORTDATA")
    description = ET.SubElement(importing, "REQUESTDESC")
    ET.SubElement(description, "REPORTNAME").text = "Vouchers"
    data = ET.SubElement(importing, "REQUESTDATA")

    # The one master this file creates, before any voucher that posts to it.
    message = ET.SubElement(data, "TALLYMESSAGE")
    ledger = ET.SubElement(message, "LEDGER", NAME=suspense)
    ET.SubElement(ledger, "NAME").text = suspense
    ET.SubElement(ledger, "PARENT").text = SUSPENSE_GROUP

    for statement in statements:
        bank = named or default_bank_ledger(statement)
        for txn in statement.transactions:
            message = ET.SubElement(data, "TALLYMESSAGE")
            message.append(_voucher(txn, bank=bank, suspense=suspense))

    ET.indent(envelope)
    ET.ElementTree(envelope).write(out, encoding="utf-8", xml_declaration=True)
    return certificates


def _voucher(txn: Transaction, *, bank: str, suspense: str) -> ET.Element:
    """One row as one voucher, debit and credit side summing to zero.

    Tally signs amounts from the ledger's point of view: a debit is negative
    and flagged ``ISDEEMEDPOSITIVE``. A withdrawal debits suspense and credits
    the bank; a deposit is the mirror.
    """
    withdrawal = txn.direction is Direction.DEBIT
    kind = "Payment" if withdrawal else "Receipt"
    debited, credited = (suspense, bank) if withdrawal else (bank, suspense)

    voucher = ET.Element("VOUCHER", VCHTYPE=kind, ACTION="Create")
    ET.SubElement(voucher, "DATE").text = txn.date.strftime("%Y%m%d")
    ET.SubElement(voucher, "VOUCHERTYPENAME").text = kind
    ET.SubElement(voucher, "PERSISTEDVIEW").text = "Accounting Voucher View"
    ET.SubElement(voucher, "NARRATION").text = _clean(txn.narration)

    amount = rupees(txn.amount)
    _entry(voucher, debited, debit=True, amount=amount)
    _entry(voucher, credited, debit=False, amount=amount)
    return voucher


def _entry(voucher: ET.Element, ledger: str, *, debit: bool, amount) -> None:
    entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(entry, "LEDGERNAME").text = ledger
    ET.SubElement(entry, "ISDEEMEDPOSITIVE").text = "Yes" if debit else "No"
    ET.SubElement(entry, "AMOUNT").text = f"-{amount}" if debit else f"{amount}"


def _clean(text: str) -> str:
    return _ILLEGAL_XML.sub("", text)


def _comment_safe(text: str) -> str:
    """XML forbids ``--`` inside a comment, and the headline is full of them."""
    text = _clean(text)
    while "--" in text:
        text = text.replace("--", "-")
    return f"\n{text}\n"
