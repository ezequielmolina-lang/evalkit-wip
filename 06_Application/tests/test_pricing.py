"""The one place the cost formula lives, and the client that meters it.

The bug this pins: `record.spend_usd` was set only from the browser driver, so
every token the framework / probe-council / scorer spent through a raw
`anthropic.Anthropic` client was never counted. A live run showed open
connections to the API while `spendUsd` stayed 0.0. The fix extracts the
formula the browser agent already had into `cost_usd`, and wraps the raw client
in a `MeteredClient` that reads `response.usage` and keeps a running bill.
"""
from types import SimpleNamespace

import pytest

from lxp_eval.evalkit.pricing import MeteredClient, cost_usd


def test_cost_usd_matches_the_browser_agents_formula():
    """`cost_usd` must reproduce the exact number the old `_cost` produced.

    The browser agent's `_cost` is the reference implementation; the whole
    point of extracting it is that the two never diverge. Assert against the
    literal published formula AND against the browser agent itself.
    """
    price_in, price_out = 5.0, 25.0
    in_tok, out_tok, cache_read, cache_write = 1_000, 200, 5_000, 300

    expected = (
        (in_tok / 1_000_000.0) * price_in
        + (cache_write / 1_000_000.0) * price_in * 1.25
        + (cache_read / 1_000_000.0) * price_in * 0.10
        + (out_tok / 1_000_000.0) * price_out
    )

    got = cost_usd(
        in_tok, out_tok,
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        price_in=price_in, price_out=price_out,
    )
    assert got == expected

    # And it must equal what the browser agent's own method computes, so the
    # refactor cannot silently change the bill the driver reports.
    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent

    a = ClaudeBrowserAgent.__new__(ClaudeBrowserAgent)
    a.price_per_m_input = price_in
    a.price_per_m_output = price_out
    assert got == a._cost(in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)


def _usage(**kw):
    """A usage object shaped like the SDK's, with only the fields given."""
    return SimpleNamespace(**kw)


class _FakeMessages:
    def __init__(self, parent):
        self._p = parent

    def create(self, **kwargs):
        self._p.calls.append(kwargs)
        # Return whatever response the fake was primed with, in order.
        return self._p.responses.pop(0)


class _FakeClient:
    """A minimal stand-in for `anthropic.Anthropic`."""

    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)
        self.messages = _FakeMessages(self)
        self.sentinel = "delegated-attr"


def test_metered_client_accumulates_spend_across_calls():
    r1 = SimpleNamespace(content=["a"], usage=_usage(
        input_tokens=1_000, output_tokens=200,
        cache_read_input_tokens=5_000, cache_creation_input_tokens=300,
    ))
    r2 = SimpleNamespace(content=["b"], usage=_usage(
        input_tokens=2_000, output_tokens=100,
        cache_read_input_tokens=1_000, cache_creation_input_tokens=0,
    ))
    client = _FakeClient([r1, r2])
    metered = MeteredClient(client, price_in=5.0, price_out=25.0)

    metered.messages.create(model="m", messages=[])
    metered.messages.create(model="m", messages=[])

    expected = cost_usd(1_000, 200, cache_read_tokens=5_000, cache_write_tokens=300,
                        price_in=5.0, price_out=25.0) \
        + cost_usd(2_000, 100, cache_read_tokens=1_000, cache_write_tokens=0,
                   price_in=5.0, price_out=25.0)

    assert metered.spend_usd == pytest.approx(expected)
    assert metered.cache_read_tokens == 6_000
    assert metered.cache_write_tokens == 300
    assert metered.input_tokens == 3_000
    assert metered.output_tokens == 300


def test_metered_client_is_transparent():
    resp = SimpleNamespace(content=["hi"], usage=_usage(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    ))
    client = _FakeClient([resp])
    metered = MeteredClient(client)

    out = metered.messages.create(model="m", messages=[{"role": "user"}], tools=[1])

    # The response object is returned unchanged.
    assert out is resp
    # Every kwarg was forwarded verbatim.
    assert client.calls == [{"model": "m", "messages": [{"role": "user"}], "tools": [1]}]
    # Anything else on the client is delegated untouched.
    assert metered.sentinel == "delegated-attr"


def test_metered_client_tolerates_missing_usage():
    """A response with no `usage` (or None fields) must count as zero, not crash.

    The service's fakes return bare `SimpleNamespace(content=[...])`; the wrapper
    has to survive that so it can sit in front of every client the pipeline uses.
    """
    no_usage = SimpleNamespace(content=["x"])
    partial = SimpleNamespace(content=["y"], usage=_usage(
        input_tokens=100, output_tokens=None,
        cache_read_input_tokens=None, cache_creation_input_tokens=None,
    ))
    metered = MeteredClient(_FakeClient([no_usage, partial]), price_in=5.0, price_out=25.0)

    metered.messages.create(model="m")
    assert metered.spend_usd == 0.0

    metered.messages.create(model="m")
    assert metered.spend_usd == pytest.approx(cost_usd(100, 0, price_in=5.0, price_out=25.0))
    assert metered.cache_read_tokens == 0
