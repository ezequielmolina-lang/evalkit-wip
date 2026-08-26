# LXP Evaluation Workflow — Agentic Application

## Empezar (un solo comando)

```bash
pip install -r requirements.txt -r requirements-service.txt
python evalkit_app.py
```

Levanta el motor y la interfaz, y abre el navegador. La primera vez instala
las dependencias de la web sola.

**Tu propia API key**: se pega en la app, en su pantalla de configuración —
queda cifrada en tu máquina, no en un archivo del repo. Si preferís una variable
de entorno (uso headless o CI), `ANTHROPIC_API_KEY` tiene prioridad.

Todo escucha en `127.0.0.1`: el servicio maneja credenciales de plataformas
ajenas y puede gastar tu API key, así que no se expone a la red por default.



**Status:** v1.0 alpha · 3 of 8 agents fully implemented, 5 stubbed · CLI + HTML dashboard

Python application that operationalizes the 13-domain framework defined in
`00_Master_Documents/LXP_Evaluation_Framework_EN_v1.docx`. Uses the Anthropic API
(Claude Opus 4.x by default) to apply an 8-agent workflow to the candidate LXP
platforms (PLATAFORMA_B, Plataforma A, Plataforma C). Outputs the populated rubric XLSX, a Markdown
narrative report, and an HTML comparison dashboard.

---

## Quick start

```bash
# 1. Install dependencies
cd 06_Application
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt

# 2. Set API key
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY

# 3. Verify install (no API calls)
python -m lxp_eval.smoke_test

# 4. Run a single agent against PLATAFORMA_B vendor docs (uses ~$0.50-$2 in API credits)
python examples/run_eligibility.py --platform plataforma_b

# 5. Run Evidence Audit Agent (uses ~$2-5)
python examples/run_evidence_audit.py --platform plataforma_b

# 6. Run full pipeline against all three platforms (uses ~$15-45)
python examples/run_full_evaluation.py --platforms plataforma_b plataforma_a plataforma_c

# 7. Open the dashboard
open dashboard/comparison.html
```

---

## What is built

The application implements 3 of the 8 agents fully and stubs 5. The 3 fully
implemented agents are the ones that demonstrate the framework's distinctive
contributions:

| Agent | Domains | Status | Why this one |
|---|---|---|---|
| **B. Eligibility Gate** | Pre-filter + 8 disqualifiers | ✅ Full | The foundational gating step. Binary, easy to verify. |
| **C. Pedagogy** | D1–D5 (50 pts) | ✅ Full | Carries half the rubric weight. Cognitive-science scoring. |
| **H. Evidence Audit** | D12 (6 pts) + Pre-filter QA7 | ✅ Full | Most distinctive: verbatim ESSA verification + 5-pattern Potemkin detection. |
| A. Setup | Platform onboarding | 📝 Stub | Mostly orchestration, no Claude call needed yet. |
| D. Content | D6 + D8 (13 pts) | 📝 Stub | Pattern identical to Agent C; follow that template. |
| E. Teacher Integration | D7 (10 pts) | 📝 Stub | Pattern identical to Agent C. |
| F. Personalization + Auditability | D9 + D10 (9 pts) | 📝 Stub | Pattern identical to Agent C. |
| G. Implementation Fidelity + Training | D11 + D13 (12 pts) | 📝 Stub | Requires classroom observation transcripts; human-in-the-loop. |

The stub agents have working prompts and follow the same `BaseAgent` interface.
To fully implement one, copy `agents/pedagogy.py`, swap in the appropriate
domain definitions from `rubric.py`, and adjust the system prompt.

---

## Architecture

```
                ┌─────────────────────────────────────┐
                │   ORCHESTRATOR (orchestrator.py)    │
                │                                     │
                │   1. Load platform config          │
                │   2. Load vendor docs (PDFs)        │
                │   3. Run agents in sequence/parallel│
                │   4. Synthesize results             │
                │   5. Write XLSX + MD + HTML         │
                └──────────────┬──────────────────────┘
                               │
        ┌──────────────────────┴───────────────────────┐
        ▼                                              ▼
┌──────────────────┐                          ┌──────────────────┐
│   Agents (8)     │                          │   Tools          │
│                  │                          │                  │
│ Each inherits    │                          │ - PDF reader     │
│ BaseAgent.       │                          │ - XLSX writer    │
│ Uses Anthropic   │                          │ - MD report      │
│ Messages API     │                          │ - HTML dashboard │
│ with tool use    │                          │                  │
│ for structured   │                          │                  │
│ scoring output.  │                          │                  │
└──────────────────┘                          └──────────────────┘
```

### Agent flow

1. Agent receives platform config + vendor documents + relevant rubric tests.
2. Agent calls Claude with a domain-specific system prompt and the documents
   as context.
3. Claude uses the `score_test` tool to emit a JSON score for each test
   (status, points, evidence, notes).
4. Agent aggregates scores and returns a structured `DomainResult`.
5. Orchestrator collects results, writes to XLSX, generates report.

### Why direct Anthropic API rather than Claude Agent SDK

The Anthropic Python SDK with tool use is sufficient for our needs and avoids
the overhead of a full agent harness. Each "agent" is a Python class that
makes one or more API calls with structured tool-use output. This pattern is
portable, testable, and cost-predictable.

---

## Configuration

`.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
LXP_MODEL_HEAVY=claude-opus-4-7-20260101    # Or current Opus 4.x identifier
LXP_MODEL_LIGHT=claude-opus-4-7-20260101    # Override to Sonnet to cut cost
LXP_DRY_RUN=false                            # true = print prompts, don't call API
LXP_MAX_PARALLEL_AGENTS=4                    # for orchestrator parallelism
```

`platforms/<name>/config.yaml` (per platform):
```yaml
name: PLATAFORMA_B
vendor: I Hate Flying Bugs
url: https://plataforma-b.ejemplo
contact: evaluador@ejemplo.org
demo_date: 2026-05-15
schools_sv: 600
status: production
vendor_docs:
  - vendor_docs/plataforma_b_privacy_policy.pdf
  - vendor_docs/plataforma_b_theory_of_change.pdf
  - vendor_docs/plataforma_b_training_manual.pdf
observation_notes:
  - observation_notes/PLATAFORMA_B_classroom_obs_2026-05-15.md
```

---

## Cost estimates

Per single platform full evaluation, using Claude Opus 4.x for all agents:

| Component | Tokens (approx) | Cost @ Opus rates |
|---|---|---|
| Agent B (Eligibility) | 20K in / 5K out | $0.65 |
| Agent C (Pedagogy D1-D5) | 100K in / 20K out | $3.00 |
| Agent D (Content D6+D8) | 50K in / 10K out | $1.50 |
| Agent E (Teacher D7) | 40K in / 8K out | $1.20 |
| Agent F (Personalization D9+D10) | 30K in / 6K out | $0.90 |
| Agent G (Implementation D11+D13) | 60K in / 12K out | $1.80 |
| Agent H (Evidence Audit D12) | 80K in / 15K out | $2.40 |
| Synthesizer | 30K in / 10K out | $1.20 |
| **Total per platform** | **410K in / 86K out** | **~$13** |
| **Three platforms (PLATAFORMA_B, Plataforma A, Plataforma C)** | | **~$40** |

Costs scale roughly linearly with vendor documentation length. Mixed Opus+Sonnet
configuration drops cost by ~3-4x. Dry-run mode (LXP_DRY_RUN=true) prints
prompts without calling the API for $0.

---

## Outputs

After a full run, `outputs/<run-id>/` contains:

- `Logging_Rubrica_LXP_ElPais_<run-id>.xlsx` — the v7.1 rubric XLSX with
  scores filled in for each evaluated platform
- `Report_<platform>.md` — per-platform narrative report
- `Comparison_Report.md` — cross-platform comparison
- `Pending_Questions_<platform>.md` — list of tests marked PENDING and why
- `Potemkin_Claims_<platform>.md` — vendor claims flagged for misrepresentation
- `agent_logs/` — full transcripts of each agent's reasoning
- `comparison.html` — interactive HTML dashboard

---

## Implementation status of each agent

### ✅ Agent B — Eligibility Gate (FULL)

Implements the 7-question UNICEF + ESSA pre-filter and 8 binary disqualifiers.
Reads vendor documentation (privacy policy, ToS, theory of change document) and
makes binary determinations. Outputs:
- `QA1-QA7` status: YES / NO / PENDING
- `DQ1-DQ8` status: PASS / FAIL / PENDING
- For each: 1-3 sentence justification + page reference

### ✅ Agent C — Pedagogy (FULL, D1-D5, 50 pts)

The largest-weight agent. Implements:
- D1 Explicit Instruction (12 pts; 7 tests)
- D2 Cognitive Load (8 pts; 6 tests)
- D3 Retrieval/Spacing/Interleaving (12 pts; 7 tests)
- D4 Feedback + Mastery + BEA 2025 (10 pts; 8 tests)
- D5 Misconceptions + BEA-ML (8 pts; 6 tests)

Requires direct platform access (transcripts of student interactions, screen
recordings, or live testing). When access is unavailable, marks tests as
PENDING with specific data request to the vendor.

### ✅ Agent H — Evidence Audit (FULL, D12, 6 pts)

Most distinctive agent. Reads any vendor-supplied evidence papers (PDFs),
classifies each according to verbatim ESSA Tier criteria, and runs the
5-pattern Potemkin detection check. Outputs:
- Tier classification per paper (1/2/3/4 or N/A)
- Verification details: n, # sites, overlap setting/samples, study design
- Potemkin flags with pattern ID (P1-P5) and justification
- Total D12 score (out of 6)

### 📝 Stubs (D, E, F, G, Setup, Synthesizer)

The stub agents have:
- A `system_prompt` constant (drafted from the framework paper)
- A `domains` constant (referencing `rubric.py`)
- An `evaluate()` method that raises `NotImplementedError("Stub — follow agents/pedagogy.py pattern")`

To complete a stub: copy `agents/pedagogy.py`, replace the domain references,
update the system prompt, and run.

---

## Limitations

1. **Most agents require classroom data the framework explicitly states must
   come from human-in-the-loop observation** (focus groups, classroom
   recordings, teacher interviews). The application can process such data
   once collected but cannot collect it.

2. **The agentic evaluation is a draft.** Per Section 9 of the framework
   paper, scoring should be validated by a human reviewer — particularly
   D12 Evidence Audit, where statistical methodology judgments are made.

3. **Model versions evolve.** Set `LXP_MODEL_HEAVY` to the current best
   Anthropic model identifier; the default placeholder may be outdated.

4. **API errors are not retried aggressively.** Rate limits trigger fallback
   to PENDING for affected tests rather than retry-with-backoff. Re-run on
   failure.

5. **No automated platform login.** The application reads vendor documentation
   and (when supplied) screenshots/transcripts of platform interactions; it
   does not automate browser interaction with the platforms.

---

## Cross-references to other project artifacts

| Artifact | Path |
|---|---|
| Framework paper (English) | `../00_Master_Documents/LXP_Evaluation_Framework_EN_v1.docx` |
| Rubric (Spanish, v7.1) | `../00_Master_Documents/Rubrica_LXP_ElPais.docx` |
| Logging XLSX (v7.1) | `../00_Master_Documents/Logging_Rubrica_LXP_ElPais.xlsx` |
| Agent workflow design | `../00_Master_Documents/Agent_Workflow_v7_0.docx` |
| External rubrics library | `../04_Reference_Frameworks/External_Rubrics/` |

---

## Authors and correspondence

la organización Education Global Practice LAC team. Internal alpha v1.0 — May 2026.
Bug reports and PR welcome.
