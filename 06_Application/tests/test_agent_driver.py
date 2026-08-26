"""AgentDriver tests — no real browser, no real API.

The adapter lets a PROSE probe step ("Abrir el curso de Matemática 3.º") be
executed by the Claude tool-use loop while satisfying the same narrow driver
interface `run_probe` already expects, so run_probe itself is untouched.
"""
from pathlib import Path

import pytest
from types import SimpleNamespace

from lxp_eval.browser_agents.agent_driver import (
    DEFAULT_MAX_ITERATIONS_PER_STEP,
    STEP_DONE_TOOL,
    STEP_TOOLS,
    AgentDriver,
    driver_kind_for,
    make_driver,
)
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.runner import run_probe
from lxp_eval.evalkit.session import SessionExpired


class FakeInnerDriver:
    """Stands in for PlaywrightDriver underneath the agent."""

    def __init__(self, *, alive=True, shots_dir=None):
        self._alive = alive
        self._shots_dir = Path(shots_dir) if shots_dir else None
        self.ops: list[str] = []
        self.session_checks = 0
        self.logged_out = False
        self.last_read_truncated = False
        self.last_read_full_chars = 0

    async def is_logged_out(self):
        return self.logged_out

    async def assert_session_alive(self):
        self.session_checks += 1
        if not self._alive:
            raise SessionExpired("la sesión expiró: la página está en /login")

    async def navigate(self, url, **kw):
        self.ops.append(f"navigate:{url}")
        return url

    async def click_by_text(self, text, **kw):
        self.ops.append(f"click:{text}")
        return True

    async def screenshot(self, name, **kw):
        self.ops.append(f"screenshot:{name}")
        path = self._shots_dir / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path

    async def read_page(self, **kw):
        self.ops.append("read_page")
        return "contenido de la página"

    async def url(self):
        return "https://app.test/lesson/5"


class FakeResult:
    def __init__(self, *, blocked=False, block_reason=None, turns=3,
                 accomplished=True, done_reason=""):
        self.blocked = blocked
        self.block_reason = block_reason
        self.turns = turns
        self.estimated_cost_usd = 0.01
        self.accomplished = accomplished
        self.done_reason = done_reason


class FakeAgent:
    """Stands in for ClaudeBrowserAgent: records the task, simulates tool calls."""

    def __init__(self, driver, *, result=None, on_run=None):
        self.driver = driver
        self.max_iterations = 25       # the adapter must tighten this
        self.cost_cap_usd = 5.0        # ...and this
        self.tools = None              # the adapter must install the step tools
        self._result = result or FakeResult()
        self._on_run = on_run
        self.tasks: list[str] = []
        self.systems: list[str] = []

    async def run(self, *, system, user_task):
        self.systems.append(system)
        self.tasks.append(user_task)
        if self._on_run is not None:
            await self._on_run(self.driver)
        return self._result


def _probe(steps, capture=("screenshot",)):
    return Probe(
        probe_id="P-1",
        test_id="2.1.2",
        goal="ver si modela",
        steps=list(steps),
        verdict_rule="PASA si modela",
        capture=list(capture),
    )


PROSE = "Abrir el curso de Matemática 3.º grado y entrar a la primera lección"


# --- a prose step reaches the agent and drives the real page --------------


@pytest.mark.asyncio
async def test_prose_step_is_handed_to_the_agent_and_drives_the_page():
    async def simulate_tool_loop(driver):
        await driver.navigate("https://app.test/math3")
        await driver.click_by_text("Lección 1")

    inner = FakeInnerDriver()
    agent = FakeAgent(inner, on_run=simulate_tool_loop)
    ad = AgentDriver(agent, inner)

    await ad.do(PROSE)

    assert agent.tasks == [PROSE] or PROSE in agent.tasks[0]  # the prose reached it
    assert inner.ops == ["navigate:https://app.test/math3", "click:Lección 1"]


@pytest.mark.asyncio
async def test_capture_and_url_delegate_to_the_inner_driver(tmp_path: Path):
    inner = FakeInnerDriver(shots_dir=tmp_path)
    ad = AgentDriver(FakeAgent(inner), inner)
    assert await ad.url() == "https://app.test/lesson/5"
    assert await ad.read_page() == "contenido de la página"
    shot = await ad.screenshot("s1")
    assert shot.exists()


# --- fail loud: a step the agent cannot accomplish ------------------------


@pytest.mark.asyncio
async def test_unaccomplishable_step_raises_naming_the_step():
    # A silently skipped step would leave the page in the wrong state and turn
    # the NEXT screenshot into false evidence. Same bug class as attaching a
    # screenshot of a different probe.
    inner = FakeInnerDriver()
    blocked = FakeResult(blocked=True, block_reason="no encontré el botón")
    ad = AgentDriver(FakeAgent(inner, result=blocked), inner)

    with pytest.raises(EvalkitError) as e:
        await ad.do(PROSE)
    msg = str(e.value)
    assert PROSE in msg                    # names the exact step
    assert "no encontré el botón" in msg   # and why the agent gave up


# --- #5: `blocked` was never the failure path that actually happened ------


def test_the_done_tool_requires_the_agent_to_say_whether_it_succeeded():
    """STEP_SYSTEM told the agent to call `done` EXPLAINING WHY IT COULD NOT do
    the step. `done` is the success path, so `blocked` stayed False, the step
    silently no-opped, and the next screenshot was evidence of the wrong page --
    exactly the bug the docstring claimed to prevent. `accomplished` is the
    field that makes the two outcomes distinguishable."""
    schema = STEP_DONE_TOOL["input_schema"]
    assert "accomplished" in schema["properties"]
    assert schema["properties"]["accomplished"]["type"] == "boolean"
    assert "accomplished" in schema["required"]
    assert "reason" in schema["properties"]
    # and it is the `done` the step loop actually offers the model
    assert [t for t in STEP_TOOLS if t["name"] == "done"] == [STEP_DONE_TOOL]


def test_the_adapter_installs_the_step_tools_on_the_agent():
    inner = FakeInnerDriver()
    agent = FakeAgent(inner)
    AgentDriver(agent, inner)
    done = [t for t in agent.tools if t["name"] == "done"]
    assert done == [STEP_DONE_TOOL]


@pytest.mark.asyncio
async def test_done_with_accomplished_false_raises_quoting_the_agents_reason():
    inner = FakeInnerDriver()
    # The realistic failure: the agent politely calls `done` to explain itself.
    excuse = "el panel del docente pide una cuenta de docente y esta es de alumno"
    result = FakeResult(blocked=False, accomplished=False, done_reason=excuse)
    ad = AgentDriver(FakeAgent(inner, result=result), inner)

    with pytest.raises(EvalkitError) as e:
        await ad.do(PROSE)
    msg = str(e.value)
    assert PROSE in msg     # names the step
    assert excuse in msg    # quotes the agent's OWN stated reason


@pytest.mark.asyncio
async def test_a_step_that_did_not_happen_captures_no_evidence(tmp_path: Path):
    """run_probe must honour it: no ledger row for a step the agent no-opped."""
    inner = FakeInnerDriver(shots_dir=tmp_path)
    result = FakeResult(accomplished=False, done_reason="no existe ese botón")
    ad = AgentDriver(FakeAgent(inner, result=result), inner)
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(EvalkitError):
        await run_probe(_probe([PROSE]), ad, ledger, platform="Plataforma A")

    assert ledger.read_all() == []
    assert "screenshot:" not in " ".join(inner.ops)


@pytest.mark.asyncio
async def test_an_agent_that_never_called_done_is_not_treated_as_success():
    inner = FakeInnerDriver()
    result = FakeResult(accomplished=None, done_reason="")
    ad = AgentDriver(FakeAgent(inner, result=result), inner)
    with pytest.raises(EvalkitError) as e:
        await ad.do(PROSE)
    assert PROSE in str(e.value)


@pytest.mark.asyncio
async def test_done_with_accomplished_true_is_the_success_path():
    inner = FakeInnerDriver()
    ad = AgentDriver(FakeAgent(inner, result=FakeResult(accomplished=True)), inner)
    await ad.do(PROSE)   # does not raise


def test_the_step_prompt_tells_the_agent_to_report_honestly():
    from lxp_eval.browser_agents.agent_driver import STEP_SYSTEM
    assert "accomplished" in STEP_SYSTEM


# --- ...and the real agent must actually HONOUR it ------------------------
# Without these two, the schema change above is decoration: the agent would go
# on sending the whole-task tool list and dropping the `accomplished` argument
# on the floor, which is the same "confidently wrong" failure in a new place.
# Constructed with object.__new__ so no API key, config or network is touched.


@pytest.mark.asyncio
async def test_the_real_agent_sends_its_instance_tool_list_not_the_global():
    from lxp_eval.browser_agents.claude_browser_agent import (
        BROWSER_TOOLS,
        AgentRunResult,
        ClaudeBrowserAgent,
    )

    agent = ClaudeBrowserAgent.__new__(ClaudeBrowserAgent)
    # a fresh agent defaults to the whole-task tools...
    assert ClaudeBrowserAgent.tools_default() == BROWSER_TOOLS
    # ...and AgentDriver's swap is what the loop would send
    agent.tools = list(STEP_TOOLS)
    assert [t for t in agent.tools if t["name"] == "done"] == [STEP_DONE_TOOL]
    assert AgentRunResult().accomplished is None  # nothing claimed until `done`


@pytest.mark.asyncio
async def test_the_real_agent_records_accomplished_from_the_done_call():
    from lxp_eval.browser_agents.claude_browser_agent import (
        AgentRunResult,
        ClaudeBrowserAgent,
    )

    agent = ClaudeBrowserAgent.__new__(ClaudeBrowserAgent)
    agent.driver = FakeInnerDriver()

    result = AgentRunResult()
    _, is_done = await agent._dispatch_tool(
        "done", {"accomplished": False, "reason": "hace falta cuenta de docente"}, result
    )
    assert is_done is True
    assert result.accomplished is False
    assert result.done_reason == "hace falta cuenta de docente"

    ok = AgentRunResult()
    await agent._dispatch_tool("done", {"accomplished": True, "reason": "abrí el panel"}, ok)
    assert ok.accomplished is True


@pytest.mark.asyncio
async def test_iteration_cap_failure_surfaces_as_an_error():
    inner = FakeInnerDriver()
    blocked = FakeResult(blocked=True, block_reason="max_iterations (8) reached")
    ad = AgentDriver(FakeAgent(inner, result=blocked), inner)
    with pytest.raises(EvalkitError) as e:
        await ad.do("dar mil vueltas")
    assert "max_iterations" in str(e.value)


# --- the loop is bounded --------------------------------------------------


def test_adapter_tightens_the_agent_caps_for_a_single_step():
    inner = FakeInnerDriver()
    agent = FakeAgent(inner)
    assert agent.max_iterations == 25  # whole-task default, too loose for one step
    AgentDriver(agent, inner)
    assert agent.max_iterations == DEFAULT_MAX_ITERATIONS_PER_STEP
    assert agent.max_iterations < 25
    assert agent.cost_cap_usd < 5.0  # per-step spend is capped too


def test_iteration_cap_is_configurable():
    inner = FakeInnerDriver()
    agent = FakeAgent(inner)
    AgentDriver(agent, inner, max_iterations_per_step=3, cost_cap_usd_per_step=0.25)
    assert agent.max_iterations == 3
    assert agent.cost_cap_usd == 0.25


# --- run_probe is unchanged and still guards the session ------------------


@pytest.mark.asyncio
async def test_run_probe_works_through_the_agent_driver(tmp_path: Path):
    async def simulate(driver):
        await driver.click_by_text("Tutor")

    inner = FakeInnerDriver(shots_dir=tmp_path)
    ad = AgentDriver(FakeAgent(inner, on_run=simulate), inner)
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")

    run = await run_probe(_probe([PROSE]), ad, ledger, platform="Plataforma A")

    assert len(run.artifacts) == 1
    rows = ledger.read_all()
    assert rows[0]["platform"] == "Plataforma A"
    assert PROSE in rows[0]["note"]  # evidence says which prose step produced it


@pytest.mark.asyncio
async def test_expired_session_still_blocks_before_the_agent_runs(tmp_path: Path):
    inner = FakeInnerDriver(alive=False, shots_dir=tmp_path)
    agent = FakeAgent(inner)
    ad = AgentDriver(agent, inner)
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(SessionExpired):
        await run_probe(_probe([PROSE]), ad, ledger, platform="Plataforma A")

    assert ledger.read_all() == []
    assert agent.tasks == []  # the agent was never asked to do anything


# --- the adapter must not swallow the runner's other guards ---------------


@pytest.mark.asyncio
async def test_the_logged_out_predicate_is_forwarded_to_the_real_browser(tmp_path: Path):
    """run_probe's belt-and-braces gate reads driver.is_logged_out(). If the
    adapter does not forward it, the gate is inert for every prose probe --
    which is all of them."""
    inner = FakeInnerDriver(shots_dir=tmp_path)
    inner.logged_out = True
    ad = AgentDriver(FakeAgent(inner), inner)
    assert await ad.is_logged_out() is True

    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(SessionExpired):
        await run_probe(_probe([PROSE]), ad, ledger, platform="Plataforma A")
    assert ledger.read_all() == []


def test_truncation_flags_are_forwarded_to_the_real_browser():
    # The runner reads these off the driver it was handed to write the ledger
    # note; through the adapter it must see the INNER driver's values.
    inner = FakeInnerDriver()
    ad = AgentDriver(FakeAgent(inner), inner)
    inner.last_read_truncated = True
    inner.last_read_full_chars = 9001
    assert ad.last_read_truncated is True
    assert ad.last_read_full_chars == 9001


# --- choosing a driver ----------------------------------------------------


def test_driver_kind_prefers_dsl_only_when_every_step_parses():
    assert driver_kind_for(["goto https://x.test", "wait 2"]) == "dsl"
    assert driver_kind_for([PROSE]) == "agent"
    # one prose step among DSL steps -> the whole probe needs the agent
    assert driver_kind_for(["goto https://x.test", PROSE]) == "agent"
    assert driver_kind_for([]) == "agent"


def test_make_driver_returns_the_right_wrapper():
    inner = FakeInnerDriver()
    agent = FakeAgent(inner)
    assert make_driver("dsl", driver=inner) is inner  # PlaywrightDriver as-is
    assert isinstance(make_driver("agent", driver=inner, agent=agent), AgentDriver)


def test_make_driver_rejects_an_unknown_kind():
    with pytest.raises(EvalkitError):
        make_driver("telepathy", driver=FakeInnerDriver())


def test_make_driver_agent_requires_an_agent():
    with pytest.raises(EvalkitError):
        make_driver("agent", driver=FakeInnerDriver(), agent=None)


# --- the agent could not see -------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_actually_receives_the_screenshot_it_takes(tmp_path, monkeypatch):
    """The screenshot tool returned a FILENAME, so the agent drove blind.

    It captured the evidence the report is built from and never saw a pixel of
    it -- every judgement about the page came from the accessibility tree. On
    Plataforma A that produced a false negative that killed a run: the agent clicked
    the tutor button, the chat panel opened (visible in the very screenshot it
    had just taken), and it reported `accomplished=false` because it could not
    confirm in text what was obvious in the image. Its instructions say to
    report false when unsure, so it behaved correctly -- it was just blind.
    """
    import base64

    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent, AgentRunResult
    from lxp_eval.config import CONFIG

    monkeypatch.setattr(CONFIG, "dry_run", True, raising=False)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nFAKEPIXELS")

    class FakeDriver:
        async def screenshot(self, label):
            return png

    agent = ClaudeBrowserAgent(FakeDriver())
    out, done = await agent._dispatch_tool("screenshot", {"label": "x"}, AgentRunResult())

    assert done is False
    assert isinstance(out, list), "the tool result must carry content blocks, not a bare string"
    image = next(b for b in out if b.get("type") == "image")
    assert image["source"]["media_type"] == "image/png"
    # the real bytes, so the model sees what the auditor will see
    assert base64.b64decode(image["source"]["data"]) == png.read_bytes()
    # the filename still travels, so the agent can refer to it
    assert any(b.get("type") == "text" and png.name in b.get("text", "") for b in out)


def test_the_adapter_forwards_the_whole_driver_interface():
    """AgentDriver stands in for PlaywrightDriver, so its signatures must match.

    `read_page` grew a `max_chars` parameter and the adapter did not, so the
    runner -- which is always handed an AgentDriver, never the raw browser --
    died with "unexpected keyword argument 'max_chars'" on the first capture.
    The unit test for the change had exercised PlaywrightDriver directly and
    could not see it. Pin the shape, not just one implementation.
    """
    import inspect

    from lxp_eval.browser_agents.agent_driver import AgentDriver
    from lxp_eval.browser_agents.playwright_driver import PlaywrightDriver

    for name in ("read_page", "screenshot", "url", "assert_session_alive", "do"):
        real = inspect.signature(getattr(PlaywrightDriver, name))
        adapter = inspect.signature(getattr(AgentDriver, name))
        assert set(real.parameters) <= set(adapter.parameters), (
            f"AgentDriver.{name}{adapter} cannot stand in for "
            f"PlaywrightDriver.{name}{real}"
        )


@pytest.mark.asyncio
async def test_the_agent_loop_caches_its_growing_prompt(monkeypatch, tmp_path):
    """A tool-use loop without prompt caching re-bills its whole history.

    The browser agent runs up to 20 turns per step, and every turn appends a
    screenshot (1-5k tokens as an image) plus a page read. Uncached, request N
    pays full price for everything turns 1..N-1 already sent — cost grows with
    the square of the turn count. The scorer was already caching; this loop
    never did, and adding image blocks to it made each turn heavier still.

    System + tools are stable across the whole step, so they cache once. The
    breakpoint then rides the LAST message, which is what lets turn N reuse
    turn N-1's prefix instead of re-reading it.
    """
    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent
    from lxp_eval.config import CONFIG

    monkeypatch.setattr(CONFIG, "dry_run", False, raising=False)
    monkeypatch.setattr(CONFIG, "anthropic_api_key", "test-key", raising=False)

    seen = []

    class _Msgs:
        @staticmethod
        def create(**kw):
            seen.append(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="listo")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )

    class _Client:
        messages = _Msgs()

    driver = SimpleNamespace(state=SimpleNamespace(screenshots_taken=[], actions_log=[], log=lambda *a, **k: None))
    agent = ClaudeBrowserAgent(driver)
    agent.client = _Client()
    await agent.run(system="s", user_task="t")

    assert seen, "the agent never called the API"
    kw = seen[0]
    sys_blocks = kw["system"] if isinstance(kw["system"], list) else []
    assert any("cache_control" in b for b in sys_blocks), \
        "the stable system prompt must be cached once, not resent every turn"
    last = kw["messages"][-1]["content"]
    blocks = last if isinstance(last, list) else []
    assert any("cache_control" in b for b in blocks), \
        "the breakpoint must ride the last message so turn N reuses turn N-1"


def test_cached_tokens_are_still_billed():
    """Caching moves tokens out of `input_tokens`; it does not make them free.

    The loop counted `input_tokens + output_tokens` only. Once caching was
    switched on, cached reads move into `cache_read_input_tokens` and writes
    into `cache_creation_input_tokens` — so the estimate would have fallen for
    free, making any before/after comparison meaningless AND letting the
    per-step cost cap under-count the real bill, so a step runs past the ceiling
    it was given.

    Published multipliers: a cache write costs 1.25x the input rate, a read
    0.1x.
    """
    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent
    from lxp_eval.config import CONFIG

    a = ClaudeBrowserAgent.__new__(ClaudeBrowserAgent)
    a.price_per_m_input = 5.0
    a.price_per_m_output = 25.0

    plain = a._cost(1_000_000, 0)
    assert plain == pytest.approx(5.0)

    # a million cached READS must cost a tenth of a million fresh ones
    assert a._cost(0, 0, cache_read=1_000_000) == pytest.approx(0.5)
    # a million cached WRITES cost a quarter more than fresh
    assert a._cost(0, 0, cache_write=1_000_000) == pytest.approx(6.25)
    # and they add up rather than replace each other
    assert a._cost(1_000_000, 0, cache_read=1_000_000) == pytest.approx(5.5)


@pytest.mark.asyncio
async def test_the_driver_keeps_a_running_bill():
    """A run that cannot say what it cost cannot be budgeted.

    Every step computed `estimated_cost_usd`, `cache_read_tokens` and
    `cache_write_tokens` and then dropped them on the floor — nothing logged,
    nothing persisted. After roughly 65 USD of live probing the only honest
    answer to "where did it go" was a shrug, and the caching change could not
    be measured because no run had ever recorded a number to compare against.
    """
    class _Agent:
        max_iterations = 0
        cost_cap_usd = 0.0
        tools: list = []

        def __init__(self):
            self.n = 0

        async def run(self, *, system, user_task):
            self.n += 1
            return SimpleNamespace(
                blocked=False, accomplished=True, turns=3,
                estimated_cost_usd=0.25 * self.n,
                input_tokens=100, output_tokens=10,
                cache_read_tokens=900, cache_write_tokens=50,
            )

    driver = SimpleNamespace()
    d = AgentDriver(_Agent(), driver)

    await d.do("un paso")
    await d.do("otro paso")

    assert d.spend_usd == pytest.approx(0.75), "cost must accumulate across steps"
    assert d.cache_read_tokens == 1800
    assert d.cache_write_tokens == 100
    assert d.steps_run == 2


def test_a_tall_screenshot_is_downscaled_before_the_api_sees_it(tmp_path):
    """A full-page shot of a long marketing page exceeds the API's 8000px limit.

    Letting the agent SEE its screenshots sends the PNG as an image block. A
    tall page (a vendor's public site) produces an image over 8000px on one
    edge, and the API rejects it with a 400 — killing the whole probe, which is
    exactly how the privacy-surface validation died live. The copy sent to the
    model must fit; the file on disk stays full-resolution for the ledger.
    """
    from PIL import Image

    from lxp_eval.browser_agents.claude_browser_agent import _image_block_for_api

    tall = tmp_path / "long.png"
    Image.new("RGB", (1280, 14000), "white").save(tall)

    block = _image_block_for_api(tall)
    import base64, io

    sent = Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"])))
    assert max(sent.size) <= 8000, f"image sent to the API is {sent.size}"
    # the file on disk is untouched — the auditor still gets the full capture
    assert Image.open(tall).size == (1280, 14000)


def test_a_normal_screenshot_is_sent_unchanged(tmp_path):
    from PIL import Image

    from lxp_eval.browser_agents.claude_browser_agent import _image_block_for_api

    ok = tmp_path / "vp.png"
    Image.new("RGB", (1280, 900), "white").save(ok)
    block = _image_block_for_api(ok)
    import base64, io

    sent = Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"])))
    assert sent.size == (1280, 900), "an in-limit image must not be re-encoded/resized"
