"""Replication harness for the LXP blind-sample findings.

Honest scope: the 11 blind cells are a SUBSET, not the 100-point rubric. This
does NOT reproduce the 58/71 deck scoreboard. It reproduces the blind-sample
findings and the blind-vs-evalkit agreement, every verdict citing real evidence.

Two forms:

* Form 1 (no API): turn each blind verdict into a TestVerdict and run the real
  aggregation. Proves the scoring math on real verdicts, over the blind sample.
* Form 2 (API): re-derive each cell with the scorer from the blind `lo_que_vi`
  (recorded as ledger evidence), then measure agreement with the blind verdict.

Agreement uses a small ordinal ladder (NO_CUMPLE < PARCIAL < CUMPLE); the
starter-kit compare_replication.py is a config-file CLI with exact-match only
and no importable function, so the ladder lives here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .criteria import Criteria
from .errors import EvalkitError
from .ledger import EvidenceLedger
from .msgs import missing_fields
from .scoring import (
    CUMPLE,
    NO_CUMPLE,
    NO_EVALUADO,
    PARCIAL,
    TestVerdict,
    _points,
    aggregate_by_platform,
    judge_fingerprint,
    score_test,
)
from .yamlsrc import line_of, load_yaml_with_lines, require_quoted

# repo-relative defaults: evalkit -> lxp_eval -> 06_Application -> repo root
_APP = Path(__file__).resolve().parents[2]
_REPO = _APP.parent
BLIND_SAMPLE = _REPO / "evidence_ledger" / "blind_protocol_sample_v2.json"
BLIND_RESULTS = _REPO / "evidence_ledger" / "blind_results.json"

#: Where the published expected verdicts come from (recorded in every row).
EXPECTED_SOURCE = "Informe_Marco_Resultados_ES.pdf §4.1"

#: The two confabulation (fake-memory) tutor cells — non-deterministic surface.
CONFAB_CELLS = ("PT-D9-H1", "PT-D9-H2")


def _repo_relative(p) -> str:
    """A repo-relative, POSIX-style path. Never absolute, so a UI can resolve it."""
    p = Path(p)
    try:
        return p.resolve().relative_to(_REPO).as_posix()
    except ValueError:
        try:
            return Path(os.path.relpath(p.resolve(), _REPO)).as_posix()
        except ValueError:  # different drive on Windows
            return p.name


# --- verdict normalization -------------------------------------------------
# The blind run used a slightly different vocabulary. NO_PUDE (couldn't reach
# the surface) and NO_OBSERVADO (didn't see it) both collapse to NO_EVALUADO --
# that collapse is lossy, so normalize_verdict returns the raw label too.
_NORMALIZE = {
    "CUMPLE": CUMPLE,
    "PARCIAL": PARCIAL,
    "NO_CUMPLE": NO_CUMPLE,  # also matches "NO CUMPLE" after space -> underscore
    "NO_PUDE": NO_EVALUADO,
    "NO_OBSERVADO": NO_EVALUADO,
}


def normalize_verdict(raw: str) -> tuple[str, str]:
    """(canonical, raw). Raises on any token outside the declared vocabulary.

    This used to fail closed to NO_EVALUADO, which sounds conservative and is
    the opposite. `agreement()` excludes NO_EVALUADO from the concordance
    DENOMINATOR, so an unrecognized token -- a typo in an input label, a vendor
    writing "CUMPLE PARCIAL" -- silently removed a pair from the comparison and
    quietly RAISED the published agreement rate. A label we do not understand
    must stop the run, not improve the headline.

    The collapse to NO_EVALUADO survives only for NO_PUDE / NO_OBSERVADO, which
    are genuinely declared ways of saying "could not evaluate".
    """
    key = (raw or "").strip().upper().replace(" ", "_")
    if key not in _NORMALIZE:
        raise EvalkitError(
            f"veredicto no reconocido: {raw!r}",
            hint=(
                "los valores válidos son: "
                + ", ".join(sorted(_NORMALIZE))
                + ". Un token desconocido se excluía del denominador y subía "
                "la tasa de concordancia publicada."
            ),
        )
    return _NORMALIZE[key], raw


# --- agreement ladder ------------------------------------------------------
_LADDER = {NO_CUMPLE: 0, PARCIAL: 1, CUMPLE: 2}


def agreement(blind: str, evalkit: str) -> tuple[bool | None, bool | None]:
    """(exact, within_one_level). Both None when either side is NO_EVALUADO --
    you can't agree on 'couldn't evaluate', so it's excluded from the rate."""
    if blind == NO_EVALUADO or evalkit == NO_EVALUADO:
        return None, None
    return blind == evalkit, abs(_LADDER[blind] - _LADDER[evalkit]) <= 1


# --- loading real data -----------------------------------------------------
def load_blind_cells(path: Path = BLIND_SAMPLE) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cells"]


def load_blind_results(path: Path = BLIND_RESULTS) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- generate criteria YAML from the blind protocol ------------------------
class _Quoted(str):
    """A string that must be emitted quoted in YAML (ids/codes are join keys)."""


def _represent_quoted(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


yaml.add_representer(_Quoted, _represent_quoted, Dumper=yaml.SafeDumper)

_DTOKEN = re.compile(r"^PT-(D\d+)-")


def _domain_code(cell_ids: list[str], sub_indicator: str) -> str:
    """The shared D-token of a group (real, from the cell ids), or the
    sub_indicator itself if the group doesn't share one."""
    tokens = {m.group(1) for cid in cell_ids if (m := _DTOKEN.match(cid))}
    return tokens.pop() if len(tokens) == 1 else sub_indicator


def build_blind_criteria_yaml(cells: list[dict]) -> str:
    """Emit a criteria YAML from the 11 blind cells. Nothing invented: domain =
    sub_indicator (grouped by the real value), test fields = the cell's fields."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for c in cells:
        si = c["sub_indicator"]
        if si not in groups:
            groups[si] = []
            order.append(si)
        groups[si].append(c)

    domains = []
    for si in order:
        gcells = groups[si]
        code = _domain_code([c["cell_id"] for c in gcells], si)
        tests = [
            {
                "id": _Quoted(c["cell_id"]),
                "name": c["pregunta"],
                "points": 1,
                "verification": c["que_hacer"],
                "pass_criteria": c["criterio_CUMPLE"],
                "fail_criteria": c["criterio_NO_CUMPLE"],
            }
            for c in gcells
        ]
        domains.append(
            {"code": _Quoted(code), "name": si, "points": len(tests), "tests": tests}
        )

    data = {
        "name": "LXP el país — submuestra ciega (11 celdas)",
        "domains": domains,
    }
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=4000)
    header = (
        "# Generado de blind_protocol_sample_v2.json — no editar a mano.\n"
        "# 11 celdas de la prueba ciega, agrupadas por sub_indicator.\n"
    )
    return header + body


# --- Form 1: deterministic aggregation of the blind verdicts (no API) ------
def blind_as_verdicts(criteria: Criteria, results: list[dict]) -> list[TestVerdict]:
    tests = {t.id: t for d in criteria.domains for t in d.tests}
    verdicts: list[TestVerdict] = []
    for row in results:
        test = tests.get(row["cell_id"])
        if test is None:
            continue
        norm, _raw = normalize_verdict(row["verdict"])
        earned, possible = _points(norm, test.points)
        verdicts.append(
            TestVerdict(
                test_id=row["cell_id"],
                platform=row["platform"],
                status=norm,
                points_earned=earned,
                points_possible=possible,
                rationale=row.get("lo_que_vi", ""),
                evidence=[row["evidencia"]] if row.get("evidencia") else [],
                runs=[norm],
            )
        )
    return verdicts


def form1_aggregate(criteria: Criteria, results: list[dict]):
    """Per-platform, per-sub_indicator scores over the blind SUBSAMPLE.

    NOT the 100-point deck scoreboard -- this is the 11-cell blind subset.
    """
    return aggregate_by_platform(criteria, blind_as_verdicts(criteria, results))


# --- Form 2: re-derive with the scorer, compare to the blind verdict -------
def load_evidence_map(path: Path, *, repo_root: Path | None = None) -> dict[tuple[str, str], list[Path]]:
    """Explicit (cell_id, platform) -> artifacts. No guessing.

    The previous heuristic gave PT-D9-H1 and PT-D9-H2 the SAME PLATAFORMA_B capture, so
    one of them was judged on an image of a different probe. A pair with no entry
    here gets text-only evidence; a mapped artifact that is missing on disk is a
    loud failure, not a silent skip.
    """
    path = Path(path)
    repo_root = Path(repo_root) if repo_root is not None else _REPO
    raw = load_yaml_with_lines(path, error_cls=EvalkitError)

    if not isinstance(raw, dict) or "entries" not in raw:
        raise EvalkitError(
            "el mapa de evidencia debe ser un mapa con una clave 'entries'",
            path=path, line=1,
        )

    out: dict[tuple[str, str], list[Path]] = {}
    for i, entry in enumerate(raw["entries"] or []):
        line = line_of(entry)
        if not isinstance(entry, dict):
            raise EvalkitError(
                f"la entrada #{i + 1} debe ser un mapa (clave: valor)", path=path, line=line
            )
        missing = [k for k in ("cell_id", "platform", "artifacts") if k not in entry]
        if missing:
            raise EvalkitError(
                missing_fields("entrada", f"#{i + 1}", missing), path=path, line=line
            )

        cell_id = require_quoted(entry, "cell_id", noun="el cell_id", path=path, error_cls=EvalkitError)
        platform = require_quoted(entry, "platform", noun="la plataforma", path=path, error_cls=EvalkitError)

        artifacts = entry["artifacts"]
        if not isinstance(artifacts, list) or not artifacts:
            raise EvalkitError(
                f'el campo "artifacts" de {cell_id}/{platform} debe ser una lista no vacía',
                path=path, line=line,
            )

        resolved: list[Path] = []
        for a in artifacts:
            candidate = (repo_root / str(a)).resolve()
            if not candidate.exists():
                raise EvalkitError(
                    f"el artefacto mapeado para {cell_id}/{platform} no existe: {a}",
                    path=path,
                    line=line,
                    hint="(las rutas son relativas a la raíz del repositorio)",
                )
            resolved.append(candidate)
        out[(cell_id, platform)] = resolved

    return out


def load_expected(path: Path) -> dict[tuple[str, str], dict]:
    """Published verdicts to compare against: (id, platform) -> {verdict, nota}."""
    path = Path(path)
    raw = load_yaml_with_lines(path, error_cls=EvalkitError)
    if not isinstance(raw, dict) or "entries" not in raw:
        raise EvalkitError(
            "el archivo de veredictos esperados debe tener una clave 'entries'",
            path=path, line=1,
        )

    out: dict[tuple[str, str], dict] = {}
    for i, entry in enumerate(raw["entries"] or []):
        line = line_of(entry)
        missing = [k for k in ("id", "platform", "verdict") if k not in entry]
        if missing:
            raise EvalkitError(
                missing_fields("entrada", f"#{i + 1}", missing), path=path, line=line
            )
        eid = require_quoted(entry, "id", noun="el id", path=path, error_cls=EvalkitError)
        platform = require_quoted(entry, "platform", noun="la plataforma", path=path, error_cls=EvalkitError)
        out[(eid, platform)] = {"verdict": entry["verdict"], "nota": entry.get("nota", "")}
    return out


def expected_as_rows(expected: dict[tuple[str, str], dict]) -> list[dict]:
    """Normalize published verdicts into the same row shape the blind run uses,
    so one rederive() path serves both modes."""
    return [
        {
            "cell_id": cell_id,
            "platform": platform,
            "verdict": value["verdict"],
            "lo_que_vi": value.get("nota", ""),
            "evidencia": EXPECTED_SOURCE,
        }
        for (cell_id, platform), value in expected.items()
    ]


def rederive(
    criteria: Criteria,
    results: list[dict],
    client,
    *,
    work_dir: Path,
    evidence_map: dict[tuple[str, str], list[Path]],
    mode: str = "blind",
    model: str | None = None,
) -> list[dict]:
    """Re-derive each (platform, cell) with the scorer and compare to the
    expected verdict (blind run, or the published report).

    The observation note becomes a real hashable artifact; screenshots come ONLY
    from the explicit evidence_map -- an unmapped pair is judged on text alone
    rather than on a guessed image. repeats=3 for the non-deterministic tutor
    cells, 1 otherwise.
    """
    tests = {t.id: t for d in criteria.domains for t in d.tests}
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    ledger = EvidenceLedger(work_dir / f"{mode}_ledger.jsonl")

    rows: list[dict] = []
    for row in results:
        cell_id, platform = row["cell_id"], row["platform"]
        test = tests.get(cell_id)
        if test is None:
            continue

        note = row.get("lo_que_vi", "") or "(sin nota)"
        obs = work_dir / f"{mode}_{platform}_{cell_id}_obs.txt"
        obs.write_text(note, encoding="utf-8")
        ledger.append(
            probe_id=f"{mode.upper()}-{cell_id}",
            test_id=cell_id,
            platform=platform,
            run_index=0,
            url=row.get("evidencia", "") or "",
            artifact=obs,
            kind="observation",
            note=note,
            timestamp=mode,
        )
        for shot in evidence_map.get((cell_id, platform), []):
            ledger.append(
                probe_id=f"{mode.upper()}-{cell_id}-shot",
                test_id=cell_id,
                platform=platform,
                run_index=0,
                url=str(shot),
                artifact=shot,
                kind="screenshot",
                note="captura mapeada explícitamente para esta sonda",
                timestamp=mode,
            )

        evidence = ledger.for_platform_test(platform, cell_id)
        repeats = 3 if cell_id in CONFAB_CELLS else 1
        verdict = score_test(test, evidence, client, platform=platform, repeats=repeats, model=model)

        expected_norm, expected_raw = normalize_verdict(row["verdict"])
        exact, within1 = agreement(expected_norm, verdict.status)
        rows.append(
            {
                "mode": mode,
                "cell_id": cell_id,
                "platform": platform,
                "criterion_name": test.name,
                "pass_criteria": test.pass_criteria,
                "fail_criteria": test.fail_criteria,
                "expected_raw": expected_raw,
                "expected_norm": expected_norm,
                "evalkit_verdict": verdict.status,
                "rationale": verdict.rationale,
                "runs": verdict.runs,
                "evidence": [
                    {
                        "path": _repo_relative(e["artifact"]),
                        "kind": e["kind"],
                        "exists": Path(e["artifact"]).exists(),
                    }
                    for e in evidence
                ],
                "cited": [_repo_relative(c) for c in verdict.evidence],
                "exact": exact,
                "within1": within1,
            }
        )
    return rows


def write_results_json(
    out_path: Path, summary: dict, rows: list[dict], manifest: dict | None = None
) -> Path:
    """Machine-readable results for a viewer. All paths are repo-relative.

    `manifest` (see build_manifest) records the resolved model, temperature,
    repeats and the judge/evidence fingerprints. Without it the file states a
    number nobody outside this machine can reproduce.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest": manifest or {},
        "summary": summary,
        "rows": rows,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


# ===========================================================================
# Statistics
#
# A single raw-agreement percentage is the most challengeable number in this
# package. It hides four things at once: which denominator produced it, how
# wide its uncertainty is, whether the disagreements run in one direction, and
# the fact that 26 comparisons are 11 cells x 2 platforms rather than 26
# independent draws. Each function below closes one of those holes.
# ===========================================================================

#: Ordered levels for the weighted statistics. Ordinal, low to high.
ORDERED_LEVELS = (NO_CUMPLE, PARCIAL, CUMPLE)
#: Every category the confusion matrix reports, abstention included.
ALL_LEVELS = (NO_CUMPLE, PARCIAL, CUMPLE, NO_EVALUADO)


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k/n, as fractions.

    Wilson rather than the normal approximation because n here is ~18 and the
    proportion is near the boundary, where Wald intervals run past 100% and
    collapse to zero width at k==n. On 15/18 this returns [0.608, 0.942]: the
    published "83%" carries 33 points of uncertainty, which is the single most
    important fact about it.
    """
    if n <= 0:
        return 0.0, 1.0  # no data is not a point estimate
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, centre - margin), min(1.0, centre + margin)


def format_proportion(k: int, n: int) -> str:
    """'15/18 (83%, IC95 61-94)'.

    Integer percentages on purpose. At n=18 one decimal place implies a
    precision of 0.1 points on an estimate whose 95% interval is 33 points
    wide, which is false precision in the literal sense.
    """
    if n <= 0:
        return f"0/0 (sin datos)"
    lo, hi = wilson_ci(k, n)
    return f"{k}/{n} ({round(100 * k / n):.0f}%, IC95 {round(100 * lo):.0f}-{round(100 * hi):.0f})"


def _proportion(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {
        "k": k,
        "n": n,
        "pct": round(100 * k / n) if n else None,
        "ci95": [round(lo, 4), round(hi, 4)],
        "label": format_proportion(k, n),
    }


def cohens_kappa(
    pairs: list[tuple[str, str]],
    categories: list[str] | tuple[str, ...] | None = None,
    weights: str | None = None,
) -> float | None:
    """Cohen's kappa; `weights="linear"` gives the linearly-weighted variant.

    Weighted kappa is the correct statistic for this instrument: the verdict
    scale is ORDERED (NO_CUMPLE < PARCIAL < CUMPLE), so CUMPLE-vs-PARCIAL is a
    near miss and CUMPLE-vs-NO_CUMPLE is a real conflict. Unweighted kappa
    scores those two identically.

    Returns None when kappa is undefined (no pairs, or chance agreement of
    exactly 1 because every rating landed in one category) rather than
    inventing a number for a degenerate table.
    """
    if not pairs:
        return None
    cats = list(categories) if categories is not None else sorted(
        {c for pair in pairs for c in pair}
    )
    index = {c: i for i, c in enumerate(cats)}
    n = len(pairs)
    span = max(1, len(cats) - 1)

    def w(a: str, b: str) -> float:
        """Disagreement weight: 0 identical, 1 maximally apart."""
        if weights == "linear":
            return abs(index[a] - index[b]) / span
        return 0.0 if a == b else 1.0

    rows_marginal: Counter[str] = Counter(a for a, _ in pairs)
    cols_marginal: Counter[str] = Counter(b for _, b in pairs)

    observed = sum(w(a, b) for a, b in pairs) / n
    expected = sum(
        rows_marginal[a] * cols_marginal[b] * w(a, b) / (n * n) for a in cats for b in cats
    )
    if expected == 0:
        # chance agreement is exactly 1 (every rating in one category on both
        # sides), so kappa is 0/0. Undefined, not "perfect".
        return None
    return 1 - observed / expected


def confusion_matrix(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Full 4x4 expected-vs-evalkit table, NO_EVALUADO included as a category.

    Published alongside the rate so a reader can reconstruct every denominator
    themselves instead of trusting the one we chose to headline.
    """
    m = {a: {b: 0 for b in ALL_LEVELS} for a in ALL_LEVELS}
    for r in rows:
        a, b = r.get("expected_norm"), r.get("evalkit_verdict")
        if a in m and b in m[a]:
            m[a][b] += 1
    return m


def sign_test(n_a: int, n_b: int) -> float | None:
    """Two-sided exact binomial p on discordant pairs (marginal homogeneity).

    Raw agreement is blind to DIRECTION. In the last run all 5 disagreements
    ran the same way -- evalkit harsher than the blind human every time -- which
    is a systematic bias, not noise, and an 83% agreement rate cannot show it.
    None when there is nothing discordant to test.
    """
    n = n_a + n_b
    if n == 0:
        return None
    tail = sum(math.comb(n, i) for i in range(min(n_a, n_b) + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def _eligible(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("exact") is not None]


def cluster_bootstrap_ci(
    rows: list[dict], *, n_boot: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Percentile CI on the exact-agreement rate, resampling CELLS not rows.

    The 26 comparisons are 11 cells x 2 platforms. A cell is the unit that was
    drawn: both platforms are judged against the SAME probe text and the same
    criterion wording, so their two verdicts are correlated by construction.
    Treating the 26 rows as independent draws understates the interval; this
    resamples cell_ids with replacement and keeps both platforms together.
    """
    by_cell: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cell[r.get("cell_id", "")].append(r)
    cells = sorted(by_cell)
    if not any(_eligible(by_cell[c]) for c in cells):
        return None

    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(n_boot):
        drawn = [r for c in (rng.choice(cells) for _ in cells) for r in by_cell[c]]
        elig = _eligible(drawn)
        if elig:
            rates.append(sum(int(r["exact"]) for r in elig) / len(elig))
    if not rates:
        return None
    rates.sort()
    lo = rates[int((alpha / 2) * (len(rates) - 1))]
    hi = rates[int((1 - alpha / 2) * (len(rates) - 1))]
    return round(lo, 4), round(hi, 4)


def _within1_chance_floor(pairs: list[tuple[str, str]]) -> float | None:
    """Expected within-one-level agreement under the OBSERVED marginals.

    On a 3-point ladder "within one level" is close to vacuous: only the two
    extreme corners count as a miss, so independent raters clear ~73% by
    construction. within1 must never be reported without this number beside it.
    """
    if not pairs:
        return None
    n = len(pairs)
    rows_marginal = Counter(a for a, _ in pairs)
    cols_marginal = Counter(b for _, b in pairs)
    idx = {c: i for i, c in enumerate(ORDERED_LEVELS)}
    total = sum(
        rows_marginal[a] * cols_marginal[b]
        for a in ORDERED_LEVELS
        for b in ORDERED_LEVELS
        if abs(idx[a] - idx[b]) <= 1
    )
    return total / (n * n)


def _citation_stats(rows: list[dict]) -> dict:
    """How often a verdict cited something OTHER than our own observation note.

    The "abrí la carpeta" guarantee is satisfied by any citation, and in the
    last run 24 of 30 rows cited only the harness-written `_obs.txt` -- our own
    note, not an artifact from the platform. That makes the guarantee vacuous
    in 80% of the sample. The COERCION rule is deliberately unchanged (tightening
    it mid-flight would move the estimand); this measures and publishes the gap.
    """
    harness_kinds = {"observation"}
    any_cite = independent = 0
    for r in rows:
        kinds = {e.get("path"): e.get("kind") for e in r.get("evidence") or []}
        cited = r.get("cited") or []
        if cited:
            any_cite += 1
        if any(kinds.get(c) not in harness_kinds for c in cited):
            independent += 1
    n = len(rows)
    return {
        "rows": n,
        "rows_with_any_citation": any_cite,
        "rows_citing_independent_artifact": independent,
        "independent_pct": round(100 * independent / n) if n else None,
        "note": (
            "una cita al artefacto de observación escrito por el propio arnés no "
            "es evidencia independiente de la plataforma"
        ),
    }


def _core_summary(rows: list[dict]) -> dict:
    """Every statistic except the per-platform split (which recurses into this)."""
    elig = _eligible(rows)
    pairs = [
        (r["expected_norm"], r["evalkit_verdict"])
        for r in elig
        if r.get("expected_norm") in ORDERED_LEVELS
        and r.get("evalkit_verdict") in ORDERED_LEVELS
    ]
    n_all = len(rows)

    exact_k = sum(int(r["exact"]) for r in elig)
    within_k = sum(int(r["within1"]) for r in elig)
    all_k = sum(
        1 for r in rows if r.get("expected_norm") == r.get("evalkit_verdict")
    )
    abstained = sum(1 for r in rows if r.get("evalkit_verdict") == NO_EVALUADO)

    harsher = {"evalkit": 0, "expected": 0}
    rank = {c: i for i, c in enumerate(ORDERED_LEVELS)}
    for a, b in pairs:
        if rank[b] < rank[a]:
            harsher["evalkit"] += 1
        elif rank[b] > rank[a]:
            harsher["expected"] += 1

    eligible_prop = _proportion(exact_k, len(elig))
    eligible_prop["cluster_ci95"] = list(cluster_bootstrap_ci(rows) or (0.0, 1.0))

    floor = _within1_chance_floor(pairs)
    return {
        # --- the three denominators, published together so nobody can claim
        # the number improves when the tool sees LESS -------------------------
        "exact_eligible": eligible_prop,
        "exact_all": _proportion(all_k, n_all),
        "coverage": _proportion(len(elig), n_all),
        "abstention": _proportion(abstained, n_all),
        # --- inference -------------------------------------------------------
        "kappa": None if (k := cohens_kappa(pairs, ORDERED_LEVELS)) is None else round(k, 4),
        "kappa_weighted": (
            None
            if (kw := cohens_kappa(pairs, ORDERED_LEVELS, weights="linear")) is None
            else round(kw, 4)
        ),
        "direction": {
            "discordant": sum(1 for a, b in pairs if a != b),
            "evalkit_harsher": harsher["evalkit"],
            "expected_harsher": harsher["expected"],
            "p_value": sign_test(harsher["evalkit"], harsher["expected"]),
        },
        # --- demoted: near-vacuous on a 3-point ladder, so it never appears
        # without the chance floor it has to beat ------------------------------
        "within1": {
            "k": within_k,
            "n": len(elig),
            "pct": round(100 * within_k / len(elig)) if elig else None,
            "chance_floor_pct": None if floor is None else round(100 * floor),
        },
        "confusion": confusion_matrix(rows),
        "citations": _citation_stats(rows),
        # --- legacy flat keys, kept so existing consumers do not break -------
        "compared": len(elig),
        "excluded": n_all - len(elig),
        "exact": exact_k,
        "exact_pct": eligible_prop["pct"],
    }


def agreement_summary(rows: list[dict]) -> dict:
    """Every denominator, its uncertainty, its direction, and its split.

    Reporting only the concordance-eligible rate lets a reader claim the number
    improves when the tool sees less -- every cell the judge abstains on leaves
    the denominator. So all three are published: eligible, all-pairs (with
    NO_EVALUADO as a fourth category), and coverage.

    Pooling across platforms hides more. In the last run the pooled 83.3%
    concealed PLATAFORMA_B at 12/12 = 100% against Plataforma A at 9/14 = 64.3%, with all five
    disagreements on Plataforma A. In a two-vendor procurement that must never be a
    single number.
    """
    summary = _core_summary(rows)
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_platform[r.get("platform", "")].append(r)
    summary["by_platform"] = {p: _core_summary(rs) for p, rs in sorted(by_platform.items())}
    return summary


# --- reproducibility manifest ---------------------------------------------
def build_manifest(
    *,
    model: str,
    evidence_map: dict[tuple[str, str], list[Path]],
) -> dict:
    """What an independent party needs to reproduce this run, and nothing less.

    NO TEMPERATURE FIELD -- deliberately. The published rate did not reproduce
    (an independent re-run got 82.4% against a published 88.2%) and every
    reviewer prescribed `temperature=0`. The API rejected it:

        400 invalid_request_error: `temperature` is deprecated for this model.

    Sampling parameters were removed on Opus 4.8/4.7. So bit-identical re-runs
    are NOT achievable on this model, and a manifest claiming `temperature: 0`
    would be asserting a guarantee we do not have.

    What we can honestly pin: the resolved model, a fingerprint of the judge
    (system prompt + forced-tool schema), a fingerprint of the evidence (every
    mapped artifact's own sha256), and how many times each cell was judged.
    `sampling` states plainly that run-to-run variation is expected and is to
    be measured via repeats, not suppressed.
    """
    entries = []
    for (cell_id, platform), artifacts in sorted(evidence_map.items()):
        for a in artifacts:
            p = Path(a)
            entries.append({
                "cell_id": cell_id,
                "platform": platform,
                "artifact": _repo_relative(p),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None,
            })
    evidence_sha = hashlib.sha256(
        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return {
        "model": model,
        "sampling": (
            "no determinista — los parámetros de muestreo (temperature/top_p/top_k) "
            "fueron eliminados en este modelo y la API los rechaza con 400. Se espera "
            "variación entre corridas; se mide con repeats, no se suprime."
        ),
        "repeats_per_cell": {
            "default": 1,
            "confab": 3,
            "confab_cells": list(CONFAB_CELLS),
        },
        "judge_sha256": judge_fingerprint(),
        "evidence_map_sha256": evidence_sha,
        "evidence_artifacts": entries,
        "expected_source": EXPECTED_SOURCE,
    }
