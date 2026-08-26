"""Build a self-contained HTML dashboard from the replication results.

Reads `runs/replicate_lxp/replication_results.json` (written by replicate_lxp.py),
embeds the cited screenshots as base64 so the output is ONE shareable file, and
writes `runs/replicate_lxp/dashboard.html`.

Regenerate it after every replication run:

    python examples/replicate_lxp.py && python examples/build_dashboard.py
"""
from __future__ import annotations

import base64
import html
import json
import sys
from pathlib import Path

_EX = Path(__file__).resolve().parent
REPO = _EX.parents[1]
RESULTS = _EX.parent / "runs" / "replicate_lxp" / "replication_results.json"
OUT = _EX.parent / "runs" / "replicate_lxp" / "dashboard.html"

_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
_MAX_EMBED_BYTES = 4 * 1024 * 1024


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def data_uri(rel_path: str) -> str | None:
    """Embed an image as base64 so the dashboard is a single portable file."""
    p = (REPO / rel_path).resolve()
    if not p.exists() or p.suffix.lower() not in _MEDIA:
        return None
    if p.stat().st_size > _MAX_EMBED_BYTES:
        return None
    return f"data:{_MEDIA[p.suffix.lower()]};base64," + base64.b64encode(p.read_bytes()).decode()


def verdict_class(v: str) -> str:
    return {
        "CUMPLE": "v-ok",
        "PARCIAL": "v-mid",
        "NO_CUMPLE": "v-bad",
        "NO_EVALUADO": "v-none",
    }.get(v, "v-none")


def agreement_badge(row: dict) -> str:
    if row.get("exact") is None:
        return '<span class="badge b-none">excluida</span>'
    if row["exact"]:
        return '<span class="badge b-ok">coincide</span>'
    if row.get("within1"):
        return '<span class="badge b-warn">difiere &plusmn;1</span>'
    return '<span class="badge b-bad">difiere</span>'


def rate_card(label: str, s: dict) -> str:
    return f"""
    <div class="card">
      <div class="kick">{esc(label)}</div>
      <div class="big">{s['exact_eligible']['pct']}<span class="pct">%</span></div>
      <div class="sub">coincidencia exacta &middot; {s['exact_eligible']['label']}</div>
      <div class="row2"><span>todas las celdas: <b>{s['exact_all']['label']}</b></span><span>cobertura: <b>{s['coverage']['label']}</b></span></div>
      <div class="row2"><span>kappa: <b>{s['kappa']}</b></span><span>kappa ponderada: <b>{s['kappa_weighted']}</b></span></div>
      <div class="row2"><span>desacuerdos evalkit m&aacute;s duro: <b>{s['direction']['evalkit_harsher']}/{s['direction']['discordant']}</b></span><span>signo p: <b>{s['direction']['p_value']}</b></span></div>
    </div>"""


def evidence_block(row: dict) -> str:
    """Show the cited evidence: images inline, text artifacts as a filename chip."""
    cited = row.get("cited") or []
    imgs, texts = [], []
    for path in cited:
        uri = data_uri(path)
        name = Path(path).name
        if uri:
            imgs.append(f'<figure><img src="{uri}" alt="{esc(name)}" loading="lazy"><figcaption>{esc(name)}</figcaption></figure>')
        else:
            texts.append(f'<span class="file">{esc(name)}</span>')
    if not imgs and not texts:
        return '<div class="noev">sin evidencia citada</div>'
    return f'<div class="shots">{"".join(imgs)}</div><div class="files">{"".join(texts)}</div>'


def hallazgo_card(row: dict) -> str:
    disagree = row.get("exact") is False
    runs = row.get("runs") or []
    runs_note = ""
    if len(runs) > 1:
        uniq = "unánime" if len(set(runs)) == 1 else "NO unánime"
        runs_note = f'<div class="runs">{len(runs)} corridas &middot; {uniq} &middot; {esc(", ".join(runs))}</div>'
    return f"""
    <article class="hcard{' disagree' if disagree else ''}">
      <header>
        <div><span class="cid">{esc(row['cell_id'])}</span><span class="plat">{esc(row['platform'])}</span></div>
        {agreement_badge(row)}
      </header>
      <h3>{esc(row['criterion_name'])}</h3>
      <div class="verdicts">
        <div><span class="vlab">informe</span><span class="verdict {verdict_class(row['expected_norm'])}">{esc(row['expected_norm'])}</span></div>
        <div class="arrow">&rarr;</div>
        <div><span class="vlab">evalkit</span><span class="verdict {verdict_class(row['evalkit_verdict'])}">{esc(row['evalkit_verdict'])}</span></div>
      </div>
      {runs_note}
      {evidence_block(row)}
      <details><summary>criterio y razonamiento</summary>
        <p class="crit"><b>Cumple si:</b> {esc(row.get('pass_criteria'))}</p>
        <p class="crit"><b>No cumple si:</b> {esc(row.get('fail_criteria'))}</p>
        <p class="rat">{esc(row.get('rationale'))}</p>
      </details>
    </article>"""


def blind_row(row: dict) -> str:
    cited = (row.get("cited") or [])
    ev = Path(cited[0]).name if cited else "&mdash;"
    return f"""<tr class="{'dis' if row.get('exact') is False else ''}">
      <td class="mono">{esc(row['cell_id'])}</td><td>{esc(row['platform'])}</td>
      <td class="mono small">{esc(row['expected_raw'])}</td>
      <td><span class="verdict {verdict_class(row['expected_norm'])}">{esc(row['expected_norm'])}</span></td>
      <td><span class="verdict {verdict_class(row['evalkit_verdict'])}">{esc(row['evalkit_verdict'])}</span></td>
      <td>{agreement_badge(row)}</td>
      <td class="mono small">{ev}</td></tr>"""


def build(data: dict) -> str:
    rows = data["rows"]
    summ = data["summary"]
    hall = sorted([r for r in rows if r["mode"] == "hallazgos"], key=lambda r: (r["cell_id"], r["platform"]))
    blind = [r for r in rows if r["mode"] == "blind"]
    n_dis = sum(1 for r in rows if r.get("exact") is False)

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>evalkit &middot; replicación de los hallazgos LXP</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{{--ink:#161A17;--mut:#5F6B62;--line:#DDE3DC;--paper:#F7F5F0;--card:#fff;
--ok:#0E9A6C;--mid:#C98A12;--bad:#C0442E;--none:#8A9490;--acc:#0E7A57;
--serif:'Fraunces',Georgia,serif;--sans:'Inter','Segoe UI',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5;padding:40px 28px 80px}}
.wrap{{max-width:1120px;margin:0 auto}}
h1{{font-family:var(--serif);font-weight:700;font-size:44px;line-height:1.05;letter-spacing:-.01em}}
.lead{{color:var(--mut);font-size:17px;margin-top:10px;max-width:760px}}
.kick{{font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--acc)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin:28px 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px}}
.big{{font-size:52px;font-weight:800;letter-spacing:-.03em;line-height:1;margin-top:8px;font-variant-numeric:tabular-nums}}
.pct{{font-size:26px;margin-left:2px}}
.sub{{color:var(--mut);font-size:14px;margin-top:6px}}
.row2{{display:flex;gap:18px;margin-top:12px;font-size:13px;color:var(--mut);border-top:1px solid var(--line);padding-top:10px}}
h2{{font-family:var(--serif);font-weight:700;font-size:27px;margin:44px 0 6px}}
.note{{color:var(--mut);font-size:15px;margin-bottom:18px;max-width:820px}}
.hgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}}
.hcard{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}}
.hcard.disagree{{background:#FDF6F3;border-color:#EBC4B8}}
.hcard header{{display:flex;justify-content:space-between;align-items:center;gap:10px}}
.cid{{font-weight:700;font-size:13px;letter-spacing:.04em}}
.plat{{margin-left:8px;font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:999px;padding:2px 9px}}
.hcard h3{{font-family:var(--serif);font-weight:500;font-size:19px;line-height:1.25;margin:12px 0 14px}}
.verdicts{{display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.vlab{{display:block;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);margin-bottom:3px}}
.verdict{{display:inline-block;font-size:12px;font-weight:700;padding:4px 10px;border-radius:6px;letter-spacing:.02em}}
.v-ok{{background:#E3F3EC;color:var(--ok)}} .v-mid{{background:#FBF0DA;color:var(--mid)}}
.v-bad{{background:#FBE7E2;color:var(--bad)}} .v-none{{background:#ECEFED;color:var(--none)}}
.arrow{{color:var(--mut);font-size:19px;align-self:flex-end;padding-bottom:3px}}
.badge{{font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;white-space:nowrap}}
.b-ok{{background:#E3F3EC;color:var(--ok)}} .b-warn{{background:#FBF0DA;color:var(--mid)}}
.b-bad{{background:#FBE7E2;color:var(--bad)}} .b-none{{background:#ECEFED;color:var(--none)}}
.runs{{font-size:12px;color:var(--mut);margin-top:10px}}
.shots{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}}
figure{{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff;max-width:100%}}
figure img{{display:block;width:100%;max-width:520px;height:auto}}
figcaption{{font-size:11px;color:var(--mut);padding:6px 9px;border-top:1px solid var(--line);word-break:break-all}}
.files{{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}}
.file{{font-size:11px;color:var(--mut);background:#F0F2F0;border-radius:5px;padding:3px 8px;word-break:break-all}}
.noev{{font-size:12px;color:var(--none);margin-top:12px;font-style:italic}}
details{{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}}
summary{{cursor:pointer;font-size:12px;font-weight:600;color:var(--acc)}}
.crit,.rat{{font-size:13px;color:var(--mut);margin-top:8px}}
.rat{{color:var(--ink);background:#F4F6F4;padding:10px 12px;border-radius:8px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th{{text-align:left;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);padding:11px 12px;border-bottom:1px solid var(--line)}}
td{{padding:10px 12px;border-bottom:1px solid #EEF1EE;font-size:13px;vertical-align:middle}}
tr.dis td{{background:#FDF6F3}}
.mono{{font-family:ui-monospace,Consolas,monospace}} .small{{font-size:11.5px;color:var(--mut)}}
.warnbox{{background:#FFF9E8;border:1px solid #E8D9A8;border-radius:12px;padding:18px 20px;margin-top:36px}}
.warnbox h3{{font-family:var(--serif);font-size:18px;margin-bottom:8px}}
.warnbox p{{font-size:14px;color:#6B5D34;margin-top:6px}}
.foot{{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}}
</style></head><body><div class="wrap">

<div class="kick">Clase de prueba &middot; Educación LAC &middot; el país</div>
<h1>El método, corrido otra vez.</h1>
<p class="lead">evalkit vuelve a puntuar la evidencia guardada de la evaluación LXP y se compara
con los veredictos originales. Cada veredicto cita el archivo que miró.</p>

<div class="cards">
  {rate_card("Hallazgos publicados (informe §4.1)", summ["hallazgos"])}
  {rate_card("Submuestra ciega (11 celdas)", summ["blind"])}
  <div class="card"><div class="kick">Trazabilidad</div>
    <div class="big">{len(rows)}</div>
    <div class="sub">veredictos, todos con evidencia citada</div>
    <div class="row2"><span>discrepancias: <b>{n_dis}</b></span><span>modos: <b>2</b></span></div></div>
</div>

<h2>Hallazgos publicados</h2>
<p class="note">Los hallazgos centrales del informe. evalkit mira la captura real y decide por su cuenta;
la columna «informe» es el veredicto publicado. Las discrepancias se muestran, no se esconden.</p>
<div class="hgrid">{"".join(hallazgo_card(r) for r in hall)}</div>

<h2>Submuestra ciega</h2>
<p class="note">Las 11 celdas de la prueba de replicación ciega, en las dos plataformas.</p>
<table><thead><tr><th>celda</th><th>plat</th><th>original</th><th>esperado</th><th>evalkit</th><th>acuerdo</th><th>evidencia</th></tr></thead>
<tbody>{"".join(blind_row(r) for r in blind)}</tbody></table>

<div class="warnbox">
  <h3>Qué mide esto — y qué no</h3>
  <p><b>Mide</b> que el paso de puntuación es reproducible: dada la misma evidencia, evalkit llega
  al mismo veredicto que la persona que la escribió.</p>
  <p><b>No mide</b> que evalkit observe la plataforma tan bien como un humano — eso requiere la
  corrida en vivo, donde el agente captura su propia evidencia. Por eso estos porcentajes
  <b>no son comparables</b> con el 72%/94% de la prueba ciega, que incluía la varianza de observación.</p>
  <p>Las 11 celdas ciegas son una <b>submuestra</b>, no la rúbrica de 100 puntos: esto no reproduce
  el tablero 58/71. El tutor y el juicio del modelo no son determinísticos.</p>
</div>

<div class="foot">Generado {esc(data.get("generated_at"))} &middot; fuente:
<span class="mono">runs/replicate_lxp/replication_results.json</span> &middot;
regenerar con <span class="mono">python examples/replicate_lxp.py &amp;&amp; python examples/build_dashboard.py</span></div>
</div></body></html>"""


def main() -> int:
    if not RESULTS.exists():
        print(f"No existe {RESULTS}. Corré primero: python examples/replicate_lxp.py")
        return 1
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    OUT.write_text(build(data), encoding="utf-8")
    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f"Dashboard: {OUT}  ({size_mb:.1f} MB, {len(data['rows'])} veredictos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
