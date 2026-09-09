"""Die Kommandozeile nimmt dasselbe entgegen wie das Formular.

Wer 'flightopt search BER TYO SEL BER' tippt, will nicht wissen, dass Tokio
zwei Flughaefen hat. Diese Tests fassen kein Netz an: geprueft wird nur, was
aus der Eingabe fuer eine Suche wird.
"""

from __future__ import annotations

from datetime import date

import pytest

from flightopt import cli
from flightopt.domain import airports as registry
from flightopt.storage import db
from flightopt.storage.watchlist import list_routes


def parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def test_window_accepts_two_dates_and_the_colon_form():
    assert cli.parse_window(["2027-03-01", "2027-04-30"]) == (
        date(2027, 3, 1), date(2027, 4, 30)
    )
    assert cli.parse_window(["2027-03-01:2027-04-30"]) == (
        date(2027, 3, 1), date(2027, 4, 30)
    )
    start, end = cli.parse_window(["2027-03-01"])
    assert (start, end) == (date(2027, 3, 1), date(2027, 4, 29))


def test_stops_are_resolved_from_names_and_groups():
    assert cli.resolve_stops(["BER", "Tokio", "Seoul", "ber"]) == [
        "BER", "TYO", "SEL", "BER"
    ]


def test_an_unknown_stop_is_a_clear_error():
    with pytest.raises(SystemExit, match="qqzzxx"):
        cli.resolve_stops(["BER", "qqzzxx"])


def test_a_group_route_becomes_every_concrete_variant():
    args = parse(["search", "BER", "Tokio", "Seoul", "BER",
                  "--window", "2027-03-01", "2027-04-30",
                  "--stay", "7-14", "--stay", "4-8"])
    specs = cli.build_specs(args)

    assert sorted(s.route for s in specs) == [
        "BER-HND-GMP-BER", "BER-HND-ICN-BER",
        "BER-NRT-GMP-BER", "BER-NRT-ICN-BER",
    ]
    assert [(s.min_nights, s.max_nights) for s in specs[0].stays] == [(7, 14), (4, 8)]
    assert specs[0].window_start == date(2027, 3, 1)
    assert specs[0].max_stops is None


def test_max_stops_reaches_every_variant():
    args = parse(["search", "BER", "Tokio", "BER",
                  "--window", "2027-03-01", "2027-04-30",
                  "--stay", "7-14", "--max-stops", "1"])

    assert all(s.max_stops == 1 for s in cli.build_specs(args))


def test_a_group_explosion_is_refused_before_any_request():
    args = parse(["search", "Deutschland", "Tuerkei",
                  "--window", "2027-03-01", "2027-04-30"])

    with pytest.raises(registry.TooManyVariants):
        cli.build_specs(args)


def test_the_group_explosion_reaches_the_shell_as_a_message():
    """Auf der Kommandozeile ist ein Traceback keine Fehlermeldung."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["search", "Deutschland", "Tuerkei",
                  "--window", "2027-03-01", "2027-04-30"])

    assert "Flughafenkombinationen" in str(excinfo.value)
    assert not isinstance(excinfo.value.code, int)


def test_a_malformed_window_is_a_message_too():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["search", "BER", "NRT", "--window", "2027-13-99"])

    assert "2027-13-99" in str(excinfo.value)


def test_the_help_names_the_new_switches(capsys):
    with pytest.raises(SystemExit):
        cli.main(["search", "--help"])

    text = capsys.readouterr().out
    assert "--max-stops {0,1,2}" in text
    assert "--window WINDOW [WINDOW ...]" in text


# --- Was die Ausgabe zeigen muss ---------------------------------------------

from datetime import datetime, timedelta  # noqa: E402

from flightopt.domain.fx import Rates  # noqa: E402
from flightopt.domain.models import Money  # noqa: E402

RATES = Rates(base="EUR", rates={"JPY": 165.0}, fetched_at=datetime(2027, 3, 1))


class JapanCalendar:
    """Steht fuer Kiwi: deckt jede Strecke ab, preist ab BER in JPY."""

    name = "fakekiwi"
    carrier = ""
    carriers = ()
    supports_calendar = True
    supports_search = False
    indicative = True
    accepts_max_stops = True

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, *,
                             currency="EUR", max_stops=None):
        prices = {}
        day = start
        while day <= end:
            prices[day] = (Money(4850000, "JPY") if origin == "BER"
                           else Money(45000, "EUR"))
            day += timedelta(days=1)
        return prices


def search_args(tmp_path, *extra: str):
    return parse(["search", "BER", "NRT", "BER",
                  "--window", "2027-03-01", "2027-03-08",
                  "--stay", "3-4", "--db", str(tmp_path / "cli.db"), *extra])


def offline(monkeypatch, sources: list) -> None:
    async def fake_rates(conn, **kw):
        return RATES

    monkeypatch.setattr(cli, "build_catalogue", lambda wanted, **kw: sources)
    monkeypatch.setattr(cli.fx_store, "current_rates", fake_rates)


def test_a_leg_line_names_the_original_price_and_the_stops():
    leg = {"origin": "BER", "destination": "NRT", "date": "2027-03-10",
           "price": 293.94, "stops": 1,
           "price_native": {"amount": 48500.0, "currency": "JPY"}}

    assert cli.format_leg(leg) == (
        "BER-NRT  2027-03-10  293.94 EUR  umgerechnet aus 48.500 JPY  1 Umstieg"
    )


def test_a_leg_line_stays_quiet_about_what_it_does_not_know():
    leg = {"origin": "NRT", "destination": "BER", "date": "2027-03-14",
           "price": 450.0, "stops": None}

    assert cli.format_leg(leg) == "NRT-BER  2027-03-14  450.00 EUR"


def test_a_direct_flight_says_so():
    leg = {"origin": "BER", "destination": "NRT", "date": "2027-03-10",
           "price": 293.94, "stops": 0}

    assert cli.format_leg(leg).endswith("Direktflug")


@pytest.mark.asyncio
async def test_the_output_names_every_source_and_the_original_currency(
    tmp_path, monkeypatch, capsys
):
    args = search_args(tmp_path, "--top", "1")
    offline(monkeypatch, [JapanCalendar()])

    code = await cli.run_search(args)
    out = capsys.readouterr().out

    assert code == 0
    # Welche Quelle wie viele Tage geliefert hat, stand frueher schon da.
    assert "fakekiwi: " in out
    assert "priced dates" in out
    assert "umgerechnet aus 48.500 JPY" in out


@pytest.mark.asyncio
async def test_a_window_that_fits_no_stay_is_exit_code_one(tmp_path, capsys):
    args = parse(["search", "BER", "NRT", "BER",
                  "--window", "2027-03-01", "2027-03-03",
                  "--stay", "30-40", "--db", str(tmp_path / "cli.db")])

    assert await cli.run_search(args) == 1
    assert "No combination fits" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_search_without_any_price_is_exit_code_two(tmp_path, monkeypatch, capsys):
    args = search_args(tmp_path)
    offline(monkeypatch, [])

    assert await cli.run_search(args) == 2
    assert "no prices for" in capsys.readouterr().out


def test_watch_add_stores_the_route_and_prints_the_window(tmp_path, capsys):
    """Der Weg fuer den ersten Eintrag, ohne den Dienst neu zu starten."""
    path = str(tmp_path / "watch-cli.db")

    code = cli.main(["watch", "add", "Berlin", "Athen", "--db", path,
                     "--lead-min", "10", "--lead-max", "40"])

    assert code == 0
    conn = db.connect(path)
    routes = list_routes(conn)
    conn.close()
    assert [(r.origin, r.destination) for r in routes] == [("BER", "ATH")]
    assert (routes[0].lead_min_days, routes[0].lead_max_days) == (10, 40)
    out = capsys.readouterr().out
    assert "BER-ATH" in out
    assert "10 bis 40" in out


def test_watch_add_refuses_a_route_nobody_can_read(tmp_path):
    path = str(tmp_path / "watch-bad.db")

    with pytest.raises(SystemExit, match="qqzzxx"):
        cli.main(["watch", "add", "qqzzxx", "ATH", "--db", path])


def test_watch_add_refuses_an_airport_group(tmp_path):
    """Eine Beobachtung haengt an genau einem Schluessel, nicht an einer Gruppe."""
    path = str(tmp_path / "watch-group.db")

    with pytest.raises(SystemExit, match="Flughafengruppe"):
        cli.main(["watch", "add", "Tokio", "ATH", "--db", path])


def test_watch_list_shows_the_state_and_the_empty_case(tmp_path, capsys):
    path = str(tmp_path / "watch-list.db")

    assert cli.main(["watch", "list", "--db", path]) == 0
    assert "Keine Strecke" in capsys.readouterr().out

    cli.main(["watch", "add", "BER", "ATH", "--db", path])
    capsys.readouterr()

    assert cli.main(["watch", "list", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "BER-ATH" in out
    assert "an" in out


def test_watch_off_and_on_flip_the_switch(tmp_path, capsys):
    path = str(tmp_path / "watch-switch.db")
    cli.main(["watch", "add", "BER", "ATH", "--db", path])
    conn = db.connect(path)
    route_id = list_routes(conn)[0].id
    conn.close()

    assert cli.main(["watch", "off", str(route_id), "--db", path]) == 0
    conn = db.connect(path)
    assert list_routes(conn)[0].enabled is False
    conn.close()

    assert cli.main(["watch", "on", str(route_id), "--db", path]) == 0
    conn = db.connect(path)
    assert list_routes(conn)[0].enabled is True
    conn.close()


def test_watch_off_names_an_unknown_route(tmp_path):
    path = str(tmp_path / "watch-404.db")

    with pytest.raises(SystemExit, match="404"):
        cli.main(["watch", "off", "404", "--db", path])


def test_watch_run_collects_and_reports(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "watch-run.db")
    cli.main(["watch", "add", "BER", "ATH", "--db", path])
    capsys.readouterr()

    async def fake_run(conn, *, now=None):
        return {"routes": 1, "observations": 60, "calls": 2,
                "errors": ["BER-ATH: fake 403"], "due_left": 0}

    monkeypatch.setattr(cli, "run_watchlist", fake_run)

    assert cli.main(["watch", "run", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "60" in out
    assert "fake 403" in out


def test_the_watch_summary_counts_in_german(tmp_path, capsys):
    """"1 Strecken" liest sich wie ein Zaehlfehler, und genau danach sieht es aus."""
    path = str(tmp_path / "watch-plural.db")
    cli.main(["watch", "add", "BER", "ATH", "--db", path])
    capsys.readouterr()

    cli.main(["watch", "list", "--db", path])
    one = capsys.readouterr().out
    assert "1 Strecke," in one
    assert "1 Strecken" not in one

    cli.main(["watch", "add", "BER", "FCO", "--db", path])
    capsys.readouterr()
    cli.main(["watch", "list", "--db", path])

    assert "2 Strecken," in capsys.readouterr().out


def test_the_group_message_names_the_airports_it_stands_for(tmp_path):
    path = str(tmp_path / "watch-names.db")

    with pytest.raises(SystemExit, match="FCO"):
        cli.main(["watch", "add", "BER", "Rom", "--db", path])
