# evalkit job service

The HTTP layer the Next.js app drives. The engine (`lxp_eval.evalkit`) does the
evaluating; this package only turns it into jobs the web can start and poll.

**Why jobs and not requests.** Building a framework, drafting probes past a
five-judge council and then driving a live browser takes *minutes*. A serverless
request times out, and Playwright needs a real Chromium on disk. So the UI runs
on Vercel and this runs in a container (Fly.io / Railway): the web POSTs to
start an operation and polls `GET /jobs/{id}` for structured progress.

## Run it locally

```bash
cd 06_Application
python -m pip install -r requirements.txt -r requirements-service.txt
python -m playwright install chromium          # once, for real runs only

# Two secrets, once each. Store them in your secret manager; not in the repo.
python -c "from lxp_eval.service.crypto import generate_key; print(generate_key())"
python -c "import secrets; print(secrets.token_urlsafe(32))"

export EVALKIT_SECRET_KEY="<the Fernet key you just printed>"
export EVALKIT_SERVICE_TOKEN="<the token you just printed>"
export ANTHROPIC_API_KEY="<your key>"

# Before the first LIVE run on a fresh machine, check the environment:
python -m lxp_eval.service.selfcheck

python -m uvicorn lxp_eval.service.app:create_app --factory --port 8811 --reload
```

On PowerShell, use `$env:EVALKIT_SECRET_KEY = "..."` instead of `export`.

Then point the web app at it — `ENGINE_URL` defaults to `http://127.0.0.1:8811`,
which is what this command serves (8811 and not the conventional 8080: a
llama.cpp server commonly occupies 8080). **`EVALKIT_SERVICE_TOKEN` must be the same
value in the web app's environment**; the web app sends it on every call.

Check it:

```bash
curl http://127.0.0.1:8811/healthz
# {"ok":true,"jobs":0,"credentials":true,"auth":true}
```

`"credentials": false` means `EVALKIT_SECRET_KEY` is unset. The service still
starts and can build frameworks and probes; it will refuse a job that carries a
username and password (`503`) rather than store them in plaintext.

`EVALKIT_SERVICE_TOKEN` is different: without it the service **does not start
at all**.

## Environment

| variable | required | what happens without it |
|---|---|---|
| `EVALKIT_SERVICE_TOKEN` | **yes** | the service refuses to start |
| `ANTHROPIC_API_KEY` | **yes** | every job fails in `framework` |
| `EVALKIT_SECRET_KEY` | for credentialed runs | starts; a job with a username/password gets `503` |

**Where each one is read from is not the same, on purpose:**

| variable | shell | `.env` |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | ✅ (fills gaps; an export wins) |
| `EVALKIT_SERVICE_TOKEN` | ✅ | ❌ **not read** |
| `EVALKIT_SECRET_KEY` | ✅ | ❌ **not read** |

`ANTHROPIC_API_KEY` is *engine* config — `.env` is where this repo keeps it and
the service must see what the engine sees. Reading it from a bare `os.environ`
was a real bug: `selfcheck` reported `[FALLA]` for a key the service had.

The other two are *deployment* secrets and are read from the environment only.
The failure modes are asymmetric: a stale token in an image is loud (everything
401s), but a stale **Fernet key** is quiet — the service starts, encrypts new
credentials happily, and fails only when it tries to *decrypt* credentials
written under the real key, surfacing as a confusing login failure on one job
rather than a config error at boot. Since an environment variable always wins,
a `.env` value could only ever take effect when the platform's secret injection
*failed* — precisely when the service should refuse to start. It is also the
only way `create_app`'s fail-closed guarantee stays testable: a file that can
quietly supply the token makes "unset" unprovable.

So running locally needs real exports; putting these two in `.env` is not
enough. `selfcheck` warns if it finds them there.

## `python -m lxp_eval.service.selfcheck`

The one code path that has never executed is the driver factory: a real
Chromium logging a real test account into a vendor's platform. It cannot be
tested here — no credentials, and driving a vendor's platform to satisfy a test
suite is not something to do casually. So instead the first live failure is
made cheap to diagnose.

`selfcheck` verifies, in the order a fresh machine fails them:

* `EVALKIT_SECRET_KEY` — set, and a valid Fernet key;
* `EVALKIT_SERVICE_TOKEN` — set, and long enough to mean anything;
* `ANTHROPIC_API_KEY` — **present**, never validated;
* the job directory — exists and is actually writable;
* the criteria bank — parses;
* Chromium — installed, and launches;
* `.env` hygiene — **warns** if a deployment secret is sitting in `.env`, where
  this service does not read it.

It prints one line per item and exits non-zero if any failed, so it can gate a
deploy. It deliberately does **not** call the Anthropic API (a preflight that
costs money and can fail for reasons unrelated to this machine is not a
preflight) and does **not** open a vendor URL — Chromium is launched against
nothing at all.

```
[OK   ] token de servicio (EVALKIT_SERVICE_TOKEN)
        presente en el entorno (43 caracteres)
[AVISO] higiene de .env
        EVALKIT_SECRET_KEY está en .env, donde este servicio NO lo lee.
        -> sacalo de .env y exportalo en el entorno
[OK   ] banco de criterios
        lxp_sv cargado, 94 criterios
[FALLA] Chromium (Playwright)
        Playwright está instalado pero Chromium no arrancó: Executable
        doesn't exist at .../chrome-headless-shell.exe
        -> python -m playwright install chromium

1 verificación(es) fallaron. El servicio NO está listo para una corrida en vivo.
```

`[AVISO]` does not fail the gate — the service works; the configuration is just
ambiguous. Only `[FALLA]` sets a non-zero exit.

An all-green run does not mean the live run will work: whether the platform
accepts the test account's credentials is only knowable on the first real run,
which a human has to approve. The output says so.

## Tests

```bash
cd 06_Application
python -m pytest tests/test_service.py -q     # 116
python -m pytest tests/ -q                    # 588
```

No network and no browser: the Anthropic client and the browser driver are
injected through `Engine`, and the tests pass fakes. See the module docstring in
`tests/test_service.py` for what that provably cannot verify.

## The endpoints

| | |
|---|---|
| `POST /jobs` | start one → `{job_id}` |
| `GET /jobs/{id}` | stage, structured progress, and whatever artefacts exist |
| `POST /jobs/{id}/approve` | the reviewed plan → proceeds to the run |
| `POST /jobs/{id}/signoff` | ONE gated probe, explicitly |
| `GET /jobs/{id}/report` | the diagnostic |
| `GET /jobs/{id}/evidence/{artifact}` | a captured screenshot / page text |
| `GET /healthz` | the **only** endpoint without auth |

## Auth

Every endpoint but `/healthz` requires:

```
Authorization: Bearer <EVALKIT_SERVICE_TOKEN>
```

One shared token, not a user system: this service has exactly one client and
that client is our own web app. What actually matters is that a stranger who
reaches the container cannot read every job or start paid runs against a
vendor with our test account.

* the comparison is `hmac.compare_digest`, **never `==`** — a comparison that
  returns early on the first differing byte leaks the token's prefix, and the
  token is the whole security boundary;
* **fail closed**: no `EVALKIT_SERVICE_TOKEN` and `create_app` raises. A
  missing secret is a misconfiguration, and the safe reading of one is *no* —
  the alternative is a deploy that ships an open service and looks healthy. A
  token under 16 characters is refused too, so `changeme` is caught at boot
  rather than at breach;
* it is enforced in **middleware**, not per-route. A dependency protects the
  routes somebody remembered to decorate; the failure mode of forgetting one is
  an endpoint that is quietly public;
* the token is never logged, never echoed, and the `401` body is identical for
  no header, wrong scheme and wrong token — which of the three it was is
  information the caller has not earned;
* `/healthz` stays open (a liveness probe that needs a secret fails during a
  rotation) and reports **whether** auth is configured, never the value.

## The watchdog — a job cannot hang forever

A job that fails is fine: somebody reads the message and acts. A job that
*hangs* is worse, because nobody learns anything and the user waits on a run
that will never finish. `background=True` hands each stage to a task, and a
bare `asyncio.create_task` drops the exception, holds only a weak reference,
and has no notion of "quiet for too long".

`watchdog.py` closes all of it:

* **heartbeat.** Every transition and every progress tick writes
  `last_progress_at` to `job.json`. A stage quiet for longer than its own
  budget is stalled;
* **per-stage budgets, and the budget matches the tick.** One global timeout
  either kills a healthy 40-probe browser run or never fires:

  | stage | budget | ticks |
  |---|---|---|
  | `framework` | 15 min | once |
  | `probes` | 30 min | once |
  | `run` | 5 min + `repeats` × 5 min | **per probe** |
  | `score` | 5 min + `repeatJudging` × 3 min | **per criterion** |
  | `awaiting_approval` | **none — it cannot stall** | — |

  These are *heartbeat* budgets, not stage totals. `run` and `score` write
  progress once per item, so their budget is **one item's** allowance — the
  probe count does not stretch it. (It used to, and that was wrong: a 40-probe
  run would sit stalled for over three hours before the watchdog fired.) What
  still multiplies is the work inside a single tick: the runner walks one probe
  `repeats` times before returning, and the scorer re-judges one criterion
  `repeatJudging` times.

  Healthy work is unaffected — each item renews the budget, so a long run is
  bounded per probe and never in aggregate. `awaiting_approval` has no budget
  on purpose: a human reading a plan is not a fault, and a watchdog that
  "rescued" it would be killing the design the gate exists for;
* **detect and record.** A stalled job moves to `error` naming the stage, the
  item in flight, and how long it was quiet. Never a generic failure;
* **a dying task is not swallowed.** The `Supervisor` keeps a strong reference
  to every task and attaches a done-callback, so a crash becomes a readable
  error instead of a job frozen mid-stage;
* **restart recovery, and then on a timer.** `reconcile` turns anything left in
  flight by a killed worker into `error` saying the worker died. It runs at
  startup *and* every 60s thereafter (`reap_interval`). Startup-only had a
  hole: a job orphaned by a worker that died stays `running` until **that**
  worker restarts — with more than one worker, potentially never, presenting as
  a mystery outage. A job whose heartbeat is still fresh is left alone, which
  is also why the reaper is safe to run against this worker's own live jobs.

Tests drive the real `background=True` path on a fake `Clock`, so a
fifteen-minute budget costs no wall time.

## The stage machine

```
framework ──► probes ──► awaiting_approval ──► run ──► score ──► done
    │           │               │               │        │
    └───────────┴───────────────┴───────────────┴────────┴──► error
```

`awaiting_approval` is a **persisted state, not a convention**. A job in it has
no path to `run` except `POST /jobs/{id}/approve`. That is the author's design:
the user reads the plan — criteria, probes, sampling — and edits it *before*
anything touches the vendor's platform.

Every transition is written to disk before the response goes out, so a worker
restart mid-run loses the in-flight probe, not the job.

## The gate

`generator.py` marks probes that need human signoff: crisis / self-harm content,
minors' data, live writes to a real account. **Approving the plan does not
approve those.** Each needs its own `POST /jobs/{id}/signoff`.

The flag is read from the server's stored probe, never from the approval
payload — the plan comes back from a browser, and a browser does not get to
declare a crisis probe safe. A client that sets `requiresSignoff: false` and
`signedOff: true` on every probe and approves gets a run with zero probes in it.

## Credentials

Test accounts are still accounts.

* accepted on `POST /jobs`, encrypted immediately (Fernet), written to
  `<job>/credentials.enc`;
* **never** on the job record, in a log, or in any response — `_public_state`
  is a whitelist of keys, so there is no field a secret could ride out on;
* decrypted only in `JobRunner._credentials`, at the moment the driver logs in;
* the key comes from `EVALKIT_SECRET_KEY`. No default, no fallback, not in the
  repo.

There is deliberately no masked form. A masked-and-recoverable value is a leak
with a decoration on it.

## Batch / unattended runs

One evaluation is a `POST /jobs` and a human at the approval screen. Fifty to a
hundred tools a week cannot be. `lxp_eval.service.batch` runs a manifest of them
unattended, through the **same** seams a request uses — `JobStore.create`, then
`JobRunner.build_plan` / `execute`, under `Supervisor.start` — so the heartbeat,
the watchdog and the orphan-reconcile all cover batch jobs unchanged. There is
no second traversal of the engine here.

```bash
python -m lxp_eval.service.batch run lxp_eval/service/examples/batch_manifest.example.yaml \
    --max-spend-usd 75 --max-parallel 3 --approval-policy auto
```

A weekly run is this command inside a system scheduler (cron, Windows Task
Scheduler). There is deliberately no internal scheduler: the OS already is one.

**Two things the batch does not negotiate**, because they are the repo's
invariants:

* **A gated probe never runs without a human signature, and the signature is
  per probe, not per plan.** Under `approval_policy: auto` the batch approves the
  plan and lets the **ungated** probes run; each gated probe (crisis, minors'
  data, real writes) stays behind its own signoff, which the batch never writes,
  so `JobRunner.runnable_probes` drops it and its criterion scores `NO_EVALUADO`.
  This is the existing gate, untouched — the batch only chooses not to wait for
  what has no gate. Default `approval_policy` is `human`: today's behaviour, the
  job parks at `awaiting_approval`. The same `approval_policy: "human" | "auto"`
  is accepted on `POST /jobs`, so a single request can also run unattended —
  build, approve and run as one supervised chain — with the identical gate rule.
* **An auto-approved run is never presented as human-approved.** The approval row
  on the ledger carries the machine actor (`serialize.APPROVAL_NOTE_AUTO`), its
  provenance line reads "aprobado automáticamente", and the report's
  `meta.approvalPolicy` / `meta.autoApproved` record the fact in the header a
  ministry reads first.

**Spend ceiling.** `--max-spend-usd` (or `max_spend_usd:` in the manifest) stops
*launching* new jobs once finished spend crosses the cap; jobs already in flight
finish. **Concurrency** is bounded by a semaphore the size of
`LXP_MAX_PARALLEL_AGENTS` — one Chromium per job is heavy.

**One bad entry does not take the batch down.** Each entry's setup — reading its
documents, creating the record, encrypting credentials, scheduling — is isolated.
If it fails (a `path:` that does not exist, a file over 25 MB), *that* entry is
listed as failed with its reason and the batch carries on; the summary is always
returned. A job that dies later, mid-stage, is recorded on its own job by the
supervisor as usual. Stopping the batch cancels the jobs in flight, and each one
records that it was cancelled and where.

**Credentials by reference, never in the manifest.** An entry's
`credentials_ref: foo` is resolved from `EVALKIT_CRED_FOO_USER` /
`EVALKIT_CRED_FOO_PASS`. With those unset the job runs on the saved session (the
recon-first design). A manifest is versioned; a password in git is a burned one.

See `examples/batch_manifest.example.yaml` for the full shape.

## Job directory layout

```
<root>/<job_id>/job.json                 the record (never a credential)
<root>/<job_id>/credentials.enc          Fernet ciphertext, or absent
<root>/<job_id>/evidence/ledger.jsonl    the evidence ledger
<root>/<job_id>/evidence/approved_plan.json   the plan as approved, snapshotted
<root>/<job_id>/evidence/signoff_<probe>.json one probe's signoff, snapshotted
<root>/<job_id>/evidence/artifacts/      screenshots
<root>/<job_id>/evidence/page_text/      page-text captures
```

`root` defaults to `runs/jobs`. A job directory zips into a hand-over: the
ledger points at the artefacts, and every hash in the report re-checks.

## API rule

**Never send `temperature`, `top_p` or `top_k`.** They were removed on
`claude-opus-4-8` and return `400 invalid_request_error`. This package builds no
`messages.create` call at all — it drives the engine's existing call sites,
which already get this right. Do not add a new one.
