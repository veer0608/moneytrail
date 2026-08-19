"""Who has paid, decided without ever storing who they are.

The obvious way to sell this is accounts and subscriptions: sign up, log in,
a row per customer. That way is closed here. It needs a database, a session, a
password reset flow and somewhere to keep an email address -- and the sentence
this whole product rests on is that there is no database and nothing is kept
between requests. A paid tier that forces storage would sell the reason anyone
trusts it in order to charge for it.

So: licence keys. The buyer pays on Gumroad, gets a key, and pastes it into the
page. The key travels with each upload and is checked against Gumroad on the
way past. Nothing is stored, nothing is registered, and there is no account to
breach. A key is a low-value bearer token, which is the right shape for
something that unlocks batch conversion and nothing else.

Three rules this module exists to keep:

**The certificate is never paywalled.** It is the entire argument for the
product and the only thing no competitor can print. Charging for it would leave
the free tier as one more silent converter and take away the reason to come
back. What is charged for is volume -- the free tier does one statement at a
time, and a firm reconciling a dozen clients a month pays to stop doing them
one at a time.

**No product id configured means everything is unlocked.** The CLI, a local
run, and anyone self-hosting this open-source repo must never meet a paywall.
The gate only exists where a product id has been set, which is the hosted
service and nowhere else.

**A verified key survives an outage.** Gumroad going down must not lock out
someone who paid. A key that verified recently keeps working from cache for a
grace period; a key never seen before is refused with a message that says to
try again, which is a different sentence from "that key is not valid" and has
to stay different.

HTTP is spoken over `urllib` for the same reason `llm.py` does it: the package
installs with no dependencies, and a payment check is not worth breaking that.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

#: Set this to the Gumroad product's id to turn the gate on. Unset means off.
PRODUCT_ENV = "MONEYTRAIL_GUMROAD_PRODUCT_ID"
#: Where to send someone who needs a key. Shown by the page, never hardcoded
#: into it, so the listing can move without a redeploy of the front-end.
BUY_URL_ENV = "MONEYTRAIL_BUY_URL"
#: What the page says it costs, e.g. "₹299". Kept out of the HTML because a
#: price is the thing most likely to change and the worst thing to have to
#: hunt for in markup -- and because it must match Gumroad exactly, which is
#: only true if one of them is not a copy.
PRICE_ENV = "MONEYTRAIL_PRICE"
PERIOD_ENV = "MONEYTRAIL_PRICE_PERIOD"

VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"
#: urllib introduces itself as "Python-urllib/3.11" and gets 403'd by the
#: bot rules in front of several APIs. `llm.py` learned this the hard way.
USER_AGENT = "moneytrail/0.1"

#: One statement at a time without a key. Enough to see the certificate on your
#: own bank's format, which is the only thing that decides whether this is worth
#: paying for.
FREE_FILES = 1

#: How long a verdict is reused before asking Gumroad again.
CACHE_SECONDS = 300.0
#: How long a previously-good verdict keeps working when Gumroad is unreachable.
#: A day is long enough to cover an outage and short enough that a refund still
#: takes effect the same week.
GRACE_SECONDS = 24 * 60 * 60.0


@dataclass(frozen=True)
class Entitlement:
    """What this request is allowed to do, and why."""

    files: int
    licensed: bool
    #: Empty when nothing went wrong. Set when a key was offered and refused,
    #: so the page can say which of the several different things happened.
    problem: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.problem)


FREE = Entitlement(files=FREE_FILES, licensed=False)


@dataclass
class _Remembered:
    good: bool
    problem: str
    at: float


class Licences:
    """Checks keys against Gumroad and remembers the answer briefly.

    `verify` is injectable so the tests never touch the network. Nothing here
    logs a key: they are short, they are bearer tokens, and a log line is the
    easiest way to leak one.
    """

    def __init__(
        self,
        product_id: str | None = None,
        *,
        paid_files: int,
        verify: Callable[[str, str], tuple[bool, str]] | None = None,
        cache_seconds: float = CACHE_SECONDS,
        grace_seconds: float = GRACE_SECONDS,
    ) -> None:
        self.product_id = product_id or None
        self.paid_files = paid_files
        self._verify = verify or verify_with_gumroad
        self.cache_seconds = cache_seconds
        self.grace_seconds = grace_seconds
        self._seen: dict[str, _Remembered] = {}

    @property
    def selling(self) -> bool:
        """False when no product is configured, which unlocks everything."""
        return self.product_id is not None

    def entitlement(self, key: str | None, *, now: float | None = None) -> Entitlement:
        moment = time.monotonic() if now is None else now
        key = (key or "").strip()

        if not self.selling:
            # Self-hosted or local. There is nobody to charge.
            return Entitlement(files=self.paid_files, licensed=True)
        if not key:
            return FREE

        remembered = self._seen.get(key)
        if remembered and moment - remembered.at < self.cache_seconds:
            return self._verdict(remembered)

        try:
            good, problem = self._verify(self.product_id, key)
        except OSError:
            # Gumroad is unreachable. Someone who verified recently keeps
            # working; someone new is asked to try again, and that has to read
            # differently from being told their key is bad.
            if remembered and remembered.good and moment - remembered.at < self.grace_seconds:
                return self._verdict(remembered)
            return Entitlement(
                files=FREE_FILES,
                licensed=False,
                problem="could not reach the licence server -- try again shortly",
            )

        remembered = _Remembered(good=good, problem=problem, at=moment)
        self._seen[key] = remembered
        self._forget_idle(moment)
        return self._verdict(remembered)

    def _verdict(self, remembered: _Remembered) -> Entitlement:
        if remembered.good:
            return Entitlement(files=self.paid_files, licensed=True)
        return Entitlement(files=FREE_FILES, licensed=False, problem=remembered.problem)

    def _forget_idle(self, now: float) -> None:
        if len(self._seen) <= 4096:
            return
        stale = now - self.grace_seconds
        for key in [k for k, v in self._seen.items() if v.at <= stale]:
            del self._seen[key]


def verify_with_gumroad(
    product_id: str, key: str, *, timeout: float = 8.0
) -> tuple[bool, str]:
    """``(entitled, problem)`` for one key. Raises ``OSError`` if unreachable.

    ``increment_uses`` is false: this runs on every upload, and a counter that
    climbs with each conversion would make Gumroad's "uses" column meaningless
    and eventually trip a seller's own limits.
    """
    body = urllib.parse.urlencode(
        {
            "product_id": product_id,
            "license_key": key,
            "increment_uses": "false",
        }
    ).encode()
    request = urllib.request.Request(
        VERIFY_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (403, 404):
            # Gumroad's answer for a key that does not belong to this product.
            return False, "that key is not valid for this product"
        # 5xx and the rest are outages, not verdicts. Let the caller decide.
        raise OSError(f"licence check failed: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise OSError(f"licence check failed: {error}") from error

    return read_gumroad(payload)


def read_gumroad(payload: dict) -> tuple[bool, str]:
    """Interpret Gumroad's verify response.

    Split out from the request so the awkward cases are testable without a
    network: a refunded or charged-back purchase still verifies successfully,
    and treating `success: true` as "has paid" would leave anyone who took
    their money back with permanent access.
    """
    if not payload.get("success"):
        return False, "that key is not valid for this product"

    purchase = payload.get("purchase") or {}
    for field, message in (
        ("refunded", "that purchase was refunded"),
        ("disputed", "that purchase is disputed"),
        ("chargebacked", "that purchase was charged back"),
    ):
        if purchase.get(field):
            return False, message

    if purchase.get("subscription_cancelled_at"):
        return False, "that subscription was cancelled"
    if purchase.get("subscription_failed_at"):
        return False, "that subscription's last payment failed"

    return True, ""


def from_environment(paid_files: int) -> Licences:
    """Build the gate from the environment. Unset product id means no gate."""
    return Licences(os.environ.get(PRODUCT_ENV), paid_files=paid_files)


def buy_url() -> str:
    return os.environ.get(BUY_URL_ENV, "")


def price() -> tuple[str, str]:
    """``(amount, period)`` as the page should print them.

    Empty amount means the page says nothing about money -- the honest state
    before a real listing exists, and better than shipping an invented number
    that does not match the checkout it links to.
    """
    return os.environ.get(PRICE_ENV, ""), os.environ.get(PERIOD_ENV, "a month")
