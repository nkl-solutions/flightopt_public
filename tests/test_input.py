"""Trip types, airport lookup and the airline registry.

These cover the parts a person touches directly, where a wrong answer is
silently wrong rather than an error: picking the wrong airport, or a return
trip quietly becoming a one-way.
"""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.api.main import SearchRequest
from flightopt.domain import airlines
from flightopt.domain.airports import ALIASES, by_code, expand_code, search

W = {"window_start": date(2026, 10, 1), "window_end": date(2026, 11, 30)}


# --- trip types --------------------------------------------------------------


def test_return_trip_flies_home_again():
    spec = SearchRequest(airports=["BER", "ATH"], trip="return", **W).to_spec()
    assert spec.route == "BER-ATH-BER"
    assert len(spec.legs) == 2
    assert len(spec.stays) == 1


def test_one_way_has_no_stay():
    spec = SearchRequest(airports=["BER", "ATH"], trip="one_way", **W).to_spec()
    assert spec.route == "BER-ATH"
    assert len(spec.legs) == 1
    assert spec.stays == ()


def test_one_way_ignores_extra_airports():
    # The form keeps what was typed when switching tabs; a one-way must still
    # only use the first two fields rather than silently inventing a leg.
    spec = SearchRequest(airports=["BER", "ATH", "SKG"], trip="one_way", **W).to_spec()
    assert spec.route == "BER-ATH"


def test_multi_keeps_every_stop():
    spec = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi", **W
    ).to_spec()
    assert spec.route == "BER-FCO-ATH-BER"
    assert len(spec.stays) == 2


def test_multi_needs_three_airports():
    with pytest.raises(ValueError, match="mindestens drei"):
        SearchRequest(airports=["BER", "ATH"], trip="multi", **W).to_spec()


def test_return_needs_two_airports():
    with pytest.raises(ValueError, match="Start und Ziel"):
        SearchRequest(airports=["BER"], trip="return", **W).to_spec()


def test_unknown_trip_type_is_rejected():
    with pytest.raises(ValueError, match="Unbekannte Reiseart"):
        SearchRequest(airports=["BER", "ATH"], trip="loop", **W).to_spec()


def test_one_stay_is_applied_to_every_stop():
    spec = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi", stays=[[2, 5]], **W
    ).to_spec()
    assert [(s.min_nights, s.max_nights) for s in spec.stays] == [(2, 5), (2, 5)]


def test_per_stop_stays_are_kept_apart():
    spec = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi",
        stays=[[2, 3], [7, 9]], **W,
    ).to_spec()
    assert [(s.min_nights, s.max_nights) for s in spec.stays] == [(2, 3), (7, 9)]


def test_per_stop_stays_change_the_estimated_search_size():
    uniform = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi",
        stays=[[2, 3]], **W,
    ).to_spec()
    split = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi",
        stays=[[2, 3], [7, 9]], **W,
    ).to_spec()

    from flightopt.search.dp import count_combinations

    assert count_combinations(split) != count_combinations(uniform)


def test_wrong_number_of_stays_is_rejected():
    with pytest.raises(ValueError, match="genau 2"):
        SearchRequest(
            airports=["BER", "FCO", "ATH", "BER"], trip="multi",
            stays=[[2, 3], [4, 5], [6, 7]], **W,
        ).to_spec()


def test_job_serialisation_keeps_per_stop_stays():
    from flightopt.jobs.runner import spec_to_dict

    spec = SearchRequest(
        airports=["BER", "FCO", "ATH", "BER"], trip="multi",
        stays=[[2, 3], [7, 9]], **W,
    ).to_spec()

    assert spec_to_dict(spec)["stays"] == [[2, 3], [7, 9]]


def test_codes_are_normalised():
    spec = SearchRequest(airports=[" ber ", "ath"], trip="return", **W).to_spec()
    assert spec.route == "BER-ATH-BER"


def test_region_group_expands_to_concrete_search_specs():
    specs = SearchRequest(airports=["DE-OST", "ATH"], trip="one_way", **W).to_specs()

    assert sorted(s.route for s in specs) == ["BER-ATH", "DRS-ATH", "LEJ-ATH"]


# --- airport lookup ----------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("AYT", "AYT"),
        ("antalya", "AYT"),
        ("thess", "SKG"),
        ("saloniki", "SKG"),
        ("malle", "PMI"),
        ("mallorca", "PMI"),
        ("koeln", "CGN"),
        ("munich", "MUC"),
        ("muenchen", "MUC"),
        ("zurich", "ZRH"),
        ("zuerich", "ZRH"),
        ("kreta", "HER"),
        ("danzig", "GDN"),
    ],
)
def test_first_hit_is_the_obvious_one(query, expected):
    hits = search(query, 5)
    assert hits, f"no airport found for {query!r}"
    assert hits[0].code == expected


def test_exact_code_outranks_a_name_match():
    # 'BER' is both a code and the start of 'Bergamo'; the code has to win.
    assert search("BER", 5)[0].code == "BER"


def test_city_with_several_airports_lists_them_all():
    codes = {a.code for a in search("london", 8)}
    assert {"LHR", "STN", "LGW", "LTN"} <= codes


def test_airport_region_group_is_suggested():
    hit = search("Ostflughäfen", 5)[0]

    assert hit.code == "DE-OST"
    assert hit.as_dict()["kind"] == "group"
    assert hit.as_dict()["airports"] == ["BER", "LEJ", "DRS"]
    assert expand_code(hit.code) == ("BER", "LEJ", "DRS")


def test_country_group_outranks_country_airports():
    hit = search("Deutschland", 5)[0]

    assert hit.code == "DE"
    assert "BER" in hit.as_dict()["airports"]


def test_short_and_empty_queries_are_safe():
    assert search("", 5) == []
    assert search("   ", 5) == []
    assert isinstance(search("x", 5), list)


def test_nonsense_query_finds_nothing():
    assert search("qqzzxx", 5) == []


def test_limit_is_honoured():
    assert len(search("a", 3)) <= 3


def test_known_airports_carry_a_city_and_country():
    for code in ("BER", "AYT", "ATH", "FCO"):
        a = by_code(code)
        assert a is not None, code
        assert a.city and a.country


def test_aliases_point_at_real_airports():
    missing = [code for code in ALIASES if by_code(code) is None]
    assert not missing, f"alias for unknown airport: {missing}"


# --- airline registry --------------------------------------------------------


def test_live_airlines_are_the_ones_with_adapters():
    live = {a.code for a in airlines.LIVE if a.kind == "airline"}
    assert live == {"FR", "W6", "A3", "EW", "DE", "DI", "BA", "FI"}


def test_comparison_source_is_not_an_airline():
    # Kiwi sells nothing and flies nothing; showing it as a carrier would put a
    # tail on a result no such airline operates.
    kiwi = airlines.get("KIWI")
    assert kiwi is not None
    assert kiwi.kind == "comparison"
    assert all(a.kind == "airline" for a in airlines.AIRLINES.values()
               if a.code != "KIWI")


def test_every_live_airline_names_its_adapter():
    assert all(a.source for a in airlines.LIVE)


def test_blocked_airlines_have_no_adapter():
    for a in airlines.AIRLINES.values():
        if a.status == "blocked":
            assert a.source is None, f"{a.code} is blocked but claims a source"


def test_every_airline_explains_itself():
    for a in airlines.AIRLINES.values():
        assert a.note, f"{a.code} has no note"
        assert a.color.startswith("#")


def test_registry_serialises_for_the_ui():
    rows = airlines.as_dicts()
    assert rows
    assert {"code", "name", "color", "status", "note", "kind"} == set(rows[0])


# --- adapters covering several airlines --------------------------------------


def test_condor_adapter_also_covers_marabu():
    # Both brands sell through one booking engine. Filtering on Marabu alone
    # must still reach that adapter, or the search fails with "no source".
    from flightopt.sources.condor import CondorSource

    assert set(CondorSource().carriers) == {"DE", "DI"}


def test_every_live_airline_is_reachable_by_a_filter():
    """No airline may be offered in the UI that a filter cannot select."""
    from flightopt.sources.aegean import AegeanSource
    from flightopt.sources.britishairways import BritishAirwaysSource
    from flightopt.sources.condor import CondorSource
    from flightopt.sources.eurowings import EurowingsSource
    from flightopt.sources.icelandair import IcelandairSource
    from flightopt.sources.ryanair import RyanairSource
    from flightopt.sources.wizz import WizzSource

    covered: set[str] = set()
    for src in (RyanairSource(), WizzSource(), AegeanSource(), CondorSource(),
                EurowingsSource(), BritishAirwaysSource(), IcelandairSource()):
        covered |= set(src.carriers)

    offered = {a.code for a in airlines.LIVE if a.kind == "airline"}
    assert offered <= covered, f"kein Adapter fuer: {offered - covered}"
