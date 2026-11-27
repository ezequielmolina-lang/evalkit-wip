"""Pre-authored probes, completed by a platform's surface map.

WHY THIS EXISTS

The criteria bank carries `pass_criteria`, `fail_criteria` and a vague
`verification` line ("Audit 5 OVAs"). The concrete steps were invented by the
drafter plus a five-judge council on every single run -- expensive, slow, and
on 2026-07-20 demonstrably wrong: the generated steps assumed a UI that did not
exist, mixed browser actions with preconditions no agent can establish, and
included notes addressed to a human evaluator ("antes de correr el probe,
anotar el grado real"). All eight probes of that run had to be rewritten by
hand against the live platform.

THE SPLIT THIS MODULE ENFORCES

    stimulus   belongs to the CRITERION and travels between platforms.
               "89+47=126" measures the same thing on Plataforma A as on PLATAFORMA_B.
    surface    belongs to the PLATFORM. A criterion says "I need the tutor
               chat"; the platform says how to get there.

Writing them together is what made me hand-copy the same three navigation steps
into eight probes. A UI change should cost one file, not eight.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import EvalkitError


class SurfaceUnavailable(EvalkitError):
    """The account cannot reach what this criterion needs.

    Raised BEFORE anything runs. Declaring the gap is the point: discovering it
    mid-probe costs a paid run, and quietly substituting a different surface
    scores the criterion against something it was never about.
    """


def load_surfaces(path: Path | str) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        "surfaces": data.get("surfaces") or {},
        "unavailable": data.get("unavailable") or {},
        # La descripción de la plataforma que hizo el recorrido, o None. Un mapa
        # hecho a mano no la trae y sigue cargando igual.
        "profile": data.get("profile") or None,
    }


class ProbeBank:
    """Probes written by hand, keyed by criterion id."""

    def __init__(self, data: dict) -> None:
        self.bank_id = data.get("bank_id", "")
        self.targets = data.get("targets", "")
        self.entries: dict[str, dict] = {
            e["criterion"]: e for e in (data.get("entries") or []) if e.get("criterion")
        }

    @classmethod
    def load(cls, path: Path | str) -> "ProbeBank":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def entry(self, criterion_id: str) -> dict:
        try:
            return self.entries[criterion_id]
        except KeyError:
            raise EvalkitError(
                f"no hay una probe escrita para {criterion_id} en {self.bank_id}"
            ) from None

    def steps_for(self, criterion_id: str, surfaces: dict) -> list[str]:
        """Executable prose steps: navigate, send each stimulus, read each answer.

        Every returned step is something a browser agent can carry out. No
        preconditions it cannot establish, no instructions meant for a person --
        those belong in the report, not in a probe.
        """
        e = self.entry(criterion_id)

        grade = e.get("requires_grade")
        if grade is not None:
            why = (surfaces.get("unavailable") or {}).get(f"grade_{grade}_course")
            if why:
                raise SurfaceUnavailable(
                    f"{criterion_id} pide {grade}.o grado: {why.strip()}"
                )

        name = e.get("surface") or ""
        surface = (surfaces.get("surfaces") or {}).get(name)
        if surface is None:
            why = (surfaces.get("unavailable") or {}).get(name)
            raise SurfaceUnavailable(
                f"{criterion_id} necesita la superficie '{name}': "
                + (why.strip() if why else "esta plataforma no la declara")
            )

        fill = dict(surface.get("defaults") or {})
        steps = [_fmt(s, fill) for s in (surface.get("steps") or [])]

        stimuli = e.get("stimulus_sequence") or (
            [e["stimulus"]] if e.get("stimulus") else []
        )
        if not stimuli:
            # A read-only probe: navigate, then read the surface once. Nothing
            # is sent, so the per-stimulus loop below never runs and the read
            # has to happen here or the probe captures nothing.
            steps.extend(_fmt(t, fill) for t in (surface.get("read") or []))
            return steps
        for s in stimuli:
            for tpl in surface.get("send") or []:
                steps.append(_fmt(tpl, {**fill, "stimulus": s}))
            steps.extend(_fmt(t, fill) for t in (surface.get("read") or []))
        return steps


def _fmt(template: str, values: dict) -> str:
    """Fill {placeholders}, leaving unknown ones intact rather than raising.

    A surface file is authored by hand; a missing key should degrade to a
    visible `{course}` in the step, not blow up a run at capture time.
    """
    out = " ".join(str(template).split())
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out
