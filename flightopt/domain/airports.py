"""Airport lookup for the route input.

Nobody remembers that Thessaloniki is SKG. The form takes a city name and
resolves it, so the three-letter codes stay an implementation detail.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "airports.json"


@dataclass(frozen=True, slots=True)
class Airport:
    code: str
    name: str
    city: str
    country: str
    cc: str
    carriers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "city": self.city,
            "country": self.country,
            "cc": self.cc,
            "carriers": list(self.carriers),
        }


@dataclass(frozen=True, slots=True)
class AirportGroup:
    code: str
    name: str
    city: str
    country: str
    cc: str
    airports: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    carriers: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "city": self.city,
            "country": self.country,
            "cc": self.cc,
            "carriers": list(self.carriers),
            "kind": "group",
            "airports": list(self.airports),
        }


def _fold(value: str) -> str:
    """Reduce a name to one comparable key.

    Umlauts reach us in three spellings: 'Zürich', 'Zuerich' and 'Zurich'.
    Collapsing every one of them to the bare vowel makes all three match,
    which is what someone typing quickly will produce.
    """
    lowered = value.lower().strip()
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    for long, short in (("ae", "a"), ("oe", "o"), ("ue", "u"), ("ss", "s")):
        ascii_only = ascii_only.replace(long, short)
    return ascii_only


# The upstream list is English, the person typing is German. Either spelling
# has to find the airport, so both are searched.
ALIASES: dict[str, tuple[str, ...]] = {
    "CGN": ("koeln", "cologne", "koln"),
    "MUC": ("munich", "muenchen", "munchen"),
    "VIE": ("vienna", "wien"),
    "PRG": ("prag", "prague"),
    "WAW": ("warschau", "warsaw"),
    "BGY": ("mailand", "milan", "bergamo"),
    "MXP": ("mailand", "milan", "malpensa"),
    "LIN": ("mailand", "milan", "linate"),
    "FCO": ("rom", "rome", "fiumicino"),
    "CIA": ("rom", "rome", "ciampino"),
    "VCE": ("venedig", "venice", "venezia"),
    "FLR": ("florenz", "florence", "firenze"),
    "NAP": ("neapel", "naples", "napoli"),
    "TRN": ("turin", "torino"),
    "GVA": ("genf", "geneva", "geneve"),
    "ZRH": ("zuerich", "zurich"),
    "BSL": ("basel", "basle", "mulhouse"),
    "CPH": ("kopenhagen", "copenhagen"),
    "OSL": ("oslo",),
    "LIS": ("lissabon", "lisbon", "lisboa"),
    "ATH": ("athen", "athens", "athina"),
    "SKG": ("saloniki", "thessaloniki"),
    "HER": ("kreta", "crete", "heraklion", "iraklion"),
    "RHO": ("rhodos", "rhodes"),
    "JTR": ("santorin", "santorini", "thira"),
    "CFU": ("korfu", "corfu", "kerkyra"),
    "IST": ("istanbul",),
    "SAW": ("istanbul", "sabiha"),
    "AYT": ("antalya",),
    "ADB": ("izmir", "smyrna"),
    "BJV": ("bodrum",),
    "DLM": ("dalaman", "marmaris", "fethiye"),
    "LHR": ("london", "heathrow"),
    "STN": ("london", "stansted"),
    "LGW": ("london", "gatwick"),
    "LTN": ("london", "luton"),
    "CDG": ("paris", "roissy"),
    "ORY": ("paris", "orly"),
    "BVA": ("paris", "beauvais"),
    "BCN": ("barcelona",),
    "MAD": ("madrid",),
    "PMI": ("mallorca", "palma", "malle"),
    "AGP": ("malaga", "costa del sol"),
    "TFS": ("teneriffa", "tenerife"),
    "LPA": ("gran canaria", "las palmas"),
    "ACE": ("lanzarote",),
    "FUE": ("fuerteventura",),
    "BUD": ("budapest",),
    "OTP": ("bukarest", "bucharest"),
    "KRK": ("krakau", "krakow", "cracow"),
    "GDN": ("danzig", "gdansk"),
    "TLL": ("tallinn", "reval"),
    "RIX": ("riga",),
    "VNO": ("wilna", "vilnius"),
    "DUB": ("dublin",),
    "EDI": ("edinburgh", "edinburg"),
    "BER": ("berlin", "brandenburg", "schoenefeld"),
    "TXL": ("berlin", "tegel"),
    "HAM": ("hamburg",),
    "FRA": ("frankfurt",),
    "DUS": ("duesseldorf", "dusseldorf"),
    "NRN": ("weeze", "niederrhein"),
    "STR": ("stuttgart",),
    "NUE": ("nuernberg", "nuremberg"),
    "LEJ": ("leipzig", "halle"),
    "DRS": ("dresden",),
    "BRE": ("bremen",),
    "HAJ": ("hannover", "hanover"),
    "AMS": ("amsterdam", "schiphol"),
    "BRU": ("bruessel", "brussels"),
    "CRL": ("bruessel", "charleroi"),
}

GROUPS: dict[str, AirportGroup] = {
    "DE-OST": AirportGroup(
        code="DE-OST",
        name="Berlin, Leipzig/Halle, Dresden",
        city="Ostflughäfen",
        country="Deutschland",
        cc="DE",
        airports=("BER", "LEJ", "DRS"),
        aliases=("ost", "ostdeutschland", "ostflughafen", "ostflughäfen",
                 "berlin leipzig dresden", "leipzig dresden berlin"),
    ),
    "DE": AirportGroup(
        code="DE",
        name="Alle deutschen Flughäfen",
        city="Deutschland",
        country="Deutschland",
        cc="DE",
        airports=("BER", "LEJ", "DRS", "HAM", "FRA", "DUS", "CGN", "STR",
                  "MUC", "NUE", "BRE", "HAJ", "NRN"),
        aliases=("germany", "deutsche flughafen", "deutsche flughäfen",
                 "alle deutschen flughafen", "alle deutschen flughäfen"),
    ),
}


@lru_cache(maxsize=1)
def _load() -> tuple[list[Airport], dict[str, Airport]]:
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    airports = [
        Airport(
            code=r["code"],
            name=r["name"],
            city=r.get("city") or r["name"],
            country=r.get("country", ""),
            cc=r.get("cc", ""),
            carriers=tuple(r.get("carriers") or []),
        )
        for r in rows
    ]
    return airports, {a.code: a for a in airports}


def all_airports() -> list[Airport]:
    return _load()[0]


def by_code(code: str) -> Airport | None:
    return _load()[1].get(code.upper())


def expand_code(code: str) -> tuple[str, ...]:
    group = GROUPS.get(code.strip().upper())
    return group.airports if group else (code.strip().upper(),)


def search(query: str, limit: int = 8) -> list[Airport | AirportGroup]:
    """Rank matches so an exact code wins, then city, then anything containing it."""
    airports, by_c = _load()
    q = _fold(query)
    if not q:
        return []

    exact = by_c.get(query.strip().upper())
    exact_group = GROUPS.get(query.strip().upper())
    scored: list[tuple[int, str, Airport | AirportGroup]] = []
    for group in GROUPS.values():
        if exact_group is not None and group.code == exact_group.code:
            continue
        code = group.code.lower()
        fields = [_fold(group.city), _fold(group.name), _fold(group.country)]
        fields.extend(_fold(alias) for alias in group.aliases)
        if code == q:
            score = 0
        elif any(field.startswith(q) for field in fields):
            score = 1
        elif any(q in field for field in fields):
            score = 4
        else:
            continue
        scored.append((score, group.city, group))
    for a in airports:
        if exact is not None and a.code == exact.code:
            continue
        city, name, country = _fold(a.city), _fold(a.name), _fold(a.country)
        code = a.code.lower()
        aliases = [_fold(x) for x in ALIASES.get(a.code, ())]
        if code == q:
            score = 1
        elif city.startswith(q) or any(x.startswith(q) for x in aliases):
            score = 2
        elif name.startswith(q):
            score = 3
        elif code.startswith(q):
            score = 4
        elif q in city or q in name or any(q in x for x in aliases):
            score = 5
        elif q in country:
            score = 6
        else:
            continue
        scored.append((score, a.city, a))

    scored.sort(key=lambda t: (t[0], t[1]))
    out: list[Airport | AirportGroup] = []
    if exact_group is not None:
        out.append(exact_group)
    if exact is not None:
        out.append(exact)
    out.extend(a for _, _, a in scored)
    return out[:limit]
