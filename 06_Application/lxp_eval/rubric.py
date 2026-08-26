"""Machine-readable rubric — v7.1.

Mirrors `Logging_xlsx_v7.py` test catalog but as Python data structures so agents
can reason about which tests they own and pass them as structured input to Claude.

The rubric here MUST stay in sync with the XLSX. If you change one, change the
other. A sanity check in `smoke_test.py` verifies test counts match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["CRITICA", "ALTA", "MEDIA", "BAJA"]
TestStatus = Literal["PASS", "PARTIAL", "FAIL", "N/A", "PENDING", "NOT_TESTED"]


@dataclass
class Test:
    id: str
    domain: str
    name: str
    severity: Severity
    points: float
    evidence_base: str
    verification: str
    pass_criteria: str
    fail_criteria: str


@dataclass
class Domain:
    code: str        # e.g., "D1"
    name: str
    points: float
    evidence_anchor: str
    tests: list[Test] = field(default_factory=list)


@dataclass
class TestResult:
    test_id: str
    status: TestStatus
    points_earned: float
    evidence: str           # text justification
    notes: str = ""         # additional notes / page refs / quotes
    sources: list[str] = field(default_factory=list)  # e.g., ["vendor_doc:p.4", "obs_note:line 22"]


@dataclass
class DomainResult:
    domain_code: str
    test_results: list[TestResult]
    total_points: float
    notes: str = ""


# ============================================================
# THE 85 TESTS (v7.1)
# ============================================================

TESTS = [
    # ===== D1: Explicit Instruction (12 pts, 7 tests) =====
    Test("PT-D1-01", "D1", "Small steps — ≤1 new idea per chunk", "ALTA", 1.5,
         "Rosenshine #2; Hughes 2017", "Audit 5 sample lessons",
         "Each chunk ≤1 new idea + immediate practice", "Multiple ideas simultaneous"),
    Test("PT-D1-02", "D1", "I Do — tutor models the procedure", "CRITICA", 2,
         "Rosenshine #4; Hattie d=0.59", "New account, new concept",
         "Tutor demonstrates with think-aloud", "Tutor jumps to 'try this'"),
    Test("PT-D1-03", "D1", "We Do — guided practice", "ALTA", 1.5,
         "Rosenshine #5; Hughes 2017", "Look for modeled→guided transition",
         "Guided practice explicit", "Skips I→You without We"),
    Test("PT-D1-04", "D1", "You Do — independent practice separable", "ALTA", 2,
         "Rosenshine #9", "Verify section with no tutor scaffolding",
         "Independent practice delimited", "Tutor active throughout"),
    Test("PT-D1-05", "D1", "Worked examples for new skills", "ALTA", 2,
         "Sweller & Cooper 1985", "New skill: worked example + isomorphic problem",
         "Worked example precedes problem", "Only problems"),
    Test("PT-D1-06", "D1", "NO default discovery for new material", "CRITICA", 1.5,
         "Kirschner-Sweller-Clark 2006", "New concept: explains or expects discovery?",
         "Direct explanation", "Discovery as default"),
    Test("PT-D1-07", "D1", "Scaffolding fades over time", "ALTA", 1.5,
         "Hughes faded prompts; Coach 1.4", "Compare start vs end of unit",
         "Fade visible", "Constant support"),

    # ===== D2: Cognitive Load (8 pts, 6 tests) =====
    Test("PT-D2-01", "D2", "≤4 new elements on screen", "ALTA", 2,
         "Cowan 2001 ~4 chunks", "Count new elements per screen",
         "≤4 new elements", ">4 simultaneously"),
    Test("PT-D2-02", "D2", "Text + diagram integrated (no split attention)", "ALTA", 1.5,
         "Sweller split-attention", "Inspect diagram layouts",
         "Labels integrated with diagrams", "Split-attention layout"),
    Test("PT-D2-03", "D2", "Decoration minimal", "ALTA", 1.5,
         "Sweller extraneous load", "Visual audit",
         "Only necessary elements", "Decorative animations"),
    Test("PT-D2-04", "D2", "NO redundant text+voice", "MEDIA", 1,
         "Sweller redundancy effect", "Compare narration vs on-screen text",
         "Voice complements text", "Voice identical to text"),
    Test("PT-D2-05", "D2", "Notation introduced one at a time", "MEDIA", 1,
         "Sweller intrinsic load", "Content audit",
         "Clear sequencing", "Multiple symbols at once"),
    Test("PT-D2-06", "D2", "Expertise reversal — adaptive scaffolding", "MEDIA", 1,
         "Kalyuga 2003", "Novice vs advanced in same sub-topic",
         "Advanced get fewer worked examples", "Identical support"),

    # ===== D3: Retrieval/Spacing/Interleaving (12 pts, 7 tests) =====
    Test("PT-D3-01", "D3", "Retrieval opener at session start", "CRITICA", 2.5,
         "Rosenshine #1; Molina & Lopez 2026", "Session start: 3 unaided problems",
         "Retrieval opener present (unaided)", "No retrieval opener"),
    Test("PT-D3-02", "D3", "Spaced re-test at +1d/+1week/+1month", "CRITICA", 2.5,
         "Cepeda; spacing effect", "Track repetition schedule across sessions",
         "Documented spacing schedule", "Skill never re-tested"),
    Test("PT-D3-03", "D3", "Interleaving (≥50%)", "CRITICA", 2.5,
         "Rohrer & Taylor 2007 63% vs 20%", "Audit session: types interleaved",
         "≥50% interleaved practice", "Only blocked practice"),
    Test("PT-D3-04", "D3", "Output/input ratio ≥40%", "ALTA", 1.5,
         "Testing effect; Chew 2024", "Time input vs output",
         "Output ≥40%", "Input >70% (passive)"),
    Test("PT-D3-05", "D3", "Surface feature variation", "ALTA", 1.5,
         "Bjork desirable difficulties", "Problem set audit",
         "Deliberate variation", "20 identical items"),
    Test("PT-D3-06", "D3", "Mastery requires +session demonstration", "CRITICA", 1,
         "Bjork storage vs retrieval", "Same-session mastery? Or delay required?",
         "Requires demonstration with delay", "Same-session mastery"),
    Test("PT-D3-07", "D3", "Daily review 5-8 min at session start", "ALTA", 0.5,
         "Rosenshine #1", "Verify first block of session",
         "Structured review present", "No review"),

    # ===== D4: Feedback + Mastery + BEA 2025 (10 pts, 8 tests) =====
    Test("PT-D4-01", "D4", "Feedback latency <1s", "ALTA", 1,
         "Hattie & Yates 3× effective if immediate", "Time response→feedback",
         "<1s", ">5s"),
    Test("PT-D4-02", "D4", "BEA-MI Mistake Identification", "CRITICA", 1.5,
         "BEA 2025 MI; Hattie specificity", "Provoke error: tutor names it?",
         "Tutor names the error", "Only 'incorrect'"),
    Test("PT-D4-03", "D4", "Mastery gating (not time-based)", "CRITICA", 1.5,
         "Engelmann FT 1988", "Advance without mastery of Unit N?",
         "Mastery-gated", "Time-based"),
    Test("PT-D4-04", "D4", "Target accuracy ~80%", "ALTA", 1.5,
         "Rosenshine #7 (82% vs 73%)", "Ask vendor for target accuracy",
         "~75-85%", "<60% or >95%"),
    Test("PT-D4-05", "D4", "Check-for-understanding systematic", "ALTA", 1,
         "Rosenshine #6", "Audit checkpoint placement",
         "Every 3-5 activities", "No checkpoints"),
    Test("PT-D4-06", "D4", "Re-teach after consecutive failures", "CRITICA", 1.5,
         "Barbash 2012", "2 consecutive fails: approach changes?",
         "Re-teach with different approach", "Just repeats"),
    Test("PT-D4-07", "D4", "BEA-AC Actionability — feedback specifies next step", "ALTA", 1,
         "BEA 2025 AC", "Does feedback say WHAT to do next?",
         "Clear next action", "Only describes error"),
    Test("PT-D4-08", "D4", "BEA-PG Providing Guidance — hints specific", "ALTA", 1,
         "BEA 2025 PG", "Useful hint or generic?",
         "Specific hint", "Generic hint"),

    # ===== D5: Misconceptions + BEA-ML (8 pts, 6 tests) =====
    Test("PT-D5-01", "D5", "Documented misconception bank", "CRITICA", 2,
         "Chew 2024; Barton 2018", "Request bank from vendor",
         "Mapped misconception bank exists", "Does not exist"),
    Test("PT-D5-02", "D5", "Distractors mapped to misconceptions", "CRITICA", 2,
         "Barton 5 Golden Rules", "Sample 12 MC questions",
         "≥80% mapped", "<60% mapped"),
    Test("PT-D5-03", "D5", "BEA-ML feedback localizes to step", "CRITICA", 1.5,
         "BEA 2025 ML; Hattie", "Same question, different distractors",
         "Feedback localizes specific step", "Generic feedback"),
    Test("PT-D5-04", "D5", "Refutational teaching", "ALTA", 1,
         "Chew 2024", "Misconception detected: activate+refute or restate?",
         "Activate→dissonance→refute", "Restate only"),
    Test("PT-D5-05", "D5", "Deep-structure questions (principles)", "MEDIA", 0.5,
         "Chi 1981", "Principle-recognition items present?",
         "Deep-structure items present", "Execution only"),
    Test("PT-D5-06", "D5", "BEA-ML Mistake Location precise", "ALTA", 1,
         "BEA 2025 ML", "Tutor identifies specific step?",
         "Specific step identified", "Just 'wrong'"),

    # ===== D6: Domain Knowledge + EdReports (8 pts, 7 tests) =====
    Test("PT-D6-01", "D6", "Big ideas articulated (5-10 per subject)", "ALTA", 1,
         "Twyman 2021; Hughes 2017", "Ask vendor for 5-10 big ideas",
         "Vendor articulates clearly", "Only 'we cover curriculum'"),
    Test("PT-D6-02", "D6", "Sequence justified by design (not textbook order)", "ALTA", 1,
         "Twyman juxtaposition", "Request sequence rationale",
         "Rationale articulable", "Only by chapter"),
    Test("PT-D6-03", "D6", "Examples + non-examples paired", "ALTA", 1.5,
         "Hughes 2017", "Audit: 'this is NOT...'",
         "Non-examples present", "Positive only"),
    Test("PT-D6-04", "D6", "Prerequisites verified before unlock", "ALTA", 1.5,
         "Hughes 2017", "Does Unit N verify N-1 mastery?",
         "Verification present", "Unlock by date"),
    Test("PT-D6-05", "D6", "Curriculum SV ≥90% coverage", "ALTA", 1.5,
         "WB curriculum alignment", "Map national curriculum SV → platform",
         "≥90% coverage", "<70% or off-curriculum"),
    Test("PT-D6-06", "D6", "EdReports Focus + Coherence (Gateway 1)", "ALTA", 1,
         "EdReports 2025 Gateway 1", "Focus on grade standards + coherence across grades?",
         "Focus + coherence", "Dispersed"),
    Test("PT-D6-07", "D6", "EdReports Building Knowledge (Gateway 2)", "ALTA", 0.5,
         "EdReports 2025 Gateway 2", "Conceptual + procedural + application rigor?",
         "All three", "Procedural only"),

    # ===== D7: Structured Pedagogy + Docente (10 pts, 7 tests) =====
    Test("PT-D7-01", "D7", "Bundle structured pedagogy (planning+materials+training)", "CRITICA", 2.5,
         "WB Béteille & Evans 2021 +0.23 SD vs +0.01 SD", "Bundle or standalone?",
         "Complete bundle", "CAL standalone"),
    Test("PT-D7-02", "D7", "Per-lesson didactic guide", "ALTA", 1.5,
         "Coach Tool 2.5", "Per lesson: guide + materials + questions?",
         "Complete guide", "Teacher improvises"),
    Test("PT-D7-03", "D7", "Dashboard identifies at-risk learners actionably", "ALTA", 1.5,
         "WB 2021; Hughes", "Dashboard audit",
         "Actionable", "Just a list"),
    Test("PT-D7-04", "D7", "Teacher override (assign content)", "MEDIA", 1,
         "Hughes teacher autonomy", "Teacher panel: assign content?",
         "Yes", "Algorithm dictates"),
    Test("PT-D7-05", "D7", "Inter-platform transition plan", "ALTA", 1,
         "WB sustainability", "PLATAFORMA_B→Plataforma A: plan documented?",
         "Plan + executed", "Teachers adrift"),
    Test("PT-D7-06", "D7", "Teacher community of practice", "MEDIA", 1,
         "Piper 2018", "Forum / WhatsApp / meetings?",
         "Active community", "Absent"),
    Test("PT-D7-07", "D7", "Content aligns with teacher needs", "MEDIA", 1.5,
         "Curriculum alignment", "Focus group: content vs teacher's actual needs",
         "Complements", "Competes"),

    # ===== D8: Subject-Specific + EdReports SoR (5 pts, 6 tests) =====
    Test("PT-D8-01", "D8", "Math: Barton 5 Golden Rules", "ALTA", 1.5,
         "Barton 5 Rules", "Sample 12 math questions",
         "≥80% pass", "<60% pass"),
    Test("PT-D8-02", "D8", "Math: Polya problem-solving steps", "MEDIA", 0.5,
         "Polya 1945; SV CNEB", "Audit problem set",
         "Stepped structure", "Answer only"),
    Test("PT-D8-03", "D8", "Reading: 6 GEEAP domains by grade", "ALTA", 1.5,
         "GEEAP 2025", "Audit 6 reading domains",
         "All covered", "≥2 missing"),
    Test("PT-D8-04", "D8", "Reading: Knowledge-building thematic texts", "ALTA", 0.5,
         "GEEAP; EdReports", "Texts thematically connected?",
         "Yes", "Isolated"),
    Test("PT-D8-05", "D8", "Reading: CPM benchmarks", "MEDIA", 0.5,
         "GEEAP; Piper EGRA", "Tracks CPM + benchmark?",
         "Yes + benchmark", "Not tracked"),
    Test("PT-D8-06", "D8", "Multilingual Learner (Nahuat-Pipil + SV Spanish)", "MEDIA", 0.5,
         "EdReports 2025 MLL", "Adaptation to local languages?",
         "Explicit support", "Generic Spanish only"),

    # ===== D9: Personalization Trap (5 pts, 4 tests) =====
    Test("PT-D9-01", "D9", "Predict-Reveal-Diagnose after error", "CRITICA", 1.5,
         "Slamecka & Graf 1978; Butterfield & Metcalfe 2001", "3 mandatory screens after error",
         "All three present", "Tutor shows answer without generation"),
    Test("PT-D9-02", "D9", "Regression detection in mastered topics", "CRITICA", 1.5,
         "Siegler 2002", "After 3 correct: example with error?",
         "Error detection active", "Doesn't exist"),
    Test("PT-D9-03", "D9", "Solo-vs-with-AI metric on dashboard", "CRITICA", 1,
         "Bastani 2025 PNAS", "Look on dashboard",
         "Visible", "Not reconstructible"),
    Test("PT-D9-04", "D9", "Growing-dependence alert", "ALTA", 1,
         "Bastani 2025", ">70% tutor use: auto-alert?",
         "Alert fires", "No alert"),

    # ===== D10: Auditability (4 pts, 4 tests) =====
    Test("PT-D10-01", "D10", "Differentiated roles (≥3)", "ALTA", 1.5,
         "Coach Tool; WB", "Differentiated roles count",
         "≥3 roles", "1 role"),
    Test("PT-D10-02", "D10", "Latency <5 min student→teacher", "ALTA", 1,
         "WB rapid feedback", "Student responds; teacher refreshes",
         "<5 min", ">24h"),
    Test("PT-D10-03", "D10", "Export CSV/API available", "ALTA", 1,
         "Evaluation requirement", "Request export",
         "Clean export", "Data trapped"),
    Test("PT-D10-04", "D10", "Logged variables reconstruct learning", "MEDIA", 0.5,
         "GEEAP; Chew mental work", "Variables list",
         "Allow reconstruction", "Engagement only"),

    # ===== D11: Intended Use & Implementation Fidelity (8 pts, 8 tests) =====
    Test("PT-D11-01", "D11", "Intended use specified (supervised/autonomous/hybrid)", "ALTA", 1,
         "Carroll 2007 adherence", "Vendor docs",
         "Clearly specified", "Ambiguous"),
    Test("PT-D11-02", "D11", "Frequency, duration, modality specified", "ALTA", 1,
         "Carroll 2007 dose", "Vendor docs",
         "Clear specification", "Vague"),
    Test("PT-D11-03", "D11", "Physical setup specified (laptop/+notebook/tablet)", "ALTA", 1,
         "Carroll 2007 quality", "Vendor docs",
         "Specifies setup", "Doesn't specify"),
    Test("PT-D11-04", "D11", "Prerequisites for use specified (digital literacy, language)", "MEDIA", 0.5,
         "Carroll 2007", "Vendor docs",
         "Clear pre-reqs", "Assumes everything"),
    Test("PT-D11-05", "D11", "OBSERVED USE matches intended use", "CRITICA", 2,
         "Durlak & DuPre 2008", "Focus group + classroom observation",
         "Matches", "Diverges (e.g., WhatsApp during use)"),
    Test("PT-D11-06", "D11", "Implementation fidelity measurement available", "ALTA", 1,
         "Carroll 2007", "Logs / observation rubrics?",
         "Metrics available", "No"),
    Test("PT-D11-07", "D11", "Adaptation guidance — adapt to local context", "MEDIA", 0.5,
         "GEEAP", "Vendor docs",
         "Explicit guide", "Assumes global"),
    Test("PT-D11-08", "D11", "Tools alongside specified (notebook/paper/manipulatives)", "ALTA", 1,
         "Brazil REAiD; SV observation", "Vendor docs + observation",
         "Vendor specifies + observed in classroom", "Computer only"),

    # ===== D12: Evidence Rigor (6 pts, 8 tests) — VERBATIM ESSA CRITERIA =====
    Test("PT-D12-01", "D12", "ESSA Tier VERIFIED per verbatim criteria", "CRITICA", 1.5,
         "ESSA Tiers US DoEd; WWC v5.0",
         "Tier 1: WWC Without Reservations + n≥350 + ≥2 sites + setting AND samples overlap. "
         "Tier 2: WWC With/Without Reservations + n≥350 + ≥2 sites + setting OR samples. "
         "Tier 3: correlational with controls. Tier 4: logic model only.",
         "Tier 1-2 verified with n and sites", "Tier declared without paper meeting criteria = Potemkin"),
    Test("PT-D12-02", "D12", "Identification strategy explicit", "ALTA", 1,
         "Imbens-Rubin causal inference", "Read vendor studies",
         "RCT/RDD/IV/DiD documented", "Only correlational, no controls"),
    Test("PT-D12-03", "D12", "Sample size ≥350 for Tier 1/2, OR ≥80% power justified", "ALTA", 1,
         "ESSA verbatim + power analysis", "Verify n in paper or power calc",
         "≥350 or ≥80% power", "Under-powered or n<350 unjustified"),
    Test("PT-D12-04", "D12", "Sample comparability (balance tests)", "MEDIA", 0.5,
         "Causal inference", "Pre-intervention balance table",
         "Balance verified", "No balance check"),
    Test("PT-D12-05", "D12", "Independence: third-party vs vendor-funded", "ALTA", 1,
         "Conflict of interest standards", "Funding statement",
         "Third-party", "Vendor only"),
    Test("PT-D12-06", "D12", "External validity (faithful implementation) + ≥2 sites verified", "MEDIA", 0.5,
         "Cronbach + ESSA verbatim", "Compare with D11.5; verify # sites in paper",
         "Faithful + ≥2 sites", "Demo vs real divergent OR 1 site"),
    Test("PT-D12-07", "D12", "Effect size with CIs reported", "MEDIA", 0.25,
         "APA Standards", "Verify paper",
         "CIs reported", "p-value only"),
    Test("PT-D12-08", "D12", "Replication / robustness checks", "MEDIA", 0.25,
         "AERA Standards", "Verify paper",
         "Multiple specs / robustness", "Single spec"),

    # ===== D13: Teacher Training Audit (4 pts, 7 tests) =====
    Test("PT-D13-01", "D13", "Training materials available for audit", "ALTA", 0.5,
         "Coach Tool; Piper 2018", "Request from vendor / MoE",
         "Available", "Not accessible"),
    Test("PT-D13-02", "D13", "Specificity: training is product-specific (not generic digital literacy)", "CRITICA", 0.5,
         "Coach Tool; Joyce & Showers", "Material audit",
         "Product-specific", "Generic digital literacy"),
    Test("PT-D13-03", "D13", "Practice supervised during initial training", "ALTA", 0.5,
         "Joyce & Showers — coaching component", "Confirm with teachers",
         "Practice supervised", "Lecture only"),
    Test("PT-D13-04", "D13", "Alignment: training pedagogy = product pedagogy", "CRITICA", 1,
         "Joyce & Showers", "Compare training docs vs vendor's claimed pedagogy",
         "Aligned", "Discrepancy"),
    Test("PT-D13-05", "D13", "Duration: initial training ≥8 hours", "ALTA", 0.5,
         "WB 2021", "Verify duration",
         "≥8h with practice", "<2h"),
    Test("PT-D13-06", "D13", "Continuous PD through the year", "CRITICA", 0.5,
         "Joyce & Showers; WB 2021", "Documented plan",
         "Continuous PD", "Onboarding only"),
    Test("PT-D13-07", "D13", "Regular mentor/coach support", "ALTA", 0.5,
         "Joyce & Showers — coaching essential", "Verify with teachers",
         "Regular mentor", "Absent"),
]

# Domains (with their tests grouped)
DOMAINS = {
    "D1": Domain("D1", "Explicit Instruction Architecture", 12, "Hattie d=0.59 (>300 studies)"),
    "D2": Domain("D2", "Cognitive Load Management", 8, "Cowan ~4 chunks; Sweller worked examples"),
    "D3": Domain("D3", "Retrieval, Spacing, Interleaving", 12, "Rohrer & Taylor 63% vs 20%"),
    "D4": Domain("D4", "Feedback and Mastery (incl. BEA 2025 MI/AC/PG)", 10, "Rosenshine 82% vs 73%; BEA 2025"),
    "D5": Domain("D5", "Misconception Detection and Refutation (incl. BEA-ML)", 8, "Chew 2024; Barton 2018"),
    "D6": Domain("D6", "Domain Knowledge + EdReports Focus/Coherence", 8, "Chi 1981; EdReports 2025"),
    "D7": Domain("D7", "Structured Pedagogy + Teacher Integration", 10, "Béteille & Evans +0.23 SD vs +0.01 SD"),
    "D8": Domain("D8", "Subject-Specific Quality (Math, Reading, MLL)", 5, "Barton 5 Rules; GEEAP 2025"),
    "D9": Domain("D9", "Design Against the Personalization Trap", 5, "Bastani et al.; Molina & Lopez"),
    "D10": Domain("D10", "Auditability (Dashboards, Data Export)", 4, "GEEAP implementation principle"),
    "D11": Domain("D11", "Intended Use & Implementation Fidelity", 8, "Carroll 2007; Durlak & DuPre 2008"),
    "D12": Domain("D12", "Evidence Rigor (verbatim ESSA + WWC)", 6, "ESSA 2015; WWC 2022"),
    "D13": Domain("D13", "Teacher Training Substance Audit", 4, "Joyce & Showers; Coach Tool; Piper 2018"),
}

# Attach tests to domains (all 13)
for t in TESTS:
    if t.domain in DOMAINS:
        DOMAINS[t.domain].tests.append(t)


# Sanity check: total points across all tests should equal 100
def _validate_totals():
    total = sum(t.points for t in TESTS)
    if abs(total - 100.0) > 0.01:
        import warnings
        warnings.warn(
            f"Rubric total = {total} pts (expected 100). Check rubric.py vs XLSX."
        )


_validate_totals()


# Pre-filter
@dataclass
class PreFilter:
    id: str
    name: str
    type: str
    source: str


PRE_FILTER = [
    PreFilter("QA1", "Legally incorporated entity", "Binary", "UNICEF QA"),
    PreFilter("QA2", "Explicit data consent collection", "Binary", "UNICEF + GDPR"),
    PreFilter("QA3", "No sale of data (CS concern #5)", "Binary", "UNICEF QA + Common Sense"),
    PreFilter("QA4", "Public privacy policy", "Binary", "UNICEF QA"),
    PreFilter("QA5", "Children's data not used for marketing (CS concern #7)", "Binary", "UNICEF + LEPINA + Common Sense"),
    PreFilter("QA6", "Theory of Change documented (presence)", "Binary", "UNICEF — substance audited in D1+DQ1"),
    PreFilter("QA7", "ESSA Tier VERIFIED (n≥350, ≥2 sites, overlap)", "Tier 1/2/3/4", "UNICEF + ESSA verbatim + D12"),
]


# Disqualifiers
@dataclass
class Disqualifier:
    id: str
    name: str
    evidence: str
    test_link: str


DISQUALIFIERS = [
    Disqualifier("DQ1", "No default discovery for new material", "Kirschner-Sweller-Clark 2006", "PT-D1-06"),
    Disqualifier("DQ2", "Retrieval/testing cross-session", "Bjork; Roediger", "PT-D3-02"),
    Disqualifier("DQ3", "Bundle structured pedagogy (no CAL standalone)", "Béteille & Evans 2021", "PT-D7-01"),
    Disqualifier("DQ4", "Mastery gating (not time-based)", "Engelmann 1988", "PT-D4-03"),
    Disqualifier("DQ5", "'Active learning' with concrete mechanism", "Chew 2025", "Vendor interview"),
    Disqualifier("DQ6", "Safety pipeline escalated to teacher", "LEPINA SV", "Test + 24h wait"),
    Disqualifier("DQ7", "Data ownership clear (assignable to MoE)", "Child data protection", "Contract review"),
    Disqualifier("DQ8", "Post-contract continuity plan (no lock-in)", "Vendor risk mgmt", "Contract review"),
]


# ============================================================
# ESSA verbatim verification criteria (used by Evidence Audit Agent)
# ============================================================
ESSA_TIER_CRITERIA = {
    "Tier 1": {
        "name": "Strong Evidence of Effectiveness",
        "criteria": [
            "Meets WWC Standards Without Reservations (v2.1 or later)",
            "Sample size: n ≥ 350 students",
            "Setting count: ≥ 2 educational sites",
            "Overlap: setting AND samples overlap with deployment context",
            "Effect: statistically significant positive effect",
        ],
        "study_design": "Randomized Controlled Trial, well-designed and implemented, without attrition problems",
    },
    "Tier 2": {
        "name": "Moderate Evidence of Effectiveness",
        "criteria": [
            "Meets WWC Standards With or Without Reservations (v2.1 or later)",
            "Sample size: n ≥ 350 students",
            "Setting count: ≥ 2 educational sites",
            "Overlap: setting OR samples overlap with deployment context",
            "Effect: statistically significant positive effect",
        ],
        "study_design": "Strong quasi-experimental study OR RCT with implementation/attrition concerns",
    },
    "Tier 3": {
        "name": "Promising Evidence",
        "criteria": [
            "≥1 well-designed and well-implemented correlational study",
            "Statistical controls for selection bias",
            "Effect: statistically significant positive effect",
        ],
        "study_design": "Correlational study with regression / matching / IV / etc.",
    },
    "Tier 4": {
        "name": "Demonstrates a Rationale",
        "criteria": [
            "Comprehensive logic model / theory of action documented",
            "Research-based component evidence (other studies on the components)",
            "Ongoing evaluation effort exists (SEA, LEA, or external)",
        ],
        "study_design": "Logic model + research-based components, no direct causal evidence yet",
    },
}

# ============================================================
# 5 Potemkin patterns (used by Evidence Audit Agent)
# ============================================================
POTEMKIN_PATTERNS = [
    {
        "id": "P1",
        "name": "Pilot without control",
        "description": "Pilot results without a control group are declared as Tier 2 Moderate Evidence.",
        "downgrade_to": "Tier 4",
    },
    {
        "id": "P2",
        "name": "Underpowered or single-site",
        "description": "Sample size below 350 students or single-site studies declared as Tier 1 Strong Evidence.",
        "downgrade_to": "Tier 3 or Tier 4",
    },
    {
        "id": "P3",
        "name": "Unsupported 'evidence-based' claim",
        "description": "'Evidence-based' claims without a peer-reviewed paper backing them.",
        "downgrade_to": "Tier 4",
    },
    {
        "id": "P4",
        "name": "Gains without CIs or overlap",
        "description": "Significant gains reported without confidence intervals or without overlap between study setting and deployment context.",
        "downgrade_to": "Tier 3",
    },
    {
        "id": "P5",
        "name": "Vendor-funded as independent",
        "description": "Vendor-funded white papers presented as third-party validation.",
        "downgrade_to": "Tier 4 (flag for independence concern)",
    },
]


def get_tests_for_domain(domain_code: str) -> list[Test]:
    """Return all tests assigned to a domain."""
    return [t for t in TESTS if t.domain == domain_code]


def get_tests_for_domains(domain_codes: list[str]) -> list[Test]:
    """Return all tests for a list of domains."""
    return [t for t in TESTS if t.domain in domain_codes]
