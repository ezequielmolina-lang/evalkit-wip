"""Append-only evidence ledger with content hashes.

Provenance spine for the whole run. Every score in the final report points at a
ledger row; every row records what was probed, where, and a sha256 of the
artifact (a screenshot, a page-text dump) captured as proof. This is what backs
"no hay que creernos, abrí la carpeta": an auditor can re-hash any artifact and
confirm it is the exact bytes the score was based on.

Append-only by design -- entries are never rewritten, so the file is a
tamper-evident log, not a mutable store. The ledger is JSON Lines: one entry per
line, written utf-8 with ensure_ascii=False so Spanish notes stay human-readable
(`lección`, not `\\u2026`) when an auditor opens the file.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..textio import scrub_surrogates

# The kinds that may reach the judge. The ledger also carries governance rows
# (approvals, signoffs) that record WHO authorized a run -- a signoff points at
# a real test_id, so the (test_id, platform) filters alone would hand it to the
# judge and change the frozen instrument. Allowlist, not blocklist: a row kind
# that does not exist yet is excluded by default, never by someone remembering.
EVIDENCE_KINDS = frozenset({"screenshot", "page_text", "observation"})

# The kinds that record a HUMAN decision: the plan approval and each gated
# probe's signoff. Together with EVIDENCE_KINDS these PARTITION the ledger's
# kinds -- scoring keeps only EVIDENCE_KINDS (fail-closed, above), the report's
# provenance section keeps only GOVERNANCE_KINDS, and a kind in neither set is
# invisible to both consumers. So a NEW row kind must be added to exactly one
# of the two sets, deliberately: to EVIDENCE_KINDS if a judge may weigh it, to
# GOVERNANCE_KINDS if the report must list it as an authorization.
GOVERNANCE_KINDS = frozenset({"approval", "signoff"})


@dataclass(frozen=True)
class EvidenceEntry:
    probe_id: str
    test_id: str
    platform: str
    run_index: int
    url: str
    kind: str
    artifact: str
    sha256: str
    note: str
    timestamp: str


def _sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def rows_since(rows, since: str | None):
    """Ledger rows captured at or after `since`. Everything, if `since` is empty.

    The ledger is append-only BY DESIGN -- that is what makes it evidence -- so
    a re-probed criterion keeps the discarded attempt alongside the good one.
    PB-PT-D8-01 ended up with 108 rows: 54 whose page text had been truncated
    to the course sidebar, then 54 captured after that was fixed. Both carry
    the same test_id, so scoring handed the judge all 108 and weighed a
    criterion partly on a run that had been thrown away.

    `approved_at` is the exact boundary rather than a timestamp-gap heuristic:
    every approval stamps it, and a run's evidence is what came after it.
    Nothing is deleted -- the older rows stay in the ledger for audit, they
    just stop being the basis of a verdict.
    """
    if not since:
        return list(rows)
    return [r for r in rows if str(r.get("timestamp") or "") >= since]


class EvidenceLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        probe_id: str,
        test_id: str,
        platform: str,
        run_index: int,
        url: str,
        artifact: Path,
        kind: str,
        note: str,
        timestamp: str,
    ) -> EvidenceEntry:
        """Hash the artifact, record the entry, and return it.

        The hash is taken now, at capture time, so the ledger pins the exact
        bytes; a later `verify` re-hashes the file on disk to detect any swap.
        """
        entry = EvidenceEntry(
            probe_id=probe_id,
            test_id=test_id,
            platform=platform,
            run_index=run_index,
            url=url,
            kind=kind,
            artifact=str(artifact),
            sha256=_sha256(artifact),
            note=note,
            timestamp=timestamp,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            # Scrubbed because notes and URLs come off a live page, so an
            # undecodable byte is likelier here than anywhere else -- and an
            # append that raises mid-run kills the run, not just the write.
            # The artifact's sha256 is taken from the file's own bytes above,
            # so this never touches the provenance chain.
            fh.write(
                json.dumps(scrub_surrogates(asdict(entry)), ensure_ascii=False) + "\n"
            )
        return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def verify(self, entry: EvidenceEntry) -> bool:
        """True iff the artifact still exists and hashes to the recorded value."""
        p = Path(entry.artifact)
        return p.exists() and _sha256(p) == entry.sha256

    @staticmethod
    def _dedupe(rows: list[dict]) -> list[dict]:
        """Collapse re-captures of identical bytes, keeping the FIRST.

        The log itself is append-only and stays that way -- that is what makes
        it tamper-evident. But the read accessors feed the judge, and re-running
        a derivation appends the same screenshot again: three `rederive` calls
        put three copies of one image into a SINGLE judge prompt, paying 3x the
        image tokens and handing the model a repetition that can shift the
        verdict on its own. Identity is (test_id, platform, sha256): the same
        bytes are still distinct evidence for a different cell or a different
        platform.
        """
        seen: set[tuple[str, str, str]] = set()
        out: list[dict] = []
        for r in rows:
            key = (r["test_id"], r["platform"], r["sha256"])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def for_test(self, test_id: str) -> list[dict]:
        """Every entry for one test id, ACROSS platforms. Deduped.

        The scorer must not use this to score a platform -- PLATAFORMA_B and Plataforma A share
        test ids, so this blends them. Use for_platform_test for scoring.
        """
        return self._dedupe(
            [
                r
                for r in self.read_all()
                if r["test_id"] == test_id and r.get("kind") in EVIDENCE_KINDS
            ]
        )

    def for_platform_test(self, platform: str, test_id: str) -> list[dict]:
        """Every entry for one test id on one platform, deduped by content hash.

        The scoring accessor: what this returns is exactly what the judge sees.
        Kinds outside EVIDENCE_KINDS never appear here -- governance rows share
        the file and the test_id, but they are about the run, not the platform.
        """
        return self._dedupe(
            [
                r
                for r in self.read_all()
                if r["test_id"] == test_id
                and r["platform"] == platform
                and r.get("kind") in EVIDENCE_KINDS
            ]
        )
