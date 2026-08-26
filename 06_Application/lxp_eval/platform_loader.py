"""Platform configuration + document loader."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pypdf import PdfReader

logger = logging.getLogger(__name__)


@dataclass
class PlatformConfig:
    """One platform under evaluation."""
    name: str
    vendor: str
    url: str = ""
    contact: str = ""
    demo_date: str = ""
    schools_sv: int = 0
    status: str = "unknown"
    vendor_docs: list[Path] = field(default_factory=list)
    observation_notes: list[Path] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "_")


@dataclass
class LoadedDocument:
    """A loaded vendor document, ready to pass to Claude."""
    name: str
    path: Path
    content: str           # extracted text
    pages: int = 0
    kind: str = "pdf"      # pdf | md | txt

    @property
    def header(self) -> str:
        return f"[Document: {self.name} · {self.kind}, {self.pages} page(s)]"


class PlatformNotFound(Exception):
    """No platform directory claims the host we were asked to evaluate."""


def _hosts_of(raw: dict) -> set[str]:
    """Every host a platform answers to.

    Two, usually, and they differ: `url` is where the vendor sells the product
    and `auth.login_url` is where a student actually logs in. Plataforma A sells at
    plataforma_a.com and serves at app.plataforma-a.ejemplo.
    """
    candidates = [raw.get("url") or ""]
    auth = raw.get("auth")
    if isinstance(auth, dict):
        candidates.append(auth.get("login_url") or "")
    hosts = set()
    for c in candidates:
        host = (urlparse(str(c)).hostname or "").strip().lower()
        if host:
            hosts.add(host[4:] if host.startswith("www.") else host)
    return hosts


def resolve_platform_dir(url: str, platforms_root: Path | str) -> str:
    """The platform DIRECTORY that serves `url`.

    Distinct from the ledger's display label, and that distinction is the whole
    point. The two used to be one field: a label derived from the hostname was
    handed to `auto_login` as a config key, so a run booted a browser, loaded
    the page, and only then discovered there was no
    `platforms/app.plataforma-a.ejemplo/`. One value, two namespaces, and they
    only ever agreed by coincidence.

    Raises rather than falling back to the hostname. A wrong guess here means
    logging into a real tenant with the wrong platform's auth recipe.
    """
    root = Path(platforms_root)
    host = (urlparse(url).hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]

    seen = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        cfg = d / "config.yaml"
        if not cfg.exists():
            continue
        seen.append(d.name)
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if host and host in _hosts_of(raw):
            return d.name

    raise PlatformNotFound(
        f'ninguna plataforma declara el host "{host or url}": revisá que su '
        f"config.yaml lo tenga en url: o en auth.login_url:\n"
        f"(plataformas conocidas: {', '.join(seen) or 'ninguna'})"
    )


def load_platform(platform_dir: Path) -> PlatformConfig:
    """Load a platform's config.yaml + document list."""
    cfg_path = platform_dir / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No config.yaml at {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    vendor_docs = [
        platform_dir / d for d in raw.get("vendor_docs", [])
    ]
    obs_notes = [
        platform_dir / d for d in raw.get("observation_notes", [])
    ]

    return PlatformConfig(
        name=raw["name"],
        vendor=raw.get("vendor", ""),
        url=raw.get("url", ""),
        contact=raw.get("contact", ""),
        demo_date=raw.get("demo_date", ""),
        schools_sv=int(raw.get("schools_sv", 0)),
        status=raw.get("status", "unknown"),
        vendor_docs=vendor_docs,
        observation_notes=obs_notes,
        extra={k: v for k, v in raw.items() if k not in {
            "name", "vendor", "url", "contact", "demo_date", "schools_sv",
            "status", "vendor_docs", "observation_notes",
        }},
    )


def load_pdf(path: Path) -> LoadedDocument:
    """Extract text from a PDF using pypdf."""
    reader = PdfReader(str(path))
    pages_text = []
    for i, page in enumerate(reader.pages, start=1):
        pages_text.append(f"--- page {i} ---\n{page.extract_text()}")
    return LoadedDocument(
        name=path.name,
        path=path,
        content="\n\n".join(pages_text),
        pages=len(reader.pages),
        kind="pdf",
    )


def load_text(path: Path) -> LoadedDocument:
    """Load a markdown or text file."""
    content = path.read_text(encoding="utf-8")
    return LoadedDocument(
        name=path.name,
        path=path,
        content=content,
        pages=1,
        kind=path.suffix.lstrip(".") or "txt",
    )


def load_documents(paths: list[Path]) -> list[LoadedDocument]:
    """Load a list of documents (auto-detecting PDF vs text)."""
    docs = []
    for p in paths:
        if not p.exists():
            logger.warning("Document not found: %s — skipping", p)
            continue
        try:
            if p.suffix.lower() == ".pdf":
                docs.append(load_pdf(p))
            else:
                docs.append(load_text(p))
        except Exception as exc:  # noqa: BLE001 — log and continue
            logger.error("Failed to load %s: %s", p, exc)
    return docs


def format_documents_for_prompt(docs: list[LoadedDocument], max_chars_each: int = 60_000) -> str:
    """Format loaded documents for inclusion in a Claude prompt.

    Each document is wrapped in <document> tags with metadata, truncated if too long.
    """
    parts = []
    for d in docs:
        truncated = d.content[:max_chars_each]
        truncation_note = ""
        if len(d.content) > max_chars_each:
            truncation_note = f"\n[... document truncated at {max_chars_each} chars; original is {len(d.content)} chars]"
        parts.append(
            f"<document name=\"{d.name}\" kind=\"{d.kind}\" pages=\"{d.pages}\">\n"
            f"{truncated}{truncation_note}\n"
            f"</document>"
        )
    return "\n\n".join(parts)
