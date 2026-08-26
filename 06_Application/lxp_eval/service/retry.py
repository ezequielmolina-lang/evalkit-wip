"""Bounded retry with backoff around the API calls the service drives.

A real run died outright on:

    500 - {'type': 'api_error', 'message': 'Internal server error'}

during the framework stage. The job went straight to `error` and everything
built so far was discarded. A 500 is precisely what should be retried.

This matters more than it looks. A browser run takes tens of minutes, and the
score stage runs after it. A transient 500 twenty minutes in currently throws
away the whole run AND the money already spent driving a live platform. The
cost of one more attempt is a second; the cost of not making it is the run.

WHAT THE SDK ALREADY DOES (verified against anthropic 0.116.0, not assumed --
`anthropic._constants.DEFAULT_MAX_RETRIES` and `BaseClient._should_retry`):

    * `max_retries=2` by default, i.e. 3 attempts total;
    * retries 408, 409, 429, and every status >= 500 (so 500/502/503/529 are
      all in), plus connection errors and the `x-should-retry` header;
    * exponential backoff from 0.5s, capped at 8s -- so all three attempts are
      spent inside roughly one second.

That is tuned for a request-shaped workload. It is not enough for a job that
has already spent twenty minutes of browser time and cannot be cheaply redone:
a provider incident lasting longer than a second still kills the run. So this
adds an OUTER layer on top of the SDK's inner one, with far longer waits, and
deliberately does not disable the SDK's -- the two compose, and the inner one
handles the common single-blip case without ever reaching here.

WHAT IS DELIBERATELY NOT RETRIED: 400 and 401. A malformed request and a bad
key fail identically forever; retrying only delays a clear error by half a
minute and makes the log harder to read. `400 invalid_request_error` is also
how this model reports a `temperature` / `top_p` / `top_k` parameter, and that
is a bug to surface immediately, never to paper over. Nothing in this package
constructs those parameters (see the module docstring in `jobs.py`), and a
retry loop that hid them would be actively harmful.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["RetryPolicy", "RETRYABLE_STATUS", "FATAL_STATUS", "is_transient"]

#: Statuses worth another attempt. 429 is rate limiting; 500/502/503 are the
#: provider having a bad moment; 529 is the API's own "overloaded".
RETRYABLE_STATUS = (429, 500, 502, 503, 529)

#: Statuses that will fail identically forever. Named explicitly rather than
#: left to "not in RETRYABLE_STATUS" so the intent is greppable.
FATAL_STATUS = (400, 401)


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status an exception carries, if any.

    Read by attribute rather than by isinstance against the SDK's exception
    tree, so a test can raise a plain object with a `status_code` and exercise
    the real policy. The SDK sets `status_code` on `APIStatusError`; httpx puts
    it on `.response`.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _connection_error_types() -> tuple[type, ...]:
    """Connection/timeout classes, resolved lazily and defensively.

    Imported inside the function so this module stays importable in an
    environment without the SDK -- the alternative is a service that cannot
    even report why it will not start.
    """
    types: list[type] = [ConnectionError, TimeoutError]
    try:
        import anthropic

        types.append(anthropic.APIConnectionError)  # APITimeoutError subclasses it
    except Exception:  # noqa: BLE001 -- absence is not an error here
        pass
    try:
        import httpx

        types.append(httpx.TransportError)
    except Exception:  # noqa: BLE001
        pass
    return tuple(types)


def is_transient(exc: BaseException) -> bool:
    """Is this worth trying again?

    A status we recognise decides on its own: an explicit 400 is fatal even if
    it arrived over a flaky socket. Only when there is NO status at all do we
    fall back to asking whether it was a connection failure.
    """
    status = _status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    return isinstance(exc, _connection_error_types())


@dataclass
class RetryPolicy:
    """How many times, and how long between. Injectable so tests do not sleep."""

    #: Total attempts, not retries. 1 disables the outer layer entirely.
    max_attempts: int = 5
    #: First wait, in seconds. Doubles each time, capped by `max_delay_s`.
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    #: Fraction of the delay added as random jitter, so a fleet of workers
    #: coming back from a provider incident does not stampede in lockstep.
    jitter: float = 0.25
    sleep: Callable[[float], None] = time.sleep
    _rand: Callable[[], float] = field(default=random.random, repr=False)

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait after `attempt` (1-based) has failed."""
        raw = self.base_delay_s * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay_s)
        return capped * (1.0 + self.jitter * self._rand())

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run `fn`, retrying transient failures with backoff.

        Re-raises the LAST exception once attempts run out, so the job's error
        record still names the real provider failure rather than a wrapper.
        """
        attempts = max(1, int(self.max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 -- re-raised below
                if attempt >= attempts or not is_transient(exc):
                    raise
                delay = self.delay_for(attempt)
                logger.warning(
                    "fallo transitorio de la API (%s: %s); reintento %d de %d "
                    "en %.1fs",
                    exc.__class__.__name__, exc, attempt + 1, attempts, delay,
                )
                self.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def wrap(self, client: Any) -> Any:
        """A drop-in stand-in for the client, retrying `messages.create`.

        Wrapping the CLIENT rather than each call site is what makes this cover
        every stage the service drives -- framework, probes, and score all go
        through `Engine.client_factory`, and the run stage drives a browser and
        makes no API call at all (see `evalkit.runner`, whose only transience
        is a dropped session and is handled by `SessionExpired`).

        Everything other than `messages` is delegated untouched, so this cannot
        quietly become a second, divergent client.
        """
        if self.max_attempts <= 1:
            return client
        return _RetryingClient(client, self)


class _RetryingClient:
    """Delegates everything; retries `messages.create`."""

    def __init__(self, client: Any, policy: RetryPolicy) -> None:
        self._client = client
        self._policy = policy
        self.messages = _RetryingMessages(client.messages, policy)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _RetryingMessages:
    def __init__(self, messages: Any, policy: RetryPolicy) -> None:
        self._messages = messages
        self._policy = policy

    def create(self, **kwargs: Any) -> Any:
        # NO `temperature` / `top_p` / `top_k` is added, removed, or defaulted
        # here. This layer forwards exactly what the engine built; it is a
        # retry, not a request builder.
        return self._policy.call(self._messages.create, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)
