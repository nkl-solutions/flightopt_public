"""Was nicht mehr gebraucht wird, darf auch nicht mehr installiert werden.

Die drei Spike-Bibliotheken wurden nur von spike/p0_probe.py importiert, zogen
aber ein gutes Dutzend Pakete ins Image, und die git-Quelle zwang das Dockerfile
dazu, git zu installieren. Der Consent-Code war nie live im Einsatz.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import flightopt

ROOT = Path(flightopt.__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_project_is_named_after_the_package():
    assert 'name = "flightopt"' in read("pyproject.toml")


def test_spike_only_dependencies_are_gone():
    text = read("pyproject.toml")

    assert "fast-flights" not in text
    assert "swoop-flights" not in text
    assert '"flights"' not in text
    assert "[tool.uv.sources]" not in text


def test_the_lockfile_no_longer_carries_the_spike_tree():
    text = read("uv.lock")

    assert 'name = "fast-flights"' not in text
    assert 'name = "swoop-flights"' not in text
    assert 'name = "flights"' not in text
    assert 'name = "primp"' not in text
    assert 'name = "selectolax"' not in text


def test_the_image_does_not_install_git_any_more():
    text = read("Dockerfile")

    assert "install -y --no-install-recommends ca-certificates" in text
    assert "git ca-certificates" not in text


def test_the_consent_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import flightopt.sources.google_consent  # noqa: F401


def test_the_consent_fixture_is_gone():
    assert not (ROOT / "tests" / "fixtures" / "google_consent_wall.html").exists()


def test_the_old_spike_names_its_missing_dependencies():
    assert "nicht mehr in pyproject" in read("spike/p0_probe.py")
