"""Was auf dem VPS ankommt, und was dort ausdruecklich nicht ankommt.

Der Portainer-Stack baut aus dem oeffentlichen Repo; was hier nicht steht,
existiert dort nicht. Diese Datei ist deshalb kein Stilwaechter, sondern die
Zusicherung, dass genau zwei Bauwege existieren und dass der schlanke der
Standard bleibt: ein Chromium wiegt mehrere hundert Megabyte, und wer ihn
versehentlich mitbaut, merkt es erst auf dem Server.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.portainer.yml").read_text(encoding="utf-8")
DEPLOYMENT = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

VARIANT = "FLIGHTOPT_VARIANT"


def stages() -> dict[str, str]:
    """Die Bauabschnitte der Dockerfile, jeweils mit ihrem Rumpf."""
    found: dict[str, str] = {}
    name = ""
    for line in DOCKERFILE.splitlines():
        match = re.match(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", line.strip(), re.IGNORECASE)
        if match:
            name = match.group(1)
            found[name] = ""
            continue
        if name:
            found[name] += line + "\n"
    return found


def test_the_dockerfile_has_a_lean_and_a_hotels_stage():
    built = stages()

    assert "lean-env" in built
    assert "hotels-env" in built


def test_the_lean_stage_installs_no_browser():
    """Der Standardweg bleibt klein. Sonst zahlt ihn jeder mit."""
    lean = stages()["lean-env"]

    assert "playwright" not in lean.lower()
    assert "chromium" not in lean.lower()
    assert "uv sync --frozen --no-dev" in lean


def test_the_hotels_stage_installs_playwright_and_chromium():
    hotels = stages()["hotels-env"]

    assert "--group hotels" in hotels
    assert "playwright install" in hotels
    assert "chromium" in hotels


def test_the_hotels_stage_switches_booking_on_by_itself():
    """Ein Image mit Browser und ohne Schalter waere ein Image ohne Zweck."""
    hotels = stages()["hotels-env"]

    assert "FLIGHTOPT_HOTELS_BOOKING=1" in hotels


def test_the_variant_defaults_to_lean():
    """`docker build .` ohne Argument darf nie den fetten Weg nehmen."""
    default = re.search(rf"^ARG\s+{VARIANT}=(\S+)\s*$", DOCKERFILE, re.MULTILINE)

    assert default is not None
    assert default.group(1) == "lean"


def test_the_runtime_stage_is_chosen_by_the_variant():
    assert re.search(rf"^FROM\s+\$\{{{VARIANT}\}}-env\s+AS\s+", DOCKERFILE, re.MULTILINE)


def test_the_application_is_copied_after_the_browser():
    """Sonst faellt bei jeder Codeaenderung ein halbes Gigabyte neu an."""
    browser = DOCKERFILE.index("playwright install")
    application = DOCKERFILE.index("COPY flightopt")

    assert browser < application


def test_the_compose_passes_the_variant_as_a_build_argument():
    assert f"{VARIANT}: ${{{VARIANT}:-lean}}" in COMPOSE


def compose_launcher() -> tuple[str, list[str]]:
    command = re.search(r"^    command:\n((?:      .*\n|\n)+)", COMPOSE, re.M)
    assert command is not None, "Compose must preserve image defaults before starting uvicorn"
    body = command.group(1)
    script = re.search(r"^      - \|\n((?:        .*\n|\n)+)", body, re.M)
    assert script is not None
    arguments = re.findall(r"^      - (.+)$", body, re.M)
    assert arguments[:3] == ["python", "-c", "|"]
    return textwrap.dedent(script.group(1)).replace("$$", "$"), [
        argument.strip('"') for argument in arguments[3:]
    ]


@pytest.mark.parametrize("variant", ["lean", "hotels"])
@pytest.mark.parametrize("override", [None, "", "0", "false", "no", "off", "1"])
def test_booking_runtime_preserves_image_defaults_and_explicit_overrides(
    variant, override, monkeypatch
):
    assert "FLIGHTOPT_HOTELS_BOOKING_OVERRIDE: ${FLIGHTOPT_HOTELS_BOOKING:-}" in COMPOSE
    assert not re.search(r"^\s+FLIGHTOPT_HOTELS_BOOKING:", COMPOSE, re.M)
    script, _ = compose_launcher()
    image_setting = re.search(
        r"^ENV FLIGHTOPT_HOTELS_BOOKING=(\S+)$", stages()[f"{variant}-env"], re.M
    )
    image_default = image_setting.group(1) if image_setting else None
    monkeypatch.delenv("FLIGHTOPT_HOTELS_BOOKING", raising=False)
    monkeypatch.delenv("FLIGHTOPT_HOTELS_BOOKING_OVERRIDE", raising=False)
    if image_default is not None:
        monkeypatch.setenv("FLIGHTOPT_HOTELS_BOOKING", image_default)
    if override is not None:
        monkeypatch.setenv("FLIGHTOPT_HOTELS_BOOKING_OVERRIDE", override)
    executions = []
    monkeypatch.setattr(sys, "argv", ["-c", "launcher-probe", "--test"])
    monkeypatch.setattr(os, "execvp", lambda *args: executions.append(args))
    exec(compile(script, "<compose booking launcher>", "exec"), {})
    assert os.environ.get("FLIGHTOPT_HOTELS_BOOKING", "0") == (
        override or image_default or "0"
    )
    assert executions == [("launcher-probe", ["launcher-probe", "--test"])]


def test_the_compose_launcher_executes_the_image_command():
    _, arguments = compose_launcher()
    image_command = re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M)
    assert image_command is not None
    assert arguments == json.loads(image_command.group(1))


def test_the_compose_passes_the_optional_discord_webhook():
    assert "FLIGHTOPT_DISCORD_WEBHOOK: ${FLIGHTOPT_DISCORD_WEBHOOK:-}" in COMPOSE


def test_the_compose_keeps_a_memory_limit_and_makes_it_settable():
    """Ein Chromium ohne Speichergrenze nimmt den Wirt mit.

    Die Grenze bleibt Pflicht und bleibt bei 512m, solange niemand sie
    hochsetzt: wer den Browser einschaltet, soll den Wert bewusst mitgeben.
    """
    assert re.search(r"^\s*mem_limit:\s*\$\{FLIGHTOPT_MEM_LIMIT:-512m\}", COMPOSE, re.M)


def test_the_compose_carries_the_browser_budget():
    for key in (
        "FLIGHTOPT_HOTELS_BOOKING",
        "FLIGHTOPT_HOTELS_BROWSER_PAGES",
        "FLIGHTOPT_HOTELS_BROWSER_IDLE",
    ):
        assert key in COMPOSE


def test_the_env_example_names_the_new_switches_without_values():
    example = (ROOT / "deploy" / "portainer.env.example").read_text(encoding="utf-8")

    assert f"{VARIANT}=" in example
    assert "FLIGHTOPT_MEM_LIMIT=" in example
    assert "FLIGHTOPT_HOTELS_BOOKING=" in example


def test_the_deployment_notes_no_longer_call_booking_local_only():
    """Der Satz "bleibt dem lokalen Rechner vorbehalten" war der alte Stand."""
    assert "dem lokalen Rechner vorbehalten" not in DEPLOYMENT
    assert "hotels-env" in DEPLOYMENT or "FLIGHTOPT_VARIANT" in DEPLOYMENT
