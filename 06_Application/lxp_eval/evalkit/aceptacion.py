"""La aceptación de la lectura congelada: pre-registrada, contra veredictos de personas.

POR QUÉ EXISTE. Congelar la lectura de la regla hace que el juez repita su
veredicto casi por construcción: si siempre lee la regla igual, vuelca menos.
Eso NO dice que la lectura congelada sea la correcta. Una lectura nítida y
equivocada hace que todos los juicios coincidan, y medida por acuerdo parece un
éxito. Lo único que distingue "más reproducible" de "mejor" es comparar con lo
que deciden personas mirando la misma evidencia.

Y hay que decidir cómo se va a medir ANTES de mirar un número. El 2026-11-27 se
midió el piso de ruido del juez sobre una muestra de criterios elegida porque
volcaban, y la baja que apareció después de un arreglo no se pudo atribuir al
arreglo: un criterio elegido por volcar tiende a volcar menos la vez siguiente
aunque nada cambie. Por eso acá el conjunto reservado se sortea y se hashea
antes, nunca se usa para reescribir lecturas, y se mide una sola vez.

QUÉ HAY Y QUÉ NO. Esto es el ARNÉS: sortea y bloquea el conjunto reservado,
calcula el acuerdo entre personas (el techo), la validez de cada brazo contra
las personas y el test-retest, y dice si la regla pre-registrada se cumple. No
corre ningún juicio, no llama al modelo y NO ENCIENDE NADA: el contrato de la
lectura congelada se enciende a mano (`Engine.frozen_readings`), por una
decisión de su dueño. Las etiquetas de las personas no existen todavía.

Lo que se reporta, siempre separado y por criterio:

* el acuerdo persona-persona, PRIMERO: es el techo. Si dos personas no
  coinciden, ningún juez puede "acertar";
* la validez: el juez contra las personas, por brazo. ES EL ÚNICO CRÉDITO;
* el test-retest sobre evidencia congelada: sube casi por construcción con la
  lectura congelada, así que se muestra y NO cuenta como crédito;
* la cobertura: cuántos criterios se puntuaron. Una mejora que viene de dejar
  de responder no es una mejora.

Las medidas de acuerdo son alfa de Krippendorff ordinal (corregida por azar),
nunca el porcentaje de acuerdo crudo. NO_EVALUADO no está en la escala
NO_CUMPLE < PARCIAL < CUMPLE: entra como dato faltante y se cuenta aparte, en
la cobertura.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from .errors import EvalkitError
from .scoring import CUMPLE, NO_CUMPLE, NO_EVALUADO, PARCIAL

#: La escala, de menos a más. NO_EVALUADO no está: no es "menos que NO_CUMPLE".
SCALE = (NO_CUMPLE, PARCIAL, CUMPLE)

#: Los dos brazos que se comparan, sobre el MISMO conjunto reservado y en la
#: misma sesión. "Antes" es el comportamiento de hoy con el defecto de la
#: lectura vacía ya arreglado (lectura generada al puntuar), no el histórico.
ARM_BEFORE = "lectura_en_vivo"
ARM_AFTER = "lectura_congelada"

#: Los valores por defecto de la pre-registración (decididos el 2026-11-27).
DEFAULTS = {
    "fraccion_reservada": 1 / 3,
    "etiquetadores_minimos": 2,
    "criterios_reservados_minimos": 10,
    "rondas_de_reescritura_maximas": 2,
}


class AcceptanceError(EvalkitError):
    """La aceptación no se puede medir así: falta algo que se había comprometido de antemano."""


# --- el alfa de Krippendorff ---------------------------------------------------


def krippendorff_alpha(units: Sequence[Sequence], *, metric: str = "ordinal",
                       order: Sequence | None = None) -> float | None:
    """Alfa de Krippendorff por matriz de coincidencias. None si no se puede calcular.

    `units` es una lista de unidades; cada unidad, la lista de valores que le
    dieron los codificadores (None = no dio valor). `metric` es "ordinal" o
    "nominal"; con "ordinal", `order` es la escala de menos a más.

    Verificada contra el ejemplo publicado por Krippendorff (2011), cuatro
    observadores y doce unidades: nominal 0,743 y ordinal 0,815 (ver los tests).
    Devuelve None cuando no hay desacuerdo esperado (todos dieron siempre el
    mismo valor, o hay menos de dos valores comparables): ahí alfa no está
    definida, y decir "1" sería afirmar un acuerdo que no se midió.
    """
    clean = [[v for v in u if v is not None] for u in units]
    cats = list(order) if order is not None else sorted({v for u in clean for v in u}, key=str)
    index = {c: i for i, c in enumerate(cats)}
    k = len(cats)
    o = [[0.0] * k for _ in range(k)]
    for u in clean:
        m = len(u)
        if m < 2:
            continue
        count = Counter(u)
        for a in count:
            for b in count:
                pairs = count[a] * (count[b] - (1 if a == b else 0))
                if pairs:
                    o[index[a]][index[b]] += pairs / (m - 1)
    n_c = [sum(row) for row in o]
    n = sum(n_c)
    if n <= 1:
        return None

    def delta2(a: int, b: int) -> float:
        if a == b:
            return 0.0
        if metric == "nominal":
            return 1.0
        lo, hi = min(a, b), max(a, b)
        return (sum(n_c[lo:hi + 1]) - (n_c[lo] + n_c[hi]) / 2.0) ** 2

    observed = sum(o[a][b] * delta2(a, b) for a in range(k) for b in range(k)) / n
    expected = sum(n_c[a] * n_c[b] * delta2(a, b) for a in range(k) for b in range(k)) / (n * (n - 1))
    if expected <= 0:
        return None
    return 1.0 - observed / expected


def _on_scale(verdict: str | None) -> str | None:
    """El veredicto si está en la escala; None (faltante) si es NO_EVALUADO o no hay."""
    return verdict if verdict in SCALE else None


# --- la pre-registración -------------------------------------------------------


def _sha(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def split(criteria: Iterable[str], *, seed: str, fraction: float = DEFAULTS["fraccion_reservada"]) -> dict:
    """Sortea el conjunto reservado. Determinista: la misma semilla y los mismos criterios dan lo mismo.

    Cada criterio se ordena por el hash de (semilla, id) y se reserva la
    fracción pedida, sin mirar nada del criterio ni de sus resultados. Lo que
    queda es el conjunto de ajuste, el único sobre el que se pueden reescribir
    lecturas.
    """
    ids = sorted({str(c) for c in criteria})
    if not ids:
        raise AcceptanceError("no hay criterios para sortear")
    ranked = sorted(ids, key=lambda c: _sha([seed, c]))
    take = max(1, round(len(ids) * fraction))
    held = sorted(ranked[:take])
    return {"reservado": held, "ajuste": sorted(set(ids) - set(held))}


def preregister(
    criteria: Iterable[str], *, seed: str, noise_floor: float, noise_floor_source: str,
    registered_at: str, fraction: float = DEFAULTS["fraccion_reservada"],
    min_labelers: int = DEFAULTS["etiquetadores_minimos"],
    min_held_out: int = DEFAULTS["criterios_reservados_minimos"],
    max_rounds: int = DEFAULTS["rondas_de_reescritura_maximas"],
) -> dict:
    """El compromiso, escrito antes de mirar un número. Devuelve el registro, con su huella.

    `noise_floor` es el piso de ruido en las unidades de la medida de validez
    (alfa ordinal): cuánto se mueve esa medida por volver a juzgar la misma
    evidencia sin cambiar nada. Tiene que salir de una MEDICIÓN, y
    `noise_floor_source` dice de cuál. No hay valor por defecto: un piso
    supuesto es justo lo que la pre-registración existe para evitar.

    La regla que queda escrita: la validez del brazo de la lectura congelada
    supera a la del brazo de la lectura en vivo por al menos el piso, y la
    cobertura no cae. El conjunto reservado se mide una sola vez.
    """
    if not str(noise_floor_source or "").strip():
        raise AcceptanceError("el piso de ruido necesita decir de qué medición sale")
    if not 0 <= float(noise_floor) < 2:
        raise AcceptanceError("el piso de ruido va en unidades de alfa: entre 0 y 2")
    parts = split(criteria, seed=seed, fraction=fraction)
    if len(parts["reservado"]) < min_held_out:
        raise AcceptanceError(
            f"el conjunto reservado tendría {len(parts['reservado'])} criterios y el mínimo "
            f"comprometido es {min_held_out}: con menos, la medida no distingue nada"
        )
    record = {
        "version": 1,
        "registrado_en": registered_at,
        "semilla": seed,
        "reservado": parts["reservado"],
        "ajuste": parts["ajuste"],
        "hash_reservado": _sha(parts["reservado"]),
        "brazos": {"antes": ARM_BEFORE, "despues": ARM_AFTER},
        "medida": "alfa de Krippendorff ordinal, juez contra personas, sobre el conjunto reservado",
        "piso_de_ruido": float(noise_floor),
        "piso_de_ruido_fuente": str(noise_floor_source).strip(),
        "etiquetadores_minimos": int(min_labelers),
        "criterios_reservados_minimos": int(min_held_out),
        "rondas_de_reescritura_maximas": int(max_rounds),
        "regla": (
            "se enciende sólo si la validez del brazo de la lectura congelada supera a la "
            "del brazo de la lectura en vivo por al menos el piso de ruido, la cobertura no "
            "cae, y el conjunto reservado se midió una sola vez"
        ),
    }
    record["hash_registro"] = _sha(record)
    return record


def write_preregistration(path: Path | str, record: dict) -> Path:
    """Guarda la pre-registración. Se niega a pisar una que ya existe."""
    path = Path(path)
    if path.exists():
        raise AcceptanceError(
            f"ya hay una pre-registración en {path}: no se reescribe. Cambiarla después "
            "de registrada es elegir la regla mirando el resultado"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_preregistration(path: Path | str) -> dict:
    """Lee la pre-registración y comprueba que nadie la tocó después de registrarla."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    body = {k: v for k, v in record.items() if k != "hash_registro"}
    if _sha(body) != record.get("hash_registro") or _sha(record.get("reservado")) != record.get("hash_reservado"):
        raise AcceptanceError(
            "la pre-registración no coincide con su huella: fue cambiada después de registrada"
        )
    return record


def labeling_sheet(record: dict) -> list[dict]:
    """La planilla que llena cada persona: los criterios reservados, y NADA del juez.

    A ciegas del brazo: quien etiqueta no ve qué dijo el juez con la lectura en
    vivo ni con la congelada. Sólo el criterio y un lugar para su veredicto.
    """
    return [{"criterio": c, "veredicto": "", "nota": ""} for c in record["reservado"]]


# --- la medición ----------------------------------------------------------------


def _alpha_between(a: dict[str, str], b: dict[str, str], criteria: Sequence[str]) -> float | None:
    return krippendorff_alpha(
        [[_on_scale(a.get(c)), _on_scale(b.get(c))] for c in criteria], order=SCALE,
    )


def _validity(arm: dict[str, str], humans: Sequence[dict[str, str]], criteria: Sequence[str]) -> float | None:
    """El juez contra las personas: el promedio de su alfa con cada una."""
    values = [_alpha_between(arm, h, criteria) for h in humans]
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _coverage(arm: dict[str, str], criteria: Sequence[str]) -> int:
    return sum(1 for c in criteria if arm.get(c) in SCALE)


def evaluate(
    record: dict, *, humans: Sequence[dict[str, str]], before: dict[str, str], after: dict[str, str],
    retest: dict[str, Sequence[dict[str, str]]] | None = None, rewrite_rounds: int = 0,
    already_measured: bool = False,
) -> dict:
    """La aceptación, medida contra lo pre-registrado. Devuelve el informe; no enciende nada.

    `humans` son los veredictos de cada persona (criterio -> veredicto);
    `before` y `after`, los del juez en cada brazo; `retest`, opcional, las
    repeticiones de cada brazo sobre la misma evidencia ({brazo: [corridas]}).

    Se niega a medir (`AcceptanceError`) si falta algo comprometido: menos
    personas que las registradas, criterios reservados sin etiquetar, más
    rondas de reescritura que las permitidas, o si el conjunto reservado ya se
    midió. `cumple_la_regla` es True, False, o None cuando la medida no se pudo
    calcular (por ejemplo, todos los veredictos iguales): None no es un sí.
    """
    held = list(record["reservado"])
    if _sha(held) != record.get("hash_reservado"):
        raise AcceptanceError("el conjunto reservado no es el registrado")
    if already_measured:
        raise AcceptanceError(
            "el conjunto reservado ya se midió una vez: volver a medirlo después de "
            "ver el resultado es elegir el resultado"
        )
    if len(humans) < int(record["etiquetadores_minimos"]):
        raise AcceptanceError(
            f"hay {len(humans)} persona(s) etiquetando y se comprometieron "
            f"{record['etiquetadores_minimos']}: con una sola no hay techo que medir"
        )
    if rewrite_rounds > int(record["rondas_de_reescritura_maximas"]):
        raise AcceptanceError(
            f"hubo {rewrite_rounds} rondas de reescritura y el máximo registrado es "
            f"{record['rondas_de_reescritura_maximas']}"
        )
    for i, labels in enumerate(humans, start=1):
        missing = [c for c in held if not str(labels.get(c) or "").strip()]
        if missing:
            raise AcceptanceError(
                f"a la persona {i} le faltan {len(missing)} de {len(held)} criterios "
                "reservados: " + ", ".join(missing[:5])
            )

    ceiling = krippendorff_alpha(
        [[_on_scale(h.get(c)) for h in humans] for c in held], order=SCALE,
    )
    v_before, v_after = _validity(before, humans, held), _validity(after, humans, held)
    c_before, c_after = _coverage(before, held), _coverage(after, held)
    floor = float(record["piso_de_ruido"])
    gain = None if v_before is None or v_after is None else v_after - v_before

    stability = {}
    for arm, runs in (retest or {}).items():
        stability[arm] = krippendorff_alpha(
            [[_on_scale(r.get(c)) for r in runs] for c in held], order=SCALE,
        )

    reasons = []
    if gain is None:
        ok = None
        reasons.append("la validez no se pudo calcular en al menos un brazo: no hay con qué decidir")
    else:
        ok = gain >= floor and c_after >= c_before
        if gain < floor:
            reasons.append(
                f"la validez sube {gain:+.3f} y el piso de ruido registrado es {floor:.3f}: "
                "la diferencia no se distingue de volver a juzgar"
            )
        if c_after < c_before:
            reasons.append(
                f"la cobertura cae: {c_after} de {len(held)} criterios puntuados contra "
                f"{c_before} de {len(held)}"
            )
        if ok:
            reasons.append(
                f"la validez sube {gain:+.3f}, por encima del piso de ruido ({floor:.3f}), "
                f"y la cobertura no cae ({c_after} de {len(held)} contra {c_before} de {len(held)})"
            )

    def per_criterion(c: str) -> dict:
        return {
            "criterio": c,
            "personas": [h.get(c) for h in humans],
            "personas_coinciden": len({h.get(c) for h in humans}) == 1,
            ARM_BEFORE: before.get(c),
            ARM_AFTER: after.get(c),
        }

    return {
        "hash_registro": record.get("hash_registro"),
        "criterios_reservados": len(held),
        # PRIMERO el techo: lo que coinciden las personas entre sí.
        "acuerdo_entre_personas": ceiling,
        "personas_que_coinciden": f"{sum(1 for c in held if len({h.get(c) for h in humans}) == 1)} de {len(held)}",
        # El único crédito.
        "validez": {ARM_BEFORE: v_before, ARM_AFTER: v_after, "diferencia": gain},
        "piso_de_ruido": floor,
        "cobertura": {
            ARM_BEFORE: f"{c_before} de {len(held)}", ARM_AFTER: f"{c_after} de {len(held)}",
        },
        # Se muestra y NO es crédito: con la lectura congelada sube casi por construcción.
        "test_retest_no_es_credito": stability,
        "por_criterio": [per_criterion(c) for c in held],
        "cumple_la_regla": ok,
        "motivos": reasons,
        "nota": (
            "Este informe no enciende nada. El contrato de la lectura congelada se "
            "enciende a mano, por decisión de su dueño."
        ),
    }
