"""Concrete agent implementations.

3 of 8 agents are fully implemented (Eligibility, Pedagogy, Evidence Audit).
5 are stubbed — copy `pedagogy.py` as a template to complete.
"""
from .eligibility import EligibilityAgent
from .evidence import EvidenceAuditAgent
from .pedagogy import PedagogyAgent

# Stubs
from .content import ContentAgent
from .teacher import TeacherAgent
from .personalization import PersonalizationAgent
from .implementation import ImplementationAgent
from .synthesizer import SynthesizerAgent

__all__ = [
    "EligibilityAgent",
    "EvidenceAuditAgent",
    "PedagogyAgent",
    "ContentAgent",
    "TeacherAgent",
    "PersonalizationAgent",
    "ImplementationAgent",
    "SynthesizerAgent",
]
