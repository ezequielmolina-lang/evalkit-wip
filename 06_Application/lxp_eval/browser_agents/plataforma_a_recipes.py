"""Plataforma A-specific browser recipes.

Plataforma A's student / teacher app lives at `https://app.plataforma-a.ejemplo/login/`.
This module mirrors `plataforma_b_recipes.py`: login, navigate to a class, open a
lesson, optionally open the AI tutor.

Plataforma A uses English-language UI by default but content is bilingual in the SV
deployment. Selectors here try English first, then Spanish fallbacks.

Compared to B-Nacional, Plataforma A's notable differences:
- Login is at a separate /login/ URL
- The post-login dashboard is called "My Classes" / "Mis clases"
- Lessons are organized into "OVAs" (Objetos Virtuales de Aprendizaje) for
  the SV deployment
- The AI tutor in Plataforma A is called "Plataforma A" (the brand name) and is the primary
  surface — not a floating button
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from .playwright_driver import PlaywrightDriver

logger = logging.getLogger(__name__)


PLATAFORMA_A_LOGIN_URL = "https://app.plataforma-a.ejemplo/login/"
PLATAFORMA_A_HOME_URL = "https://app.plataforma-a.ejemplo/"

SUBJECT_LABELS = {
    "math": ["Math", "Mathematics", "Matemática", "Matematica"],
    "language": ["Language", "Reading", "Lenguaje", "Lengua"],
}

GRADE_LABELS = {
    1: ["1st", "Grade 1", "1°", "Primer"],
    2: ["2nd", "Grade 2", "2°", "Segundo"],
    3: ["3rd", "Grade 3", "3°", "Tercer"],
    4: ["4th", "Grade 4", "4°", "Cuarto"],
    5: ["5th", "Grade 5", "5°", "Quinto"],
    6: ["6th", "Grade 6", "6°", "Sexto"],
    7: ["7th", "Grade 7", "7°", "Séptimo"],
    8: ["8th", "Grade 8", "8°", "Octavo"],
    9: ["9th", "Grade 9", "9°", "Noveno"],
}


@dataclass
class Plataforma ACredentials:
    email: str
    password: str
    role: Literal["student", "teacher"]

    @classmethod
    def from_env(cls, role: Literal["student", "teacher"] = "teacher") -> "Plataforma ACredentials":
        # NB: BT-2 normally runs as student. Plataforma A's SV accounts are seeded as
        # teacher accounts with a role-toggle to student view, so default
        # here is teacher.
        if role == "student":
            email = os.getenv("PLATAFORMA_A_STUDENT_EMAIL", "")
            password = os.getenv("PLATAFORMA_A_STUDENT_PASSWORD", "")
        else:
            email = os.getenv("PLATAFORMA_A_TEACHER_EMAIL", "")
            password = os.getenv("PLATAFORMA_A_TEACHER_PASSWORD", "")
        if not email or not password:
            raise RuntimeError(
                f"Plataforma A {role} credentials missing — set PLATAFORMA_A_{role.upper()}_EMAIL and "
                f"PLATAFORMA_A_{role.upper()}_PASSWORD in .env"
            )
        return cls(email=email, password=password, role=role)


async def login(driver: PlaywrightDriver, creds: Plataforma ACredentials) -> bool:
    """Log into Plataforma A. Returns True on apparent success."""
    logger.info("Plataforma A login as %s (%s)", creds.role, creds.email)
    await driver.navigate(PLATAFORMA_A_LOGIN_URL)
    await driver.screenshot("plataforma_a_landing")

    email_ok = (
        await driver.fill_by_label("Email", creds.email)
        or await driver.fill_by_label("Email address", creds.email)
        or await driver.fill_by_label("Correo electrónico", creds.email)
        or await driver.fill_by_placeholder("Email", creds.email)
        or await driver.fill_by_selector("input[type='email']", creds.email)
        or await driver.fill_by_selector("input[name='email']", creds.email)
        or await driver.fill_by_selector("input[name='username']", creds.email)
    )
    if not email_ok:
        logger.error("Plataforma A: could not locate email field")
        await driver.screenshot("plataforma_a_login_email_fail")
        return False

    pwd_ok = (
        await driver.fill_by_label("Password", creds.password)
        or await driver.fill_by_label("Contraseña", creds.password)
        or await driver.fill_by_placeholder("Password", creds.password)
        or await driver.fill_by_selector("input[type='password']", creds.password)
    )
    if not pwd_ok:
        logger.error("Plataforma A: could not locate password field")
        await driver.screenshot("plataforma_a_login_password_fail")
        return False

    submitted = (
        await driver.click_by_role("button", name="Sign in")
        or await driver.click_by_role("button", name="Log in")
        or await driver.click_by_role("button", name="Iniciar sesión")
        or await driver.click_by_text("Sign in")
        or await driver.click_by_text("Iniciar sesión")
    )
    if not submitted:
        await driver.press_key("Enter")

    await driver.wait_for_load("networkidle", timeout_ms=20_000)
    await driver.screenshot("plataforma_a_post_login")

    url = await driver.get_url()
    if "login" not in url.lower():
        return True
    text = await driver.read_page(max_chars=2000)
    if any(kw in text for kw in ("My Classes", "Mis clases", "Lesson Studio", "Sign out", "Cerrar sesión")):
        return True
    logger.error("Plataforma A login did not appear to succeed; url=%s", url)
    return False


async def open_class(
    driver: PlaywrightDriver,
    *,
    subject: str,
    grade: int,
) -> bool:
    """Open a class card matching subject + grade."""
    subject_labels = SUBJECT_LABELS.get(subject.lower(), [])
    grade_labels = GRADE_LABELS.get(int(grade), [])
    if not subject_labels or not grade_labels:
        logger.error("Unknown subject/grade combination %s / %s", subject, grade)
        return False

    candidates: list[str] = []
    for s in subject_labels:
        for g in grade_labels:
            candidates.append(f"{s} {g}")
            candidates.append(f"{g} {s}")
            candidates.append(f"{s} - {g}")

    for candidate in candidates:
        logger.info("Trying to open Plataforma A class card: %r", candidate)
        if await driver.click_by_text(candidate, exact=False):
            await driver.wait_for_load("networkidle", timeout_ms=15_000)
            await driver.screenshot(f"plataforma_a_class_{subject}_{grade}")
            return True

    logger.error("Could not find Plataforma A class subject=%s grade=%s", subject, grade)
    await driver.screenshot(f"plataforma_a_class_NOTFOUND_{subject}_{grade}")
    return False


async def open_lesson(driver: PlaywrightDriver, lesson_code: str) -> bool:
    """Open a lesson / OVA by code."""
    for candidate in (lesson_code, f"OVA {lesson_code}", f"Lesson {lesson_code}", f"Lección {lesson_code}"):
        if await driver.click_by_text(candidate, exact=False):
            await driver.wait_for_load("networkidle", timeout_ms=15_000)
            await driver.screenshot(f"plataforma_a_lesson_{lesson_code}")
            return True
    logger.error("Could not find Plataforma A lesson %r", lesson_code)
    return False


async def open_ai_tutor(driver: PlaywrightDriver) -> bool:
    """Open the Plataforma A AI tutor panel."""
    for label in ("Ask Plataforma A", "Plataforma A", "Chat with Plataforma A", "Tutor", "Tutor IA"):
        if await driver.click_element_description(label):
            await driver.screenshot("plataforma_a_ai_tutor_open")
            return True
    return False
