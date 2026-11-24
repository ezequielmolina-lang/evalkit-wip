"""Los bancos de criterios externos: procedencia completa y anclas honestas.

El producto se defiende diciendo "cada criterio sale de un instrumento
publicado". Estas pruebas son lo que hace que esa frase sea verificable y no
una promesa: si un criterio pierde su procedencia, o si alguien inventa una
ancla que la fuente no dice, acá se rompe.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lxp_eval.evalkit.framework import load_bank

BANKS_DIR = Path(__file__).resolve().parents[1] / "lxp_eval" / "evalkit" / "banks"

#: Marca textual que lleva un criterio cuya fuente NO enuncia un umbral. No se
#: inventa un anclaje: se declara que falta. El builder y el test comparten la
#: constante a propósito.
NO_ANCHOR_MARK = "[SIN ANCLA EN LA FUENTE]"

#: Bancos nuevos, los que este archivo custodia. El banco LXP tiene su propia
#: cobertura en test_framework.py.
EXTERNAL_BANKS = ("emc_peru_ai_tutor.yaml", "external_rubrics.yaml")

ALL_BANKS = ("lxp_sv_v7_4.yaml",) + EXTERNAL_BANKS


def _raw(name: str) -> dict:
    return yaml.safe_load((BANKS_DIR / name).read_text(encoding="utf-8"))


def _raw_entries(name: str) -> list[dict]:
    return _raw(name)["entries"]


# --- lo mínimo: cargan ------------------------------------------------------


@pytest.mark.parametrize("name", ALL_BANKS)
def test_bank_round_trips_through_load_bank(name):
    """Un banco que no carga con el loader existente no es un banco."""
    bank = load_bank(BANKS_DIR / name)
    assert bank.entries, f"{name} no aporta ningún criterio"
    assert bank.areas


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_external_bank_declares_its_frameworks(name):
    bank = load_bank(BANKS_DIR / name)
    declared = {f["id"] for f in bank.frameworks}
    used = {e.source.framework for e in bank.entries}
    assert used <= declared, (
        f"{name}: criterios citan marcos no declarados: {sorted(used - declared)}"
    )
    assert declared == used, (
        f"{name}: marcos declarados sin ningún criterio: {sorted(declared - used)}"
    )


# --- procedencia ------------------------------------------------------------


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_every_entry_has_complete_provenance(name):
    bank = load_bank(BANKS_DIR / name)
    for e in bank.entries:
        s = e.source
        assert s.type and s.framework and s.original_id and s.location, (
            f"{name}/{e.id}: procedencia incompleta"
        )
        assert s.is_sourced, (
            f"{name}/{e.id}: type={s.type!r} -- un criterio del banco externo "
            "no puede ser 'generated'"
        )


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_location_is_specific_enough_to_verify(name):
    """"El PDF de ISTE" no es una ubicación. Página, sección o fila, sí.

    La regla operativa: la ubicación nombra un archivo Y un ancla dentro de él
    (`#p. 14`, `#tabla 3`, `#§2.1b`). Sin el ancla, nadie puede verificar la
    transcripción sin leer el documento entero.
    """
    bank = load_bank(BANKS_DIR / name)
    for e in bank.entries:
        loc = e.source.location
        assert "#" in loc, f"{name}/{e.id}: la ubicación {loc!r} no apunta dentro del documento"
        inner = loc.split("#", 1)[1].strip()
        assert len(inner) >= 3, f"{name}/{e.id}: ancla de ubicación vacía en {loc!r}"


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_ids_are_quoted_in_the_yaml(name):
    """`1.10` sin comillas se lee 1.1 y `no` se lee False. Los ids son claves."""
    text = (BANKS_DIR / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        for key in ("id:", "original_id:", "area:"):
            if stripped.startswith(key):
                value = stripped[len(key):].strip()
                assert value.startswith('"') and value.endswith('"'), (
                    f"{name}: {stripped!r} -- los ids van entre comillas"
                )


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_no_duplicate_ids_within_a_bank(name):
    bank = load_bank(BANKS_DIR / name)
    ids = [e.id for e in bank.entries]
    assert len(ids) == len(set(ids))
    originals = [(e.source.framework, e.source.original_id) for e in bank.entries]
    assert len(originals) == len(set(originals)), (
        f"{name}: dos criterios dicen venir de la misma fila del mismo instrumento"
    )


# --- anclas honestas --------------------------------------------------------


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_missing_anchors_are_declared_not_invented(name):
    """La regla central: si la fuente no dice el umbral, el criterio lo admite.

    Un criterio sin ancla debe llevar `needs_anchors: true` Y la marca textual
    en el campo correspondiente. Las dos cosas, para que ni el lector humano ni
    el juez de abajo puedan confundir "transcrito" con "faltante".
    """
    for e in _raw_entries(name):
        marked = bool(e.get("needs_anchors"))
        has_mark = NO_ANCHOR_MARK in str(e.get("pass_criteria", "")) or (
            NO_ANCHOR_MARK in str(e.get("fail_criteria", ""))
        )
        assert marked == has_mark, (
            f"{name}/{e['id']}: needs_anchors={marked} pero la marca "
            f"{NO_ANCHOR_MARK} {'sí' if has_mark else 'no'} aparece en las anclas "
            "-- declarar la falta y marcarla van juntos"
        )


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_anchored_entries_carry_source_wording(name):
    """Un criterio anclado cita a su fuente, no la parafrasea al estilo de casa.

    Chequeo proxy y deliberadamente barato: las anclas transcritas llevan la
    atribución `(<sección de la fuente>)` que el builder emite al copiar. Si
    alguien reescribe el texto a mano y borra la atribución, esto avisa.
    """
    for e in _raw_entries(name):
        if e.get("needs_anchors"):
            continue
        for field in ("pass_criteria", "fail_criteria"):
            assert str(e[field]).strip(), f"{name}/{e['id']}: {field} vacío"


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_no_entry_claims_an_anchor_it_also_says_is_missing(name):
    for e in _raw_entries(name):
        both = NO_ANCHOR_MARK in str(e.get("pass_criteria", "")) and (
            NO_ANCHOR_MARK in str(e.get("fail_criteria", ""))
        )
        if both:
            assert e.get("needs_anchors") is True


# --- solapamiento entre marcos ---------------------------------------------


@pytest.mark.parametrize("name", EXTERNAL_BANKS)
def test_overlap_tags_point_at_real_entries(name):
    """Dos marcos pueden decir lo mismo; el banco lo declara en vez de esconderlo.

    Se conservan ambos (la procedencia difiere y la corroboración independiente
    vale), pero etiquetados, para que el juez de no-solapamiento vea que están
    emparentados y no pondere dos veces la misma dimensión.
    """
    known = {
        e.id for b in ALL_BANKS for e in load_bank(BANKS_DIR / b).entries
    }
    for e in _raw_entries(name):
        for other in e.get("overlaps") or []:
            assert other in known, (
                f"{name}/{e['id']}: overlaps apunta a {other!r}, que no existe "
                "en ninguno de los bancos"
            )
            assert other != e["id"]


# --- el banco EMC en particular --------------------------------------------


def test_emc_bank_is_the_peru_ai_tutor_framework():
    bank = load_bank(BANKS_DIR / "emc_peru_ai_tutor.yaml")
    assert bank.bank_id == "emc_peru_ai_tutor"
    assert all(e.source.framework == "emc_peru_ai_tutor_v1" for e in bank.entries)
    assert all(
        "AI_Tutor_Assessment_Framework_Complete.docx" in e.source.location
        for e in bank.entries
    )


def test_emc_scored_dimensions_sum_to_the_hundred_the_source_declares():
    """Part 8 dice "Total: 100 points". Si la transcripción no suma 100, se copió mal."""
    bank = load_bank(BANKS_DIR / "emc_peru_ai_tutor.yaml")
    scored = [e for e in bank.entries if e.source.original_id.startswith("8.")]
    assert len(scored) == 6
    assert sum(e.points for e in scored) == 100


def test_emc_unscored_entry_is_flagged_as_unscored_by_the_source():
    """6.1 es un test real del marco que Part 8 no puntúa. El peso es nuestro, y se dice."""
    entries = {e["id"]: e for e in _raw_entries("emc_peru_ai_tutor.yaml")}
    unscored = [e for e in entries.values() if e.get("unscored_in_source")]
    assert unscored, "ningún criterio declara que la fuente no le asigna puntaje"
    for e in unscored:
        assert "no le asigna puntaje" in e["evidence_base"] or (
            "no asigna puntaje" in e["evidence_base"]
        ), f"{e['id']}: el peso propio no está declarado en evidence_base"


# --- idioma operativo: las anclas que lee el juez estan en espanol -----------

#: Entradas cuyo texto en ingles ES la fuente citable y no debe traducirse.
#: PT-D12-01 cita terminologia oficial del WWC ("Without Reservations",
#: "overlap setting AND samples"): traducir el estandar romperia la cita.
ENGLISH_BY_DESIGN = {"PT-D12-01"}


def _english_heuristic():
    """La MISMA heuristica del miner (scripts/mine_verdicts.py), importada para
    que test y reporte no puedan divergir."""
    import sys

    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from mine_verdicts import is_english_anchor

    return is_english_anchor


@pytest.mark.needs_real_platform
def test_operative_bank_text_is_in_spanish():
    """330 veredictos pagados mostraron al juez citando anclas en ingles como
    defecto -- con razon: es una herramienta para el país. El texto
    OPERATIVO (lo que el juez lee) tiene que estar en espanol; el original se
    conserva aparte (test siguiente), no en el campo operativo."""
    is_english = _english_heuristic()
    data = yaml.safe_load((BANKS_DIR / "lxp_sv_v7_4.yaml").read_text(encoding="utf-8"))

    flagged = []
    for e in data["entries"]:
        if e["id"] in ENGLISH_BY_DESIGN:
            continue
        for field in ("pass_criteria", "fail_criteria", "verification", "name"):
            if is_english(e.get(field, "")):
                flagged.append(f'{e["id"]}.{field}')
    assert flagged == [], f"texto operativo en ingles: {flagged}"


def test_translated_entries_preserve_the_original_verbatim():
    """La traduccion es un artefacto DECLARADO, no un silencio: el banco cita el
    XLSX como fuente, asi que un auditor que vaya a la fuente encontraria otro
    texto. Cada entrada traducida conserva el original palabra por palabra en
    `original_en`, y el operativo tiene que diferir de el (si son iguales, no
    hubo traduccion)."""
    data = yaml.safe_load((BANKS_DIR / "lxp_sv_v7_4.yaml").read_text(encoding="utf-8"))
    translated = [e for e in data["entries"] if e.get("original_en")]

    assert translated, "las 7 entradas en ingles tienen que declarar su original"
    for e in translated:
        for field, original in e["original_en"].items():
            assert original.strip(), f'{e["id"]}.original_en.{field} vacio'
            assert e.get(field, "") != original, (
                f'{e["id"]}.{field}: el operativo es identico al original -- '
                "no se tradujo"
            )


def test_the_real_loader_reads_the_translated_bank():
    """`original_en` es una clave extra que el lector ignora; lo operativo que
    llega al juez es el espanol. Se fija con el lector REAL, no leyendo YAML."""
    bank = load_bank(BANKS_DIR / "lxp_sv_v7_4.yaml")
    e = {x.id: x for x in bank.entries}["PT-D9-H1"]
    assert "no hemos hablado" in e.pass_criteria.lower()
    assert "recuerdo" in e.fail_criteria.lower() or "memoria" in e.fail_criteria.lower()
