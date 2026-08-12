"""Talking to a model, when there is one to talk to.

Deliberately thin. The model's only job in this project is to turn English
into a `Query`; the engine computes every number that gets reported, so this
module never has to be trusted with arithmetic and never sees a rupee figure.

Every provider worth comparing speaks the OpenAI chat-completions shape --
Groq, OpenAI, Gemini's compatibility endpoint, Together, OpenRouter, a local
Ollama -- so there is one client here and a table of base URLs, and swapping
provider is one environment variable. It is written against `urllib` from the
standard library rather than an SDK, which is why `moneytrail` still installs
with no dependencies at all and still works with no key present.

Every call records what it cost: model id, both token counts, price, and
latency. An unpriced model reports `None` rather than zero -- a scorecard
column of confident zeroes is exactly the failure this project exists to
avoid, and it is better for the table to say it does not know.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: One value swaps the provider. The model can be named separately, or left to
#: the provider's default below.
PROVIDER_ENV = "MONEYTRAIL_PROVIDER"
MODEL_ENV = "MONEYTRAIL_MODEL"

#: urllib introduces itself as "Python-urllib/3.11", and providers sitting
#: behind Cloudflare refuse that outright -- Groq answers it with a 403 and
#: "error code: 1010", a browser-signature ban that never reaches the API and
#: looks nothing like an auth failure. So the client says who it is.
USER_AGENT = "moneytrail/0.1"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_env: str | None  # None means the endpoint needs no key, e.g. a local one
    default_model: str


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        "groq",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "llama-3.3-70b-versatile",
    ),
    "openai": Provider(
        "openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"
    ),
    "gemini": Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-2.0-flash",
    ),
    "together": Provider(
        "together",
        "https://api.together.xyz/v1",
        "TOGETHER_API_KEY",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "openrouter": Provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "meta-llama/llama-3.3-70b-instruct",
    ),
    "ollama": Provider("ollama", "http://localhost:11434/v1", None, "llama3.1"),
}

#: USD per million tokens, (prompt, completion). Published list prices, and the
#: date they were read, because they move and a stale number in a scorecard is
#: worse than no number.
#:
#: Groq's free tier bills none of this. List price is still the honest column:
#: what the comparison is for is choosing a model to run at volume, and "free
#: while the tier lasts" is not that answer.
#:
#: A model absent from this table is not free -- it is unpriced, and everything
#: downstream reports it as unknown rather than as zero.
PRICES_READ_ON = "2026-08-12"
PRICES: dict[str, tuple[float, float]] = {
    # groq -- https://groq.com/pricing
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
    # openai -- https://openai.com/api/pricing
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    # gemini -- https://ai.google.dev/pricing
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}


@dataclass(frozen=True)
class Usage:
    """What one call actually cost, in tokens, money and time."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    #: None when the model is not in `PRICES`. Never silently zero.
    cost_usd: float | None
    latency_ms: float

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage


class LLMClient(Protocol):
    """The seam. Anything with this shape can drive the model parser."""

    model: str

    def complete(self, *, system: str, user: str, json_object: bool = False) -> Completion:
        ...


class LLMError(RuntimeError):
    """A call that did not come back with an answer.

    Carried rather than raised through the eval: a model that fails on a
    question has scored zero on it, which is a result and not a crash.
    """


def price_of(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    rates = PRICES.get(model)
    if rates is None:
        return None
    prompt_rate, completion_rate = rates
    return (prompt_tokens * prompt_rate + completion_tokens * completion_rate) / 1e6


class OpenAICompatibleClient:
    """One client for every provider that speaks /chat/completions.

    Retries on the codes that mean "ask again" -- rate limits and transient
    server faults -- and gives up on the ones that mean "this will never
    work", so a wrong key fails in a second rather than after a minute of
    polite backoff.
    """

    RETRY_ON = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 60.0,
        attempts: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._attempts = attempts

    def complete(
        self, *, system: str, user: str, json_object: bool = False
    ) -> Completion:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Nothing here wants creativity. The same question should produce
            # the same query, or the eval is measuring the weather.
            "temperature": 0,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        body = self._post("/chat/completions", payload)
        latency_ms = (time.perf_counter() - started) * 1000

        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"no completion in response: {body!r:.300}") from exc

        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        # Prefer the model id the provider echoes back: it resolves aliases, so
        # the scorecard names what actually ran rather than what was asked for.
        served = body.get("model") or self.model
        return Completion(
            text=text,
            usage=Usage(
                model=served,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=price_of(served, prompt_tokens, completion_tokens),
                latency_ms=latency_ms,
            ),
        )

    def _post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = json.dumps(payload).encode()

        last = "no attempt was made"
        for attempt in range(1, self._attempts + 1):
            request = urllib.request.Request(
                f"{self.base_url}{path}", data=data, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                last = f"HTTP {exc.code}: {detail}"
                if exc.code not in self.RETRY_ON or attempt == self._attempts:
                    raise LLMError(last) from exc
                self._wait(exc, attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt == self._attempts:
                    raise LLMError(last) from exc
                self._wait(None, attempt)
        raise LLMError(last)

    def _wait(self, exc: urllib.error.HTTPError | None, attempt: int) -> None:
        """Honour Retry-After when the server sends one; back off when it does not."""
        delay = min(2.0 ** (attempt - 1), 30.0)
        if exc is not None:
            header = exc.headers.get("Retry-After") if exc.headers else None
            if header:
                try:
                    delay = min(float(header), 60.0)
                except ValueError:
                    pass
        time.sleep(delay)


#: A `.env` written by PowerShell is UTF-16, and one written by `Set-Content`
#: carries a UTF-8 BOM. Reading either as plain UTF-8 turns the key name into
#: mojibake and the key silently fails to load, which looks exactly like
#: having no key at all -- so the encoding is detected rather than assumed.
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def _decode(raw: bytes) -> str:
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


def load_dotenv(root: Path | None = None) -> None:
    """Read `.env` into the environment without overwriting what is already set.

    A convenience, not a dependency: keys live in `.env`, which is gitignored,
    so that running the eval never means putting a key on a command line where
    the shell will remember it.
    """
    target = (root or Path.cwd()) / ".env"
    if not target.is_file():
        return
    try:
        text = _decode(target.read_bytes())
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def resolve(
    provider: str | None = None, model: str | None = None
) -> tuple[Provider, str] | None:
    """Which provider and model to use, or None when nothing is configured."""
    name = (provider or os.environ.get(PROVIDER_ENV) or "").strip().lower()
    if not name:
        # Nothing named: use whichever provider has a key sitting ready.
        for candidate in PROVIDERS.values():
            if candidate.key_env and os.environ.get(candidate.key_env):
                name = candidate.name
                break
    if name not in PROVIDERS:
        return None
    chosen = PROVIDERS[name]
    return chosen, (model or os.environ.get(MODEL_ENV) or chosen.default_model)


def build_client(
    provider: str | None = None, model: str | None = None, **kwargs
) -> LLMClient | None:
    """A client, or None when there is no key -- never a client that cannot work.

    Returning None rather than raising is what lets `ask` fall back to the
    deterministic parser silently: with no provider configured, moneytrail
    behaves exactly as it did before this module existed.
    """
    load_dotenv()
    resolved = resolve(provider, model)
    if resolved is None:
        return None
    chosen, model_id = resolved
    api_key = os.environ.get(chosen.key_env) if chosen.key_env else None
    if chosen.key_env and not api_key:
        return None
    return OpenAICompatibleClient(
        base_url=chosen.base_url, api_key=api_key, model=model_id, **kwargs
    )
