"""Airport lookup for the route input.

Nobody remembers that Thessaloniki is SKG. The form takes a city name and
resolves it, so the three-letter codes stay an implementation detail.

The list itself is built by `scripts/build_airports.py` from OurAirports and
covers roughly 3,500 airports worldwide. Groups (metro areas and countries)
live in `data/airport_groups.json` so a new destination is a data change, not
a code change.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA = DATA_DIR / "airports.json"
GROUP_DATA = DATA_DIR / "airport_groups.json"

EARTH_RADIUS_KM = 6371.0088
LONG_HAUL_KM = 3500.0
"""Above this great-circle distance a leg gets two stops instead of one."""

MAX_LEG_VARIANTS = 24
"""Most origin/destination pairs one leg may fan out into."""
MAX_TOTAL_VARIANTS = 64
"""Most complete route variants one search may fan out into."""


class TooManyVariants(ValueError):
    """The groups on this route would produce more searches than we run."""


@dataclass(frozen=True, slots=True)
class Airport:
    code: str
    name: str
    city: str
    country: str
    cc: str
    lat: float | None = None
    lon: float | None = None
    size: str = "medium"
    """'large' or 'medium'. Ranks large airports first in the type-ahead."""
    carriers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "city": self.city,
            "country": self.country,
            "cc": self.cc,
            "lat": self.lat,
            "lon": self.lon,
            "size": self.size,
            "carriers": list(self.carriers),
            "aliases": list(self.aliases),
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


ALIAS_DATA = DATA_DIR / "airport_aliases_de.json"


def _load_aliases() -> dict[str, tuple[str, ...]]:
    """German city names and colloquialisms, keyed by IATA code.

    This file is the editing source: adding 'Malle' for Palma is a data change,
    not a release. It is not what search reads, though - `scripts/build_airports.py`
    bakes every entry into the matching row of `airports.json`, so a change here
    only reaches the type-ahead after a rebuild. Kept loaded so a test can hold
    both files against each other.
    """
    raw = json.loads(ALIAS_DATA.read_text(encoding="utf-8"))
    return {code: tuple(names) for code, names in raw.items()}


ALIASES: dict[str, tuple[str, ...]] = _load_aliases()


@dataclass(frozen=True, slots=True)
class _Keys:
    """One entry's search keys, folded once at load time.

    `search` runs over 3.245 airports on every keystroke. Folding four fields
    per airport per call cost more than the whole rest of the request; folding
    them once at load costs nothing measurable.
    """

    code: str
    """Uppercased, never folded: `_fold` turns 'AER' into 'ar' (ae -> a)."""
    city: str
    name: str
    country: str
    aliases: tuple[str, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.city, self.name, self.country, *self.aliases)


def _keys_for(entry: Airport | AirportGroup) -> _Keys:
    return _Keys(
        code=entry.code.upper(),
        city=_fold(entry.city),
        name=_fold(entry.name),
        country=_fold(entry.country),
        aliases=tuple(_fold(alias) for alias in entry.aliases),
    )


def _load_groups() -> dict[str, AirportGroup]:
    """Metro, country and region groups, keyed by their own code.

    A group whose natural code is already an airport carries the suffix
    `-ALL` (`IST-ALL`, `BKK-ALL`, `SHA-ALL`, `DXB-ALL`), so the bare code keeps
    meaning the airport and nothing resolves to two different routes.
    """
    raw = json.loads(GROUP_DATA.read_text(encoding="utf-8"))
    return {
        code: AirportGroup(
            code=code,
            name=row["name"],
            city=row.get("city") or row["name"],
            country=row.get("country", ""),
            cc=row.get("cc", ""),
            airports=tuple(row["airports"]),
            aliases=tuple(row.get("aliases") or ()),
        )
        for code, row in raw.items()
    }


GROUPS: dict[str, AirportGroup] = _load_groups()
GROUP_KEYS: dict[str, _Keys] = {code: _keys_for(g) for code, g in GROUPS.items()}


@lru_cache(maxsize=1)
def _load() -> tuple[list[Airport], dict[str, Airport], list[tuple[_Keys, Airport]]]:
    """The airport list, an index by code, and the folded search keys.

    Aliases come from `airports.json` alone. The build script bakes them in
    from `airport_aliases_de.json`; reading that file again here as a fallback
    would only paper over a base that was never rebuilt, and a test holds the
    two files against each other instead.
    """
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    airports = [
        Airport(
            code=r["code"],
            name=r["name"],
            city=r.get("city") or r["name"],
            country=r.get("country", ""),
            cc=r.get("cc", ""),
            lat=r.get("lat"),
            lon=r.get("lon"),
            size=r.get("size") or "medium",
            carriers=tuple(r.get("carriers") or []),
            aliases=tuple(r.get("aliases") or ()),
        )
        for r in rows
    ]
    return (
        airports,
        {a.code: a for a in airports},
        [(_keys_for(a), a) for a in airports],
    )


def all_airports() -> list[Airport]:
    return _load()[0]


def by_code(code: str) -> Airport | None:
    return _load()[1].get(code.upper())


def expand_code(code: str) -> tuple[str, ...]:
    group = GROUPS.get(code.strip().upper())
    return group.airports if group else (code.strip().upper(),)


def distance_km(origin: str, destination: str) -> float | None:
    """Great-circle distance, or None when either airport has no coordinates."""
    a, b = by_code(origin), by_code(destination)
    if a is None or b is None:
        return None
    if a.lat is None or a.lon is None or b.lat is None or b.lon is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    # Rundungsfehler koennen h ueber 1 schieben; asin wuerfe dann einen Fehler
    # statt der halben Erdumrundung, die gemeint ist.
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def auto_max_stops(origin: str, destination: str) -> int:
    """How many connections a leg needs to be priceable at all.

    Berlin to Tokyo has no non-stop service, so a one-stop cap returns an empty
    calendar. Short legs keep the tighter cap because two stops on a two-hour
    hop is never the option somebody wants.
    """
    km = distance_km(origin, destination)
    if km is None:
        return 1
    return 2 if km >= LONG_HAUL_KM else 1


GROUP_FIRST, AIRPORT_SECOND = 0, 1
"""At the same match quality a group outranks a single airport."""


def search(query: str, limit: int = 8) -> list[Airport | AirportGroup]:
    """Rank matches from the sharpest evidence to the vaguest.

    Groups and airports do not share one ladder; a group prefix outranks an
    airport city prefix, because someone typing 'Tok' wants Tokyo, not one of
    its two airports. In order:

    1. the airport whose code is exactly the query
    2. the group whose code is exactly the query
    3. group: city, name, country or alias equals the query;
       airport: city, name or alias equals the query - group first
    4. group: one of those fields starts with the query
    5. airport: city or alias starts with the query
    6. airport: name starts with the query
    7. airport: code starts with the query
    8. group and airport: the query sits somewhere inside one of the fields -
       group first
    9. airport: the query sits inside the country name

    An exact name beats a longer name that merely starts with it, otherwise
    'Berlin' lands on the DE-OST group, whose alias list happens to begin with
    the word. Within one rank a group goes before a single airport and a large
    airport before a medium one: without that, typing 'Ber' offers Berlevag
    before Berlin.

    Names are compared folded (see `_fold`), codes are not: folding would turn
    'AER' into 'ar' and hand Sochi every airport code starting with AR.
    """
    _, by_c, folded = _load()
    q = _fold(query)
    if not q:
        return []

    wanted = query.strip().upper()
    exact = by_c.get(wanted)
    exact_group = GROUPS.get(wanted)
    scored: list[tuple[int, int, int, str, str, Airport | AirportGroup]] = []

    for group in GROUPS.values():
        if exact_group is not None and group.code == exact_group.code:
            continue
        if group.airports == (group.code,):
            # A group holding only its own airport would show up as a duplicate
            # row. It still expands, it just does not need its own suggestion.
            continue
        # A test may swap GROUPS out; GROUP_KEYS is filled once at import.
        keys = GROUP_KEYS.get(group.code) or _keys_for(group)
        fields = keys.fields
        # An exact code never reaches this loop - `exact_group` took it out.
        if any(field == q for field in fields):
            score = 1
        elif any(field.startswith(q) for field in fields):
            score = 2
        elif any(q in field for field in fields):
            score = 6
        else:
            continue
        scored.append((score, GROUP_FIRST, 0, keys.city, group.code, group))

    for keys, a in folded:
        if exact is not None and a.code == exact.code:
            continue
        city, name, aliases = keys.city, keys.name, keys.aliases
        # An exact code never reaches this loop either - `exact` took it out.
        if city == q or name == q or q in aliases:
            score = 1
        elif city.startswith(q) or any(x.startswith(q) for x in aliases):
            score = 3
        elif name.startswith(q):
            score = 4
        elif keys.code.startswith(wanted):
            score = 5
        elif q in city or q in name or any(q in x for x in aliases):
            score = 6
        elif q in keys.country:
            score = 7
        else:
            continue
        size_rank = 0 if a.size == "large" else 1
        scored.append((score, AIRPORT_SECOND, size_rank, city, a.code, a))

    scored.sort(key=lambda t: t[:5])
    out: list[Airport | AirportGroup] = []
    if exact is not None:
        out.append(exact)
    if exact_group is not None and (exact is None or exact_group.airports != (exact.code,)):
        out.append(exact_group)
    out.extend(item for *_, item in scored)
    return out[:limit]


def resolve_entry(query: str) -> Airport | AirportGroup | None:
    """The airport or group a typed word stands for, or None.

    One rule for every caller - CLI, voice and API - so the same sentence
    cannot mean two different routes. An exact airport code wins, then an
    exact group code, then the best name match, where a group goes first.
    'IST' is therefore the airport in Istanbul and 'IST-ALL' the metro group;
    no code means two things at once.
    """
    wanted = query.strip().upper()
    if (airport := by_code(wanted)) is not None:
        return airport
    if (group := GROUPS.get(wanted)) is not None:
        return group
    hits = search(query, 1)
    return hits[0] if hits else None


def resolve(query: str) -> str | None:
    """The code a typed word stands for, or None when nothing matches."""
    entry = resolve_entry(query)
    return entry.code if entry is not None else None


def variant_counts(stops: Sequence[str]) -> tuple[list[int], int]:
    """Pairs per leg and total route variants for a list of stops."""
    sizes = [len(expand_code(code)) for code in stops]
    per_leg = [sizes[i] * sizes[i + 1] for i in range(len(sizes) - 1)]
    total = 1
    for n in sizes:
        total *= n
    return per_leg, total


def _replace_hint(codes: Sequence[str]) -> str:
    """Name the groups worth replacing, biggest first.

    A bare "too many combinations" leaves the person guessing which of three
    fields to change; the biggest group is almost always the one to drop.
    """
    seen: list[str] = []
    for code in codes:
        if len(expand_code(code)) > 1 and code not in seen:
            seen.append(code)
    if not seen:
        return "Nutze weniger Stopps."
    seen.sort(key=lambda code: -len(expand_code(code)))
    if len(seen) == 1:
        listed = seen[0]
    else:
        listed = ", ".join(seen[:-1]) + " oder " + seen[-1]
    return f"Ersetze die Gruppe {listed} durch einen einzelnen Flughafen."


def check_variant_budget(stops: Sequence[str]) -> None:
    """Refuse route fan-outs that would take minutes and hammer every source.

    Two country groups next to each other multiply: Germany (13) to Turkey (6)
    is 78 calendar fetches for one leg, before the second leg even starts. The
    message names the leg and both codes as typed, because it is shown as-is.
    """
    codes = [code.strip().upper() for code in stops]
    per_leg, total = variant_counts(codes)
    for index, pairs in enumerate(per_leg):
        if pairs > MAX_LEG_VARIANTS:
            origin, destination = codes[index], codes[index + 1]
            raise TooManyVariants(
                f"Leg {index + 1} ({origin} nach {destination}) ergibt {pairs} "
                f"Flughafenkombinationen, erlaubt sind {MAX_LEG_VARIANTS}. "
                + _replace_hint([origin, destination])
            )
    if total > MAX_TOTAL_VARIANTS:
        raise TooManyVariants(
            f"Die Route {'-'.join(codes)} ergibt {total} Routenvarianten, "
            f"erlaubt sind {MAX_TOTAL_VARIANTS}. " + _replace_hint(codes)
        )
