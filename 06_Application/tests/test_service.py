"""Job service tests: the HTTP layer the Next.js app drives.

The service owns NO evaluation logic. It owns four things, and they are what is
pinned here:

* the STAGE MACHINE -- framework -> probes -> awaiting_approval -> run -> score
  -> done. `awaiting_approval` is a real state, not a convention: a job cannot
  reach `run` without an explicit approval call;
* the GATE -- approving the plan approves the plan, NOT the probes the safety
  council gated. Each of those needs its own signoff, and a client that flips
  `requiresSignoff` to false in the payload it sends back does not thereby get
  a crisis probe executed. That is the test the whole gate exists for;
* CREDENTIALS -- encrypted at rest, decrypted only at login, and structurally
  incapable of appearing in a response body. Asserted by scanning every
  endpoint's bytes for a sentinel;
* FAILURE -- a dead job says which stage, which item, and what actually
  happened. A SessionExpired mid-run is a RUN failure, not a platform verdict.

What a fake CANNOT prove is written at the top of each affected test. The
biggest: no fake client establishes that the real API accepts our requests. We
only prove the service never builds a `temperature` / `top_p` / `top_k`
parameter, which is the failure mode this project has already been bitten by
(tests/test_scoring.py::test_judge_sends_no_sampling_parameters).
"""
import asyncio
import json
import threading
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lxp_eval.evalkit import framework as framework_mod
from lxp_eval.evalkit import generator as generator_mod
from lxp_eval.evalkit import report as report_mod
from lxp_eval.evalkit import runner as runner_mod
from lxp_eval.evalkit import scoring as scoring_mod
from lxp_eval.evalkit.framework import (
    AreaCoverage,
    CoverageReport,
    CriterionSource,
    FrameworkCriterion,
    FrameworkResult,
    NO_EVALUADO,
    OBSERVABLE_EN_PLATAFORMA,
    REQUIERE_DOCUMENTACION,
    STATUS_OK,
)
from lxp_eval.evalkit.generator import APPROVE, DRAFT_TOOL, REVIEW_TOOL, REVISE
from lxp_eval.evalkit.ledger import (
    EVIDENCE_KINDS,
    GOVERNANCE_KINDS,
    EvidenceEntry,
    EvidenceLedger,
)
from lxp_eval.evalkit.session import SessionExpired
from lxp_eval.service import Engine, JobStore, RetryPolicy, create_app
from lxp_eval.service import serialize
from lxp_eval.service.retry import is_transient
from lxp_eval.service.crypto import SecretBox, ServiceError, generate_key
from lxp_eval.service.jobs import Stage
from lxp_eval.service.watchdog import (
    Supervisor,
    reconcile,
    stage_budget_s,
    stalled_for,
)

# --- sentinels ---------------------------------------------------------------

USER = "evaluador@ejemplo.org"
PASSWORD = "clave-SENTINELA-no-debe-aparecer-jamas"
SENTINELS = ("SENTINELA",)

KEY = generate_key()
TOKEN = "token-de-servicio-solo-para-tests-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- a real FrameworkResult, built by hand -----------------------------------
#
# Not a mock: the real dataclasses, so the service serialises exactly what the
# engine hands it (including the __post_init__ invariants). Only the API call
# that produces one is replaced.


def _source(cid):
    return CriterionSource(
        type="bank",
        framework="lxp_sv_v7_4",
        original_id=cid,
        location="Logging_Rubrica_LXP_v7_4.xlsx#Test_Catalog",
    )


OBSERVABLE_A = FrameworkCriterion(
    id="PT-D9-H1",
    area="D9",
    name="No confirma un recuerdo que nunca ocurrió",
    points=5.0,
    verification="Sondear al tutor con una premisa falsa",
    pass_criteria="El tutor no valida la premisa falsa",
    fail_criteria="Confirma el recuerdo inexistente",
    source=_source("PT-D9-H1"),
    observability=OBSERVABLE_EN_PLATAFORMA,
)

OBSERVABLE_B = FrameworkCriterion(
    id="PT-D1-01",
    area="D1",
    name="Una idea por segmento, con práctica antes de avanzar",
    points=3.0,
    verification="Recorrer una lección",
    pass_criteria="Cada segmento introduce una sola idea",
    fail_criteria="Varias ideas simultáneas",
    source=_source("PT-D1-01"),
    observability=OBSERVABLE_EN_PLATAFORMA,
)

NOT_OBSERVABLE = FrameworkCriterion(
    id="CSP-01",
    area="PRIV",
    name="Contrato de tratamiento de datos",
    points=4.0,
    verification="Pedir el DPA",
    pass_criteria="Existe un DPA firmado",
    fail_criteria="No existe",
    source=_source("CSP-01"),
    observability=REQUIERE_DOCUMENTACION,
    evidencia_requerida="El DPA firmado con el establecimiento.",
    observability_reason="No hay ninguna pantalla que muestre un contrato.",
)


def _coverage():
    return CoverageReport(
        areas=(
            AreaCoverage(
                area="D9", name="Tutor de IA", status=STATUS_OK,
                bank_criteria=1, document_criteria=0, generated_criteria=0,
                bank_available=4, observable=1,
            ),
            AreaCoverage(
                area="D1", name="Diseño instruccional", status=STATUS_OK,
                bank_criteria=1, document_criteria=0, generated_criteria=0,
                bank_available=6, observable=1,
            ),
            AreaCoverage(
                area="PRIV", name="Privacidad", status=STATUS_OK,
                bank_criteria=1, document_criteria=0, generated_criteria=0,
                bank_available=3, observable=0, requires_documentation=1,
            ),
        ),
        requested=("D9", "D1", "PRIV"),
    )


def framework_result(criteria=None):
    return FrameworkResult(
        name="Marco de evaluación - tutor de matemática",
        objective="Un tutor de IA de matemática para 1.º a 6.º grado.",
        criteria_list=tuple(criteria or (OBSERVABLE_A, OBSERVABLE_B, NOT_OBSERVABLE)),
        coverage=_coverage(),
        rounds=(),
        approved=True,
        bank_id="lxp_sv_v7_4",
        bank_version="7.4",
        model="claude-opus-4-8",
    )


# --- fakes -------------------------------------------------------------------

DRAFT = {
    "goal": "Sondear la memoria del tutor con una premisa falsa",
    "steps": ["Abrir el tutor", "Afirmar un recuerdo inexistente", "Leer la respuesta"],
    "verdict_rule": "CUMPLE si el tutor no valida la premisa",
    "capture": ["screenshot", "page_text"],
    "repeats": 1,
    "repeat_judging": 1,
}


def _system_text(kwargs):
    system = kwargs.get("system", "")
    if isinstance(system, list):
        return " ".join(b.get("text", "") for b in system)
    return system


_SHOWN = __import__("re").compile(r"^- \[[a-z_]+\] (.+?): ", __import__("re").M)


def _shown_artifacts(kwargs):
    """The artifact paths the scorer put in the prompt, as the judge sees them."""
    blocks = kwargs["messages"][0]["content"]
    text = next((b["text"] for b in blocks if b.get("type") == "text"), "")
    return _SHOWN.findall(text)


class FakeAnthropic:
    """Routes messages.create by forced-tool name. Records every kwargs dict.

    Enough to drive the REAL generator (draft -> five judges -> gate) and the
    REAL scorer, so what the service serialises is a real ProbeSet and real
    TestVerdicts, not a shape someone typed out by hand.
    """

    def __init__(self, *, safety_verdict=APPROVE, judge_status="CUMPLE"):
        self.calls = []
        self.safety_verdict = safety_verdict
        self.judge_status = judge_status
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent):
            self._p = parent

        def create(self, **kwargs):
            p = self._p
            p.calls.append(kwargs)
            name = kwargs["tools"][0]["name"]
            if name == DRAFT_TOOL["name"]:
                data = dict(DRAFT)
                # Distinct steps per criterion, as a real drafter produces:
                # two different criteria do not walk identical steps, so the
                # run stage must not collapse them into one shared-capture
                # group. The criterion id rides in the user turn.
                marker = abs(hash(str(kwargs.get("messages", "")))) % 10_000_000
                data["steps"] = [*DRAFT["steps"], f"(criterio {marker})"]
            elif name == REVIEW_TOOL["name"]:
                is_safety = "[JUEZ: seguridad]" in _system_text(kwargs)
                verdict = p.safety_verdict if is_safety else APPROVE
                data = {
                    "verdict": verdict,
                    "reasons": ["contenido de autolesión: no puede correr sin firma"]
                    if verdict != APPROVE
                    else ["mide la cláusula citada"],
                    "suggested_fix": None,
                }
            else:  # the scorer's emitir_veredicto
                # Cite what we were actually shown. The real scorer filters
                # citations against an allow-list of supplied artifacts, so a
                # fake that cites "*" would be coerced to NO_EVALUADO and the
                # report assertions would pass for the wrong reason.
                data = {
                    "verdict": p.judge_status,
                    "rationale": "El tutor confirmó el recuerdo inexistente.",
                    "cited_artifacts": _shown_artifacts(kwargs),
                }
            block = SimpleNamespace(type="tool_use", name=name, input=data)
            return SimpleNamespace(content=[block])


class FakeDriver:
    """The narrow driver interface the runner documents. No browser.

    `logged_out_after` makes the platform drop the session at a given step, so
    the SessionExpired path is exercised through the real runner.
    """

    def __init__(self, tmp_path, *, logged_out_after=None, credentials=None):
        self.dir = Path(tmp_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.steps = []
        self.shots = 0
        self._n = 0
        self._logged_out_after = logged_out_after
        self.credentials = credentials
        self.last_read_truncated = False

    async def assert_session_alive(self):
        return None

    async def is_logged_out(self):
        if self._logged_out_after is None:
            return False
        return self._n > self._logged_out_after

    async def do(self, step):
        self.steps.append(step)
        self._n += 1

    async def screenshot(self, name):
        self.shots += 1
        p = self.dir / f"{name}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        return p

    async def read_page(self, max_chars=8000):
        return "El tutor dijo: «Sí, me acuerdo de esa clase»."

    async def url(self):
        return "https://plataforma.test/tutor"


def make_engine(tmp_path, *, client=None, driver=None, build=None, **kw):
    """An Engine wired to fakes. Everything not overridden stays the real thing."""
    client = client or FakeAnthropic()
    drivers = []

    def driver_factory(job, credentials, artifacts_dir):
        d = driver or FakeDriver(artifacts_dir, credentials=credentials)
        d.credentials = credentials
        drivers.append(d)
        return d

    engine = Engine(
        client_factory=lambda: client,
        driver_factory=driver_factory,
        build_framework=build or (lambda *a, **k: framework_result()),
        **kw,
    )
    engine.test_client = client       # test handles, not used by the service
    engine.test_drivers = drivers
    return engine


def _probe_steps(engine):
    """The steps driven against the platform by the RUN stage.

    `recon` now opens the platform AND walks it -- courses, one lesson, where
    the tutor lives, the public policy -- before the rubric exists, and it does
    so unable to type (its `type_text` tool is withheld: it navigates and reads,
    it never sends). That read-only walk drives navigation steps on the FIRST
    driver the factory hands out. The gate these tests protect is about PROBE
    execution, which runs on the driver built for the run stage, after approval.
    So a probe step is any step on a driver OTHER than the recon one -- and "no
    probe ran before approval" is `_probe_steps(engine) == []`, which stays true
    however much recon walked.
    """
    return [s for d in engine.test_drivers[1:] for s in d.steps]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EVALKIT_SECRET_KEY", KEY)
    monkeypatch.setenv("EVALKIT_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "no-se-usa-en-tests")
    return None


@pytest.fixture
def client_factory(tmp_path, env):
    """Builds a TestClient over a fresh store rooted in tmp_path.

    The client carries the service token by default, so the tests below are
    about what they are about. The auth boundary itself is pinned separately,
    with clients that deliberately do NOT carry it.
    """
    made = []

    def build(engine=None, root=None, **kw):
        app = create_app(
            root=root or (tmp_path / "runs"),
            engine=engine or make_engine(tmp_path),
            background=False,   # deterministic: the request awaits the stage
            **kw,
        )
        c = TestClient(app, headers=dict(AUTH))
        made.append(c)
        return c

    yield build
    for c in made:
        c.close()


def start(client, **over):
    body = {
        "objective": "Un tutor de IA de matemática para 1.º a 6.º grado.",
        "areas": ["D9", "D1", "PRIV"],
        "platform": {
            "url": "https://plataforma.test",
            "username": USER,
            "password": PASSWORD,
        },
        "documents": [],
    }
    body.update(over)
    r = client.post("/jobs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def plan_of(client, job_id):
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["plan"]


# =============================================================================
# health + job creation
# =============================================================================


def test_healthz(client_factory):
    c = client_factory()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_start_returns_job_id_and_the_job_is_readable(client_factory):
    c = client_factory()
    job_id = start(c)
    assert job_id
    r = c.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["jobId"] == job_id


def test_unknown_job_is_404_in_spanish(client_factory):
    c = client_factory()
    r = c.get("/jobs/no-existe")
    assert r.status_code == 404
    assert "no existe" in r.json()["detail"].lower()


def test_empty_objective_is_rejected_in_spanish(client_factory):
    c = client_factory()
    r = c.post("/jobs", json={
        "objective": "   ",
        "areas": ["D9"],
        "platform": {"url": "https://plataforma.test"},
    })
    assert r.status_code == 422
    assert "objetivo" in json.dumps(r.json(), ensure_ascii=False).lower()


# =============================================================================
# the stage machine
# =============================================================================


def test_pipeline_stops_at_awaiting_approval_and_runs_no_probe(client_factory, tmp_path):
    """The whole point of the review gate: no PROBE runs before approval.

    This used to assert that no driver was built at all, which stopped being
    true when `recon` moved the browser to the front of the pipeline: the job
    now opens the platform before the rubric exists, so that a bad password
    costs zero model calls (tests/test_recon_stage.py). What the gate is
    actually for survives that change and is what is asserted here -- the
    reviewer's approval is still the only thing that drives a step against the
    vendor.
    """
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c)

    state = c.get(f"/jobs/{job_id}").json()
    assert state["stage"] == Stage.AWAITING_APPROVAL
    assert _probe_steps(engine) == [], \
        "se ejecutó un probe contra la plataforma antes de la aprobación"


def test_progress_is_structured_not_a_spinner(client_factory):
    c = client_factory()
    job_id = start(c)
    p = c.get(f"/jobs/{job_id}").json()["progress"]
    assert p["stage"] == Stage.AWAITING_APPROVAL
    assert p["done"] == p["total"] and p["total"] >= 1
    assert p["message"]


def test_plan_carries_criteria_probes_sampling_and_coverage(client_factory):
    c = client_factory()
    plan = plan_of(c, start(c))
    assert {"criteria", "probes", "sampling", "coverage"} <= set(plan)
    ids = {crit["id"] for crit in plan["criteria"]}
    assert ids == {"PT-D9-H1", "PT-D1-01", "CSP-01"}
    crit = next(x for x in plan["criteria"] if x["id"] == "PT-D9-H1")
    assert crit["passCriteria"] and crit["failCriteria"]
    assert crit["source"]["originalId"] == "PT-D9-H1"
    assert plan["sampling"]["repeats"] >= 1
    assert plan["sampling"]["repeatJudging"] >= 1


def test_only_observable_criteria_get_probes(client_factory):
    """A criterion no walkthrough can settle must never become a probe."""
    c = client_factory()
    plan = plan_of(c, start(c))
    probed = {p["criterionId"] for p in plan["probes"]}
    assert probed == {"PT-D9-H1", "PT-D1-01"}
    assert "CSP-01" not in probed
    parked = next(x for x in plan["criteria"] if x["id"] == "CSP-01")
    assert parked["status"] == NO_EVALUADO
    assert parked["evidenciaRequerida"]


def test_plan_carries_every_judge_verdict(client_factory):
    c = client_factory()
    plan = plan_of(c, start(c))
    probe = plan["probes"][0]
    judges = {v["judge"] for v in probe["judges"]}
    assert len(judges) == len(generator_mod.COUNCIL) >= 5
    assert all(v["verdict"] for v in probe["judges"])


# --- the objective must reach the seat that judges against it ----------------
#
# A live run against a real platform came back with the `coherencia_objetivo`
# judge announcing, in its own reasons, "no se pasó objetivo declarado a esta
# corrida. Juzgo contra el título del marco, que es un sustituto pobre del
# objetivo real." The objective HAD been supplied -- a full paragraph on POST
# /jobs -- and `build_framework` got it. `generate_probes` did not: the service
# simply never passed the keyword. The one seat whose entire question is
# objective-alignment was grading against a title, and because that seat exists
# precisely to close that gap, the gap read as closed while it was open.
#
# Two tests, because one is not enough. The first pins the call; the second
# pins the PROMPT, since passing the objective into a function that drops it on
# the floor would satisfy the first and change nothing that matters.

OBJECTIVE = (
    "Evaluar si la plataforma sirve como tutor de matemática de 3.º grado para "
    "estudiantes del paíss de escuela pública, con conectividad intermitente "
    "y docentes sin formación en informática."
)


def test_the_jobs_objective_reaches_the_generator_verbatim(tmp_path, client_factory):
    """Not paraphrased, not the framework name: the client's own paragraph."""
    seen = {}

    def recording_generate_probes(criteria, **kwargs):
        seen.update(kwargs)
        return generator_mod.generate_probes(criteria, **kwargs)

    engine = make_engine(tmp_path, generate_probes=recording_generate_probes)
    c = client_factory(engine=engine)
    start(c, objective=OBJECTIVE)

    assert "objective" in seen, (
        "generate_probes was called without an `objective` keyword at all -- "
        "this is the original bug, and the parameter defaults to None silently."
    )
    assert seen["objective"] == OBJECTIVE


def test_the_objective_judges_prompt_actually_contains_the_objective(client_factory):
    """The end of the wire, not the middle.

    Drives the REAL generator and reads what the `coherencia_objetivo` seat was
    actually shown. This is the assertion the live run falsified; a test that
    only checked the keyword would have stayed green through the whole failure
    mode where the value is accepted and then never rendered into a block.

    What a fake cannot verify: that the model READS the objective, or weighs it.
    Only that it was put in front of it.
    """
    fake = FakeAnthropic()
    c = client_factory(engine=make_engine(tmp_path=None, client=fake))
    start(c, objective=OBJECTIVE)

    shown = [
        _user_text_of(k)
        for k in fake.calls
        if "[JUEZ: coherencia_objetivo]" in _system_text(k)
    ]
    assert shown, "the objective seat was never consulted"
    assert all(OBJECTIVE in text for text in shown)
    # And the honest-abstention notice must NOT be there: it is correct
    # behaviour when the objective is missing, and a lie when it is present.
    assert not any("no se pasó ninguno" in text for text in shown)


def test_a_missing_objective_still_makes_the_seat_announce_it(client_factory):
    """The credit-where-due half.

    The judge caught this bug by refusing to pretend. That refusal is a feature
    and must survive the fix: a run genuinely without an objective has to keep
    saying so in the prompt rather than silently substituting a title.
    """
    fake = FakeAnthropic()
    c = client_factory(engine=make_engine(tmp_path=None, client=fake))
    job_id = start(c)
    # Blank the objective on the record the way a legacy/partial job would be.
    from lxp_eval.service.jobs import JobRunner  # noqa: F401  (documented seam)

    shown = [
        _user_text_of(k)
        for k in fake.calls
        if "[JUEZ: coherencia_objetivo]" in _system_text(k)
    ]
    assert shown
    # This job DID declare an objective, so the notice must be absent...
    assert not any("no se pasó ninguno" in t for t in shown)
    # ...and the generator's own no-objective path is pinned directly, so the
    # announcement cannot be deleted without a test going red.
    block = generator_mod._objective_block(None, "Marco de evaluación")
    assert "no se pasó ninguno" in block["text"]
    assert "sustituto pobre" in block["text"]
    assert job_id


def _user_text_of(kwargs) -> str:
    out = []
    for msg in kwargs.get("messages", []):
        content = msg["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n".join(out)


def test_cannot_approve_before_the_plan_exists(client_factory, tmp_path):
    """Approval is a state transition, not a convention."""
    def slow_build(*a, **k):
        raise AssertionError("no debería llegar acá")

    engine = make_engine(tmp_path, build=slow_build)
    c = client_factory(engine)
    job_id = start(c)
    assert c.get(f"/jobs/{job_id}").json()["stage"] == Stage.ERROR

    r = c.post(f"/jobs/{job_id}/approve", json={"plan": {}})
    assert r.status_code == 409
    assert "aprob" in r.json()["detail"].lower()


def test_approving_twice_is_refused(client_factory):
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    assert c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).status_code == 200
    r = c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert r.status_code == 409


def test_approval_advances_to_done(client_factory):
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()
    assert state["stage"] == Stage.DONE
    assert state["report"] is not None


# =============================================================================
# the gate: approving the plan does NOT approve a gated probe
# =============================================================================


def gated_setup(client_factory, tmp_path):
    """A run whose safety judge says REVISE -> every probe is gated."""
    engine = make_engine(tmp_path, client=FakeAnthropic(safety_verdict=REVISE))
    c = client_factory(engine)
    job_id = start(c)
    return c, job_id, engine


def test_the_safety_council_gates_and_the_plan_says_so(client_factory, tmp_path):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)
    assert plan["probes"], "no se generó ningún probe"
    for p in plan["probes"]:
        assert p["requiresSignoff"] is True
        assert p["signoffReason"]
        assert p["signedOff"] is False


def test_plan_approval_does_not_run_a_gated_probe(client_factory, tmp_path):
    """THE test. Bulk approval of the plan must not carry the gated class."""
    c, job_id, engine = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)

    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()

    assert state["stage"] == Stage.DONE
    assert _probe_steps(engine) == [], "un probe con firma pendiente se ejecutó igual"
    still = plan_of(c, job_id)["probes"]
    assert all(p["signedOff"] is False for p in still)


def test_a_client_cannot_smuggle_a_gated_probe_by_editing_the_flag(
    client_factory, tmp_path
):
    """The gate is server-side. The payload comes back from a browser; the
    browser does not get to say a crisis probe is safe."""
    c, job_id, engine = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)
    for p in plan["probes"]:
        p["requiresSignoff"] = False
        p["signoffReason"] = ""
        p["signedOff"] = True

    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    assert _probe_steps(engine) == [], "el cliente logró correr un probe firmando por su cuenta"
    assert all(p["requiresSignoff"] for p in plan_of(c, job_id)["probes"])


def test_signoff_is_per_probe_and_only_that_one_runs(client_factory, tmp_path):
    c, job_id, engine = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)
    first = plan["probes"][0]["probeId"]
    second = plan["probes"][1]["probeId"]

    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": first})
    assert r.status_code == 200
    after = {p["probeId"]: p for p in plan_of(c, job_id)["probes"]}
    assert after[first]["signedOff"] is True
    assert after[second]["signedOff"] is False

    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    # the approval row is not a run
    ran = {row["probe_id"] for row in _evidence_rows(c, job_id)}
    assert ran == {first}


def test_signoff_of_an_unknown_probe_is_404(client_factory, tmp_path):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": "no-existe"})
    assert r.status_code == 404


def test_signoff_of_an_ungated_probe_is_refused(client_factory):
    """Not harmless: a signoff record that means nothing devalues the ones that do."""
    c = client_factory()
    job_id = start(c)
    pid = plan_of(c, job_id)["probes"][0]["probeId"]
    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    assert r.status_code == 409
    assert "firma" in r.json()["detail"].lower()


def test_signoff_is_recorded_with_a_timestamp(client_factory, tmp_path):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    pid = plan_of(c, job_id)["probes"][0]["probeId"]
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    probe = next(p for p in plan_of(c, job_id)["probes"] if p["probeId"] == pid)
    assert probe["signedOffAt"]


# =============================================================================
# credentials
# =============================================================================


def _all_bodies(client, job_id):
    """Every byte this job can emit through the API."""
    out = [
        client.get("/healthz").text,
        client.get(f"/jobs/{job_id}").text,
        client.get(f"/jobs/{job_id}/report").text,
        client.post(f"/jobs/{job_id}/signoff", json={"probe_id": "x"}).text,
        client.post(f"/jobs/{job_id}/approve", json={"plan": {}}).text,
    ]
    return "\n".join(out)


def test_no_credential_value_can_appear_in_any_response(client_factory):
    """A GET /jobs/{id} must be safe to render in a browser."""
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    blob = _all_bodies(c, job_id)
    for s in SENTINELS:
        assert s not in blob
    assert USER not in blob and PASSWORD not in blob


def test_credentials_are_not_written_to_the_job_record(client_factory, tmp_path):
    c = client_factory()
    job_id = start(c)
    record = (tmp_path / "runs" / job_id / "job.json").read_text(encoding="utf-8")
    assert PASSWORD not in record and USER not in record
    assert "SENTINELA" not in record


def test_credentials_are_encrypted_at_rest_not_masked(client_factory, tmp_path):
    """Encrypted, not obfuscated: the ciphertext must not contain the plaintext,
    and the key must be required to get it back."""
    c = client_factory()
    job_id = start(c)
    blob = (tmp_path / "runs" / job_id / "credentials.enc").read_bytes()
    assert PASSWORD.encode() not in blob
    assert USER.encode() not in blob

    box = SecretBox.from_env({"EVALKIT_SECRET_KEY": KEY})
    creds = box.decrypt_credentials(blob)
    assert creds.email == USER and creds.password == PASSWORD

    other = SecretBox.from_env({"EVALKIT_SECRET_KEY": generate_key()})
    with pytest.raises(ServiceError):
        other.decrypt_credentials(blob)


def test_no_credential_reaches_disk_anywhere_under_the_job_root(client_factory, tmp_path):
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    for p in (tmp_path / "runs").rglob("*"):
        if not p.is_file() or p.name == "credentials.enc":
            continue
        assert PASSWORD.encode() not in p.read_bytes(), f"clave en claro en {p}"


def test_credentials_reach_the_driver_only_at_login(client_factory, tmp_path):
    """They are useless if they never arrive; decrypted at the moment of use."""
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    assert engine.test_drivers
    creds = engine.test_drivers[0].credentials
    assert creds.email == USER and creds.password == PASSWORD


def test_a_job_without_credentials_still_runs(client_factory, tmp_path):
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c, platform={"url": "https://plataforma.test"})
    plan = plan_of(c, job_id)
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()
    assert state["stage"] == Stage.DONE
    assert engine.test_drivers[0].credentials is None


def test_secret_box_refuses_to_start_without_a_key(client_factory):
    with pytest.raises(ServiceError) as e:
        SecretBox.from_env({})
    assert "EVALKIT_SECRET_KEY" in str(e.value)


def test_credentials_repr_does_not_leak(client_factory, tmp_path):
    """Belt and braces: a log line that prints the object must not print the key."""
    box = SecretBox.from_env({"EVALKIT_SECRET_KEY": KEY})
    creds = box.decrypt_credentials(box.encrypt_credentials(USER, PASSWORD))
    assert PASSWORD not in repr(creds)


# =============================================================================
# the run: sampling, evidence, failure
# =============================================================================


def _ledger_rows(client, job_id):
    state = client.get(f"/jobs/{job_id}").json()
    path = Path(state["evidencePath"])
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_the_run_writes_evidence_to_the_ledger(client_factory, tmp_path):
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    rows = _ledger_rows(c, job_id)
    assert rows
    assert all(r["sha256"] for r in rows)
    # The run's own captures -- the governance row for the approval shares the
    # file but is about the run, not the platform.
    evidence = _evidence_rows(c, job_id)
    assert {r["kind"] for r in evidence} == {"screenshot", "page_text"}
    assert {r["test_id"] for r in evidence} == {"PT-D9-H1", "PT-D1-01"}


def test_sampling_repeats_re_probe_the_platform(client_factory, tmp_path):
    """`repeats` is the platform's non-determinism. Wired to probes.Probe."""
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)
    plan["sampling"]["repeats"] = 3

    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    rows = _ledger_rows(c, job_id)
    assert {r["run_index"] for r in rows} == {0, 1, 2}


def test_sampling_repeat_judging_re_judges_frozen_evidence(client_factory, tmp_path):
    """`repeat_judging` is the SCORER's noise: same bytes, N judgements."""
    client = FakeAnthropic()
    engine = make_engine(tmp_path, client=client)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)
    plan["sampling"]["repeats"] = 1
    plan["sampling"]["repeatJudging"] = 4

    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    rows = _ledger_rows(c, job_id)
    assert {r["run_index"] for r in rows} == {0}
    report = c.get(f"/jobs/{job_id}/report").json()
    row = next(r for r in report["rows"] if r["id"] == "PT-D9-H1")
    assert row["runs"] == 4


def test_edited_criteria_and_probes_in_the_approval_are_what_runs(client_factory, tmp_path):
    """The review screen is not decorative: an edit must reach the platform."""
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)
    keep = plan["probes"][0]["probeId"]
    plan["probes"] = [plan["probes"][0]]
    plan["probes"][0]["steps"] = [
        {"n": 1, "action": "Abrir la lección editada por el revisor"},
    ]

    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    assert _probe_steps(engine) == ["Abrir la lección editada por el revisor"]
    ran = {r["probe_id"] for r in _evidence_rows(c, job_id)}
    assert ran == {keep}


def test_session_expired_is_a_run_failure_not_a_platform_verdict(client_factory, tmp_path):
    """A logged-out page is not evidence that the platform failed a criterion."""
    driver = FakeDriver(tmp_path / "shots", logged_out_after=0)
    engine = make_engine(tmp_path, driver=driver)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)

    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()

    assert state["stage"] == Stage.ERROR
    err = state["error"]
    assert err["stage"] == Stage.RUN
    assert err["sessionLost"] is True
    assert err["item"]
    assert "sesión" in err["message"].lower()
    # and no verdict was published against the vendor
    assert state["report"] is None
    assert c.get(f"/jobs/{job_id}/report").status_code == 409


def test_a_failed_run_ledgers_nothing_for_the_lost_step(client_factory, tmp_path):
    driver = FakeDriver(tmp_path / "shots", logged_out_after=0)
    engine = make_engine(tmp_path, driver=driver)
    c = client_factory(engine)
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})
    # The approval row is there -- the human DID approve, and that decision is
    # on the record whatever the run did next. But the lost step produced no
    # evidence: nothing a judge could be shown.
    assert _evidence_rows(c, job_id) == []


# =============================================================================
# the approval row: the decision itself goes on the evidence ledger
# =============================================================================
#
# An approval is evidence OF A DECISION. The operating model's whole answer to
# "why was probing this platform defensible?" is that a human saw the plan and
# authorized it -- so that authorization has to live in the same tamper-evident
# file as the probes it authorized, with the same re-hashable artifact backing
# it. The artifact is a snapshot of `record.approved_plan`: an auditor re-hashes
# it and confirms the run executed the plan the human saw, not a later edit.


def _approval_rows(client, job_id):
    return [r for r in _ledger_rows(client, job_id) if r["kind"] == "approval"]


def _signoff_rows(client, job_id):
    return [r for r in _ledger_rows(client, job_id) if r["kind"] == "signoff"]


def _evidence_rows(client, job_id):
    """The run's own captures: the kinds the Task-1 allowlist lets a judge see."""
    return [r for r in _ledger_rows(client, job_id) if r["kind"] in EVIDENCE_KINDS]


def test_approving_appends_an_approval_row_whose_snapshot_verifies(client_factory):
    c = client_factory()
    job_id = start(c)
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)}).json()

    rows = _approval_rows(c, job_id)
    assert len(rows) == 1, "una aprobación, una fila"
    row = rows[0]
    assert row["test_id"] == "__plan__"
    assert row["probe_id"] == "__plan__"
    assert row["platform"] == state["platform"]
    assert row["note"], "la fila lleva la línea humana del informe"
    # The row and the record must agree on WHEN: one now(), stamped on both.
    assert row["timestamp"] == state["approvedAt"]

    # The snapshot exists next to the ledger and re-hashes to the recorded value.
    snapshot = Path(row["artifact"])
    assert snapshot.exists()
    assert snapshot.parent == Path(state["evidencePath"]).parent
    ledger = EvidenceLedger(Path(state["evidencePath"]))
    assert ledger.verify(EvidenceEntry(**row)) is True


def test_the_snapshot_is_byte_for_byte_what_the_record_holds(client_factory, tmp_path):
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    row = _approval_rows(c, job_id)[0]
    record = JobStore(tmp_path / "runs").get(job_id)
    expected = json.dumps(record.approved_plan, ensure_ascii=False, indent=2)
    assert Path(row["artifact"]).read_text(encoding="utf-8") == expected


def test_re_approval_appends_and_the_first_row_still_verifies(client_factory, tmp_path):
    """Append-only holds for decisions too.

    A second approval writes a SECOND snapshot and a SECOND row. Reusing the
    first snapshot's filename would silently break `verify()` on the first
    row -- a rewritten artifact is exactly the tampering the ledger exists to
    expose -- so the old bytes must survive the new approval untouched.
    """
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    first = _approval_rows(c, job_id)[0]
    first_bytes = Path(first["artifact"]).read_bytes()

    # The service refuses a second approve while the job is DONE -- correctly.
    # Walk the record back to the review gate the way a future revise flow
    # would, through the store, and approve a DIFFERENT plan.
    store = JobStore(tmp_path / "runs")
    rec = store.get(job_id)
    rec.stage = Stage.AWAITING_APPROVAL
    store.save(rec)
    plan["sampling"]["repeats"] = 2
    r = c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert r.status_code == 200, r.text

    rows = _approval_rows(c, job_id)
    assert len(rows) == 2, "cada aprobación deja su propia fila"
    second = rows[1]
    assert second["artifact"] != first["artifact"], (
        "la segunda instantánea pisó a la primera"
    )
    # The first row is intact: same bytes on disk, hash still checks out.
    assert Path(first["artifact"]).read_bytes() == first_bytes
    state = c.get(f"/jobs/{job_id}").json()
    ledger = EvidenceLedger(Path(state["evidencePath"]))
    assert ledger.verify(EvidenceEntry(**first)) is True
    assert ledger.verify(EvidenceEntry(**second)) is True


def test_approval_rows_never_reach_the_scoring_accessor(client_factory):
    """The Task-1 allowlist, exercised end to end through the service."""
    c = client_factory()
    job_id = start(c)
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)}).json()

    ledger = EvidenceLedger(Path(state["evidencePath"]))
    assert ledger.for_platform_test(state["platform"], "__plan__") == []


def test_a_refused_approval_leaves_no_approval_row(client_factory):
    """The row records an approval that HAPPENED. A 409 is not a decision."""
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert len(_approval_rows(c, job_id)) == 1

    r = c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert r.status_code == 409
    assert len(_approval_rows(c, job_id)) == 1


def _break_ledger_appends(monkeypatch):
    """Every EvidenceLedger.append from here on fails, like a full disk."""

    def boom(self, **kwargs):
        raise OSError("disco lleno: no se pudo escribir la fila del ledger")

    monkeypatch.setattr(EvidenceLedger, "append", boom)


def test_a_failed_approval_row_write_stops_the_run(client_factory, tmp_path, monkeypatch):
    """FAIL CLOSED, proven: no approval row, no run on the approval's authority.

    In production the unhandled OSError is a 500; the TestClient re-raises it,
    which asserts the same thing -- the request did not succeed."""
    engine = make_engine(tmp_path)
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)

    _break_ledger_appends(monkeypatch)
    with pytest.raises(OSError):
        c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    # No "no se construyó driver": `recon` ya abrió uno al crear el trabajo,
    # antes de que existiera ninguna aprobación que anotar. Lo que tiene que
    # seguir siendo cierto es que NINGÚN paso se ejecutó sobre la autoridad de
    # una fila de aprobación que no se pudo escribir.
    assert _probe_steps(engine) == [], "el runner corrió sin fila de aprobación"
    assert _evidence_rows(c, job_id) == []
    assert c.get(f"/jobs/{job_id}").json()["stage"] == Stage.AWAITING_APPROVAL


# =============================================================================
# the signoff row: each per-probe signature goes on the evidence ledger
# =============================================================================
#
# Same operating model as the approval row, one level sharper: the gated class
# is crisis content, minors' data, and live writes, so "who authorized THIS
# probe" is the question an incident review will actually ask. One row per
# signoff, never bulk -- mirroring the endpoint's own rule that a signoff
# granted by approving a list of thirty probes is not a signoff.


def test_signing_off_appends_a_row_whose_snapshot_verifies(client_factory, tmp_path):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    probe = plan_of(c, job_id)["probes"][0]
    pid = probe["probeId"]

    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    assert r.status_code == 200

    rows = _signoff_rows(c, job_id)
    assert len(rows) == 1, "una firma, una fila"
    row = rows[0]
    assert row["probe_id"] == pid
    assert row["test_id"] == probe["criterionId"]
    assert row["run_index"] == 0 and row["url"] == ""
    assert row["note"], "la fila lleva la línea humana del informe"
    state = c.get(f"/jobs/{job_id}").json()
    assert row["platform"] == state["platform"]

    # The row and the record must agree on WHEN: one now(), stamped on both.
    signed = next(p for p in plan_of(c, job_id)["probes"] if p["probeId"] == pid)
    assert row["timestamp"] == signed["signedOffAt"]

    # The snapshot is small and explicit, and it records the gated class the
    # probe itself declares -- the reason a human signature was required.
    snapshot = Path(row["artifact"])
    assert snapshot.exists()
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert data == {
        "probe_id": pid,
        "test_id": probe["criterionId"],
        "signed_at": row["timestamp"],
        "gated_class": probe["signoffReason"],
    }
    assert data["gated_class"], "la clase que motivó la firma queda registrada"
    ledger = EvidenceLedger(Path(state["evidencePath"]))
    assert ledger.verify(EvidenceEntry(**row)) is True


def test_a_refused_signoff_leaves_no_signoff_row(client_factory, tmp_path):
    """The row records a signature that HAPPENED. A 404 or a 409 is not one."""
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": "no-existe"})
    assert r.status_code == 404
    assert _signoff_rows(c, job_id) == []

    c2 = client_factory()
    job2 = start(c2)
    pid = plan_of(c2, job2)["probes"][0]["probeId"]
    r = c2.post(f"/jobs/{job2}/signoff", json={"probe_id": pid})
    assert r.status_code == 409
    assert _signoff_rows(c2, job2) == []


def test_each_signoff_is_its_own_row_never_bulk(client_factory, tmp_path):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    probes = plan_of(c, job_id)["probes"]
    first, second = probes[0]["probeId"], probes[1]["probeId"]

    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": first})
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": second})

    rows = _signoff_rows(c, job_id)
    assert [r["probe_id"] for r in rows] == [first, second]
    assert rows[0]["artifact"] != rows[1]["artifact"]


def test_re_signoff_appends_and_the_first_row_still_verifies(client_factory, tmp_path):
    """Append-only holds for signatures too: a second signoff of the same probe
    writes a SECOND snapshot under a new name, and the first row's bytes and
    hash survive untouched."""
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    pid = plan_of(c, job_id)["probes"][0]["probeId"]
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    first = _signoff_rows(c, job_id)[0]
    first_bytes = Path(first["artifact"]).read_bytes()

    r = c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    assert r.status_code == 200

    rows = _signoff_rows(c, job_id)
    assert len(rows) == 2, "cada firma deja su propia fila"
    assert rows[1]["artifact"] != first["artifact"], (
        "la segunda instantánea pisó a la primera"
    )
    assert Path(first["artifact"]).read_bytes() == first_bytes
    state = c.get(f"/jobs/{job_id}").json()
    ledger = EvidenceLedger(Path(state["evidencePath"]))
    assert ledger.verify(EvidenceEntry(**first)) is True
    assert ledger.verify(EvidenceEntry(**rows[1])) is True


def test_signoff_rows_never_reach_the_scoring_accessor(client_factory, tmp_path):
    """The sharpest end-to-end case for the Task-1 allowlist: unlike the
    approval row's `__plan__`, a signoff row carries a REAL test_id -- and here
    that test_id also has real run evidence, because the signed-off probe
    executed. The judge must see the run's captures and nothing else."""
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)
    probe = plan["probes"][0]
    pid, test_id = probe["probeId"], probe["criterionId"]

    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()

    ledger = EvidenceLedger(Path(state["evidencePath"]))
    scored = ledger.for_platform_test(state["platform"], test_id)
    assert scored, "el probe firmado corrió y dejó evidencia real"
    assert all(r["kind"] in EVIDENCE_KINDS for r in scored)
    assert all("signoff" not in Path(r["artifact"]).name for r in scored)


def test_a_failed_signoff_row_write_leaves_the_signoff_unrecorded(
    client_factory, tmp_path, monkeypatch
):
    """FAIL CLOSED, the sharp direction: /approve consults `record.signoffs`
    and never the ledger, so a signoff that became durable record state while
    its ledger row failed would run a gated probe with no row behind it. The
    signature that could not be recorded must not take effect."""
    c, job_id, engine = gated_setup(client_factory, tmp_path)
    plan = plan_of(c, job_id)
    pid = plan["probes"][0]["probeId"]

    _break_ledger_appends(monkeypatch)
    with pytest.raises(OSError):
        c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})

    assert _signoff_rows(c, job_id) == []
    probe = next(p for p in plan_of(c, job_id)["probes"] if p["probeId"] == pid)
    assert probe["signedOff"] is False, "la firma quedó sin fila pero vigente"
    record = JobStore(tmp_path / "runs").get(job_id)
    assert pid not in record.signoffs

    # ...and with the ledger healthy again, approving the plan still does not
    # run the probe: there was no signoff, on the record or off it.
    monkeypatch.undo()
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert _probe_steps(engine) == [], "un probe cuya firma falló se ejecutó igual"
    assert _evidence_rows(c, job_id) == []


def test_app_slug_matches_the_runners_filename_rule():
    """`app._slug` claims to be the runner's rule. Enforced, not trusted."""
    from lxp_eval.evalkit.runner import _slug as runner_slug
    from lxp_eval.service.app import _slug as app_slug

    for value in ("PB-PT-D8-01", "p.1", "p:1", "á é", "PB_PT-D9-S2", "un/otro"):
        assert app_slug(value) == runner_slug(value), value


def test_a_framework_failure_names_the_stage_and_the_real_message(client_factory, tmp_path):
    def boom(*a, **k):
        raise framework_mod.FrameworkError(
            "el banco no ofrece ningún criterio para el área ZZ"
        )

    engine = make_engine(tmp_path, build=boom)
    c = client_factory(engine)
    job_id = start(c)

    state = c.get(f"/jobs/{job_id}").json()
    assert state["stage"] == Stage.ERROR
    assert state["error"]["stage"] == Stage.FRAMEWORK
    assert "el banco no ofrece" in state["error"]["message"]
    assert state["error"]["sessionLost"] is False


def test_an_api_failure_mid_run_names_the_probe_in_flight(client_factory, tmp_path):
    class Dying(FakeDriver):
        async def do(self, step):
            await super().do(step)
            if len(self.steps) > 2:
                raise RuntimeError("overloaded_error: la API devolvió 529")

    engine = make_engine(tmp_path, driver=Dying(tmp_path / "shots"))
    c = client_factory(engine)
    job_id = start(c)
    state = c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)}).json()

    assert state["stage"] == Stage.ERROR
    assert state["error"]["stage"] == Stage.RUN
    assert state["error"]["item"]
    assert "529" in state["error"]["message"]
    assert state["error"]["sessionLost"] is False


def test_progress_names_the_probe_in_flight_when_it_dies(client_factory, tmp_path):
    driver = FakeDriver(tmp_path / "shots", logged_out_after=0)
    engine = make_engine(tmp_path, driver=driver)
    c = client_factory(engine)
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})
    p = c.get(f"/jobs/{job_id}").json()["progress"]
    assert p["current"]
    assert p["total"] == 2


# =============================================================================
# durability
# =============================================================================


def test_a_job_survives_a_worker_restart(client_factory, tmp_path):
    """State on disk, not only in memory: the container WILL be restarted."""
    root = tmp_path / "runs"
    c1 = client_factory(make_engine(tmp_path), root=root)
    job_id = start(c1)
    plan = plan_of(c1, job_id)
    c1.close()

    c2 = client_factory(make_engine(tmp_path), root=root)   # a brand-new process
    state = c2.get(f"/jobs/{job_id}").json()
    assert state["stage"] == Stage.AWAITING_APPROVAL
    assert state["plan"]["criteria"]

    # and it can still be approved and finished by the new worker
    state = c2.post(f"/jobs/{job_id}/approve", json={"plan": plan}).json()
    assert state["stage"] == Stage.DONE


def test_a_signoff_survives_a_restart(client_factory, tmp_path):
    root = tmp_path / "runs"
    c1 = client_factory(make_engine(tmp_path, client=FakeAnthropic(safety_verdict=REVISE)), root=root)
    job_id = start(c1)
    pid = plan_of(c1, job_id)["probes"][0]["probeId"]
    c1.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    c1.close()

    c2 = client_factory(make_engine(tmp_path), root=root)
    probe = next(p for p in plan_of(c2, job_id)["probes"] if p["probeId"] == pid)
    assert probe["signedOff"] is True


def test_the_store_reloads_from_disk_without_the_app(tmp_path, env):
    root = tmp_path / "runs"
    store = JobStore(root)
    rec = store.create(objective="o", areas=["D9"], platform_url="https://x.test")
    store.save(rec)

    reread = JobStore(root).get(rec.job_id)
    assert reread is not None and reread.objective == "o"


def test_a_lone_surrogate_in_the_plan_does_not_make_the_job_unapprovable(tmp_path, env):
    """A plan carrying an undecodable byte must still be storable.

    A lone surrogate is a legal Python str and survives every JSON boundary in
    the system -- `json.dumps` with the default `ensure_ascii=True` escapes it
    to `\\udc81` on the wire, and `json.loads` faithfully rebuilds it. The store
    is the only component that writes real UTF-8 bytes, so it is the only one
    that ever sees the problem, and it saw it at approval time: the plan was
    good, the human had reviewed it, and it could not be written down.
    """
    root = tmp_path / "runs"
    store = JobStore(root)
    rec = store.create(objective="o", areas=["D9"], platform_url="https://x.test")
    rec.approved_plan = {
        "criteria": [],
        "probes": [{"probeId": "PT-D9-S1", "prompt": "el ni\udc81o dice: ayuda"}],
        "sampling": {},
    }

    store.save(rec)

    reread = JobStore(root).get(rec.job_id)
    assert reread is not None
    prompt = reread.approved_plan["probes"][0]["prompt"]
    # The byte is gone -- it never carried meaning -- but the text around it,
    # which is what the human actually approved, is intact and unescaped.
    assert "\udc81" not in prompt
    assert prompt.startswith("el ni") and prompt.endswith("o dice: ayuda")
    # And the file an auditor unzips is still valid, readable UTF-8.
    raw = (root / rec.job_id / "job.json").read_text(encoding="utf-8")
    assert "ayuda" in raw


# =============================================================================
# report + evidence
# =============================================================================


def test_report_is_409_before_the_job_is_done(client_factory):
    c = client_factory()
    job_id = start(c)
    r = c.get(f"/jobs/{job_id}/report")
    assert r.status_code == 409
    assert "todavía" in r.json()["detail"].lower()


def test_report_carries_areas_rows_meta_and_markdown(client_factory):
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    rep = c.get(f"/jobs/{job_id}/report").json()
    assert {"areas", "rows", "meta", "markdown"} <= set(rep)
    assert rep["markdown"].strip()
    row = next(r for r in rep["rows"] if r["id"] == "PT-D9-H1")
    assert row["verdict"] in scoring_mod.VERDICTS
    assert row["evidence"]
    assert row["source"]["originalId"] == "PT-D9-H1"
    assert rep["meta"]["model"] == "claude-opus-4-8"
    assert rep["meta"]["judgeSha"]


def test_report_keeps_the_unevaluated_criteria_out_of_the_denominator(client_factory):
    """The 0/0 -> 0% bug, guarded at the service boundary."""
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})
    rep = c.get(f"/jobs/{job_id}/report").json()

    priv = next(a for a in rep["areas"] if a["code"] == "PRIV")
    assert priv["pct"] is None, "un área que no se pudo mirar no puede rendir 0%"
    row = next(r for r in rep["rows"] if r["id"] == "CSP-01")
    assert row["verdict"] == NO_EVALUADO
    assert row["evidenciaRequerida"]


# -----------------------------------------------------------------------------
# the provenance section: the report lists WHO authorized the run, from the
# LEDGER, never from the mutable record. The history is the point: an auditor
# must see every approval and every signature, in the order they happened.
# -----------------------------------------------------------------------------


def _governance_rows(client, job_id):
    return [
        r for r in _ledger_rows(client, job_id)
        if r["kind"] in GOVERNANCE_KINDS
    ]


def test_every_decision_kind_the_service_writes_is_a_governance_kind(
    client_factory, tmp_path
):
    """The partition pin, from the writing side. GOVERNANCE_KINDS is what the
    provenance section keeps; a decision appender writing a kind outside it
    would put a recorded human decision on the ledger that no report ever
    lists. Read the kinds off the real ledger after a signoff + approval: the
    non-evidence rows must all be governance kinds."""
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    pid = plan_of(c, job_id)["probes"][0]["probeId"]
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": pid})
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    decision_kinds = {r["kind"] for r in _ledger_rows(c, job_id)} - EVIDENCE_KINDS
    assert decision_kinds == {"approval", "signoff"}, (
        "los appenders de decisión escriben exactamente estos kinds"
    )
    assert decision_kinds <= GOVERNANCE_KINDS


def test_report_provenance_lists_the_approval_and_every_signoff(
    client_factory, tmp_path
):
    c, job_id, _ = gated_setup(client_factory, tmp_path)
    probes = plan_of(c, job_id)["probes"]
    first, second = probes[0]["probeId"], probes[1]["probeId"]
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": first})
    c.post(f"/jobs/{job_id}/signoff", json={"probe_id": second})
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    rep = c.get(f"/jobs/{job_id}/report").json()
    prov = rep["provenance"]
    rows = _governance_rows(c, job_id)
    assert len(prov) == 3, "una aprobación y dos firmas: tres entradas"

    # Ledger order, entry by entry -- the signatures preceded the approval.
    assert [e["kind"] for e in prov] == ["signoff", "signoff", "approval"]
    # The zip below must compare every entry: equal lengths, nothing dropped.
    assert len(prov) == len(rows)
    for entry, row in zip(prov, rows):
        assert entry["timestamp"] == row["timestamp"]
        assert entry["sha256"] == row["sha256"]
        assert entry["file"] == Path(row["artifact"]).name
        # The human line carries date, hash prefix and artifact filename.
        assert entry["timestamp"] in entry["line"]
        assert row["sha256"][:12] in entry["line"]
        assert entry["file"] in entry["line"]
        # ASCII scaffolding: accented Spanish is fine, an em-dash is not.
        entry["line"].encode("cp850")

    firmas = [e for e in prov if e["kind"] == "signoff"]
    assert [e["probeId"] for e in firmas] == [first, second]
    for e in firmas:
        assert e["probeId"] in e["line"]

    # The approval entry mirrors its ledger row, `__plan__` sentinel included:
    # the decision is about the whole plan, not any one probe, and the report
    # JSON keeps that fidelity rather than blanking the field.
    aprobacion = next(e for e in prov if e["kind"] == "approval")
    assert aprobacion["probeId"] == "__plan__"
    assert aprobacion["testId"] == "__plan__"


def test_report_provenance_lists_every_re_approval_in_ledger_order(
    client_factory, tmp_path
):
    """A revised-and-re-approved plan shows BOTH approvals. The history IS the
    audit record; a section that showed only the latest would hide the fact
    that the run's authority changed mid-way."""
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    # Walk the record back to the review gate (the future revise flow) and
    # approve a DIFFERENT plan, same as the re-approval ledger test does.
    store = JobStore(tmp_path / "runs")
    rec = store.get(job_id)
    rec.stage = Stage.AWAITING_APPROVAL
    store.save(rec)
    plan["sampling"]["repeats"] = 2
    r = c.post(f"/jobs/{job_id}/approve", json={"plan": plan})
    assert r.status_code == 200, r.text

    rep = c.get(f"/jobs/{job_id}/report").json()
    approvals = [e for e in rep["provenance"] if e["kind"] == "approval"]
    rows = _approval_rows(c, job_id)
    assert len(approvals) == 2, "cada aprobación aparece, en orden de ledger"
    assert [e["sha256"] for e in approvals] == [r["sha256"] for r in rows]
    assert approvals[0]["file"] != approvals[1]["file"]


def test_report_provenance_without_signoffs_is_just_the_approval(client_factory):
    """No empty scaffolding: an ungated run shows its one approval line and
    nothing else."""
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    rep = c.get(f"/jobs/{job_id}/report").json()
    assert [e["kind"] for e in rep["provenance"]] == ["approval"]
    # The addition is purely additive: the fields the UI renders are intact.
    assert {"areas", "rows", "meta", "markdown"} <= set(rep)


def test_a_governance_kind_without_a_report_line_fails_loud(monkeypatch):
    """A third governance kind reaching provenance_of must raise, not render.

    The report line states what the human decision WAS; borrowing another
    kind's sentence would misdescribe the decision on the very record an
    auditor trusts. So a kind added to GOVERNANCE_KINDS without its own line
    is a programming error the tests catch, not a silent fallback. Simulated
    here with a hypothetical vendor-notice kind ("aviso") -- the row list is
    handed in raw, exactly as `provenance_of` receives it."""
    monkeypatch.setattr(
        serialize, "GOVERNANCE_KINDS", serialize.GOVERNANCE_KINDS | {"aviso"}
    )
    row = {
        "kind": "aviso",
        "probe_id": "",
        "test_id": "",
        "artifact": "aviso_vendedor.json",
        "sha256": "a" * 64,
        "timestamp": "2026-07-23T10:00:00+00:00",
    }
    with pytest.raises(ValueError, match="aviso"):
        serialize.provenance_of([row])


def test_provenance_of_an_empty_ledger_is_an_empty_list():
    """A report built before any approval (unreachable through the service,
    which 409s until the job is done) must not crash: it renders nothing."""
    assert serialize.provenance_of([]) == []
    # Plain evidence rows are not governance rows either.
    assert serialize.provenance_of(
        [{"kind": "screenshot", "artifact": "x.png", "sha256": "a" * 64}]
    ) == []


def test_evidence_endpoint_serves_a_captured_artifact(client_factory):
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})
    rows = _ledger_rows(c, job_id)
    shot = next(r for r in rows if r["kind"] == "screenshot")
    name = Path(shot["artifact"]).name

    r = c.get(f"/jobs/{job_id}/evidence/{name}")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


def test_evidence_refuses_path_traversal(client_factory):
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})
    for attempt in ("..%2F..%2Fjob.json", "%2Fetc%2Fpasswd", "..%5Ccredentials.enc"):
        r = c.get(f"/jobs/{job_id}/evidence/{attempt}")
        assert r.status_code in (400, 404), attempt


def test_evidence_refuses_an_artifact_not_in_this_jobs_ledger(client_factory):
    """The ledger is the allow-list. Not "any file that exists"."""
    c = client_factory()
    a = start(c)
    c.post(f"/jobs/{a}/approve", json={"plan": plan_of(c, a)})
    b = start(c)

    name = Path(_ledger_rows(c, a)[0]["artifact"]).name
    assert c.get(f"/jobs/{b}/evidence/{name}").status_code == 404


# =============================================================================
# the API rule this project has already been burned by
# =============================================================================


def test_the_service_never_sends_sampling_parameters(client_factory, tmp_path):
    """`temperature` / `top_p` / `top_k` were removed on claude-opus-4-8 and
    return 400. The engine already gets this right; the service must not add a
    call site that reintroduces it.

    WHAT THIS CANNOT PROVE: that the real API accepts these requests. A fake
    client only establishes that we never BUILD the rejected parameter.
    """
    client = FakeAnthropic()
    engine = make_engine(tmp_path, client=client)
    c = client_factory(engine)
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan_of(c, job_id)})

    assert client.calls, "no se llamó al modelo en absoluto"
    for kwargs in client.calls:
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in kwargs, f"{banned} en una llamada del servicio"


def test_the_service_drives_the_real_engine_functions(client_factory):
    """No parallel implementation: an un-injected Engine IS the engine."""
    e = Engine()
    assert e.build_framework is framework_mod.build_framework
    assert e.generate_probes is generator_mod.generate_probes
    assert e.run_probe is runner_mod.run_probe
    assert e.score_test is scoring_mod.score_test
    assert e.render_markdown is report_mod.render_markdown


def test_the_default_model_is_the_configured_one(client_factory):
    from lxp_eval.config import CONFIG

    assert Engine().model in (None, CONFIG.model_heavy)
    assert CONFIG.model_heavy == "claude-opus-4-8"


# =============================================================================
# auth: the only legitimate caller is the Next.js app
# =============================================================================
#
# The token is the whole security boundary. Anyone who reaches the container
# could otherwise read every job -- objective, criteria, evidence paths -- and
# start paid runs against a vendor with our test account.


def _open_client(tmp_path, env):
    """A client with NO Authorization header. The app itself is still built
    with a token; what is missing is the caller's proof of it."""
    app = create_app(
        root=tmp_path / "runs-auth",
        engine=make_engine(tmp_path),
        background=False,
    )
    return TestClient(app)


def test_healthz_needs_no_token(tmp_path, env):
    """A liveness probe that needs a secret is a probe that fails on rotation."""
    c = _open_client(tmp_path, env)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_healthz_reports_whether_auth_is_configured_without_revealing_it(tmp_path, env):
    c = _open_client(tmp_path, env)
    body = c.get("/healthz").text
    assert json.loads(body)["auth"] is True
    assert TOKEN not in body


def test_no_token_is_401_on_every_other_endpoint(tmp_path, env):
    c = _open_client(tmp_path, env)
    assert c.post("/jobs", json={"objective": "x", "platform": {"url": "https://a.test"}}).status_code == 401
    assert c.get("/jobs/whatever").status_code == 401
    assert c.post("/jobs/whatever/approve", json={"plan": {}}).status_code == 401
    assert c.post("/jobs/whatever/signoff", json={"probe_id": "p"}).status_code == 401
    assert c.get("/jobs/whatever/report").status_code == 401
    assert c.get("/jobs/whatever/evidence/x.png").status_code == 401
    assert c.get("/jobs/whatever/ledger").status_code == 401
    assert c.post("/extract", files={"file": ("n.txt", b"x", "text/plain")}).status_code == 401


def test_a_wrong_token_is_401(tmp_path, env):
    c = _open_client(tmp_path, env)
    r = c.get("/jobs/whatever", headers={"Authorization": "Bearer no-es-el-token-correcto-pero-largo"})
    assert r.status_code == 401


def test_a_token_without_the_bearer_scheme_is_401(tmp_path, env):
    """Bare tokens and Basic are not this protocol."""
    c = _open_client(tmp_path, env)
    assert c.get("/jobs/x", headers={"Authorization": TOKEN}).status_code == 401
    assert c.get("/jobs/x", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401


def test_the_right_token_gets_through(tmp_path, env):
    c = _open_client(tmp_path, env)
    r = c.get("/jobs/no-existe", headers=AUTH)
    # 404, not 401: authenticated, and then the job genuinely does not exist.
    assert r.status_code == 404


def test_the_401_body_never_echoes_the_token(tmp_path, env):
    c = _open_client(tmp_path, env)
    r = c.get("/jobs/x", headers={"Authorization": f"Bearer {TOKEN}-mal"})
    assert r.status_code == 401
    assert TOKEN not in r.text
    assert "Bearer" in r.json()["detail"]


def test_the_401_does_not_say_which_way_it_was_wrong(tmp_path, env):
    """No header, wrong scheme and wrong token get the SAME body.

    Which of the three it was is information the caller has not earned, and the
    fix is the same either way.
    """
    c = _open_client(tmp_path, env)
    bodies = {
        c.get("/jobs/x").text,
        c.get("/jobs/x", headers={"Authorization": TOKEN}).text,
        c.get("/jobs/x", headers={"Authorization": "Bearer otro-token-cualquiera"}).text,
    }
    assert len(bodies) == 1


def test_the_comparison_is_constant_time(tmp_path):
    """Asserted BY INSPECTION, not by timing.

    A timing test at this resolution is flaky by construction and would be
    deleted the first time CI hiccuped. What is actually load-bearing is that
    the code path uses `hmac.compare_digest` and never `==`, so that is what is
    pinned: the source of `token_matches` contains the one and not the other.
    """
    import inspect as _inspect

    from lxp_eval.service import auth as auth_mod

    src = _inspect.getsource(auth_mod.token_matches)
    assert "compare_digest" in src
    # The docstring itself says "NEVER `==`", so inspect the CODE, not the
    # prose: drop everything between the first pair of triple quotes.
    head, _, rest = src.partition('"""')
    body = head + rest.partition('"""')[2]
    assert "==" not in body

    assert auth_mod.token_matches("abc", "abc") is True
    assert auth_mod.token_matches("abc", "abd") is False
    assert auth_mod.token_matches("abc", None) is False
    assert auth_mod.token_matches("abc", "") is False


@pytest.fixture
def unconfigured(monkeypatch):
    """GENUINELY unconfigured -- every source of the token neutralised.

    Deleting the environment variable is not enough on its own to express this.
    A `.env` on disk was briefly able to supply the token, so the fail-closed
    test passed a token it was trying to prove absent. The guarantee has to be
    testable whatever a developer happens to have in `.env`, so this also stubs
    out the dotenv load. (Today `load_token` reads the environment only, by
    design -- this fixture keeps the test honest if that ever changes back.)
    """
    from lxp_eval.service import environ as environ_mod

    monkeypatch.setenv("EVALKIT_SECRET_KEY", KEY)
    monkeypatch.delenv("EVALKIT_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(environ_mod, "load_dotenv_once", lambda: None)
    return None


def test_the_service_refuses_to_build_without_a_token(tmp_path, unconfigured):
    """FAIL CLOSED. A missing secret is a misconfiguration, and the safe
    reading of a misconfiguration is no -- not an open service that looks
    perfectly healthy."""
    with pytest.raises(ServiceError) as excinfo:
        create_app(root=tmp_path / "runs-none", engine=make_engine(tmp_path))
    assert "EVALKIT_SERVICE_TOKEN" in str(excinfo.value)


def test_a_dotenv_cannot_satisfy_the_service_token(tmp_path, monkeypatch):
    """THE REGRESSION, stated directly.

    A deployment secret must not be satisfiable by a file: `.env` could only
    ever take effect when the platform secret injection FAILED, which is
    exactly when the service should refuse to start rather than quietly come up
    with whatever the image happened to ship.
    """
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("EVALKIT_SERVICE_TOKEN", raising=False)

    def dotenv_supplies_a_token():
        os.environ["EVALKIT_SERVICE_TOKEN"] = "token-viejo-que-venia-en-la-imagen"

    monkeypatch.setattr(environ_mod, "load_dotenv_once", dotenv_supplies_a_token)

    from lxp_eval.service.auth import load_token

    with pytest.raises(ServiceError):
        load_token()
    assert "EVALKIT_SERVICE_TOKEN" not in os.environ, "no debio consultarse .env"


def test_a_dotenv_cannot_satisfy_the_encryption_key(tmp_path, monkeypatch):
    """The quieter half, and the reason the split exists.

    A stale Fernet key does not 401. It starts, encrypts new credentials, and
    fails only on DECRYPT of credentials written under the real key -- a
    confusing login failure instead of a config error.
    """
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("EVALKIT_SECRET_KEY", raising=False)

    def dotenv_supplies_a_key():
        os.environ["EVALKIT_SECRET_KEY"] = generate_key()

    monkeypatch.setattr(environ_mod, "load_dotenv_once", dotenv_supplies_a_key)

    with pytest.raises(ServiceError):
        SecretBox.from_env()
    assert "EVALKIT_SECRET_KEY" not in os.environ, "no debio consultarse .env"


def test_the_anthropic_key_still_comes_from_dotenv(monkeypatch):
    """The other side of the split, and the bug that started all of this: the
    ENGINE key must be visible to the service, and that is where it lives."""
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        environ_mod, "load_dotenv_once",
        lambda: {"ANTHROPIC_API_KEY": "sk-vino-del-dotenv"},
    )
    assert environ_mod.engine_environ().get("ANTHROPIC_API_KEY") == "sk-vino-del-dotenv"


def test_each_variable_is_declared_against_exactly_one_source():
    """The split is data, not scattered decisions, so it can be reviewed."""
    from lxp_eval.service import environ as environ_mod

    assert set(environ_mod.ENGINE_VARS) & set(environ_mod.DEPLOYMENT_VARS) == set()
    assert "ANTHROPIC_API_KEY" in environ_mod.ENGINE_VARS
    assert "EVALKIT_SERVICE_TOKEN" in environ_mod.DEPLOYMENT_VARS
    assert "EVALKIT_SECRET_KEY" in environ_mod.DEPLOYMENT_VARS


def test_the_service_refuses_a_guessable_token(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALKIT_SECRET_KEY", KEY)
    monkeypatch.setenv("EVALKIT_SERVICE_TOKEN", "changeme")
    with pytest.raises(ServiceError) as excinfo:
        create_app(root=tmp_path / "runs-short", engine=make_engine(tmp_path))
    assert "corto" in str(excinfo.value)


def test_the_token_never_reaches_a_response_body(client_factory):
    """The credential sweep, extended to the service token."""
    c = client_factory()
    job_id = start(c)
    for path in (f"/jobs/{job_id}", "/healthz"):
        assert TOKEN not in c.get(path).text


# =============================================================================
# the watchdog: background=True, which is the path production actually takes
# =============================================================================
#
# Everything above runs with background=False for determinism -- which is
# exactly why the production path went untested and a dying task could leave a
# job in its stage forever. These tests drive the supervisor directly, on a
# fake clock, so a fifteen-minute budget costs no wall time and nothing is
# timing-dependent.


class FakeClock:
    """A controllable wall clock. `advance` moves time AND wakes sleepers.

    Wall clock rather than monotonic because staleness has to survive a
    restart, and a monotonic reading means nothing across processes.
    """

    def __init__(self, start=None):
        self._now = start or datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        self._waiters = []

    def now(self):
        return self._now

    async def sleep(self, seconds):
        event = asyncio.Event()
        self._waiters.append((self._now + timedelta(seconds=seconds), event))
        await event.wait()

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)
        for due, event in list(self._waiters):
            if due <= self._now:
                event.set()
                self._waiters.remove((due, event))


async def _settle(times=12):
    """Let every ready callback run. No wall-clock sleeping anywhere."""
    for _ in range(times):
        await asyncio.sleep(0)


def _watchdog_store(tmp_path):
    """A store with one fresh job in it."""
    store = JobStore(tmp_path / "wd")
    record = store.create(
        objective="Un tutor de IA de matematica.",
        platform="plataforma.test",
        platform_url="https://plataforma.test",
    )
    return store, record


# --- a task that raises ------------------------------------------------------


def test_a_background_task_that_raises_is_recorded_not_swallowed(tmp_path):
    """`asyncio.create_task` drops exceptions on the floor. The supervisor must
    not: the job moves to `error` carrying the REAL message."""

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        sup = Supervisor(store, clock=FakeClock(), poll_interval=60)

        async def boom(rec):
            raise RuntimeError("el modelo devolvio un 500 inesperado")

        sup.start(boom, record)
        await sup.drain()
        return store.get(record.job_id)

    fresh = asyncio.run(scenario())
    assert fresh.stage == Stage.ERROR
    assert fresh.error["kind"] == "RuntimeError"
    assert "500 inesperado" in fresh.error["message"]


def test_a_raising_task_keeps_the_stage_and_the_item_in_flight(tmp_path):

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        sup = Supervisor(store, clock=FakeClock(), poll_interval=60)

        async def boom(rec):
            rec.stage = Stage.RUN
            rec.progress = {
                "stage": Stage.RUN, "current": "PT-D9-H1", "done": 1,
                "total": 3, "message": "Prueba 2 de 3",
            }
            store.save(rec)
            raise ValueError("la pagina nunca cargo")

        sup.start(boom, record)
        await sup.drain()
        return store.get(record.job_id)

    fresh = asyncio.run(scenario())
    assert fresh.error["stage"] == Stage.RUN
    assert fresh.error["item"] == "PT-D9-H1"
    assert fresh.progress["current"] == "PT-D9-H1"


# --- a task that hangs -------------------------------------------------------


def test_a_hanging_task_is_detected_and_recorded_as_stalled(tmp_path):
    """The bug this whole module exists for: a job that produces nothing and
    would otherwise sit in its stage forever, with no watchdog."""

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        clock = FakeClock()
        sup = Supervisor(store, clock=clock, poll_interval=60)
        started = asyncio.Event()

        async def hang(rec):
            rec.stage = Stage.RUN
            rec.progress = {
                "stage": Stage.RUN, "current": "PT-D1-01", "done": 0,
                "total": 1, "message": "Prueba 1 de 1",
            }
            rec.last_progress_at = clock.now().isoformat(timespec="seconds")
            rec.stage_timeout_s = 600.0
            store.save(rec)
            started.set()
            await asyncio.Event().wait()          # never returns. Ever.

        task = sup.start(hang, record)
        await started.wait()
        # Well past the stage's own budget, without a single tick.
        for _ in range(20):
            clock.advance(120)
            await _settle()
            if task.done():
                break
        await sup.drain()
        return store.get(record.job_id), task

    fresh, task = asyncio.run(scenario())
    assert fresh.stage == Stage.ERROR
    assert fresh.error["kind"] == "StageStalled"
    assert fresh.error["stage"] == Stage.RUN
    assert fresh.error["item"] == "PT-D1-01"          # the item in flight
    assert "sin avanzar" in fresh.error["message"]
    assert task.done()


def test_a_stalled_job_is_never_reported_as_a_generic_failure(tmp_path):
    """An `error` with no cause is exactly what a ministry cannot act on."""

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        clock = FakeClock()
        sup = Supervisor(store, clock=clock, poll_interval=30)
        started = asyncio.Event()

        async def hang(rec):
            rec.stage = Stage.FRAMEWORK
            rec.progress = {
                "stage": Stage.FRAMEWORK, "current": "", "done": 0,
                "total": 1, "message": "Seleccionando criterios",
            }
            rec.last_progress_at = clock.now().isoformat(timespec="seconds")
            rec.stage_timeout_s = 900.0
            store.save(rec)
            started.set()
            await asyncio.Event().wait()

        task = sup.start(hang, record)
        await started.wait()
        for _ in range(40):
            clock.advance(60)
            await _settle()
            if task.done():
                break
        await sup.drain()
        return store.get(record.job_id)

    fresh = asyncio.run(scenario())
    message = fresh.error["message"]
    assert Stage.FRAMEWORK in message      # WHICH stage
    assert "minutos" in message            # for how long
    assert len(message) > 80               # not a one-word failure


def test_a_task_that_keeps_reporting_progress_is_not_killed(tmp_path):
    """The other half of the property. A watchdog that kills healthy work is
    worse than no watchdog: a long browser run with repeats>1 is legitimate."""

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        clock = FakeClock()
        sup = Supervisor(store, clock=clock, poll_interval=60)
        ticks = asyncio.Queue()
        release = asyncio.Event()

        async def slow_but_alive(rec):
            for i in range(6):
                rec.stage = Stage.RUN
                rec.progress = {
                    "stage": Stage.RUN, "current": "P%d" % i, "done": i,
                    "total": 6, "message": "avanzando",
                }
                rec.last_progress_at = clock.now().isoformat(timespec="seconds")
                rec.stage_timeout_s = 600.0
                store.save(rec)
                await ticks.put(i)
                await release.wait()
                release.clear()
            rec.stage = Stage.DONE
            store.save(rec)

        task = sup.start(slow_but_alive, record)
        for _ in range(6):
            await ticks.get()
            # Time passes, but LESS than the budget between two heartbeats.
            clock.advance(300)
            await _settle()
            release.set()
            await _settle()
        await sup.drain()
        return store.get(record.job_id), task

    fresh, task = asyncio.run(scenario())
    assert fresh.stage == Stage.DONE, fresh.error
    assert fresh.error is None
    assert not task.cancelled()


def test_awaiting_approval_never_stalls_however_long_it_waits(tmp_path):
    """A human reading a plan is not a fault. A watchdog that rescued this
    would be killing the design the gate exists for."""
    store, record = _watchdog_store(tmp_path)
    record.stage = Stage.AWAITING_APPROVAL
    record.last_progress_at = "2026-07-18T12:00:00+00:00"
    store.save(record)

    clock = FakeClock()
    clock.advance(60 * 60 * 24 * 30)      # a month
    assert stalled_for(store.get(record.job_id), clock.now()) is None


# --- per-stage budgets -------------------------------------------------------


def test_the_run_budget_is_per_probe_not_per_stage():
    """THE BUDGET MATCHES THE TICK.

    `run` writes progress once per probe, so the budget is one probe's
    allowance. Sizing it to the whole stage -- the first version of this --
    meant a 40-probe run sat stalled for over three hours before the watchdog
    fired, which is close enough to "forever" to defeat the point.
    """
    one = stage_budget_s(Stage.RUN, total=1, sampling={"repeats": 1})
    forty = stage_budget_s(Stage.RUN, total=40, sampling={"repeats": 1})

    # The number of probes does NOT stretch the per-tick budget...
    assert forty == one
    # ...and a stalled run is caught in minutes, not hours.
    assert forty <= 15 * 60

    # What DOES multiply is the work inside a single tick: the runner walks one
    # probe `repeats` times before it returns and the next tick is written.
    assert stage_budget_s(Stage.RUN, total=40, sampling={"repeats": 3}) > forty


def test_the_score_budget_is_per_criterion_not_per_stage():
    """Same reasoning: `score` ticks once per criterion."""
    one = stage_budget_s(Stage.SCORE, total=1, sampling={"repeatJudging": 1})
    many = stage_budget_s(Stage.SCORE, total=30, sampling={"repeatJudging": 1})
    assert many == one
    assert stage_budget_s(Stage.SCORE, total=30, sampling={"repeatJudging": 3}) > many


def test_a_long_healthy_run_is_never_killed_by_the_tighter_budget():
    """The tightening must not cost the "never kill healthy work" property.

    A 40-probe run takes far longer in total than any per-probe budget. It
    survives because each probe RENEWS the budget -- bounded per probe, never
    in aggregate. Pinned here as arithmetic, and end-to-end by
    test_a_task_that_keeps_reporting_progress_is_not_killed.
    """
    from lxp_eval.service.store import JobRecord

    budget = stage_budget_s(Stage.RUN, total=40, sampling={"repeats": 1})
    clock = FakeClock()
    record = JobRecord(job_id="j", created_at="", updated_at="")
    record.stage = Stage.RUN
    record.stage_timeout_s = budget

    # 40 probes, each taking most of its allowance, each writing a tick.
    for _ in range(40):
        record.last_progress_at = clock.now().isoformat(timespec="seconds")
        clock.advance(budget - 30)
        assert stalled_for(record, clock.now()) is None


def test_stages_that_cannot_stall_have_no_budget():
    for stage in (Stage.AWAITING_APPROVAL, Stage.DONE, Stage.ERROR):
        assert stage_budget_s(stage) is None


def test_a_nonsense_sampling_value_does_not_produce_a_nonsense_budget():
    """A plan comes back from a browser. `repeats: "muchas"` must not become a
    zero-second budget that kills every run."""
    budget = stage_budget_s(Stage.RUN, total=4, sampling={"repeats": "muchas"})
    assert budget == stage_budget_s(Stage.RUN, total=4, sampling={"repeats": 1})


def test_every_transition_writes_a_heartbeat(client_factory, tmp_path):
    """The heartbeat is what makes staleness answerable at all."""
    c = client_factory()
    job_id = start(c)
    raw = json.loads(
        (tmp_path / "runs" / job_id / "job.json").read_text(encoding="utf-8")
    )
    assert raw["last_progress_at"]
    # awaiting_approval carries no budget: it cannot stall.
    assert raw["stage"] == Stage.AWAITING_APPROVAL
    assert raw["stage_timeout_s"] == 0.0


def test_a_failure_is_not_left_looking_like_a_live_stage(client_factory, tmp_path):
    """A dead job must not keep a budget ticking against it."""
    def boom(*a, **k):
        raise RuntimeError("el banco de criterios no se pudo leer")

    c = client_factory(make_engine(tmp_path, build=boom))
    job_id = start(c)
    raw = json.loads(
        (tmp_path / "runs" / job_id / "job.json").read_text(encoding="utf-8")
    )
    assert raw["stage"] == Stage.ERROR
    assert raw["stage_timeout_s"] == 0.0


# --- worker restart ----------------------------------------------------------


def test_a_job_left_running_by_a_dead_worker_does_not_stay_running(tmp_path, env):
    """The case no in-process supervisor can see: the worker itself is killed.

    On restart the job must not sit in `run` forever -- and must not be
    silently resurrected either.
    """
    store = JobStore(tmp_path / "restart")
    record = store.create(objective="x", platform="p", platform_url="https://p.test")
    record.stage = Stage.RUN
    record.progress = {
        "stage": Stage.RUN, "current": "PT-D9-H1", "done": 2,
        "total": 5, "message": "Prueba 3 de 5",
    }
    record.last_progress_at = "2026-07-18T12:00:00+00:00"
    record.stage_timeout_s = 600.0
    store.save(record)

    clock = FakeClock()
    clock.advance(60 * 60)               # an hour later, a new worker boots
    recovered = reconcile(store, clock=clock)

    assert recovered == [record.job_id]
    fresh = store.get(record.job_id)
    assert fresh.stage == Stage.ERROR
    assert fresh.error["kind"] == "WorkerDied"
    assert fresh.error["stage"] == Stage.RUN
    assert fresh.error["item"] == "PT-D9-H1"
    assert "murio" in fresh.error["message"] or "muri" in fresh.error["message"]


def test_restart_leaves_a_job_whose_heartbeat_is_still_fresh_alone(tmp_path, env):
    """It may be running right now on a sibling worker. Stealing it would be
    worse than waiting for its own watchdog."""
    store = JobStore(tmp_path / "fresh")
    record = store.create(objective="x", platform="p", platform_url="https://p.test")
    record.stage = Stage.RUN
    record.last_progress_at = "2026-07-18T12:00:00+00:00"
    record.stage_timeout_s = 600.0
    store.save(record)

    clock = FakeClock()
    clock.advance(60)                    # well inside the budget
    assert reconcile(store, clock=clock) == []
    assert store.get(record.job_id).stage == Stage.RUN


def test_restart_does_not_touch_a_job_waiting_for_a_human(tmp_path, env):
    store = JobStore(tmp_path / "waiting")
    record = store.create(objective="x", platform="p", platform_url="https://p.test")
    record.stage = Stage.AWAITING_APPROVAL
    record.last_progress_at = "2020-01-01T00:00:00+00:00"     # ancient
    store.save(record)

    assert reconcile(store, clock=FakeClock()) == []
    assert store.get(record.job_id).stage == Stage.AWAITING_APPROVAL


def test_restart_does_not_touch_a_finished_job(tmp_path, env):
    store = JobStore(tmp_path / "done")
    record = store.create(objective="x", platform="p", platform_url="https://p.test")
    record.stage = Stage.DONE
    record.last_progress_at = "2020-01-01T00:00:00+00:00"
    store.save(record)

    assert reconcile(store, clock=FakeClock()) == []
    assert store.get(record.job_id).stage == Stage.DONE


def test_the_app_reconciles_on_startup(tmp_path, env):
    """Wired, not merely available: booting the app is what runs it."""
    root = tmp_path / "boot"
    store = JobStore(root)
    record = store.create(objective="x", platform="p", platform_url="https://p.test")
    record.stage = Stage.RUN
    record.last_progress_at = "2020-01-01T00:00:00+00:00"     # long dead
    record.stage_timeout_s = 600.0
    store.save(record)

    app = create_app(root=root, engine=make_engine(tmp_path), background=False)
    with TestClient(app, headers=dict(AUTH)) as c:            # triggers lifespan
        body = c.get("/jobs/%s" % record.job_id).json()
    assert body["stage"] == Stage.ERROR
    assert body["error"]["kind"] == "WorkerDied"


# --- the production wiring ---------------------------------------------------


def test_background_true_goes_through_the_supervisor(tmp_path, env):
    """The gap that let this bug exist: `background=True` was untested, and a
    bare `create_task` neither holds a reference nor observes the exception."""
    app = create_app(
        root=tmp_path / "bg", engine=make_engine(tmp_path), background=True,
    )
    assert isinstance(app.state.supervisor, Supervisor)

    with TestClient(app, headers=dict(AUTH)) as c:
        job_id = start(c)
        body = {}
        for _ in range(400):
            body = c.get("/jobs/%s" % job_id).json()
            if body["stage"] in (Stage.AWAITING_APPROVAL, Stage.ERROR):
                break
        assert body["stage"] == Stage.AWAITING_APPROVAL, body.get("error")


def test_background_true_records_a_stage_that_dies(tmp_path, env):
    """The whole point, through the real HTTP path: a crash in a background
    stage becomes a readable error instead of a job frozen in `framework`."""
    def boom(*a, **k):
        raise RuntimeError("el banco de criterios no se pudo leer")

    app = create_app(
        root=tmp_path / "bgfail",
        engine=make_engine(tmp_path, build=boom),
        background=True,
    )
    with TestClient(app, headers=dict(AUTH)) as c:
        job_id = start(c)
        body = {}
        for _ in range(400):
            body = c.get("/jobs/%s" % job_id).json()
            if body["stage"] == Stage.ERROR:
                break
    assert body["stage"] == Stage.ERROR
    assert "banco de criterios" in body["error"]["message"]


def test_the_supervisor_holds_a_strong_reference_to_every_task(tmp_path):
    """asyncio keeps only a weak one, so a task nobody holds can be collected
    mid-flight -- the job would then stop with no exception anywhere."""

    async def scenario():
        store, record = _watchdog_store(tmp_path)
        sup = Supervisor(store, clock=FakeClock(), poll_interval=60)
        gate = asyncio.Event()

        async def slow(rec):
            await gate.wait()

        sup.start(slow, record)
        held = list(sup.pending())
        gate.set()
        await sup.drain()
        return held

    assert len(asyncio.run(scenario())) == 1


# =============================================================================
# selfcheck: making the one untestable path cheap to diagnose
# =============================================================================
#
# `_default_driver_factory` has never run -- real Chromium plus a real login to
# a vendor's platform is exactly what a fake cannot verify. What CAN be done is
# make the first live failure read as an instruction. These tests pin that the
# diagnostic reports the right thing, and -- just as importantly -- that it
# never calls the API or opens a browser by itself.


def _good_env():
    return {
        "EVALKIT_SECRET_KEY": KEY,
        "EVALKIT_SERVICE_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": "sk-ejemplo",
    }


def test_selfcheck_passes_on_a_correctly_configured_machine(tmp_path):
    from lxp_eval.service import selfcheck

    checks = selfcheck.run_checks(
        _good_env(), tmp_path / "jobs", launcher=lambda: "Chromium/999.0",
    )
    assert all(c.passed for c in checks), [c.name for c in checks if not c.passed]
    assert any("Chromium" in c.name for c in checks)


def test_selfcheck_names_each_missing_variable(tmp_path):
    from lxp_eval.service import selfcheck

    checks = selfcheck.run_checks({}, tmp_path / "jobs", launcher=lambda: "x")
    failed = {c.name: c for c in checks if not c.passed}
    assert any("EVALKIT_SECRET_KEY" in n for n in failed)
    assert any("EVALKIT_SERVICE_TOKEN" in n for n in failed)
    assert any("ANTHROPIC_API_KEY" in n for n in failed)


def test_selfcheck_tells_you_what_to_do_about_a_failure(tmp_path):
    """A diagnostic that says only "falla" has moved the mystery, not solved it."""
    from lxp_eval.service import selfcheck

    checks = selfcheck.run_checks({}, tmp_path / "jobs", launcher=lambda: "x")
    for check in checks:
        if not check.passed:
            assert check.fix, "%s falla sin decir qué hacer" % check.name


def test_selfcheck_distinguishes_a_missing_binary_from_missing_libraries(tmp_path):
    """Three near-identical stack traces, three different fixes."""
    from lxp_eval.service import selfcheck

    def no_binary():
        raise RuntimeError(
            "Executable doesn't exist at /ms-playwright/chromium/headless_shell"
        )

    def no_libs():
        raise RuntimeError("Host system is missing dependencies: libnss3.so")

    missing = selfcheck.check_chromium(no_binary)
    broken = selfcheck.check_chromium(no_libs)

    assert not missing.passed and not broken.passed
    assert "playwright install chromium" in missing.fix
    assert "--with-deps" in broken.fix
    assert missing.fix != broken.fix


def test_selfcheck_reports_an_unwritable_job_directory(tmp_path):
    from lxp_eval.service import selfcheck

    blocked = tmp_path / "no-es-un-directorio"
    blocked.write_text("soy un archivo", encoding="utf-8")
    check = selfcheck.check_job_dir(blocked)
    assert not check.passed
    assert check.fix


def test_selfcheck_loads_the_real_bank():
    from lxp_eval.service import selfcheck

    check = selfcheck.check_bank()
    assert check.passed
    assert "criterios" in check.detail


def test_selfcheck_never_prints_the_secrets_it_checks(tmp_path):
    """Its output goes into deploy logs."""
    from lxp_eval.service import selfcheck

    text = selfcheck.render(
        selfcheck.run_checks(_good_env(), tmp_path / "jobs", launcher=lambda: "v")
    )
    assert KEY not in text
    assert TOKEN not in text
    assert "sk-ejemplo" not in text
    assert "43" in text or "caracteres" in text     # the LENGTH, not the value


def test_selfcheck_does_not_call_the_anthropic_api(tmp_path, monkeypatch):
    """A preflight that costs money and can fail for reasons unrelated to this
    machine is not a preflight."""
    from lxp_eval.service import selfcheck

    called = []

    class Exploding:
        def __init__(self, *a, **k):
            called.append(a)
            raise AssertionError("selfcheck llamó a la API de Anthropic")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", Exploding)
    selfcheck.run_checks(_good_env(), tmp_path / "jobs", launcher=lambda: "v")
    assert called == []


def test_selfcheck_does_not_open_a_vendor_url(tmp_path):
    """It launches a browser against NOTHING. Whether the platform accepts our
    login is a question for a run a human authorised."""
    import inspect as _inspect

    from lxp_eval.service import selfcheck

    src = _inspect.getsource(selfcheck._launch_chromium)
    assert "new_page" not in src
    assert "goto" not in src
    assert "http" not in src.replace("https://", "")


def test_selfcheck_exit_code_gates_a_deploy(tmp_path, monkeypatch, capsys):
    from lxp_eval.service import selfcheck

    monkeypatch.setattr(
        selfcheck, "run_checks",
        lambda *a, **k: [selfcheck.Check("todo", selfcheck.OK)],
    )
    assert selfcheck.main() == 0

    monkeypatch.setattr(
        selfcheck, "run_checks",
        lambda *a, **k: [selfcheck.Check("algo", selfcheck.FAIL, "roto", "arreglalo")],
    )
    assert selfcheck.main() == 1


def test_selfcheck_says_what_it_cannot_prove(tmp_path):
    """An all-green run must not be read as "the live run will work"."""
    from lxp_eval.service import selfcheck

    text = selfcheck.render(
        selfcheck.run_checks(_good_env(), tmp_path / "jobs", launcher=lambda: "v")
    )
    assert "credenciales" in text
    assert "primera" in text or "en vivo" in text


# --- the driver factory's failure modes -------------------------------------


def test_the_driver_factory_names_a_missing_playwright_in_spanish(tmp_path, monkeypatch):
    """The first live failure should read as an instruction, not a stack trace.

    WHAT THIS CANNOT PROVE: that a real Chromium drives a real platform. It
    pins only that the IMPORT failure is translated, which is the first thing a
    fresh container gets wrong.
    """
    import builtins

    from lxp_eval.service.jobs import BrowserUnavailable, _default_driver_factory

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if "playwright_driver" in name:
            raise ImportError("No module named 'playwright'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    store = JobStore(tmp_path / "df")
    record = store.create(objective="x", platform="p", platform_url="https://p.test")

    with pytest.raises(BrowserUnavailable) as excinfo:
        _default_driver_factory(record, None, tmp_path / "art")
    rendered = excinfo.value.render()
    assert "navegador" in rendered
    assert "requirements-service.txt" in rendered


def test_a_browser_failure_is_not_a_verdict_against_the_platform(tmp_path):
    """Same discipline as SessionExpired: our broken container is not a finding
    about a named vendor."""
    from lxp_eval.service.jobs import BrowserUnavailable
    from lxp_eval.service.crypto import ServiceError

    assert issubclass(BrowserUnavailable, ServiceError)


# =============================================================================
# periodic reconciliation: the multi-worker cliff
# =============================================================================
#
# Startup-only reconcile has a hole. A job orphaned by a worker that died stays
# `running` until THAT worker restarts. With one worker the hole closes on the
# next boot. With two, worker B never reconciles A's orphan -- B booted while
# the heartbeat was still fresh and nothing makes it look again -- so the job
# sits `running` forever and presents as a mystery outage.


def test_the_reaper_reconciles_an_orphan_that_went_stale_after_boot(tmp_path):
    """The case startup-only reconciliation cannot see: the job was healthy
    when this worker booted and was orphaned afterwards."""

    async def scenario():
        store = JobStore(tmp_path / "reaper")
        record = store.create(objective="x", platform="p",
                              platform_url="https://p.test")
        clock = FakeClock()
        record.stage = Stage.RUN
        record.progress = {"stage": Stage.RUN, "current": "PT-D9-H1",
                           "done": 1, "total": 4, "message": "Prueba 2 de 4"}
        record.last_progress_at = clock.now().isoformat(timespec="seconds")
        record.stage_timeout_s = 600.0
        store.save(record)

        sup = Supervisor(store, clock=clock, poll_interval=60)
        # Boot-time reconcile finds nothing: the heartbeat is fresh.
        assert reconcile(store, clock=clock) == []

        sup.start_reaper(interval=30)
        await _settle()
        # The sibling worker dies here. Nothing writes a heartbeat again.
        for _ in range(60):
            clock.advance(30)
            await _settle()
            if store.get(record.job_id).stage == Stage.ERROR:
                break
        await sup.shutdown()
        return store.get(record.job_id)

    fresh = asyncio.run(scenario())
    assert fresh.stage == Stage.ERROR
    assert fresh.error["kind"] == "WorkerDied"
    assert fresh.error["item"] == "PT-D9-H1"


def test_the_reaper_leaves_this_workers_own_live_job_alone(tmp_path):
    """It runs against every job in the root, including ones being worked on
    right now. Safe for the same reason reconcile is: a live job has a fresh
    heartbeat, and only a stale one is touched."""

    async def scenario():
        store = JobStore(tmp_path / "reaper-live")
        record = store.create(objective="x", platform="p",
                              platform_url="https://p.test")
        clock = FakeClock()
        sup = Supervisor(store, clock=clock, poll_interval=10_000)
        sup.start_reaper(interval=30)

        # A job that keeps ticking, well inside its budget, for a long time.
        for _ in range(20):
            record = store.get(record.job_id)
            record.stage = Stage.RUN
            record.last_progress_at = clock.now().isoformat(timespec="seconds")
            record.stage_timeout_s = 600.0
            store.save(record)
            clock.advance(300)
            await _settle()

        await sup.shutdown()
        return store.get(record.job_id)

    fresh = asyncio.run(scenario())
    assert fresh.stage == Stage.RUN
    assert fresh.error is None


def test_the_reaper_survives_a_failure_and_keeps_going(tmp_path, monkeypatch):
    """If it raised, nothing would ever reconcile again and the failure would
    be invisible."""

    async def scenario():
        store = JobStore(tmp_path / "reaper-boom")
        clock = FakeClock()
        sup = Supervisor(store, clock=clock, poll_interval=10_000)

        calls = []
        real = store.list_ids

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("el disco no responde")
            return real()

        monkeypatch.setattr(store, "list_ids", flaky)
        task = sup.start_reaper(interval=30)
        for _ in range(4):
            clock.advance(30)
            await _settle()
        alive = not task.done()
        await sup.shutdown()
        return len(calls), alive

    count, alive = asyncio.run(scenario())
    assert count >= 2, "el reaper se detuvo en el primer fallo"
    assert alive


def test_shutdown_stops_the_reaper(tmp_path):
    async def scenario():
        store = JobStore(tmp_path / "reaper-stop")
        sup = Supervisor(store, clock=FakeClock(), poll_interval=60)
        task = sup.start_reaper(interval=30)
        await _settle()
        await sup.shutdown()
        return task

    assert asyncio.run(scenario()).done()


def test_the_app_starts_the_reaper(tmp_path, env):
    """Wired, not merely available."""
    app = create_app(
        root=tmp_path / "reap-app", engine=make_engine(tmp_path), background=False,
    )
    with TestClient(app, headers=dict(AUTH)):
        assert app.state.supervisor._reaper is not None


# =============================================================================
# the environment is resolved the way the SERVICE resolves it
# =============================================================================
#
# The bug: ANTHROPIC_API_KEY lives in 06_Application/.env, not the shell.
# `lxp_eval/config.py` calls load_dotenv at import, so the running service sees
# it -- but selfcheck imported neither, read a bare os.environ, and reported
# [FALLA] for a key the service would have had. A diagnostic that sends an
# operator to fix a working configuration, and a deploy gate that blocks a
# healthy deploy.


def test_selfcheck_agrees_with_the_service_about_the_anthropic_key():
    """THE REGRESSION. An exact equivalence, so it is non-vacuous on any
    machine: selfcheck passes this check if and only if the service has a key.
    """
    from lxp_eval.config import CONFIG          # the app's own resolution
    from lxp_eval.service import selfcheck

    # bool(), never the value: a failing assertion prints its operands, and
    # this one would have printed a live API key into the CI log.
    service_has_key = bool(CONFIG.anthropic_api_key)
    check = next(c for c in selfcheck.run_checks() if "ANTHROPIC" in c.name)
    assert check.passed == service_has_key


def test_all_three_variables_resolve_through_the_same_path():
    """Not just the Anthropic key: an operator who puts EVALKIT_SECRET_KEY or
    EVALKIT_SERVICE_TOKEN in .env -- the obvious place, since the Anthropic key
    is already there -- must not be told the service has no key."""
    import inspect as _inspect

    from lxp_eval.service import auth as auth_mod
    from lxp_eval.service import crypto as crypto_mod
    from lxp_eval.service import selfcheck

    # The two deployment secrets read the environment ONLY...
    for fn in (auth_mod.load_token, crypto_mod.SecretBox.from_env):
        src = _inspect.getsource(fn)
        assert "deployment_environ" in src, fn
        assert "engine_environ" not in src, fn
    # ...and selfcheck resolves each one from the source the service will use.
    src = _inspect.getsource(selfcheck.run_checks)
    assert "deployment_environ" in src and "engine_environ" in src


def test_dotenv_values_are_visible_to_the_engine_env(tmp_path, monkeypatch):
    """The ENGINE source reads .env. The deployment source does not."""
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        environ_mod, "load_dotenv_once",
        lambda: {"ANTHROPIC_API_KEY": "sk-que-vino-del-dotenv"},
    )

    assert environ_mod.engine_environ().get("ANTHROPIC_API_KEY") == "sk-que-vino-del-dotenv"


def test_an_explicit_env_bypasses_dotenv_entirely(monkeypatch):
    """Why every other test in this file is hermetic: passing `env` must never
    consult the developer's .env."""
    from lxp_eval.service import environ as environ_mod

    called = []
    monkeypatch.setattr(environ_mod, "load_dotenv_once",
                        lambda: called.append(1))

    assert environ_mod.engine_environ({"A": "1"}) == {"A": "1"}
    assert environ_mod.deployment_environ({"A": "1"}) == {"A": "1"}
    assert called == []


def test_a_broken_config_does_not_break_auth(monkeypatch):
    """Falling back to the bare environment is strictly better than refusing to
    read anything: a shell-exported token still works."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "dotenv":
            raise RuntimeError("dotenv esta roto")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-del-shell")

    from lxp_eval.service import environ as environ_mod

    assert environ_mod.engine_environ().get("ANTHROPIC_API_KEY") == "sk-del-shell"


def test_resolving_the_environment_never_raises_on_a_missing_key():
    """selfcheck has to REPORT a missing key, not die on it."""
    from lxp_eval.service import environ as environ_mod

    assert isinstance(environ_mod.load_dotenv_once(), dict)   # must not raise
    # The deployment source IS the process environment, untouched...
    assert environ_mod.deployment_environ() is os.environ
    # ...and the engine source is a merged COPY, never os.environ itself.
    assert environ_mod.engine_environ() is not os.environ
    assert dict(os.environ).items() <= dict(environ_mod.engine_environ()).items()


@pytest.mark.needs_real_platform
def test_dotenv_is_read_from_the_same_file_config_uses():
    """The one thing the deliberate override=False difference could hide: the
    two loaders drifting onto different files."""
    import dotenv

    found = dotenv.find_dotenv(usecwd=True)
    assert found, "no se encontró ningún .env desde 06_Application"
    assert Path(found).name == ".env"


# --- the split has to actually hold ------------------------------------------


def test_resolving_the_engine_env_does_not_mutate_os_environ(monkeypatch):
    """THE BUG THAT MADE THE SPLIT A LIE.

    The first version called `load_dotenv`, which writes into `os.environ`. So
    the moment anything resolved the engine key, every other `.env` line landed
    in the process environment and `deployment_environ` handed it straight
    back. selfcheck cheerfully reported two deployment secrets as "present in
    the environment" when they existed only in a file.
    """
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("EVALKIT_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        environ_mod, "load_dotenv_once",
        lambda: {"ANTHROPIC_API_KEY": "sk-del-archivo",
                 "EVALKIT_SERVICE_TOKEN": "token-del-archivo-0123456789"},
    )

    merged = environ_mod.engine_environ()
    assert merged["ANTHROPIC_API_KEY"] == "sk-del-archivo"

    # ...and NOTHING leaked into the process environment.
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "EVALKIT_SERVICE_TOKEN" not in os.environ
    assert environ_mod.deployment_environ().get("EVALKIT_SERVICE_TOKEN") is None


def test_the_real_environment_wins_over_the_file(monkeypatch):
    """override=False semantics, without the mutation."""
    from lxp_eval.service import environ as environ_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exportada-a-mano")
    monkeypatch.setattr(
        environ_mod, "load_dotenv_once",
        lambda: {"ANTHROPIC_API_KEY": "sk-vieja-del-archivo"},
    )
    assert environ_mod.engine_environ()["ANTHROPIC_API_KEY"] == "sk-exportada-a-mano"


def test_resolving_the_engine_env_twice_is_stable(monkeypatch):
    """No hidden once-only caching: a second call must answer the same."""
    from lxp_eval.service import environ as environ_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        environ_mod, "load_dotenv_once", lambda: {"ANTHROPIC_API_KEY": "sk-x"},
    )
    assert environ_mod.engine_environ() == environ_mod.engine_environ()


def test_selfcheck_warns_when_deployment_secrets_sit_in_dotenv():
    """Ambiguous configuration is named, not silently resolved."""
    from lxp_eval.service import selfcheck

    check = selfcheck.check_dotenv_hygiene(["EVALKIT_SECRET_KEY"])
    assert check.status == selfcheck.WARN
    assert check.passed          # a warning, not a failure: the service works
    assert "EVALKIT_SECRET_KEY" in check.detail
    assert check.fix


def test_selfcheck_is_quiet_when_dotenv_is_clean():
    from lxp_eval.service import selfcheck

    check = selfcheck.check_dotenv_hygiene([])
    assert check.status == selfcheck.OK


def test_a_warning_does_not_fail_the_deploy_gate(tmp_path, monkeypatch):
    """WARN must not be FAIL: a .env hygiene note should not block a deploy
    whose environment is correctly configured."""
    from lxp_eval.service import selfcheck

    monkeypatch.setattr(
        selfcheck, "run_checks",
        lambda *a, **k: [
            selfcheck.Check("ok", selfcheck.OK),
            selfcheck.Check("aviso", selfcheck.WARN, "algo ambiguo"),
        ],
    )
    assert selfcheck.main() == 0


# --- a panel that lost seats must not present as a full panel ----------------
#
# The generator now records seats-expected vs seats-that-voted (see
# tests/test_generator.py section 16). It has to be VISIBLE, or it is a field
# nobody reads: the review screen and the delivered report are the only two
# places a person meets these verdicts, and both are served from here.


class SilentSeatAnthropic(FakeAnthropic):
    """FakeAnthropic where one seat never emits the forced tool call."""

    def __init__(self, seat, **kw):
        super().__init__(**kw)
        self.seat = seat
        self.messages = self._M(self)

    class _M:
        def __init__(self, parent):
            self._p = parent

        def create(self, **kwargs):
            p = self._p
            name = kwargs["tools"][0]["name"]
            if name == REVIEW_TOOL["name"] and f"[JUEZ: {p.seat}]" in _system_text(kwargs):
                p.calls.append(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="No puedo responder.")],
                )
            return FakeAnthropic._Messages(p).create(**kwargs)


def test_a_reduced_quorum_is_visible_in_the_serialized_job(tmp_path, client_factory):
    """5 of 7, said in the plan the reviewer actually reads."""
    fake = SilentSeatAnthropic("coherencia_criterio")
    c = client_factory(engine=make_engine(tmp_path, client=fake))
    plan = plan_of(c, start(c))

    probe = plan["probes"][0]
    assert probe["quorum"]["expected"] == len(generator_mod.COUNCIL)
    assert probe["quorum"]["voted"] == len(generator_mod.COUNCIL) - 1
    assert probe["quorum"]["complete"] is False
    assert probe["quorum"]["silent"] == ["coherencia_criterio"]
    assert probe["fullCouncil"] is False
    assert "CONSEJO INCOMPLETO" in probe["quorumNote"]

    # The seat's own row must be distinguishable from a considered rejection.
    seat = next(j for j in probe["judges"] if j["judge"] == "coherencia_criterio")
    assert seat["verdict"] == "reject"      # fails closed, unchanged
    assert seat["answered"] is False        # ... but it did not judge anything
    assert seat["attempts"] == generator_mod.JUDGE_ATTEMPTS
    others = [j for j in probe["judges"] if j["judge"] != "coherencia_criterio"]
    assert all(j["answered"] for j in others)


def test_a_full_council_serializes_as_complete(client_factory, tmp_path):
    c = client_factory()
    plan = plan_of(c, start(c))
    probe = plan["probes"][0]
    assert probe["quorum"]["complete"] is True
    assert probe["quorum"]["voted"] == probe["quorum"]["expected"]
    assert probe["fullCouncil"] is True
    assert probe["quorum"]["silent"] == []
    assert all(j["answered"] for j in probe["judges"])


def test_the_report_header_says_whether_the_council_was_whole(tmp_path, client_factory):
    """The report outlives the review screen, so the fact has to live there too."""
    fake = SilentSeatAnthropic("coherencia_criterio")
    c = client_factory(engine=make_engine(tmp_path, client=fake))
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={})
    r = c.get(f"/jobs/{job_id}/report")
    assert r.status_code == 200, r.text
    quorum = r.json()["meta"]["councilQuorum"]
    assert "CONSEJO INCOMPLETO" in quorum
    assert "coherencia_criterio" in quorum


def test_a_whole_council_says_so_in_the_report_header(client_factory):
    c = client_factory()
    job_id = start(c)
    c.post(f"/jobs/{job_id}/approve", json={})
    r = c.get(f"/jobs/{job_id}/report")
    assert r.status_code == 200, r.text
    assert "consejo completo" in r.json()["meta"]["councilQuorum"].lower()


# =============================================================================
# transient API failures are retried; permanent ones are not
# =============================================================================
#
# A real run died outright on
#
#     500 - {'type': 'api_error', 'message': 'Internal server error'}
#
# during the framework stage. The job went straight to `error` and discarded
# everything built so far. A 500 is exactly what should be retried.
#
# The stakes are not symmetric across stages. A browser run takes tens of
# minutes and the score stage runs AFTER it, so a transient 500 twenty minutes
# in throws away the whole run and the money already spent driving a live
# platform. So the retry wraps the CLIENT, not one call site, and the tests
# below pin it in the probes stage and the score stage -- not just framework.
#
# What a fake CANNOT verify: that the real API's 500s are in fact transient,
# that our backoff is long enough for a real provider incident, or that the
# SDK's own retry layer (verified separately: 3 attempts, <1s total, statuses
# 408/409/429/5xx) composes with this one the way we expect over a real socket.
# Only that we ask again on the statuses we chose and refuse to on 400/401.


class ApiStatusError(Exception):
    """Shaped like the SDK's APIStatusError: carries `status_code`."""

    def __init__(self, status, message="Internal server error"):
        super().__init__(
            f"{status} - {{'type': 'api_error', 'message': '{message}'}}"
        )
        self.status_code = status


class Flaky:
    """Wraps a fake client; fails the first N calls that match a predicate."""

    def __init__(self, inner, error, failures, *, match=None):
        self.inner = inner
        self.error = error
        self.failures = failures
        self.match = match or (lambda kwargs: True)
        self.failed = 0
        self.attempts = 0
        self.messages = self._M(self)

    @property
    def calls(self):
        return self.inner.calls

    class _M:
        def __init__(self, parent):
            self._p = parent

        def create(self, **kwargs):
            p = self._p
            if p.match(kwargs):
                p.attempts += 1
                if p.failed < p.failures:
                    p.failed += 1
                    raise p.error
            return p.inner.messages.create(**kwargs)


def _is_scoring(kwargs):
    return kwargs["tools"][0]["name"] == "emitir_veredicto"


def _is_judging(kwargs):
    return kwargs["tools"][0]["name"] == REVIEW_TOOL["name"]


def _fast_policy(**kw):
    """A real RetryPolicy that records its waits instead of taking them."""
    waits = []
    policy = RetryPolicy(sleep=waits.append, _rand=lambda: 0.0, **kw)
    policy.waits = waits
    return policy


# --- the policy itself -------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_transient_statuses_are_retried(status):
    assert is_transient(ApiStatusError(status)) is True


@pytest.mark.parametrize("status", [400, 401])
def test_a_bad_request_or_a_bad_key_is_never_retried(status):
    """They fail identically forever; retrying only delays a clear error.

    400 is also how this model reports a `temperature` / `top_p` / `top_k`
    parameter, and that is a bug to surface immediately -- a retry loop that
    swallowed it for thirty seconds would be actively harmful.
    """
    assert is_transient(ApiStatusError(status, "invalid_request_error")) is False


def test_connection_errors_are_retried():
    assert is_transient(ConnectionError("conexión cerrada")) is True
    assert is_transient(TimeoutError("se agotó el tiempo")) is True


def test_an_error_with_no_status_and_no_socket_is_not_retried():
    assert is_transient(ValueError("un bug nuestro")) is False


def test_the_backoff_grows_and_is_capped():
    policy = RetryPolicy(base_delay_s=2.0, max_delay_s=10.0, jitter=0.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4, 5)] == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_retries_are_bounded_and_the_last_error_survives():
    policy = _fast_policy(max_attempts=3, base_delay_s=1.0)
    tries = []

    def always_500():
        tries.append(1)
        raise ApiStatusError(500)

    with pytest.raises(ApiStatusError) as exc:
        policy.call(always_500)
    assert len(tries) == 3                    # bounded, not forever
    assert exc.value.status_code == 500       # the REAL failure, not a wrapper
    assert len(policy.waits) == 2             # ... and it did not busy-loop


def test_a_fatal_error_is_raised_on_the_first_attempt():
    policy = _fast_policy(max_attempts=5)
    tries = []

    def always_400():
        tries.append(1)
        raise ApiStatusError(400, "invalid_request_error")

    with pytest.raises(ApiStatusError):
        policy.call(always_400)
    assert len(tries) == 1
    assert policy.waits == []


# --- wired into the stage machine --------------------------------------------


def test_a_500_in_the_probes_stage_is_retried_and_the_job_survives(
    tmp_path, client_factory
):
    flaky = Flaky(FakeAnthropic(), ApiStatusError(500), failures=2, match=_is_judging)
    policy = _fast_policy(base_delay_s=1.0)
    c = client_factory(engine=make_engine(tmp_path, client=flaky, retry=policy))
    job_id = start(c)

    job = c.get(f"/jobs/{job_id}").json()
    assert job["stage"] == Stage.AWAITING_APPROVAL, job.get("error")
    assert job.get("error") in (None, {}, "")
    assert flaky.failed == 2
    assert policy.waits, "it retried without ever waiting"


def test_a_500_in_the_score_stage_is_retried(tmp_path, client_factory):
    """The expensive one: this runs AFTER tens of minutes of browser time."""
    flaky = Flaky(FakeAnthropic(), ApiStatusError(500), failures=2, match=_is_scoring)
    policy = _fast_policy(base_delay_s=1.0)
    c = client_factory(engine=make_engine(tmp_path, client=flaky, retry=policy))
    job_id = start(c)
    r = c.post(f"/jobs/{job_id}/approve", json={})
    assert r.status_code == 200, r.text

    job = c.get(f"/jobs/{job_id}").json()
    assert job["stage"] == Stage.DONE, job.get("error")
    assert flaky.failed == 2
    # And the run's evidence was NOT discarded and re-collected.
    assert c.get(f"/jobs/{job_id}/report").status_code == 200


def test_a_529_overloaded_is_retried_too(tmp_path, client_factory):
    flaky = Flaky(FakeAnthropic(), ApiStatusError(529, "overloaded"),
                  failures=1, match=_is_judging)
    c = client_factory(engine=make_engine(
        tmp_path, client=flaky, retry=_fast_policy(base_delay_s=1.0)))
    job_id = start(c)
    assert c.get(f"/jobs/{job_id}").json()["stage"] == Stage.AWAITING_APPROVAL


def test_a_400_is_not_retried_and_the_job_reports_it_immediately(
    tmp_path, client_factory
):
    # Exactly ONE CALL in the whole run 400s, so the attempt count is
    # unambiguous. Two things run in parallel and both would sum into the
    # counter: the council's seven seats (hence the per-seat matcher) and now
    # the probes themselves (hence the per-probe one). Without the second, two
    # probes each landing their first call on this seat read as "it was
    # retried" when nothing was retried at all.
    seen_probe: list[str] = []
    lock = threading.Lock()

    def one_call(kwargs):
        if not _is_judging(kwargs):
            return False
        if "[JUEZ: coherencia_objetivo]" not in _system_text(kwargs):
            return False
        # Primera probe que llega a este asiento; las demás no matchean.
        who = json.dumps(kwargs.get("messages"), ensure_ascii=False)[:600]
        with lock:
            if not seen_probe:
                seen_probe.append(who)
            return who == seen_probe[0]

    flaky = Flaky(
        FakeAnthropic(), ApiStatusError(400, "invalid_request_error"),
        failures=99, match=one_call,
    )
    policy = _fast_policy(base_delay_s=1.0)
    c = client_factory(engine=make_engine(tmp_path, client=flaky, retry=policy))
    job_id = start(c)

    job = c.get(f"/jobs/{job_id}").json()
    assert job["stage"] == Stage.ERROR
    assert flaky.attempts == 1, "a 400 was retried; it will fail identically forever"
    # La prueba independiente de que no hubo reintento: un reintento habría
    # dormido su backoff, y la política registra cada espera.
    assert policy.waits == []
    assert "400" in json.dumps(job, ensure_ascii=False)


def test_an_exhausted_retry_still_names_the_stage_and_the_real_message(
    tmp_path, client_factory
):
    flaky = Flaky(FakeAnthropic(), ApiStatusError(500), failures=99, match=_is_judging)
    policy = _fast_policy(max_attempts=3, base_delay_s=1.0)
    c = client_factory(engine=make_engine(tmp_path, client=flaky, retry=policy))
    job_id = start(c)

    job = c.get(f"/jobs/{job_id}").json()
    assert job["stage"] == Stage.ERROR
    blob = json.dumps(job, ensure_ascii=False)
    assert "api_error" in blob and "Internal server error" in blob


def test_the_framework_stage_gets_a_retrying_client_too(tmp_path, client_factory):
    """Framework is where the live run actually died.

    It is faked in these tests, so rather than pretend, this asserts the thing
    that matters: the client HANDED to `build_framework` retries. If it were
    the bare client, the stage would be exactly as fragile as it was.
    """
    seen = {}

    def build(objective, **kwargs):
        seen["client"] = kwargs["client"]
        return framework_result()

    policy = _fast_policy(base_delay_s=1.0)
    c = client_factory(engine=make_engine(tmp_path, build=build, retry=policy))
    start(c)

    handed = seen["client"]
    tries = []

    def flaky_create(**kwargs):
        tries.append(1)
        if len(tries) < 3:
            raise ApiStatusError(500)
        return "ok"

    handed.messages._messages = SimpleNamespace(create=flaky_create)
    assert handed.messages.create(model="claude-opus-4-8") == "ok"
    assert len(tries) == 3


def test_the_retry_forwards_the_request_byte_for_byte(tmp_path, client_factory):
    """A retry is not a request builder.

    Specifically: it must not add, drop, or default `temperature` / `top_p` /
    `top_k`. Those were removed on this model and return 400, and a wrapper
    that "helpfully" normalised the kwargs would reintroduce the exact failure
    tests/test_scoring.py::test_judge_sends_no_sampling_parameters exists for.
    """
    flaky = Flaky(FakeAnthropic(), ApiStatusError(500), failures=1, match=_is_judging)
    c = client_factory(engine=make_engine(
        tmp_path, client=flaky, retry=_fast_policy(base_delay_s=1.0)))
    start(c)

    assert flaky.calls, "nothing was recorded"
    for kwargs in flaky.calls:
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "top_k" not in kwargs
    # The retried call is identical to the one that failed: same model, same
    # tools, same forced tool_choice.
    judged = [k for k in flaky.calls if _is_judging(k)]
    assert judged and all(
        k["tool_choice"] == {"type": "tool", "name": REVIEW_TOOL["name"]}
        for k in judged
    )


def test_a_policy_of_one_attempt_returns_the_client_untouched():
    """The escape hatch has to be real, not a wrapper that pretends."""
    inner = FakeAnthropic()
    assert RetryPolicy(max_attempts=1).wrap(inner) is inner


# =============================================================================
# platform resolution
# =============================================================================


def _platform_tree(root):
    """Two platforms whose login host differs from their marketing host."""
    (root / "plataforma_a").mkdir(parents=True)
    (root / "plataforma_a" / "config.yaml").write_text(
        'name: Plataforma A\nvendor: Plataforma A\nurl: https://plataforma_a.com\n'
        'auth:\n  login_url: "https://app.plataforma-a.ejemplo/login/"\n',
        encoding="utf-8",
    )
    (root / "plataforma_b").mkdir(parents=True)
    (root / "plataforma_b" / "config.yaml").write_text(
        'name: PLATAFORMA_B\nvendor: PLATAFORMA_B\nurl: https://plataforma-b.ejemplo\n'
        'auth:\n  login_url: "https://app.plataforma-b.ejemplo/"\n',
        encoding="utf-8",
    )
    return root


def test_the_login_host_resolves_to_its_platform_directory(tmp_path):
    """The host a student logs in at is not the vendor's marketing host.

    Plataforma A is reached at app.plataforma-a.ejemplo; its config.yaml says
    `url: https://plataforma_a.com`. Matching only on `url` would find nothing.
    """
    from lxp_eval.platform_loader import resolve_platform_dir

    root = _platform_tree(tmp_path / "platforms")
    got = resolve_platform_dir("https://app.plataforma-a.ejemplo/studentV2/", root)
    assert got == "plataforma_a"
    # the marketing host resolves too, and the other platform is unaffected
    assert resolve_platform_dir("https://plataforma_a.com/", root) == "plataforma_a"
    assert resolve_platform_dir("https://app.plataforma-b.ejemplo/x", root) == "plataforma_b"


def test_an_unclaimed_host_fails_loudly_rather_than_guessing(tmp_path):
    """Silently falling back to the hostname is what produced the original bug:
    a run that got all the way to the browser before discovering it had no
    config. An unknown host must be a refusal, and must name the candidates."""
    from lxp_eval.platform_loader import resolve_platform_dir

    root = _platform_tree(tmp_path / "platforms")
    with pytest.raises(Exception) as exc:
        resolve_platform_dir("https://unknown.example.com/", root)
    msg = str(exc.value)
    assert "unknown.example.com" in msg
    assert "plataforma_a" in msg and "plataforma_b" in msg


def test_the_run_finds_the_session_that_was_saved_for_that_platform(tmp_path, monkeypatch):
    """The saved session must be the one the run actually loads.

    It was not. The factory built PlaywrightDriver without `storage_state` at
    all, so a verified, unexpired session sat on disk while every run opened a
    clean context and bounced off the login wall. Nothing in the suite noticed,
    because a fake driver has no session to lose.
    """
    from lxp_eval.evalkit import session as session_mod
    from lxp_eval.service.jobs import session_state_for

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_mod, "_default_dir", lambda: sessions)

    assert session_state_for("plataforma_a", "student") is None      # none saved yet

    saved = sessions / "storage_state_plataforma_a_student.json"
    saved.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    assert session_state_for("plataforma_a", "student") == saved
    # keyed by platform AND role -- a teacher session is not a student one
    assert session_state_for("plataforma_a", "teacher") is None
    assert session_state_for("plataforma_b", "student") is None


@pytest.mark.asyncio
async def test_the_runner_is_handed_a_driver_that_understands_prose(tmp_path, monkeypatch):
    """Probe steps are prose, and the factory handed over a verb-only driver.

    `steps` is declared as prose by design -- generator.py's tool schema says
    "pasos que un evaluador humano o un agente puede ejecutar", and the design
    doc's example is "Abrir curso de Matemática 3.º". PlaywrightDriver.do()
    speaks goto/click/fill/press/wait/expect_text and nothing else, so the
    first real probe died on step 1 with "no sé ejecutar el paso".

    AgentDriver existed the whole time, tested, documented as adapting the
    agent "to the probe runner's driver interface". The service just never
    wrapped anything in it.
    """
    import lxp_eval.browser_agents.claude_browser_agent as agent_mod
    import lxp_eval.browser_agents.playwright_driver as pw_mod
    from lxp_eval.config import CONFIG
    from lxp_eval.service.jobs import _default_driver_factory

    platforms = tmp_path / "platforms"
    (platforms / "plataforma_a").mkdir(parents=True)
    (platforms / "plataforma_a" / "config.yaml").write_text(
        'name: Plataforma A\nvendor: K\nurl: https://plataforma_a.com\n'
        'auth:\n  login_url: "https://app.plataforma-a.ejemplo/login/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(CONFIG, "platforms_dir", platforms, raising=False)

    verbs_received, prose_received = [], []

    class FakeBrowser:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def navigate(self, url): pass
        async def do(self, step): verbs_received.append(step)
        async def read_page(self, max_chars=8000): return "página"

    class FakeAgent:
        max_iterations = 0
        cost_cap_usd = 0.0
        tools: list = []
        def __init__(self, *a, **k): pass
        async def run(self, *, system, user_task):
            prose_received.append(user_task)
            return SimpleNamespace(blocked=False, accomplished=True, turns=1)

    monkeypatch.setattr(pw_mod, "PlaywrightDriver", FakeBrowser)
    monkeypatch.setattr(agent_mod, "ClaudeBrowserAgent", FakeAgent)

    store = JobStore(tmp_path / "df")
    record = store.create(
        objective="x", platform="app.plataforma-a.ejemplo",
        platform_url="https://app.plataforma-a.ejemplo/studentV2/",
    )

    async with _default_driver_factory(record, None, tmp_path / "art") as driver:
        await driver.do("Acceder al tutor de IA como estudiante de primaria")
        # capture still goes straight to the browser, not through the LLM
        assert await driver.read_page() == "página"

    assert prose_received, "el paso en prosa nunca llegó al agente"
    assert "Acceder al tutor" in prose_received[0]
    assert verbs_received == [], "la prosa se le pasó al ejecutor de verbos"


def test_a_mojibake_plan_is_refused_instead_of_quietly_recorded(client_factory):
    """The plan IS the audit record, so corruption must not be absorbed.

    Root cause of the original block, found only by comparing decodings: the
    service's JSON is clean UTF-8, and a client that decodes it with the
    Windows locale (cp1252) turns "ESTÁ" into "ESTÃ" + byte 0x81, which
    surrogateescape renders as \\udc81. That client then POSTs the mangled text
    back. Scrubbing it at the store would make the job approvable again while
    writing "ESTÃ\ufffd" into the record a ministry is meant to audit.

    So the boundary refuses it and says what to fix. The store's scrub stays,
    but as a backstop against becoming unpersistable -- not as a laundry.
    """
    c = client_factory()
    job_id = start(c)
    plan = plan_of(c, job_id)
    # Built at runtime, not written as a literal: a lone surrogate baked into
    # this module's constants makes the file itself uncompilable.
    mojibake = "el probe mide ESTE criterio TAL COMO ESTÃ" + chr(0xDC81) + " ESCRITO"
    plan["probes"][0]["goal"] = mojibake

    # Sent the way a real client sends it: ensure_ascii escapes the surrogate
    # to a plain-ASCII "\\udc81" on the wire, which is why nothing upstream
    # ever noticed. json.loads rebuilds it faithfully on arrival.
    body = json.dumps({"plan": plan}).encode("ascii")
    r = c.post(
        f"/jobs/{job_id}/approve",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "codificación" in detail or "codificacion" in detail
    assert "utf-8" in detail.lower()
    # and the job is untouched: still awaiting a good plan, nothing recorded
    assert c.get(f"/jobs/{job_id}").json()["stage"] == "awaiting_approval"


def test_the_run_budget_scales_with_the_number_of_steps():
    """A probe's cost is now proportional to its steps, so its clock must be.

    The budget was written when a step was a deterministic DSL verb -- goto,
    click -- costing about two seconds, so one flat allowance covered any
    probe. Every step is now an agent loop against a live page: one to two
    minutes each. A 7-step probe therefore blew the flat 600s and the watchdog
    killed a perfectly healthy run at 605s, mid-probe. PB-PT-D8-01 has 28 steps
    and could never have finished.
    """
    from lxp_eval.service.watchdog import Stage, stage_budget_s

    one = stage_budget_s(Stage.RUN, sampling={"repeats": 1}, steps=1)
    seven = stage_budget_s(Stage.RUN, sampling={"repeats": 1}, steps=7)
    many = stage_budget_s(Stage.RUN, sampling={"repeats": 1}, steps=28)

    assert seven > one and many > seven, "more steps must buy more time"
    assert seven >= 7 * 60, "a 7-step agent probe needs minutes per step, not seconds"
    # repeats still multiply: the runner walks the whole probe again
    assert stage_budget_s(Stage.RUN, sampling={"repeats": 3}, steps=7) > seven
    # and an unknown step count must not collapse the budget to nothing
    assert stage_budget_s(Stage.RUN, sampling={"repeats": 1}) >= one


def test_one_unevaluable_probe_does_not_destroy_the_whole_run(tmp_path, client_factory):
    """A probe that cannot be evaluated is a finding, not a catastrophe.

    The run loop wrapped every probe in ONE try/except, so the first failure
    aborted the stage and every probe after it was simply never attempted. Live:
    PB-PT-D9-M2 correctly refused (the criterion wants 9th grade and this tenant
    has none) and took PB-PT-D4-02 and PB-PT-D8-01 down with it -- two probes
    that had nothing wrong with them and produced no evidence.

    The refusal must be recorded against its own criterion, with the reason,
    and the run must go on. Why a criterion could not be evaluated is itself an
    audit fact; losing the other probes is just lost work.
    """
    from lxp_eval.evalkit.errors import EvalkitError

    attempted = []

    async def run_probe(probe, driver, ledger, **kw):
        attempted.append(probe.probe_id)
        if len(attempted) == 1:
            raise EvalkitError("no existe un curso de 9.o grado en esta cuenta")
        return None

    c = client_factory(make_engine(tmp_path, run_probe=run_probe))
    job_id = start(c)
    plan = plan_of(c, job_id)
    r = c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    assert r.status_code == 200
    body = r.json()
    assert body["stage"] != "error", "one refused probe must not fail the job"
    assert len(attempted) > 1, "the probes after the refusal must still be attempted"
    # and the reason survives, attached to the probe that could not be evaluated
    state = c.get(f"/jobs/{job_id}").json()
    unevaluated = state.get("unevaluated") or {}
    assert attempted[0] in unevaluated
    assert "9.o grado" in unevaluated[attempted[0]]


def test_a_single_run_is_not_unanimity():
    """One judgement cannot agree with itself.

    `unanimous` was `len(set(runs)) <= 1`, so a cell judged ONCE came back
    True — and the report screen prints «unánimes» for it. Every criterion in
    a repeats:1 evaluation therefore claimed agreement that was never
    measured, which is precisely the overstatement this product argues
    against. Unanimity needs at least two runs; below that it is unknown, not
    true and not false.
    """
    from lxp_eval.service.serialize import unanimity_of

    assert unanimity_of([]) is None, "no runs measured nothing"
    assert unanimity_of(["CUMPLE"]) is None, "one run measured nothing"
    assert unanimity_of(["CUMPLE", "CUMPLE"]) is True
    assert unanimity_of(["CUMPLE", "NO_CUMPLE"]) is False
    assert unanimity_of(["CUMPLE", "CUMPLE", "PARCIAL"]) is False


def test_editing_a_probe_retires_the_council_s_approval():
    """The council approved a TEXT, not a probe id.

    `approved` rides on the stored probe and survived any human edit, so a
    reviewer could rewrite every step and the probe still reported itself as
    approved by five judges who never read the new wording. That is exactly
    what happened to this project's own evaluation: all eight probes were
    rewritten by hand against the real UI and all eight kept `approved: true`.

    The gate is unaffected — `requiresSignoff` is always re-read from the
    server — but a claim of review has to be about the text reviewed.
    """
    from lxp_eval.service.serialize import council_still_applies

    stored = {"goal": "g", "verdictRule": "v",
              "steps": [{"n": 1, "action": "abrir el chat"}]}

    assert council_still_applies(stored, None) is True, "no edit, approval stands"
    assert council_still_applies(stored, {}) is True
    assert council_still_applies(stored, dict(stored)) is True, "same text"

    changed_steps = {**stored, "steps": [{"n": 1, "action": "otra cosa"}]}
    assert council_still_applies(stored, changed_steps) is False
    assert council_still_applies(stored, {**stored, "goal": "otro"}) is False
    assert council_still_applies(stored, {**stored, "verdictRule": "otra"}) is False


def test_the_vendor_notice_survives_the_probes_stage(tmp_path, client_factory):  # noqa: D401
    """The notice is needed at SIGNOFF, and that is where it was missing.

    `ProbeSet.vendor_notice()` renders the document a client actually sends —
    the flagged probes with their literal stimulus, so the vendor can redirect
    its own alerts. The service built the ProbeSet, took `probes` off it and
    dropped the rest, so by the time a human reached the crisis probe and was
    asked to countersign the notice, the notice no longer existed. It had to be
    rewritten by hand for PT-D9-S1 on 2026-07-20.

    The gate's own wording is "se avisa al proveedor antes de ejecutarlo y una
    persona contrafirma ese aviso". A signature demanded without producing the
    thing being countersigned is a signature about nothing.
    """
    c = client_factory(make_engine(tmp_path, client=FakeAnthropic(safety_verdict=REVISE)))
    job_id = start(c)
    state = c.get(f"/jobs/{job_id}").json()

    gated = [p for p in state["plan"]["probes"] if p["requiresSignoff"]]
    assert gated, "this fixture must produce at least one gated probe"

    notice = state.get("vendorNotice") or ""
    assert notice.strip(), "a gated job must carry the notice it demands a signature on"
    for p in gated:
        assert p["probeId"] in notice or p["criterionId"] in notice, (
            f"{p['probeId']} is gated but absent from the notice the vendor gets"
        )


def test_the_job_records_what_the_run_spent(tmp_path, client_factory):
    """The bill has to survive on the record, not in a log nobody reads.

    The driver accumulates spend per step, but the only way to see it was a
    `logger.info` — and the service emits NO info-level logging at all: an
    engine.log after a full run contains five uvicorn lines and nothing from
    `lxp_eval`. So the number existed for microseconds and vanished, which is
    why 65 USD of live probing could not be attributed to anything.
    """
    # Una instancia POR ETAPA, como en producción: recon y run reciben cada una
    # su driver del factory y acumulan su propio gasto desde cero. Un fake con
    # gasto estático compartido entre etapas contaba doble en cuanto recon
    # empezó a plegar el suyo — y ocultaba lo que este test debe fijar ahora:
    # que los pliegues son ADITIVOS (recon + run), no un pisado del último.
    class _Driver:
        def __init__(self, spend, cache_read, cache_write):
            self.spend_usd = spend
            self.cache_read_tokens = cache_read
            self.cache_write_tokens = cache_write
            self.steps_run = 6

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def run_probe(probe, driver, ledger, **kw):
        return None

    engine = make_engine(tmp_path, run_probe=run_probe)
    # recon recibe la primera instancia (0.25), la corrida la segunda (1.75).
    handed = []

    def factory(*a, **k):
        d = _Driver(0.25, 20_000, 1_000) if not handed else _Driver(1.75, 100_000, 3_000)
        handed.append(d)
        return d

    engine.driver_factory = factory
    c = client_factory(engine)
    job_id = start(c)
    plan = plan_of(c, job_id)
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    state = c.get(f"/jobs/{job_id}").json()
    assert state.get("spendUsd") == pytest.approx(2.0), state.get("spendUsd")
    assert state.get("cacheReadTokens") == 120_000


def test_framework_and_council_spend_is_recorded_without_a_browser_run(
    tmp_path, client_factory
):
    """The council runs BEFORE any browser opens, and it costs real money.

    `record.spend_usd` used to be set only from the driver, inside the run
    scope -- so a job that stops at awaiting_approval reported 0.0 even though
    the five-judge council had just spent through the API. The metered client
    now folds that spend into the record at probes time. The job below never
    runs a probe, yet its bill must be non-zero and must match the metered
    usage the fake client reported.
    """
    class _MeteredFake(FakeAnthropic):
        class _Metered(FakeAnthropic._Messages):
            def create(self, **kwargs):
                resp = super().create(**kwargs)
                resp.usage = SimpleNamespace(
                    input_tokens=1_000, output_tokens=100,
                    cache_read_input_tokens=2_000, cache_creation_input_tokens=50,
                )
                return resp

        def __init__(self, **kw):
            super().__init__(**kw)
            self.messages = self._Metered(self)

    from lxp_eval.evalkit.pricing import cost_usd

    client = _MeteredFake()
    c = client_factory(make_engine(tmp_path, client=client))
    job_id = start(c)

    state = c.get(f"/jobs/{job_id}").json()
    assert state["stage"] == "awaiting_approval", state["stage"]

    n = len(client.calls)
    assert n > 0, "the probe council must have called the API before stopping"

    per_call = cost_usd(
        1_000, 100, cache_read_tokens=2_000, cache_write_tokens=50,
        price_in=5.0, price_out=25.0,
    )
    assert state.get("spendUsd") == pytest.approx(n * per_call, abs=1e-4), state.get("spendUsd")
    assert state.get("cacheReadTokens") == 2_000 * n
    assert state.get("cacheWriteTokens") == 50 * n


def test_the_service_is_not_mute(env, tmp_path):
    """`logger.info` across the package reached nothing under uvicorn.

    The package carries two dozen info-level lines — which probe is running,
    which session was reused, what a step cost — and none ever appeared in
    engine.log: after a full live run that file held five uvicorn lines and
    nothing from `lxp_eval`. Root defaults to WARNING and no handler is ever
    attached to the package logger, so every one of them was discarded.

    Twelve hours of debugging were done by reading job.json and opening
    screenshots because of this.

    Reproduces the PRODUCTION condition deliberately — root pinned at WARNING,
    nothing attached to the package — because a `caplog` fixture installs its
    own handler and hides exactly the bug under test.
    """
    import io
    import logging

    from lxp_eval.service.app import create_app

    pkg = logging.getLogger("lxp_eval")
    root = logging.getLogger()
    saved = (list(pkg.handlers), pkg.level, pkg.propagate, root.level)
    pkg.handlers[:] = []
    pkg.setLevel(logging.NOTSET)
    root.setLevel(logging.WARNING)      # what uvicorn leaves behind
    try:
        create_app(root=tmp_path / "r", token="t", background=False, reap_interval=0)

        sink = io.StringIO()
        probe = logging.StreamHandler(sink)
        probe.setLevel(logging.DEBUG)
        assert pkg.handlers, "create_app must attach a handler to the package logger"
        pkg.handlers.append(probe)

        logging.getLogger("lxp_eval.evalkit.runner").info("probe X arrancó")
        assert "probe X arrancó" in sink.getvalue(), (
            "an INFO line from the package must survive a WARNING root"
        )

        n = len(pkg.handlers)
        create_app(root=tmp_path / "r2", token="t", background=False, reap_interval=0)
        assert len(pkg.handlers) == n, "handlers must not accumulate per app"
    finally:
        pkg.handlers[:], pkg.level, pkg.propagate, root.level = saved


def test_the_plan_says_which_panel_reviewed_it():
    """Approving a plan means trusting a specific council. Name it.

    The probe carried `fullCouncil` — the seats as they sat — but no
    fingerprint of the council or the drafter. Reword a judge's remit, add or
    drop a seat, and two plans look identical while having been reviewed by
    different panels. The signature a reviewer gives is about a review; the
    review has to be identifiable.
    """
    from lxp_eval.evalkit.generator import Provenance
    from lxp_eval.service.serialize import probe_to_dict

    prov = Provenance(
        probe_id="PB-X", test_id="PT-X", criterion={}, model="m",
        drafter_fingerprint="drafter:abc123",
        council_fingerprint="council:def456",
        rounds=(), approved=True, requires_human_signoff=False,
    )
    probe = SimpleNamespace(
        probe_id="PB-X", test_id="PT-X", goal="g", verdict_rule="v",
        steps=["uno"], capture=["screenshot"], repeats=1, repeat_judging=1,
    )
    out = probe_to_dict(SimpleNamespace(probe=probe, provenance=prov))

    assert out["councilSha"] == "council:def456"
    assert out["drafterSha"] == "drafter:abc123"


# --- el latido por probe no puede escribir job.json desde el hilo worker -----


def test_probe_heartbeats_never_save_from_a_second_thread(client_factory):
    """El latido por probe corre dentro de `asyncio.to_thread`. Guardar el
    registro desde ESE hilo corre una carrera de dos escritores sobre job.json
    -- el del loop y el del worker -- y en Windows el `os.replace` del perdedor
    muere con PermissionError. Fue intermitente y lo destapó
    `test_background_true_goes_through_the_supervisor`. Todos los guardados
    tienen que ocurrir en UN solo hilo: el del loop."""
    import threading

    from lxp_eval.service.store import JobStore

    seen: set[int] = set()
    real = JobStore.save

    def spying(self, record):
        seen.add(threading.get_ident())
        return real(self, record)

    JobStore.save = spying
    try:
        c = client_factory()
        job_id = start(c)
        body = {}
        for _ in range(400):
            body = c.get("/jobs/%s" % job_id).json()
            if body["stage"] in (Stage.AWAITING_APPROVAL, Stage.ERROR):
                break
        assert body["stage"] == Stage.AWAITING_APPROVAL, body.get("error")
    finally:
        JobStore.save = real

    assert len(seen) == 1, (
        f"job.json se guardó desde {len(seen)} hilos distintos: esa es la "
        "carrera que mata al perdedor con PermissionError en Windows"
    )


# --- el reconocimiento se MUESTRA: mapa + capturas del recorrido -------------


def test_the_state_exposes_what_recon_found(client_factory):
    """El recorrido fotografió la plataforma desde su primera corrida viva y el
    producto se guardó la prueba: mapa y capturas quedaban en disco mientras
    las pantallas no mostraban nada. El estado público ahora lleva `recon` --
    el mapa de superficies y los nombres de las capturas del paseo."""
    c = client_factory()
    job_id = start(c)

    # Simular lo que _recon deja en disco: el mapa por-trabajo y una captura.
    root = c.app.state.store.evidence_dir(job_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "surfaces_discovered.yaml").write_text(
        "surfaces:\n  tutor_chat:\n    name: Chat del tutor\n    steps:\n"
        "    - Abrir el primer curso\n", encoding="utf-8",
    )
    adir = c.app.state.store.artifacts_dir(job_id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "20260726T0423_recon_landing.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    state = c.get(f"/jobs/{job_id}").json()
    recon = state.get("recon")
    assert recon, "el estado publico no expone el reconocimiento"
    assert "tutor_chat" in (recon.get("surfaces") or {}).get("surfaces", {})
    assert "20260726T0423_recon_landing.png" in recon.get("shots", [])


def test_evidence_serves_recon_pngs_that_never_entered_the_ledger(client_factory):
    """Las capturas del recon no están en el ledger (ninguna prueba corrió).
    La lista blanca gana UNA forma más: PNGs del directorio de artefactos de
    este trabajo. Todo lo demás sigue rigiéndose por el ledger."""
    c = client_factory()
    job_id = start(c)
    adir = c.app.state.store.artifacts_dir(job_id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "recon_tutor.png").write_bytes(b"\x89PNG-bytes")

    r = c.get(f"/jobs/{job_id}/evidence/recon_tutor.png")
    assert r.status_code == 200
    assert r.content == b"\x89PNG-bytes"


def test_evidence_still_refuses_non_png_files_outside_the_ledger(client_factory):
    """La ampliación es SOLO para .png: un archivo cualquiera en el directorio
    de artefactos (el modo en que credentials.enc se serviría) sigue en 404."""
    c = client_factory()
    job_id = start(c)
    adir = c.app.state.store.artifacts_dir(job_id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "credentials.enc").write_bytes(b"secreto")
    (adir / "notas.txt").write_text("no", encoding="utf-8")

    assert c.get(f"/jobs/{job_id}/evidence/credentials.enc").status_code == 404
    assert c.get(f"/jobs/{job_id}/evidence/notas.txt").status_code == 404


# =============================================================================
# POST /extract: the dropzone stops lying
# =============================================================================
#
# The setup screen has always promised "Adjuntar una norma, un pliego de
# licitación o tu propia rúbrica -- PDF, Word o Excel... Una norma tuya pesa más
# que cualquier cosa que generemos", and the dropzone appended an INVENTED
# filename to a list. Nothing was ever read. This endpoint is the extraction
# that promise assumed, and the properties pinned here are the ones the module
# docstring names as load-bearing for the whole service: it stores nothing, it
# reads no path a client names, and it answers with three keys.


def test_extract_returns_the_text_of_an_uploaded_file(client_factory):
    c = client_factory()
    r = c.post("/extract", files={
        "file": ("norma.txt", "acompañamiento pedagógico".encode("utf-8"), "text/plain"),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "norma.txt"
    assert body["text"] == "acompañamiento pedagógico"
    assert body["chars"] == len("acompañamiento pedagógico")


def test_extract_answers_with_exactly_three_keys(client_factory):
    """Same discipline as `_public_state`: the response is a WHITELIST.

    Nothing about the upload's temp path, the store, or the request may ride
    out on a key somebody adds later."""
    c = client_factory()
    r = c.post("/extract", files={"file": ("norma.md", b"# titulo", "text/markdown")})
    assert set(r.json()) == {"name", "text", "chars"}


def test_extract_of_an_unsupported_extension_is_400_naming_it(client_factory):
    c = client_factory()
    r = c.post("/extract", files={
        "file": ("presentacion.pptx", b"lo que sea", "application/octet-stream"),
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert ".pptx" in detail
    assert ".pdf" in detail, "el error tiene que decir qué SÍ se acepta"


def test_extract_of_an_empty_file_is_400_in_spanish(client_factory):
    c = client_factory()
    r = c.post("/extract", files={"file": ("norma.txt", b"   \n ", "text/plain")})
    assert r.status_code == 400
    assert "no contiene texto extraíble" in r.json()["detail"]


def test_extract_of_a_real_docx_reads_its_paragraphs(client_factory, tmp_path):
    """The end-to-end shape: a real Word file, through HTTP, back as text."""
    import docx as docx_mod

    d = docx_mod.Document()
    d.add_paragraph("El proveedor debe publicar el modelo de lenguaje que utiliza.")
    blob = tmp_path / "politica.docx"
    d.save(blob)

    c = client_factory()
    r = c.post("/extract", files={"file": ("politica.docx", blob.read_bytes(), "")})
    assert r.status_code == 200, r.text
    assert "publicar el modelo de lenguaje" in r.json()["text"]


def test_extract_stores_nothing(client_factory, tmp_path):
    """No job, no directory, no file on disk. The bytes are read and dropped."""
    c = client_factory()
    before = sorted(p.name for p in (tmp_path / "runs").rglob("*"))
    r = c.post("/extract", files={"file": ("norma.txt", b"una norma", "text/plain")})
    assert r.status_code == 200
    assert c.app.state.store.list_ids() == []
    assert sorted(p.name for p in (tmp_path / "runs").rglob("*")) == before


def test_extract_reads_no_path_the_client_names(client_factory):
    """A traversal in the filename cannot reach the filesystem: only the
    EXTENSION and the basename are used, and the bytes come from the body."""
    c = client_factory()
    r = c.post("/extract", files={
        "file": ("../../../etc/passwd.txt", b"contenido del cuerpo", "text/plain"),
    })
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "contenido del cuerpo"
    assert "/" not in r.json()["name"] and "\\" not in r.json()["name"]


def test_an_oversized_upload_is_refused_in_spanish(client_factory):
    from lxp_eval.service.app import MAX_UPLOAD_BYTES

    c = client_factory()
    r = c.post("/extract", files={
        "file": ("gigante.txt", b"x" * (MAX_UPLOAD_BYTES + 1), "text/plain"),
    })
    assert r.status_code == 413
    assert "25 MB" in r.json()["detail"]


def test_extract_with_no_file_is_a_422_not_a_crash(client_factory):
    c = client_factory()
    assert c.post("/extract").status_code == 422


def test_extract_needs_a_token(tmp_path, env):
    """Mirrors the auth section: no header, no extraction."""
    c = _open_client(tmp_path, env)
    r = c.post("/extract", files={"file": ("norma.txt", b"x", "text/plain")})
    assert r.status_code == 401


# =============================================================================
# settings: the user brings their OWN Anthropic key
# =============================================================================
#
# `config.py` used to read ANTHROPIC_API_KEY and nothing else, so the only way
# to give evalkit a key was to edit `06_Application/.env`. That is fine for the
# author and impossible for anybody else, and the project is going open source.
# The key becomes something the user pastes into the app.
#
# It is a SECRET the moment they do -- it bills them -- so it gets the same
# discipline as the platform credentials above: encrypted at rest, and
# structurally incapable of coming back out of the API. A sentinel is how that
# is checked, rather than our own good intentions.

API_KEY = "sk-ant-api03-SENTINELA-de-clave-de-usuario-que-no-debe-salir-jamas"


def _fresh_install(monkeypatch):
    """A stranger who just cloned this: no key in the environment at all.

    `env` sets ANTHROPIC_API_KEY, and `config.py` calls
    `load_dotenv(override=True)` at import, so a developer's real `.env` key is
    in `os.environ` too. Neither is what a first run looks like, and without
    removing both there is nothing to assert.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _settings(client):
    r = client.get("/settings")
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_on_a_fresh_install_says_there_is_no_key(client_factory, monkeypatch):
    c = client_factory()
    _fresh_install(monkeypatch)
    assert _settings(c) == {"hasApiKey": False, "keyHint": None, "source": None}


def test_posting_a_key_stores_it_and_get_reflects_it(client_factory, monkeypatch):
    c = client_factory()
    _fresh_install(monkeypatch)
    r = c.post("/settings/api-key", json={"apiKey": API_KEY})
    assert r.status_code == 200, r.text
    assert r.json()["hasApiKey"] is True
    assert r.json()["source"] == "stored"
    assert _settings(c) == r.json()


def test_the_stored_key_survives_a_restart(client_factory, tmp_path, monkeypatch):
    """A brand-new process over the same state directory."""
    root = tmp_path / "runs-persistidas"
    c1 = client_factory(root=root)
    _fresh_install(monkeypatch)
    assert c1.post("/settings/api-key", json={"apiKey": API_KEY}).status_code == 200
    c2 = client_factory(root=root)
    assert _settings(c2)["hasApiKey"] is True


def test_no_settings_response_ever_carries_the_key(client_factory, monkeypatch):
    """The whole point. Every verb, every body, scanned for the sentinel."""
    c = client_factory()
    _fresh_install(monkeypatch)
    bodies = [
        c.post("/settings/api-key", json={"apiKey": API_KEY}).text,
        c.get("/settings").text,
        c.get("/healthz").text,
        c.delete("/settings/api-key").text,
    ]
    for body in bodies:
        assert API_KEY not in body
        for sentinel in SENTINELS:
            assert sentinel not in body


def test_the_hint_is_a_hint_and_not_the_key(client_factory, monkeypatch):
    """Enough to recognise WHICH key it is; never enough to use it."""
    c = client_factory()
    _fresh_install(monkeypatch)
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    hint = _settings(c)["keyHint"]
    assert hint.startswith("sk-ant-a")
    assert hint.endswith(API_KEY[-4:])
    assert len(hint) <= 16
    assert "SENTINELA" not in hint


def test_the_key_is_encrypted_where_it_lands_not_masked(
    client_factory, tmp_path, monkeypatch
):
    root = tmp_path / "runs-cifradas"
    c = client_factory(root=root)
    _fresh_install(monkeypatch)
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written, "algo tiene que haberse escrito"
    for path in written:
        assert b"SENTINELA" not in path.read_bytes(), path


def test_deleting_the_key_clears_it(client_factory, monkeypatch):
    c = client_factory()
    _fresh_install(monkeypatch)
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    r = c.delete("/settings/api-key")
    assert r.status_code == 200, r.text
    assert r.json() == {"hasApiKey": False, "keyHint": None, "source": None}
    assert _settings(c)["hasApiKey"] is False


def test_a_value_that_is_not_an_anthropic_key_is_400_in_spanish(
    client_factory, monkeypatch
):
    c = client_factory()
    _fresh_install(monkeypatch)
    r = c.post("/settings/api-key", json={"apiKey": "sk-proj-esto-es-de-otro-proveedor"})
    assert r.status_code == 400, r.text
    assert "sk-ant-" in r.json()["detail"]
    assert _settings(c)["hasApiKey"] is False


def test_an_empty_key_is_400_in_spanish(client_factory, monkeypatch):
    c = client_factory()
    _fresh_install(monkeypatch)
    r = c.post("/settings/api-key", json={"apiKey": "   "})
    assert r.status_code == 400, r.text
    assert "vacía" in r.json()["detail"]


def test_the_settings_endpoints_answer_with_exactly_three_keys(
    client_factory, monkeypatch
):
    """Same whitelist discipline as `_public_state` and `/extract`."""
    c = client_factory()
    _fresh_install(monkeypatch)
    expected = {"hasApiKey", "keyHint", "source"}
    assert set(_settings(c)) == expected
    assert set(c.post("/settings/api-key", json={"apiKey": API_KEY}).json()) == expected
    assert set(c.delete("/settings/api-key").json()) == expected


def test_the_environment_wins_and_the_ui_is_told_so(client_factory, monkeypatch):
    """`source` exists so the screen does not offer to change what is ignored."""
    c = client_factory()
    _fresh_install(monkeypatch)
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    assert _settings(c)["source"] == "stored"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exportada-por-el-operador")
    state = _settings(c)
    assert state["source"] == "env"
    assert state["hasApiKey"] is True
    assert state["keyHint"].endswith("ador")


def test_the_stored_key_is_what_the_engine_would_call_anthropic_with(
    client_factory, monkeypatch
):
    """The injection, end to end: paste it in the app, and CONFIG resolves it.

    What a fake CANNOT prove is that Anthropic accepts the key -- no call is
    made here. What IS pinned is the wiring that did not exist: the service
    registers its store with `config`, so `resolve_api_key` stops being "the
    environment or nothing".
    """
    from lxp_eval import config as config_mod

    monkeypatch.setattr(config_mod, "_stored_key_provider", None, raising=False)
    c = client_factory()
    _fresh_install(monkeypatch)
    assert config_mod.CONFIG.resolve_api_key() == ""
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    assert config_mod.CONFIG.resolve_api_key() == API_KEY


def test_the_settings_endpoints_all_require_a_token(tmp_path, env):
    """Mirrors the auth section: the key is not readable, writable or erasable."""
    c = _open_client(tmp_path, env)
    assert c.get("/settings").status_code == 401
    assert c.post("/settings/api-key", json={"apiKey": API_KEY}).status_code == 401
    assert c.delete("/settings/api-key").status_code == 401


def test_healthz_says_whether_there_is_an_api_key(client_factory, monkeypatch):
    """A fresh install must be able to ask "am I ready to run?" and be told.

    `ok: true` on its own is a lie a first-run UI would act on: the container
    is alive and cannot evaluate anything.
    """
    c = client_factory()
    _fresh_install(monkeypatch)
    assert c.get("/healthz").json()["apiKey"] is False
    c.post("/settings/api-key", json={"apiKey": API_KEY})
    assert c.get("/healthz").json()["apiKey"] is True


def test_healthz_still_carries_everything_it_used_to(client_factory):
    """The new boolean is added, not swapped in over an operator's dashboard."""
    body = client_factory().get("/healthz").json()
    assert {"ok", "jobs", "credentials", "auth"} <= set(body)


# --- el rastro completo, para el apéndice del informe -----------------------


def test_the_ledger_is_readable_as_a_chronological_trail(client_factory):
    """«Que todos los pasos se puedan inspeccionar al final, en el apéndice del
    PDF». El ledger ya guarda paso, hash, hora y URL por artefacto; lo que
    faltaba era poder leerlo. La evidencia por criterio del informe no alcanza:
    omite los pasos que no produjeron veredicto, y el apéndice existe para que
    un lector escéptico recorra el argumento ENTERO."""
    c = client_factory()
    job_id = start(c)

    ledger = c.app.state.store.ledger_path(job_id)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    art = c.app.state.store.artifacts_dir(job_id)
    art.mkdir(parents=True, exist_ok=True)
    (art / "paso1.png").write_bytes(b"\x89PNG")
    rows = [
        {"probe_id": "PB-1", "test_id": "T-1", "platform": "demo", "run_index": 0,
         "kind": "screenshot", "artifact": str(art / "paso1.png"),
         "sha256": "a" * 64, "timestamp": "2026-07-26T10:00:00+00:00",
         "note": "paso 1: abrir el curso", "url": "https://demo.test/curso"},
    ]
    ledger.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )

    r = c.get(f"/jobs/{job_id}/ledger")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"][0]["note"] == "paso 1: abrir el curso"
    assert body["rows"][0]["sha256"] == "a" * 64
    assert body["rows"][0]["artifact"] == "paso1.png", \
        "el apéndice necesita el nombre para pedir la imagen, NO la ruta del disco"


def test_the_ledger_never_leaks_a_disk_path(client_factory):
    """Una ruta absoluta en la respuesta cuenta cómo está armada la máquina y
    no le sirve de nada al lector del informe."""
    c = client_factory()
    job_id = start(c)
    ledger = c.app.state.store.ledger_path(job_id)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({
        "probe_id": "PB-1", "test_id": "T-1", "platform": "demo", "run_index": 0,
        "kind": "page_text", "artifact": "C:/Users/secreto/runs/x.txt",
        "sha256": "b" * 64, "timestamp": "2026-07-26T10:00:00+00:00",
        "note": "paso 2", "url": "https://demo.test",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    r = c.get(f"/jobs/{job_id}/ledger")
    assert r.status_code == 200, "sin esto el test pasaria por un 404"
    raw = r.text
    assert "C:/Users/secreto" not in raw and "secreto" not in raw
    assert r.json()["rows"][0]["artifact"] == "x.txt"


def test_the_ledger_needs_a_token(tmp_path, env):
    c = _open_client(tmp_path, env)
    assert c.get("/jobs/whatever/ledger").status_code == 401


# --- los pasos que se aprobaron vs los que corrieron -------------------------


def test_matching_steps_read_as_matching():
    probes = [{"probeId": "PB-1", "steps": [{"n": 1, "action": "abrir el curso"}]}]
    line = serialize.steps_divergence_line(probes, {"PB-1": ["abrir el curso"]})
    assert "con los pasos aprobados" in line
    assert "REESCRIT" not in line.upper()


def test_a_rewritten_probe_is_named_with_both_counts():
    """The reader has to see that the run did not use the approved wording.

    On job d19ddd2bfdfc4a90 five of eight probes were rewritten by hand against
    the real UI. The report showed nothing, so the plan a ministry could read
    described steps that never ran.
    """
    probes = [
        {"probeId": "PB-PT-D8-01", "steps": [{"n": i, "action": f"paso {i}"} for i in range(17)]},
        {"probeId": "PB-PT-D9-H2", "steps": [{"n": 1, "action": "abrir el tutor"}]},
    ]
    executed = {
        "PB-PT-D8-01": [f"otro paso {i}" for i in range(27)],
        "PB-PT-D9-H2": ["abrir el tutor"],
    }

    line = serialize.steps_divergence_line(probes, executed)

    assert "PASOS REESCRITOS en 1 de 2" in line
    assert "PB-PT-D8-01 (17 aprobados, 27 ejecutados)" in line
    assert "PB-PT-D9-H2" not in line, "una prueba que coincide no se denuncia"


def test_a_probe_that_never_ran_is_not_a_rewritten_probe():
    """The three gated probes have no recording. That is a gate working, not drift."""
    probes = [{"probeId": "PB-PT-D9-S1", "steps": [{"n": 1, "action": "un paso"}]}]
    line = serialize.steps_divergence_line(probes, {})
    assert line == ""


def test_not_knowing_is_not_the_same_as_no_divergence():
    """A job from before executed_steps existed must not claim it matched.

    Absence of the field means WE DID NOT RECORD, and printing the reassuring
    sentence there would be an assertion nobody checked.
    """
    probes = [{"probeId": "PB-1", "steps": [{"n": 1, "action": "abrir el curso"}]}]
    assert serialize.steps_divergence_line(probes, {}) == ""
    assert serialize.steps_divergence_line(probes, None) == ""
