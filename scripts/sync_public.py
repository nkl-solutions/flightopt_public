"""Kopiert den freigegebenen Teil von flightopt_private nach flightopt_public.

    uv run python scripts/sync_public.py --dry-run
    uv run python scripts/sync_public.py

Die Liste der Kandidaten kommt aus `git ls-files`: was Git nicht kennt, ist
lokaler Zustand und geht niemanden etwas an. Davon abgezogen werden die
internen Dokumente, die Recherche, die Spikes und die Arbeitsordner.

Danach laeuft ein Regex-Scan ueber genau die Dateien, die im oeffentlichen Baum
gelandet sind. Er findet keine cleveren Verstecke, aber die drei Faelle, die in
der Praxis passieren: ein Schluessel im Code, eine Zuweisung an `password` oder
`token`, und die eigene Domain in einer Datei, in der sie nichts zu suchen hat.
Ein Treffer beendet das Skript mit Fehlercode, bevor jemand committen kann.

Das Skript pusht nicht und committet nicht. Es schreibt nur in die Arbeitskopie
des oeffentlichen Repos; der Rest bleibt Handarbeit mit Freigabe.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_FILES = frozenset({
    "HANDOVER.md",
    "MASTERPLAN.md",
    "docs/AIRLINE_PLAN.md",
    "docs/METASUCHE.md",
    "docs/P0_FINDINGS.md",
})
"""Interne Dokumente. Sie nennen Messwerte, Fehlschlaege und Betriebsdetails."""

EXCLUDED_TREES = (
    "docs/research/",
    "docs/superpowers/",
    "spike/",
    "work/",
    ".claude/",
)
"""Ganze Baeume. Prefix-Vergleich auf dem Posix-Pfad, nicht auf dem Dateinamen."""

KEEP_IN_PUBLIC = frozenset({"README.md"})
"""Wird drueben weder ueberschrieben noch geloescht.

Die oeffentliche README ist die Portfolio-Fassung und hat mit der privaten
nichts gemein. Sie steht deshalb auf beiden Seiten still: nicht kopieren, nicht
loeschen, nicht scannen.
"""

PROTECTED_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "data",
})
"""Verzeichnisnamen, die der Abgleich im oeffentlichen Baum gar nicht erst betritt.

Ohne das wuerde der Loeschlauf die dortige virtuelle Umgebung und die lokale
Datenbank mitnehmen, nur weil es sie privat nicht gibt.
"""

BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
    ".otf", ".zip", ".gz", ".pdf", ".db", ".sqlite", ".sqlite3",
})

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("zuweisung", re.compile(
        r"""(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*["'][^"']{8,}"""
    )),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

DOMAIN = "nkl-solutions"  # sync-public: ok
DOMAIN_ALLOWED = frozenset({"docs/DEPLOYMENT.md", "docker-compose.portainer.yml"})
"""Die Domain darf nur dort stehen, wo sie fuer den Betrieb gebraucht wird."""

ALLOW_MARKER = "sync-public: ok"
"""Ausnahme fuer genau eine Zeile, als Kommentar in der Zeile selbst.

Die Muster sind absichtlich grob und treffen deshalb auch Zeilen, die ueber
Schluesselnamen reden statt Schluessel zu enthalten. Die Ausnahme steht dort,
wo man sie beim Lesen sieht, und nicht in einer Liste in diesem Skript, die
niemand pflegt.
"""


class SyncError(RuntimeError):
    """Falscher Pfad oder kein Git. Nichts, was ein Retry heilt."""


@dataclass
class Finding:
    path: str
    line: int
    pattern: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.pattern}: {self.excerpt}"


@dataclass
class Report:
    copied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def is_public(path: str) -> bool:
    """Darf diese Datei im oeffentlichen Repo stehen?

    Arbeitet nur auf dem Pfad, nicht auf dem Inhalt. Der Inhalt ist Sache des
    Secret-Scans; beides in einer Funktion zu mischen macht die Whitelist
    unlesbar.
    """
    # `lstrip("./")` waere hier falsch: es frisst jeden fuehrenden Punkt und
    # macht aus `.claude/x` ein `claude/x`, das keine Ausschlussregel trifft.
    rel = path.replace("\\", "/").removeprefix("./")
    if not rel:
        return False
    if rel in EXCLUDED_FILES or rel in KEEP_IN_PUBLIC:
        return False
    return not any(rel.startswith(tree) for tree in EXCLUDED_TREES)


def whitelist(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """Teilt die Kandidaten in kopieren und ueberspringen, Reihenfolge stabil."""
    keep: list[str] = []
    drop: list[str] = []
    for raw in paths:
        rel = raw.replace("\\", "/").strip()
        if not rel:
            continue
        (keep if is_public(rel) else drop).append(rel)
    return keep, drop


def tracked_files(private: Path) -> list[str]:
    """Alles, was Git kennt. Untracked heisst lokal und bleibt lokal."""
    try:
        out = subprocess.run(
            ["git", "-C", str(private), "ls-files"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SyncError(
            f"git ls-files in {private} fehlgeschlagen: {exc}. "
            "Alternativ --files-from mit einer Dateiliste angeben."
        ) from exc
    return [line for line in out.splitlines() if line.strip()]


def public_files(public: Path) -> list[str]:
    """Was drueben liegt, ohne geschuetzte Verzeichnisse und ohne README."""
    found: list[str] = []
    stack = [public]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if entry.is_dir():
                if entry.name in PROTECTED_DIRS:
                    continue
                stack.append(entry)
                continue
            rel = entry.relative_to(public).as_posix()
            if rel in KEEP_IN_PUBLIC:
                continue
            found.append(rel)
    return sorted(found)


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        return b"\0" in path.read_bytes()[:4096]
    except OSError:
        return True


def scan_secrets(base: Path, files: Sequence[str]) -> list[Finding]:
    """Prueft die kopierten Dateien, nicht den ganzen Baum.

    Was schon vorher drueben lag und nicht aus diesem Lauf stammt, ist nicht die
    Verantwortung dieses Laufs; sonst blockiert ein alter Fund jeden Sync.
    """
    findings: list[Finding] = []
    for rel in files:
        target = base / rel
        if not target.is_file() or _looks_binary(target):
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                continue
            for name, pattern in SECRET_PATTERNS:
                hit = pattern.search(line)
                if hit:
                    findings.append(Finding(rel, number, name, hit.group(0)[:60]))
            if DOMAIN in line and rel not in DOMAIN_ALLOWED:
                findings.append(Finding(rel, number, "domain", DOMAIN))
    return findings


def sync(
    private: Path, public: Path, candidates: Sequence[str], *, dry_run: bool = False
) -> Report:
    """Kopiert die Whitelist und raeumt drueben auf, was privat verschwunden ist."""
    if not private.is_dir():
        raise SyncError(f"Privates Repo nicht gefunden: {private}")
    if not public.is_dir():
        raise SyncError(f"Oeffentliches Repo nicht gefunden: {public}")

    keep, drop = whitelist(candidates)
    report = Report(skipped=drop)

    for rel in keep:
        source = private / rel
        if not source.is_file():
            # Git kennt die Datei, der Arbeitsbaum nicht: geloescht, aber noch
            # nicht committet. Nicht kopieren und auch nicht drueben loeschen.
            continue
        target = public / rel
        payload = source.read_bytes()
        if target.is_file() and target.read_bytes() == payload:
            report.unchanged.append(rel)
            continue
        report.copied.append(rel)
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    wanted = set(keep)
    for rel in public_files(public):
        if rel in wanted or not is_public(rel):
            continue
        report.deleted.append(rel)
        if not dry_run:
            (public / rel).unlink()

    # Im Trockenlauf steht drueben noch der alte Inhalt, also wird die Quelle
    # geprueft. Der Inhalt ist derselbe, den ein echter Lauf kopieren wuerde.
    base = private if dry_run else public
    report.findings = scan_secrets(base, [r for r in keep if (private / r).is_file()])
    return report


def _default_public(private: Path) -> Path:
    return private.parent / "flightopt_public"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_public",
        description="Whitelist aus flightopt_private nach flightopt_public kopieren.",
    )
    parser.add_argument("--private", default=None, help="Pfad zum privaten Repo")
    parser.add_argument("--public", default=None, help="Pfad zum oeffentlichen Repo")
    parser.add_argument(
        "--files-from", default=None,
        help="Datei mit einem relativen Pfad je Zeile statt git ls-files",
    )
    parser.add_argument("--dry-run", action="store_true", help="nur berichten")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    private = Path(args.private).resolve() if args.private else ROOT
    public = Path(args.public).resolve() if args.public else _default_public(private)

    try:
        if args.files_from:
            listing = Path(args.files_from).read_text(encoding="utf-8").splitlines()
        else:
            listing = tracked_files(private)
        report = sync(private, public, listing, dry_run=args.dry_run)
    except SyncError as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        return 2

    mode = " (dry-run)" if args.dry_run else ""
    print(f"privat    {private}")
    print(f"public    {public}{mode}")
    print(
        f"kopiert   {len(report.copied)}"
        f"  unveraendert {len(report.unchanged)}"
        f"  geloescht {len(report.deleted)}"
        f"  uebersprungen {len(report.skipped)}"
    )
    for rel in report.deleted:
        print(f"  weg  {rel}")

    if report.findings:
        print("", file=sys.stderr)
        print("Secret-Scan hat angeschlagen, nichts committen:", file=sys.stderr)
        for finding in report.findings:
            print(f"  {finding}", file=sys.stderr)
        return 1

    print("Secret-Scan sauber.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
