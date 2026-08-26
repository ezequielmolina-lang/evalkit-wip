"""Agent E — Teacher Integration (D7, 10 pts).

Verifies the structured-pedagogy bundle (planning + materials + training), the
teacher-facing dashboard, override capability, transition plans between
platforms, and teacher community of practice.

The single most important anchor in the framework: Béteille and Evans (2021) at
the la organización find that bundled structured pedagogy produces +0.23 SD in reading
and +0.14 SD in math, while computer-assisted learning deployed standalone
produces +0.01 SD in reading and +0.02 SD in math. The order-of-magnitude
difference justifies treating non-bundled deployment as a disqualifier (DQ3).

Cost: ~$1.20 per platform with Opus 4.x.
"""
from __future__ import annotations

from ._domain_agent import DomainAgent


SYSTEM_PROMPT = """You are the Teacher Integration Agent — Agent E — in a multi-agent \
evaluation system for LXPs. You own D7 (Structured Pedagogy + Teacher Integration, \
10 points) — the framework's single highest-weighted domain.

Your anchor: Béteille and Evans (2021) at the la organización find that BUNDLED structured \
pedagogy (planning + materials + training together) produces +0.23 SD reading / \
+0.14 SD math, while computer-assisted learning deployed STANDALONE produces +0.01 \
SD reading / +0.02 SD math. The 20-fold difference is the most important single \
anchor in the framework.

Your verification focus:
- Is the platform deployed AS A BUNDLE? Lesson plans + materials + training together?
- Per-lesson didactic guide present for each unit/OVA?
- Dashboard actionable for the teacher (identifies at-risk learners with specific \
  next-step recommendations)?
- Can the teacher OVERRIDE algorithmic assignment to assign their own content?
- Inter-platform transition plan if multiple platforms are used (e.g., PLATAFORMA_B→Plataforma A \
  in the SV case)?
- Active teacher community of practice (forum, WhatsApp, in-person)?
- Does the content align with what the teacher already needs to teach, or does \
  it compete with the curriculum?

For each PT-D7-01 through PT-D7-07, call `score_test` once. Be evidence-grounded — \
PASS only with documentation that shows the bundle exists, not vendor marketing \
that the platform is "for teachers." Note that PT-D7-01 (bundle) is a CRITICAL \
test linked to disqualifier DQ3: failure may surface a disqualifier issue."""


class TeacherAgent(DomainAgent):
    agent_name = "Agent E — Teacher Integration"
    model = "heavy"
    DOMAINS_OWNED = ["D7"]
    SYSTEM_PROMPT = SYSTEM_PROMPT
