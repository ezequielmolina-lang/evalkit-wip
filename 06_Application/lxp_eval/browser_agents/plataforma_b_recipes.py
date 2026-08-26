"""PLATAFORMA_B / B-Nacional-specific browser recipes.

B-Nacional is the el país deployment of the PLATAFORMA_B platform, served from
`https://app.plataforma-b.ejemplo/`. These recipes encode the small handful of
platform-specific flows our agents need to bootstrap into a lesson before
handing control over to Claude's reasoning loop:

1. Login as student or teacher
2. Navigate to a specific class by subject + grade
3. Open a specific lesson within a class

The recipes use deliberately resilient selectors (text + role + ARIA), and
each step records what worked so failures can be diagnosed against the live
DOM rather than guessed at.

Observed UI patterns (from REAL_obs_2026-05-22_b_nacional_student.md):
- Login form has email + password fields and a "Iniciar sesión" submit button.
- After login, student lands on a class-list view showing up to 84 classes
  split across "Clase de Matemática 3°", "Clase de Lenguaje 3°", "Refuerzo
  Matemática 3°", "Remediación Matemática 3°", etc.
- Each class card has a title we can match by text.
- Inside a class, lessons are listed with codes like "1.2 Escritura y lectura
  de unidades de millar" — these match the CNEB curriculum codes.
- A lesson has a left-side stepper sidebar with sections: "Aprendizaje de
  hoy", "Repaso y anticipación", "Comprensión", "Ejercitación modelada y
  autónoma", and a bottom-right "Tutor IA" floating button.

This module is intentionally read-only with respect to the platform: the
default flow stops after opening a lesson. Submitting an answer is left to
the higher-level agent which respects the --allow-submit gate.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from .playwright_driver import PlaywrightDriver

logger = logging.getLogger(__name__)


PLATAFORMA_B_URL = "https://app.plataforma-b.ejemplo/"

# Subjects we expect on the SV deployment. Map our lowercase agent slugs to
# the Spanish strings actually displayed in the UI.
SUBJECT_LABELS = {
    "math": ["Matemática", "Matematica", "Matemáticas"],
    "language": ["Lenguaje", "Lengua", "Comunicación"],
}

# Grade label patterns. B-Nacional uses "3°" with the ordinal symbol.
GRADE_LABELS = {
    1: ["1°", "1ro", "Primer"],
    2: ["2°", "2do", "Segundo"],
    3: ["3°", "3ro", "Tercer"],
    4: ["4°", "4to", "Cuarto"],
    5: ["5°", "5to", "Quinto"],
    6: ["6°", "6to", "Sexto"],
    7: ["7°", "7mo", "Séptimo"],
    8: ["8°", "8vo", "Octavo"],
    9: ["9°", "9no", "Noveno"],
}


@dataclass
class Plataforma BCredentials:
    email: str
    password: str
    role: Literal["student", "teacher"]

    @classmethod
    def from_env(cls, role: Literal["student", "teacher"] = "student") -> "Plataforma BCredentials":
        if role == "student":
            email = os.getenv("PLATAFORMA_B_STUDENT_EMAIL", "")
            password = os.getenv("PLATAFORMA_B_STUDENT_PASSWORD", "")
        else:
            email = os.getenv("PLATAFORMA_B_TEACHER_EMAIL", "")
            password = os.getenv("PLATAFORMA_B_TEACHER_PASSWORD", "")
        if not email or not password:
            raise RuntimeError(
                f"PLATAFORMA_B {role} credentials missing — set PLATAFORMA_B_{role.upper()}_EMAIL and "
                f"PLATAFORMA_B_{role.upper()}_PASSWORD in .env"
            )
        return cls(email=email, password=password, role=role)


async def login(driver: PlaywrightDriver, creds: Plataforma BCredentials) -> bool:
    """Log into B-Nacional. Returns True if we appear to land on the post-login view.

    Strategy: navigate to the root URL, look for an email field by label /
    placeholder / common selectors, type both credentials, submit, then
    confirm by waiting for either a URL change away from /login or for class
    cards / a "Cerrar sesión" link to appear.
    """
    logger.info("PLATAFORMA_B login as %s (%s)", creds.role, creds.email)
    await driver.navigate(PLATAFORMA_B_URL)
    await driver.screenshot("plataforma_b_landing")

    # The landing page on app.plataforma-b.ejemplo usually shows a login form
    # directly, but if there's an "Iniciar sesión" button we click it first.
    await driver.click_element_description("Iniciar sesión")

    # Try a stack of strategies for the email field
    email_ok = (
        await driver.fill_by_label("Correo electrónico", creds.email)
        or await driver.fill_by_label("Email", creds.email)
        or await driver.fill_by_placeholder("Correo", creds.email)
        or await driver.fill_by_placeholder("Email", creds.email)
        or await driver.fill_by_selector("input[type='email']", creds.email)
        or await driver.fill_by_selector("input[name='email']", creds.email)
        or await driver.fill_by_selector("input[name='username']", creds.email)
    )
    if not email_ok:
        logger.error("Could not locate email field")
        await driver.screenshot("plataforma_b_login_email_fail")
        return False

    pwd_ok = (
        await driver.fill_by_label("Contraseña", creds.password)
        or await driver.fill_by_label("Password", creds.password)
        or await driver.fill_by_placeholder("Contraseña", creds.password)
        or await driver.fill_by_selector("input[type='password']", creds.password)
    )
    if not pwd_ok:
        logger.error("Could not locate password field")
        await driver.screenshot("plataforma_b_login_password_fail")
        return False

    # Submit
    submitted = (
        await driver.click_by_role("button", name="Iniciar sesión")
        or await driver.click_by_role("button", name="Ingresar")
        or await driver.click_by_role("button", name="Entrar")
        or await driver.click_by_text("Iniciar sesión")
    )
    if not submitted:
        # Fall back to pressing Enter from the password field
        await driver.press_key("Enter")

    await driver.wait_for_load("networkidle", timeout_ms=20_000)
    await driver.screenshot("plataforma_b_post_login")

    # Heuristic success check: URL no longer contains 'login', or page text
    # contains class-related Spanish keywords.
    url = await driver.get_url()
    if "login" not in url.lower():
        logger.info("PLATAFORMA_B login appears successful (url=%s)", url)
        return True
    text = await driver.read_page(max_chars=2000)
    if any(kw in text for kw in ("Mis clases", "Cerrar sesión", "Clase de", "Matemática", "Lenguaje")):
        logger.info("PLATAFORMA_B login appears successful (text heuristic)")
        return True

    logger.error("PLATAFORMA_B login did not appear to succeed; url=%s", url)
    return False


async def open_class(
    driver: PlaywrightDriver,
    *,
    subject: str,
    grade: int,
    track: Literal["regular", "refuerzo", "remediacion"] = "regular",
) -> bool:
    """Open the class card matching subject + grade + (optional) track.

    Looks for a card whose title contains BOTH a subject label and a grade
    label. e.g. "Clase de Matemática 3°" for (math, 3, regular), or
    "Refuerzo Matemática 3°" for the refuerzo track.
    """
    subject_labels = SUBJECT_LABELS.get(subject.lower())
    if not subject_labels:
        logger.error("Unknown subject %r", subject)
        return False
    grade_labels = GRADE_LABELS.get(int(grade), [])
    if not grade_labels:
        logger.error("Unknown grade %r", grade)
        return False

    track_prefix = {"regular": "Clase", "refuerzo": "Refuerzo", "remediacion": "Remediación"}[track]

    # Try a few likely card-title combinations
    candidates: list[str] = []
    for s in subject_labels:
        for g in grade_labels:
            if track == "regular":
                candidates.append(f"{track_prefix} de {s} {g}")
                candidates.append(f"{s} {g}")
            else:
                candidates.append(f"{track_prefix} {s} {g}")

    for candidate in candidates:
        logger.info("Trying to open class card: %r", candidate)
        if await driver.click_by_text(candidate, exact=False):
            await driver.wait_for_load("networkidle", timeout_ms=15_000)
            await driver.screenshot(f"plataforma_b_class_{subject}_{grade}_{track}")
            return True

    logger.error("Could not find a class card matching subject=%s grade=%s track=%s", subject, grade, track)
    await driver.screenshot(f"plataforma_b_class_NOTFOUND_{subject}_{grade}_{track}")
    return False


async def open_lesson(driver: PlaywrightDriver, lesson_code: str) -> bool:
    """Open a lesson by its CNEB code like '1.2' or '1.4'.

    The class view lists lessons with their code as a prefix; we match by
    text. If the code isn't found verbatim, we try a "Lección {code}"
    variant.
    """
    for candidate in (lesson_code, f"Lección {lesson_code}", f"Leccion {lesson_code}"):
        if await driver.click_by_text(candidate, exact=False):
            await driver.wait_for_load("networkidle", timeout_ms=15_000)
            await driver.screenshot(f"plataforma_b_lesson_{lesson_code}")
            return True
    logger.error("Could not find lesson with code %r", lesson_code)
    await driver.screenshot(f"plataforma_b_lesson_NOTFOUND_{lesson_code}")
    return False


async def open_tutor_ia(driver: PlaywrightDriver) -> bool:
    """Open the floating 'Tutor IA' panel in the bottom-right corner."""
    for label in ("Tutor IA", "Tutor", "Asistente IA", "AI Tutor"):
        if await driver.click_element_description(label):
            await driver.screenshot("plataforma_b_tutor_ia_open")
            return True
    return False
