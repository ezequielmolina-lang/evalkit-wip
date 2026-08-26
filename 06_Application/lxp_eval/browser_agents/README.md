# `lxp_eval.browser_agents` — Playwright + Claude browser PoC

**Status:** Proof of concept (v0.1.0-poc, 2026-05-22)
**Scope:** Implements Agent BT-2 (Pedagogy Probe) end-to-end for one
(platform, subject, grade, lesson) cell. The remaining 11 browser agents
(BT-1, BT-3 through BT-7) reuse the same harness and add their own probe
prompts.

---

## What this PoC does

Per invocation, it runs a single browser-test cell:

1. **Launches** a headful Chromium browser (`headless=False`, looks like a real
   user, avoids common bot-detection heuristics).
2. **Logs in** to either PLATAFORMA_B (`https://app.plataforma-b.ejemplo/`) or Plataforma A
   (`https://app.plataforma-a.ejemplo/login/`) using credentials from `.env`.
3. **Navigates** to the target subject + grade class and opens the requested
   lesson, using deliberately resilient text + role + ARIA selectors.
4. **Hands the keyboard to Claude** via a tool-use loop with 8 tools:
   `navigate`, `click`, `type_text`, `screenshot`, `read_page`, `wait`,
   `record_observation`, `done`. Claude reads the page (accessibility tree),
   decides what to click, classifies feedback against the BEA 2025 rubric,
   optionally probes the AI tutor.
5. **Writes** a Markdown observation note + screenshots to
   `platforms/<platform>/observation_notes/BT2_<cell>_<timestamp>/`. The note
   format mirrors `REAL_obs_2026-05-22_b_nacional_student.md` so the existing
   SYNTH-1 rubric populator can consume it.

What makes this non-trivial is that **selection and classification are done by
Claude, not hardcoded**. We don't know which CSS selector points to "the
wrong-answer button" on a SPA we don't control — Claude reads the visible page
text and picks the right label.

---

## Install

From `06_Application/`:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

The second command downloads a ~130 MB Chromium build into Playwright's cache.
You only run it once per machine.

---

## Credentials

Copy `.env.example` to `.env` and fill in the credentials. The browser PoC
reads these variables:

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude tool-use loop |
| `PLATAFORMA_B_STUDENT_EMAIL` / `PLATAFORMA_B_STUDENT_PASSWORD` | BT-2 (student role) on PLATAFORMA_B |
| `PLATAFORMA_B_TEACHER_EMAIL` / `PLATAFORMA_B_TEACHER_PASSWORD` | BT-4 / BT-6 (teacher role) on PLATAFORMA_B |
| `PLATAFORMA_A_STUDENT_EMAIL` / `PLATAFORMA_A_STUDENT_PASSWORD` | Optional student account on Plataforma A |
| `PLATAFORMA_A_TEACHER_EMAIL` / `PLATAFORMA_A_TEACHER_PASSWORD` | Default for Plataforma A (toggle role inside the app) |

**Never commit a populated `.env`.** The file is in `.gitignore`.

Use the dedicated WB test accounts from `platforms/AGENTS_SPEC.md` section 5
(one per tester) — not personal logins, and not the demo accounts you used
during the manual sessions.

---

## Run

All commands are executed from the `06_Application/` directory.

```bash
# 1) Dry-run (no API calls, no browser interaction beyond setup)
LXP_DRY_RUN=true python -m lxp_eval.browser_agents.run_browser_cell \
    --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2

# 2) Real run, observation only (no answer submission, no tutor)
python -m lxp_eval.browser_agents.run_browser_cell \
    --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2

# 3) Full BT-2: open lesson + submit one wrong answer + probe AI tutor (T-1)
python -m lxp_eval.browser_agents.run_browser_cell \
    --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2 \
    --allow-submit --open-tutor

# 4) Headless (no visible window) — useful for overnight batches
python -m lxp_eval.browser_agents.run_browser_cell \
    --platform plataforma_a --subject math --grade 3 --lesson 1.1 --agent BT-2 \
    --headless --allow-submit --open-tutor --cost-cap 5
```

### CLI flags

| Flag | Default | Meaning |
|---|---|---|
| `--platform` | (required) | `plataforma_b` or `plataforma_a` |
| `--subject` | (required) | `math` or `language` |
| `--grade` | (required) | 1-9 |
| `--lesson` | (required) | Lesson code (CNEB `1.4` for PLATAFORMA_B, OVA code for Plataforma A) |
| `--agent` | `BT-2` | Only BT-2 implemented in this PoC |
| `--allow-submit` | off | Permit ONE deliberate wrong answer submission |
| `--open-tutor` | off | After lesson observation, open the AI tutor and run prompt T-1 |
| `--headless` | off | Run Chromium headless (no visible window) |
| `--cost-cap` | `5.0` | Abort if estimated API spend exceeds this USD value |
| `--max-iterations` | `25` | Hard cap on Claude turns per cell |
| `--output-root` | `./platforms/` | Override the platforms tree root for output |
| `-v` / `--verbose` | off | DEBUG-level logging |

---

## Output layout

A single cell run produces:

```
platforms/plataforma_b/observation_notes/BT2_plataforma_b_math_g3_L1.4_20260522T211530/
├── OBSERVATION.md              # Human-readable note with telemetry + per-test scoring table
├── screenshots/
│   ├── 20260522T211532_plataforma_b_landing.png
│   ├── 20260522T211535_plataforma_b_post_login.png
│   ├── 20260522T211541_plataforma_b_class_math_3_regular.png
│   ├── 20260522T211548_plataforma_b_lesson_1.4.png
│   ├── 20260522T211603_lesson_architecture.png
│   ├── 20260522T211622_wrong_answer_feedback.png
│   └── ...
└── transcripts/
    └── transcript_1716412987.json   # Full Claude conversation for audit
```

If the setup steps (login / open class / open lesson) fail, the run writes
`BLOCKED.md` instead of `OBSERVATION.md` with the failure reason and any
screenshots captured before the failure — so a human can quickly see what the
selector mismatch was and update `plataforma_b_recipes.py` or `plataforma_a_recipes.py`.

---

## Architecture

```
browser_agents/
├── playwright_driver.py        # ~280 lines  Async Playwright wrapper
├── plataforma_b_recipes.py              # ~180 lines  PLATAFORMA_B login / class / lesson navigation
├── plataforma_a_recipes.py              # ~155 lines  Plataforma A login / class / lesson navigation
├── claude_browser_agent.py     # ~320 lines  Claude tool-use loop + token/cost accounting
├── pedagogy_probe.py           # ~265 lines  BT-2 orchestrator + observation-note writer
├── run_browser_cell.py         # ~110 lines  CLI entrypoint
└── README.md                   # This file
```

Each layer has a single responsibility:

- `playwright_driver.PlaywrightDriver` knows how to drive Chromium and exposes
  click / type / screenshot / read_page primitives. It does not know about
  PLATAFORMA_B or Plataforma A.
- `*_recipes.py` knows about one specific vendor's URL structure and form
  selectors. Each platform has its own file so changes to PLATAFORMA_B's DOM don't
  affect Plataforma A and vice versa.
- `claude_browser_agent.ClaudeBrowserAgent` runs the Anthropic Messages API
  tool-use loop. It does not know about pedagogy — it just exposes browser
  tools and tracks tokens / cost.
- `pedagogy_probe.run_pedagogy_probe` chains the recipes (deterministic setup)
  with the Claude loop (judgement-driven exploration) and writes the
  observation note. Other agents (BT-3 Content, BT-5 Personalization, …)
  would each get their own module here that reuses driver + claude agent.

---

## Cost & throughput

Per-cell estimates (Opus 4.x rates, $15/M input + $75/M output, default cap $5):

| Mode | Avg turns | ≈ Input tokens | ≈ Output tokens | ≈ Cost |
|---|---|---|---|---|
| Observation only (no submit, no tutor) | 10-14 | 25k | 4k | $0.65 |
| Full BT-2 (submit + tutor T-1) | 18-25 | 50k | 8k | $1.35 |

Cap is set at $5 per cell to give 3-4x headroom for retries / re-reads on
unfamiliar pages. **Overnight batch of 200 cells = roughly $135-270** of
Anthropic spend.

Wall-clock: with `--headless`, each cell takes ~2-4 minutes. 200 cells ≈
6.5-13 hours run serially. A future scale-up should run 3-5 in parallel from
different browser contexts (each in its own subprocess) and stay under
8 hours overnight.

---

## What's tested vs what needs human verification

| Component | Tested | Needs human verify |
|---|---|---|
| `playwright_driver.py` primitives | Python syntax compiles | Live launch against a real page |
| Login form selectors (`plataforma_b_recipes.login`) | — | Must run against `app.plataforma-b.ejemplo` once; if any selector fails, update the fallback stack |
| Class card discovery | — | Depends on exact card text — verify with the first run |
| Lesson code text match | — | CNEB code format observed in session 1 but may vary by grade |
| Claude tool-use loop | Python syntax compiles | Needs a real cell to exercise; behaviour will vary with model version |
| Cost-cap math | Unit-trivial | Verify the actual invoice line matches our `_cost` estimate |
| BLOCKED note generation | — | Triggered when setup fails — first failed run will validate |

**Before any unattended overnight batch**, run one cell with `-v` and a human
watching the browser. Confirm: login works; class card is found; lesson opens;
Claude reads the page sensibly; cost stays under $1 per observation-only run.

---

## Known limitations

1. **Only BT-2 implemented.** Other browser agents (BT-1, BT-3 through BT-7)
   need their own prompt + minor orchestration modules. The harness layers
   (driver + claude agent) are reusable.
2. **Login selectors are best-effort.** We tried multiple strategies (label,
   placeholder, type=email, name=email), but if the vendor changes their
   login form the first run will fail with `LOGIN_FAILED` and the user has to
   inspect the DOM and update the recipe. The screenshots make this fast.
3. **Class / lesson selectors are text-based.** If PLATAFORMA_B renames a class card
   or Plataforma A reorganizes its OVA structure, navigation will fail. Again, the
   BLOCKED note will tell the user exactly which step failed.
4. **No parallelism.** This PoC runs one cell at a time. The next step is a
   multi-process runner that fans out 3-5 cells in parallel from separate
   browser contexts (with separate accounts).
5. **No retry-with-different-account.** If a login fails, we don't fall back
   to a second account.
6. **Cost cap is estimated, not enforced by Anthropic.** Our `_cost()` uses
   published Opus 4.x rates. If you change models, override `LXP_PRICE_INPUT`
   and `LXP_PRICE_OUTPUT` in `.env`.
7. **Read-only by default.** The `--allow-submit` flag gates wrong-answer
   submission, but there is no rate-limit on how many wrong answers Claude
   submits per cell beyond "one practice question" in the prompt. Watch the
   browser for the first few real runs to confirm Claude self-limits.
8. **No bot-detection bypass.** We disable the Blink automation flag and use
   a real-user viewport, but if either vendor adds CAPTCHA at login the PoC
   will fail and require a manual login + saved session state. (Not seen
   yet on either platform.)
9. **Cell tracker is NOT updated automatically.** The PoC writes the
   observation note but does not edit `platforms/CELL_TRACKER.md`. The
   future overnight runner should do this.

---

## Next steps to scale to a full overnight runner

1. **Multi-cell scheduler.** A driver that reads `platforms/CELL_TRACKER.md`,
   picks N `NOT_STARTED` cells assigned to BT-2, fans them out to 3-5
   subprocess workers (each with a distinct test account from the matrix),
   collects results, updates the tracker, and writes a batch summary.
2. **Saved session state.** After the first successful login per account,
   serialize `browser_context.storage_state()` and reuse it for subsequent
   cells in the same batch — avoids logging in 200 times and reduces
   anti-bot signal.
3. **Other browser agents.** BT-1 (eligibility), BT-3 (content), BT-5
   (personalization), BT-7 (coverage) each get their own probe module here
   that reuses the same driver + Claude agent.
4. **Auto-recovery.** When a cell hits BLOCKED, retry once with a different
   click strategy (e.g. fall back from text to role or vice versa) before
   giving up.
5. **Live cost dashboard.** Aggregate tokens / cost across the batch and
   emit to `outputs/batch_<timestamp>_dashboard.html` so a human can monitor
   the overnight run from any browser.
6. **Synth-1 integration.** When a batch finishes, kick off SYNTH-1 to read
   the new observation notes and populate the rubric XLSX.
