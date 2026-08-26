"""Making text safe to write down.

Lives at the top level, imported by both `service.store` and `evalkit.ledger`,
because encoding is not a concern either of those layers owns and `evalkit`
must never import `service`.
"""
from __future__ import annotations

#: U+FFFD. What an undecodable byte is supposed to look like once it has been
#: given up on.
REPLACEMENT = "�"


def first_lone_surrogate(value) -> str | None:
    """The first string in `value` that cannot be encoded as UTF-8, or None.

    Used to REFUSE such a payload at the API boundary rather than repair it.
    The two are not alternatives: `scrub_surrogates` keeps the store from ever
    becoming unable to write, and this keeps corruption from reaching the
    store in the first place, where scrubbing it would mean recording
    "EST\\ufffd" in an audit trail as though a human had approved it.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            hit = first_lone_surrogate(k) or first_lone_surrogate(v)
            if hit is not None:
                return hit
    elif isinstance(value, (list, tuple)):
        for v in value:
            hit = first_lone_surrogate(v)
            if hit is not None:
                return hit
    return None


def scrub_surrogates(value):
    """Make a value encodable as UTF-8, recursively. Returns it unchanged if it
    already is -- the overwhelmingly common case.

    A lone surrogate (`\\udc81`, the surrogateescape of byte 0x81) is a legal
    Python str with no UTF-8 encoding. It travels through the whole system
    unnoticed, because `json.dumps` at its default `ensure_ascii=True` escapes
    it happily and `json.loads` rebuilds it just as happily. Only the two
    components that write real bytes ever see the problem:

      - the job store, which hit it at approval time, on a plan that was fine
        and had already been reviewed by a human -- making any plan containing
        such a byte permanently unapprovable; and
      - the evidence ledger, which is the likelier source, since it records
        notes and URLs scraped from a live page.

    Dropping to `ensure_ascii=True` would also stop the crash, but it would
    mangle every accented character in a Spanish file into an escape and cost
    us the property both formats are built around: artefacts an auditor can
    open and read. So the repair happens at the edge and the files stay
    readable UTF-8.

    The byte is replaced, not preserved. It arrived as a decoding failure and
    carries no recoverable meaning, so there is nothing to lose by naming it
    U+FFFD -- and `surrogatepass` would buy its preservation at the price of
    writing a file that is not valid UTF-8 at all.
    """
    if isinstance(value, str):
        # Fast path: encode once, and only rebuild the string if that fails.
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_surrogates(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_surrogates(v) for v in value)
    return value
