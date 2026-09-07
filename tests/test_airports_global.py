"""Globale Flughafenbasis: Build-Skript, Filterregeln, Merge.

Die Datenbasis waechst von 244 kuratierten auf rund 3.500 Eintraege. Die
Filterregeln entscheiden, ob ein Ergebnis brauchbar bleibt, und der Merge
entscheidet, ob die handgepflegten deutschen Namen ueberleben.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_airports  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def sample_csv() -> str:
    return (FIXTURES / "ourairports_sample.csv").read_text(encoding="utf-8")


# --- Filterregeln ------------------------------------------------------------


def test_parse_csv_keeps_only_scheduled_airports_with_an_iata_code():
    rows = build_airports.parse_csv(sample_csv())
    codes = [r["code"] for r in rows]

    assert len(rows) == 24
    assert codes == sorted(codes)
    # closed, small_airport, heliport, ohne Linienverkehr, ohne IATA-Code
    assert "TXL" not in codes
    assert "GWT" not in codes
    assert "LBG" not in codes
    assert all(r["code"] for r in rows)


def test_parse_csv_maps_the_upstream_columns():
    rows = {r["code"]: r for r in build_airports.parse_csv(sample_csv())}

    ber = rows["BER"]
    assert ber["name"] == "Berlin Brandenburg Airport"
    assert ber["city"] == "Berlin"
    assert ber["country"] == "Deutschland"
    assert ber["cc"] == "DE"
    assert ber["lat"] == 52.35139
    assert ber["lon"] == 13.493889
    assert ber["size"] == "large"
    assert ber["carriers"] == []
    assert ber["aliases"] == []
    assert rows["LCY"]["size"] == "medium"
    assert rows["DMK"]["size"] == "medium"


def test_parse_csv_falls_back_to_the_iso_code_for_unknown_countries():
    text = (
        "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,"
        "iso_country,iso_region,municipality,scheduled_service,gps_code,"
        "iata_code,local_code,home_link,wikipedia_link,keywords\n"
        "1,XXXX,large_airport,Nirgendwo,1.0,2.0,3,AF,XX,XX-1,Nirgendwo,yes,XXXX,NIR,,,,\n"
    )
    assert build_airports.parse_csv(text)[0]["country"] == "XX"


HEADER = (
    "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,"
    "iso_country,iso_region,municipality,scheduled_service,gps_code,"
    "iata_code,local_code,home_link,wikipedia_link,keywords"
)
MEDIUM = "1,AAAA,medium_airport,Klein,1.0,2.0,3,EU,DE,DE-BE,Klein,yes,AAAA,DUP,,,,"
LARGE = "2,BBBB,large_airport,Gross,4.0,5.0,6,EU,DE,DE-BE,Gross,yes,BBBB,DUP,,,,"


def csv_text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADER, *lines))


def test_parse_csv_keeps_the_larger_airport_when_an_iata_code_appears_twice():
    """Upstream vergibt denselben IATA-Code manchmal zweimal.

    Ohne Entscheidung gewinnt die letzte Zeile - und das ist mal der grosse
    Flughafen, mal der Regionalplatz nebenan. Gross schlaegt mittel.
    """
    for lines in ([MEDIUM, LARGE], [LARGE, MEDIUM]):
        report = build_airports.BuildReport()
        rows = build_airports.parse_csv(csv_text(*lines), report=report)

        assert [r["code"] for r in rows] == ["DUP"]
        assert rows[0]["name"] == "Gross"
        assert rows[0]["size"] == "large"
        assert report.duplicate_codes == 1


def test_parse_csv_counts_rows_it_skips_for_broken_coordinates():
    broken = "3,CCCC,large_airport,Ohne,,,7,EU,DE,DE-BE,Ohne,yes,CCCC,NOC,,,,"
    report = build_airports.BuildReport()

    rows = build_airports.parse_csv(csv_text(broken), report=report)

    assert rows == []
    assert report.skipped_coordinates == 1


# --- Merge -------------------------------------------------------------------


def existing_rows() -> list[dict]:
    return [
        {"code": "BER", "name": "Berlin Brandenburg", "city": "Berlin",
         "country": "Germany", "cc": "DE", "lat": 52.3667, "lon": 13.5033,
         "carriers": ["FR"]},
        # Upstream fuehrt GWT als small_airport: der Filter wirft ihn raus,
        # geschlossen ist er aber nicht.
        {"code": "GWT", "name": "Sylt", "city": "Westerland",
         "country": "Germany", "cc": "DE", "lat": 54.9132, "lon": 8.3405,
         "carriers": []},
        # Upstream fuehrt TXL als closed.
        {"code": "TXL", "name": "Berlin Tegel", "city": "Berlin",
         "country": "Germany", "cc": "DE", "lat": 52.5597, "lon": 13.2877,
         "carriers": []},
    ]


def test_merge_lets_the_curated_entry_win_on_names_and_carriers():
    merged = {
        r["code"]: r
        for r in build_airports.merge(
            existing_rows(), build_airports.parse_csv(sample_csv()), {}
        )
    }

    assert merged["BER"]["name"] == "Berlin Brandenburg"
    assert merged["BER"]["carriers"] == ["FR"]
    # Groesse und Koordinaten kommen frisch aus OurAirports.
    assert merged["BER"]["size"] == "large"
    assert merged["BER"]["lat"] == 52.35139


def test_merge_takes_the_country_from_the_german_table_not_from_the_bestand():
    """Der Bestand kennt "Germany", die Basis spricht Deutsch.

    Solange das gepflegte Land gewann, trug ein Teil der Basis englische
    Landesnamen: "Deutschland" fand dann die Flughaefen nicht, die es am
    dringendsten braucht.
    """
    merged = {
        r["code"]: r
        for r in build_airports.merge(
            existing_rows(), build_airports.parse_csv(sample_csv()), {}
        )
    }

    # BER steht upstream, GWT nur im Bestand - beide auf Deutsch.
    assert merged["BER"]["country"] == "Deutschland"
    assert merged["GWT"]["country"] == "Deutschland"
    assert merged["GWT"]["cc"] == "DE"
    assert "Germany" not in {r["country"] for r in merged.values()}


def test_merge_keeps_curated_entries_that_upstream_dropped():
    report = build_airports.BuildReport()
    merged = {
        r["code"]: r
        for r in build_airports.merge(
            existing_rows(), build_airports.parse_csv(sample_csv()), {},
            report=report,
        )
    }

    assert merged["GWT"]["name"] == "Sylt"
    assert merged["GWT"]["size"] == "medium"
    assert merged["GWT"]["lat"] == 54.9132
    assert report.curated_missing_upstream == ["GWT", "TXL"]


def test_merge_drops_curated_entries_that_upstream_marks_closed():
    # Tegel ist seit 2020 zu. Der Bestand darf ihn nicht wieder einschleusen.
    report = build_airports.BuildReport()
    merged = {
        r["code"]: r
        for r in build_airports.merge(
            existing_rows(), build_airports.parse_csv(sample_csv()), {},
            closed=build_airports.closed_codes(sample_csv()),
            report=report,
        )
    }

    assert "TXL" not in merged
    assert "GWT" in merged
    assert report.curated_closed == ["TXL"]
    assert report.curated_missing_upstream == ["GWT"]


def test_closed_codes_lists_only_the_closed_rows():
    assert build_airports.closed_codes(sample_csv()) == {"TXL"}


def test_closed_codes_finds_the_code_in_the_keywords_column():
    """Upstream raeumt `iata_code` geschlossener Zeilen leer.

    Tegel steht dort inzwischen ohne IATA-Code, der Code lebt nur noch in den
    Stichworten. Wird die Spalte nicht gelesen, kehrt Tegel bei jedem Neubau
    aus dem gepflegten Bestand zurueck.
    """
    stripped = (
        "2439,EDDT,closed,Berlin Tegel,52.5597,13.2877,122,EU,DE,DE-BE,"
        "Berlin,no,,,,,,\"TXL, EDDT, Otto Lilienthal\""
    )

    assert build_airports.closed_codes(csv_text(stripped)) == {"TXL"}


def test_merge_attaches_aliases_and_ignores_unknown_codes():
    rows = build_airports.merge(
        existing_rows(),
        build_airports.parse_csv(sample_csv()),
        {"NRT": ["tokio", "narita"], "QQQ": ["nirgendwo"]},
    )
    merged = {r["code"]: r for r in rows}

    assert merged["NRT"]["aliases"] == ["tokio", "narita"]
    assert merged["ATH"]["aliases"] == []
    assert "QQQ" not in merged
    assert [r["code"] for r in rows] == sorted(r["code"] for r in rows)


def test_write_json_writes_one_airport_per_line(tmp_path):
    out = tmp_path / "airports.json"
    rows = build_airports.merge(
        existing_rows(), build_airports.parse_csv(sample_csv()), {}
    )
    build_airports.write_json(rows, out)

    text = out.read_text(encoding="utf-8")
    assert text.startswith("[\n{")
    assert text.endswith("}\n]\n")
    assert len(text.splitlines()) == len(rows) + 2
    assert json.loads(text) == rows
    # Zeilenenden bleiben LF, sonst wechselt die Datei je nach Rechner.
    assert b"\r" not in out.read_bytes()


# --- Lauf des Skripts --------------------------------------------------------


def test_main_prints_an_audit_block_before_the_summary(tmp_path, capsys):
    """Wer die Basis neu baut, muss sehen, was dabei verloren ging."""
    existing = tmp_path / "existing.json"
    existing.write_text(json.dumps(existing_rows()), encoding="utf-8")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"NRT": ["tokio"]}), encoding="utf-8")
    out = tmp_path / "airports.json"

    code = build_airports.main([
        "--source", str(FIXTURES / "ourairports_sample.csv"),
        "--existing", str(existing),
        "--aliases", str(aliases),
        "--out", str(out),
    ])
    printed = capsys.readouterr().out

    assert code == 0
    assert "uebersprungen ohne Koordinaten: 0" in printed
    assert "doppelte IATA-Codes: 0" in printed
    assert "Bestand ohne Upstream (1): GWT" in printed
    assert "Bestand upstream geschlossen (1): TXL" in printed
    assert "upstream 24, bestand 3, ergebnis 25" in printed
    assert printed.index("Bestand ohne Upstream") < printed.index("upstream 24")


# --- Schema, Gruppen, Distanz ------------------------------------------------

from flightopt.domain import airports as registry  # noqa: E402


def test_airports_carry_coordinates_and_a_size():
    ber = registry.by_code("BER")

    assert ber is not None
    assert ber.lat and ber.lon
    assert ber.size in {"large", "medium"}
    assert isinstance(ber.aliases, tuple)
    assert set(ber.as_dict()) >= {"code", "name", "city", "country", "cc",
                                  "lat", "lon", "size", "carriers", "aliases"}


def test_groups_come_from_the_json_file():
    assert len(registry.GROUPS) >= 35
    assert registry.GROUPS["TYO"].airports == ("NRT", "HND")
    assert registry.GROUPS["SEL"].airports == ("ICN", "GMP")
    assert registry.expand_code("TYO") == ("NRT", "HND")
    assert registry.expand_code("ATH") == ("ATH",)


def test_de_ost_group_is_unchanged_after_the_move_to_json():
    group = registry.GROUPS["DE-OST"]

    assert group.name == "Berlin, Leipzig/Halle, Dresden"
    assert group.city == "Ostflughäfen"
    assert group.airports == ("BER", "LEJ", "DRS")
    assert registry.expand_code("DE-OST") == ("BER", "LEJ", "DRS")


def test_a_group_that_is_only_its_own_airport_is_not_listed_twice():
    hits = registry.search("BER", 5)

    assert hits[0].code == "BER"
    assert [h.code for h in hits].count("BER") == 1
    # Fuer die Routenaufloesung bleibt die Gruppe trotzdem nutzbar.
    assert registry.expand_code("BER") == ("BER",)


def test_country_search_puts_the_country_group_first():
    assert registry.search("Japan", 5)[0].code == "JP"


def test_city_search_puts_the_metro_group_first():
    assert registry.search("Tokio", 5)[0].code == "TYO"


def test_distance_uses_the_great_circle():
    assert 1700 < registry.distance_km("BER", "ATH") < 1900
    assert 2100 < registry.distance_km("BER", "AYT") < 2300
    assert registry.distance_km("BER", "BER") == 0
    assert registry.distance_km("BER", "QQQ") is None


def test_short_legs_allow_one_stop_long_legs_two():
    assert registry.auto_max_stops("BER", "AYT") == 1
    assert registry.auto_max_stops("BER", "ATH") == 1
    # Unbekannte Strecke: lieber einen Umstieg zulassen als keinen Preis.
    assert registry.auto_max_stops("BER", "QQQ") == 1


def test_resolve_turns_names_into_codes():
    assert registry.resolve("Tokio") == "TYO"
    assert registry.resolve("ber") == "BER"
    assert registry.resolve("DE-OST") == "DE-OST"
    assert registry.resolve("qqzzxx") is None


# --- globale Basis -----------------------------------------------------------


def test_the_world_is_in_the_list():
    codes = {a.code for a in registry.all_airports()}

    assert len(codes) >= 3000
    assert {"NRT", "HND", "ICN", "GMP", "KIX", "PEK", "BKK", "SIN", "DXB",
            "JFK", "LAX", "KEF", "GRU", "YYZ", "SYD", "CPT"} <= codes
    # Der gepflegte Bestand ueberlebt den Neubau.
    assert {"BER", "AYT", "SKG"} <= codes
    # Tegel nicht: upstream fuehrt ihn als geschlossen, seit 2020 zu Recht.
    assert "TXL" not in codes


def test_the_curated_german_names_survived_the_rebuild():
    ber = registry.by_code("BER")

    assert ber.name == "Berlin Brandenburg"
    assert ber.city == "Berlin"
    assert "FR" in ber.carriers


def test_the_file_stays_under_the_size_budget():
    assert registry.DATA.stat().st_size < 700_000
    # utf-8 und LF, sonst rauscht die Datei bei jedem Rechnerwechsel durch.
    raw = registry.DATA.read_bytes()
    assert b"\r" not in raw
    raw.decode("utf-8")


def test_aliases_come_from_the_json_file():
    raw = json.loads(
        (registry.DATA_DIR / "airport_aliases_de.json").read_text(encoding="utf-8")
    )

    assert registry.ALIASES["NRT"] == tuple(raw["NRT"])
    assert "tokio" in registry.ALIASES["NRT"]
    assert registry.by_code("NRT").aliases == tuple(raw["NRT"])


def test_every_alias_is_baked_into_the_base():
    """Die Alias-Datei ist die Pflegequelle, `airports.json` die Laufzeitquelle.

    Die Suche liest nur noch die Basis. Wer einen Alias eintraegt und das
    Build-Skript vergisst, faende ihn sonst nirgends wieder - dieser Test faellt
    dann statt der Suche.
    """
    missing = {
        code: names
        for code, names in registry.ALIASES.items()
        if registry.by_code(code) is None
    }
    stale = {
        code: (names, registry.by_code(code).aliases)
        for code, names in registry.ALIASES.items()
        if registry.by_code(code) is not None
        and registry.by_code(code).aliases != names
    }

    assert not missing, f"Aliase fuer unbekannte Codes: {sorted(missing)}"
    assert not stale, f"Basis nicht neu gebaut, Aliase weichen ab: {sorted(stale)}"


def test_the_country_names_are_german_everywhere():
    """Eine Basis mit zwei Sprachen laesst die Landessuche halb ins Leere laufen."""
    airports = registry.all_airports()
    countries = {a.country for a in airports}

    assert "Germany" not in countries
    assert "Deutschland" in countries
    assert registry.by_code("BER").country == "Deutschland"
    # Der gepflegte Bestand trug englische Namen, auch dort wo upstream fehlt.
    assert registry.by_code("LXS").country == "Griechenland"
    assert registry.by_code("NRT").country == "Japan"

    # Fehlt ein Land in COUNTRIES_DE, faellt der Bau auf den ISO-Code zurueck.
    # Das ist nie falsch, aber unsuchbar: "Kongo" fand die Flughaefen nicht.
    bare = sorted(
        {a.country for a in airports
         if len(a.country) == 2 and a.country.isupper() and a.country.isalpha()}
    )

    assert bare == [], f"Laender ohne deutschen Namen: {bare}"


def test_the_country_table_covers_every_country_in_the_base():
    """Sonst faellt beim naechsten Neubau still wieder ein Land auf den Code."""
    unnamed = sorted(
        {a.cc for a in registry.all_airports()} - set(build_airports.COUNTRIES_DE)
    )

    assert unnamed == [], f"Laendercodes ohne Eintrag: {unnamed}"


def test_a_search_for_a_newly_named_country_reaches_its_airports():
    hits = registry.search("Bolivien", 20)
    reached = set()
    for hit in hits:
        reached.update(registry.expand_code(hit.code))

    assert "VVI" in reached  # Santa Cruz


def test_a_country_search_finds_german_airports_in_both_languages():
    german = {"BER", "MUC", "FRA", "HAM"}
    for query in ("Deutschland", "Germany"):
        hits = registry.search(query, 20)
        reached = set()
        for hit in hits:
            reached.update(registry.expand_code(hit.code))

        assert german <= reached, query


def test_search_folds_the_entries_once_at_load_not_once_per_keystroke(monkeypatch):
    """3.245 Eintraege mal vier Felder pro Tastendruck war der teuerste Teil."""
    before = [h.code for h in registry.search("berlin", 5)]
    calls = []
    original = registry._fold
    monkeypatch.setattr(
        registry, "_fold", lambda value: (calls.append(value), original(value))[1]
    )

    after = [h.code for h in registry.search("berlin", 5)]

    assert after == before
    # Genau einmal: fuer die Eingabe selbst.
    assert calls == ["berlin"]


def test_the_metro_group_outranks_its_own_airports():
    hits = [h.code for h in registry.search("Tokio", 5)]

    assert hits[0] == "TYO"
    assert "NRT" in hits
    assert hits.index("TYO") < hits.index("NRT")


def test_berlin_to_tokyo_is_a_long_haul_leg():
    assert 8900 < registry.distance_km("BER", "NRT") < 9100
    assert registry.auto_max_stops("BER", "NRT") == 2
    assert registry.auto_max_stops("BER", "ICN") == 2


def test_a_code_is_matched_raw_because_folding_mangles_it():
    """_fold macht aus 'AER' ein 'ar' und aus 'UET' ein 'ut'."""
    assert registry._fold("AER") == "ar"
    assert registry._fold("UET") == "ut"

    # Gefaltet faenge 'AER' jeden Code an, der mit AR beginnt, und 'UET' jeden
    # mit UT. Der Code wird deshalb ungefaltet verglichen, und der getippte
    # Code selbst steht vorn.
    assert registry.search("AER", 8)[0].code == "AER"  # Sotschi
    assert registry.search("UET", 8)[0].code == "UET"  # Quetta


def test_a_group_added_at_runtime_does_not_break_the_search(monkeypatch):
    """GROUP_KEYS wird beim Import gefuellt; GROUPS kann ein Test ersetzen."""
    extra = registry.AirportGroup(
        code="XX-TEST",
        name="Testgruppe Kanaren",
        city="Kanaren",
        country="Spanien",
        cc="ES",
        airports=("TFS", "LPA"),
        aliases=("kanaren",),
    )
    monkeypatch.setattr(registry, "GROUPS", {**registry.GROUPS, "XX-TEST": extra})

    hits = [h.code for h in registry.search("Kanaren", 5)]

    assert hits[0] == "XX-TEST"


def test_search_prefers_an_exact_name_over_a_prefix():
    """Die Rangfolge aus dem Docstring, an einem Fall der frueher kippte."""
    hits = [h.code for h in registry.search("Berlin", 6)]

    assert hits[0] == "BER"
    assert hits.index("BER") < hits.index("DE-OST")
