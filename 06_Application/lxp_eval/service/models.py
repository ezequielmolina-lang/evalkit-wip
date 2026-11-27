"""Request models. Validation errors are Spanish, like the rest of evalkit.

Only the REQUEST side is modelled with pydantic. The response side is built by
hand in `app.py` from an explicit whitelist of fields, because the property that
matters there -- a credential cannot appear in a response -- is better served by
"the serialiser can only emit these keys" than by "the model happens not to
declare that one".
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class PlatformIn(BaseModel):
    """The platform under evaluation, and (optionally) a test account for it.

    `username` / `password` are accepted here and NEVER stored on the job
    record: `app.py` encrypts them into their own file and keeps a boolean.
    """

    url: str
    username: str | None = None
    password: str | None = None
    kind: str | None = None

    @field_validator("url")
    @classmethod
    def _url_present(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError(
                "falta la URL de la plataforma - sin URL no hay nada que recorrer"
            )
        return v.strip()


class DocumentIn(BaseModel):
    """An uploaded norm, tender or rubric, already extracted to text."""

    name: str
    text: str


class StartJobIn(BaseModel):
    objective: str
    areas: list[str] = Field(default_factory=list)
    platform: PlatformIn
    documents: list[DocumentIn] = Field(default_factory=list)
    #: Quién aprueba el plan. "human" (el default) = comportamiento de hoy: el
    #: trabajo parquea en awaiting_approval hasta POST /approve. "auto" lo aprueba
    #: solo y corre las pruebas SIN compuerta; las de compuerta quedan sin firmar
    #: y vuelven NO_EVALUADO (la firma es por prueba, no por plan). Los valores
    #: viven acá en texto para no crear un ciclo de importación con `batch`.
    approval_policy: str = "human"

    @field_validator("objective")
    @classmethod
    def _objective_present(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError(
                "el objetivo está vacío - sin objetivo no hay contra qué "
                "seleccionar criterios"
            )
        return v.strip()

    @field_validator("approval_policy")
    @classmethod
    def _policy_valid(cls, v: str) -> str:
        v = (v or "human").strip() or "human"
        if v not in ("human", "auto"):
            raise ValueError(
                f'approval_policy "{v}" no existe: usá "human" (parquea para '
                'revisión) o "auto" (aprueba y corre lo que no tiene compuerta)'
            )
        return v


class ApiKeyIn(BaseModel):
    """The user's own Anthropic key, on its way to encrypted storage.

    Only the presence of a string is modelled here. What a valid key LOOKS like
    is `settings.validate_api_key`, so the CLI, the store and the endpoint all
    refuse the same values with the same Spanish message, and there is one place
    to change when Anthropic changes the prefix.
    """

    apiKey: str = ""  # noqa: N815 -- the browser's shape; the API speaks camelCase


class ApproveIn(BaseModel):
    """The reviewed plan: edited criteria, edited probes, and the sampling plan.

    Deliberately untyped (`dict`) below the top level. The plan is the engine's
    own shape round-tripped through a browser, and a second, stricter schema
    here would drift from `serialize.py` and start rejecting valid edits.
    """

    plan: dict = Field(default_factory=dict)


class ReadingEditIn(BaseModel):
    """La corrección de la lectura de la regla de un criterio: quién, y qué cambia o qué muestra elige."""

    by: str = ""
    #: campo de la lectura -> texto nuevo (None borra un campo opcional). `Any`
    #: a propósito: lo que no es texto lo rechaza el motor, con su motivo.
    changes: dict[str, Any] | None = None
    #: Cuál de las muestras vale (desde 1), en vez de corregir campos.
    sample: int | None = None


class SignoffIn(BaseModel):
    probe_id: str

    @field_validator("probe_id")
    @classmethod
    def _present(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("falta el probe_id que se está firmando")
        return v.strip()
