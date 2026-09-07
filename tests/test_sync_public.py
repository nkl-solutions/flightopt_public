"""Der Public-Sync gegen zwei Wegwerf-Verzeichnisse, ohne Git und ohne Netz.

Das Skript entscheidet, was das Portfolio-Repo zu sehen bekommt. Ein Fehler
darin ist nicht sichtbar, bevor er oeffentlich ist, deshalb bekommen die drei
Aufgaben je einen Test: filtern, aufraeumen, Alarm schlagen.

Die Beispiel-Schluessel unten sind erfunden und tragen `sync-public: ok`, sonst
wuerde der Scanner diese Datei bei jedem Sync selbst anschlagen.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_public.py"


def load_module():
    """Das Skript liegt ausserhalb des Pakets und wird direkt geladen."""
    spec = importlib.util.spec_from_file_location("sync_public", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` schlaegt im Skript nach, also muss das Modul registriert sein,
    # bevor es ausgefuehrt wird.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_public = load_module()


def write(base: Path, rel: str, text: str) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    return private, public


def run(private: Path, public: Path, files: list[str], *, dry_run: bool = False) -> int:
    listing = private / "files.txt"
    listing.write_text("\n".join(files) + "\n", encoding="utf-8", newline="\n")
    argv = [
        "--private", str(private), "--public", str(public),
        "--files-from", str(listing),
    ]
    if dry_run:
        argv.append("--dry-run")
    return sync_public.main(argv)


def test_whitelist_laesst_interne_dateien_liegen(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(private, "flightopt/cli.py", "print('hi')\n")
    write(private, "README.md", "privat\n")
    write(private, "HANDOVER.md", "intern\n")
    write(private, "MASTERPLAN.md", "intern\n")
    write(private, "docs/DEPLOYMENT.md", "oeffentlich\n")
    write(private, "docs/P0_FINDINGS.md", "intern\n")
    write(private, "docs/research/01_x.md", "intern\n")
    write(private, "docs/superpowers/specs/plan.md", "intern\n")
    write(private, "spike/probe.py", "intern\n")
    write(private, "work/dump.json", "intern\n")
    write(private, ".claude/settings.json", "intern\n")
    write(public, "README.md", "oeffentliche Fassung\n")

    files = [
        "flightopt/cli.py", "README.md", "HANDOVER.md", "MASTERPLAN.md",
        "docs/DEPLOYMENT.md", "docs/P0_FINDINGS.md", "docs/research/01_x.md",
        "docs/superpowers/specs/plan.md", "spike/probe.py", "work/dump.json",
        ".claude/settings.json",
    ]
    assert run(private, public, files) == 0

    assert (public / "flightopt/cli.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (public / "docs/DEPLOYMENT.md").is_file()
    for gone in (
        "HANDOVER.md", "MASTERPLAN.md", "docs/P0_FINDINGS.md",
        "docs/research/01_x.md", "docs/superpowers/specs/plan.md",
        "spike/probe.py", "work/dump.json", ".claude/settings.json",
    ):
        assert not (public / gone).exists(), gone
    # Die oeffentliche README wird getrennt gepflegt und nie ueberschrieben.
    assert (public / "README.md").read_text(encoding="utf-8") == "oeffentliche Fassung\n"


def test_loescht_nur_was_in_der_whitelist_liegt(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(private, "flightopt/cli.py", "neu\n")
    write(public, "flightopt/cli.py", "alt\n")
    write(public, "flightopt/entfernt.py", "war mal da\n")
    write(public, "docs/research/altbestand.md", "ausserhalb der Whitelist\n")
    write(public, "README.md", "oeffentliche Fassung\n")
    write(public, ".venv/lib/pyvenv.cfg", "lokale Umgebung\n")

    assert run(private, public, ["flightopt/cli.py"]) == 0

    assert not (public / "flightopt/entfernt.py").exists()
    assert (public / "flightopt/cli.py").read_text(encoding="utf-8") == "neu\n"
    # Ausserhalb der Whitelist und deshalb nicht Sache dieses Skripts.
    assert (public / "docs/research/altbestand.md").is_file()
    assert (public / "README.md").is_file()
    assert (public / ".venv/lib/pyvenv.cfg").is_file()


def test_dry_run_schreibt_nichts(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(private, "flightopt/cli.py", "neu\n")
    write(public, "flightopt/entfernt.py", "war mal da\n")

    assert run(private, public, ["flightopt/cli.py"], dry_run=True) == 0

    assert not (public / "flightopt/cli.py").exists()
    assert (public / "flightopt/entfernt.py").is_file()


@pytest.mark.parametrize(
    "content",
    [
        "KEY = 'sk-abcdefghijklmnopqrstuvwxyz0123'\n",  # sync-public: ok
        "aws = AKIA0123456789ABCDEF\n",  # sync-public: ok
        'password = "hunter2hunter2"\n',  # sync-public: ok
        "-----BEGIN RSA PRIVATE KEY-----\n",  # sync-public: ok
    ],
)
def test_secret_scan_blockt(repos: tuple[Path, Path], content: str) -> None:
    private, public = repos
    write(private, "flightopt/leak.py", content)

    assert run(private, public, ["flightopt/leak.py"]) == 1
    findings = sync_public.scan_secrets(public, ["flightopt/leak.py"])
    assert [f.path for f in findings] == ["flightopt/leak.py"]


def test_domain_nur_in_den_erlaubten_dateien(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(private, "docs/DEPLOYMENT.md", "Host: flightopt.nkl-solutions.de\n")  # sync-public: ok
    write(private, "flightopt/api/main.py", "BASE = 'https://nkl-solutions.de'\n")  # sync-public: ok

    assert run(private, public, ["docs/DEPLOYMENT.md", "flightopt/api/main.py"]) == 1
    findings = sync_public.scan_secrets(
        public, ["docs/DEPLOYMENT.md", "flightopt/api/main.py"]
    )
    assert [f.path for f in findings] == ["flightopt/api/main.py"]
    assert findings[0].pattern == "domain"


def test_marker_entschaerft_genau_eine_zeile(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(
        private, "tests/test_env.py",
        'assert "SERPAPI_KEY=" in example  # sync-public: ok\n'
        'password = "hunter2hunter2"\n',  # sync-public: ok
    )

    assert run(private, public, ["tests/test_env.py"]) == 1
    findings = sync_public.scan_secrets(public, ["tests/test_env.py"])
    # Nur die zweite Zeile, die erste traegt die Ausnahme.
    assert [f.line for f in findings] == [2]


def test_sauberer_lauf_meldet_nichts(repos: tuple[Path, Path]) -> None:
    private, public = repos
    write(private, "flightopt/cli.py", "print('hi')\n")
    write(private, "docs/DEPLOYMENT.md", "Host: flightopt.nkl-solutions.de\n")  # sync-public: ok

    assert run(private, public, ["flightopt/cli.py", "docs/DEPLOYMENT.md"]) == 0


def test_fehlender_pfad_endet_mit_zwei(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    listing = private / "files.txt"
    listing.write_text("", encoding="utf-8", newline="\n")
    code = sync_public.main([
        "--private", str(private),
        "--public", str(tmp_path / "gibtsnicht"),
        "--files-from", str(listing),
    ])
    assert code == 2
