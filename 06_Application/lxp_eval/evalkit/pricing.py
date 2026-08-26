"""One cost formula, and a client that meters what the pipeline actually spends.

The browser agent had the only correct cost formula in the codebase, buried in
`ClaudeBrowserAgent._cost`. Every OTHER stage that talks to the API -- the
framework selector, the five-judge probe council, the scorer -- went through a
raw `anthropic.Anthropic` client with no accounting at all. So `record.spend_usd`
captured the browser run and nothing else: a live job showed open connections to
api.anthropic.com while `spendUsd` stayed 0.0 and `cacheReadTokens` stayed 0.

This module is the shared home for the formula (`cost_usd`) so the two do not
diverge, and a transparent wrapper (`MeteredClient`) that sits in front of the
raw client, reads `response.usage`, and keeps a running bill the record can fold
in -- for the stages that never touch a browser as much as the one that does.
"""
from __future__ import annotations

import os
from typing import Any

__all__ = [
    "DEFAULT_PRICE_PER_M_INPUT",
    "DEFAULT_PRICE_PER_M_OUTPUT",
    "CACHE_WRITE_MULT",
    "CACHE_READ_MULT",
    "cost_usd",
    "MeteredClient",
]

# Published rates for claude-opus-4-8 in USD per 1M tokens, override via env.
# Same names and defaults the browser agent already used, so the number a run
# reports is the same wherever it is computed. See the note in
# `claude_browser_agent.py`: a stale price constant is a silent budget cut,
# because the per-step cap is enforced against this estimate.
DEFAULT_PRICE_PER_M_INPUT = float(os.getenv("LXP_PRICE_INPUT", "5.0"))
DEFAULT_PRICE_PER_M_OUTPUT = float(os.getenv("LXP_PRICE_OUTPUT", "25.0"))

#: Published cache multipliers on the INPUT rate: a write costs 1.25x a fresh
#: token, a read 0.1x. Caching moves tokens out of `input_tokens` into their own
#: counters, so a cost that only read `input_tokens` would report a meaningless
#: drop the moment caching was switched on -- and undercharge the cap.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def cost_usd(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    *,
    price_in: float = DEFAULT_PRICE_PER_M_INPUT,
    price_out: float = DEFAULT_PRICE_PER_M_OUTPUT,
) -> float:
    """USD for one turn. Cached tokens are cheaper, never free.

    This is the formula the browser agent's `_cost` used, lifted verbatim so the
    two cannot drift. Cached tokens are billed against the INPUT rate with the
    published multipliers; they add to the fresh-token cost rather than replacing
    it.
    """
    m = 1_000_000.0
    return (
        (input_tokens / m) * price_in
        + (cache_write_tokens / m) * price_in * CACHE_WRITE_MULT
        + (cache_read_tokens / m) * price_in * CACHE_READ_MULT
        + (output_tokens / m) * price_out
    )


def _int(value: Any) -> int:
    """A usage field coerced to int, treating None/absent as zero.

    `cache_read_input_tokens` and `cache_creation_input_tokens` are absent on
    responses that used no caching and can arrive as None, so a bare int() would
    crash exactly where the number matters least.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _MeteredMessages:
    """Delegates everything; meters `create`'s usage after it returns."""

    def __init__(self, messages: Any, meter: "MeteredClient") -> None:
        self._messages = messages
        self._meter = meter

    def create(self, **kwargs: Any) -> Any:
        # Forward EXACTLY what the caller built -- this is a meter, not a request
        # builder. It adds, removes and defaults nothing (no temperature/top_p).
        response = self._messages.create(**kwargs)
        self._meter._record(response)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)


class MeteredClient:
    """A transparent stand-in for `anthropic.Anthropic` that tallies spend.

    Everything other than `messages` is delegated untouched, so this cannot
    quietly become a second, divergent client. `messages.create` returns the
    real response object unchanged; the only side effect is that `spend_usd` and
    the token counters advance by whatever `response.usage` reported.
    """

    def __init__(
        self,
        client: Any,
        *,
        price_in: float = DEFAULT_PRICE_PER_M_INPUT,
        price_out: float = DEFAULT_PRICE_PER_M_OUTPUT,
    ) -> None:
        self._client = client
        self._price_in = price_in
        self._price_out = price_out
        self.spend_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    def _record(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        in_tok = _int(getattr(usage, "input_tokens", 0))
        out_tok = _int(getattr(usage, "output_tokens", 0))
        cache_read = _int(getattr(usage, "cache_read_input_tokens", 0))
        cache_write = _int(getattr(usage, "cache_creation_input_tokens", 0))
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.spend_usd += cost_usd(
            in_tok, out_tok, cache_read, cache_write,
            price_in=self._price_in, price_out=self._price_out,
        )

    def __getattr__(self, name: str) -> Any:
        # `messages` is built lazily and cached, so wrapping a client that has
        # no `.messages` (e.g. a test's bare sentinel that is never actually
        # called) does not blow up at construction -- only if someone reaches
        # for it. Everything else is delegated to the wrapped client untouched.
        if name == "messages":
            wrapped = _MeteredMessages(self._client.messages, self)
            object.__setattr__(self, "messages", wrapped)
            return wrapped
        return getattr(self._client, name)
