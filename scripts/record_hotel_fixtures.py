"""Zeichnet die Antworten der Hotelquellen einmalig als Testdaten auf.

    uv run python scripts/record_hotel_fixtures.py trivago
    uv run python scripts/record_hotel_fixtures.py booking
    uv run python scripts/record_hotel_fixtures.py ableiten

Nicht Teil des Pakets und nicht im Image. Die Testsuite fasst nie ein Netz an;
das Ergebnis dieses Skripts wird eingecheckt und danach nur noch gelesen.

Aufgezeichnet wird die rohe Werkzeug-Antwort beziehungsweise die rohe Seite,
nicht das geparste Ergebnis: sonst prueft der Test den Parser gegen sich selbst.

Bei Booking wandern zwei Dinge in dieselbe Datei: der Apollo-Cache, den die
Seite serverseitig mitliefert, und die ersten Ergebniskarten. Der Cache traegt
die Treffer als JSON und ist der Weg, den der Adapter zuerst geht; die Karten
sind die Rueckfallebene, und beide muessen pruefbar bleiben.

`ableiten` braucht kein Netz: es baut aus einer vorhandenen Aufzeichnung die
beiden Sonderfaelle, gegen die der Adapter beweisen muss, dass er einen Umbau
bemerkt (`booking_kaputt_*`) und ein leeres Ergebnis nicht mit einem Umbau
verwechselt (`booking_leer_*`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# Das Paket wird nicht installiert, `python scripts/...` legt nur `scripts/`
# auf den Pfad. Ohne diese Zeile laeuft das Skript nur aus dem Wurzelverzeichnis
# und nur mit gesetztem PYTHONPATH.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flightopt.hotels.models import HotelQuery  # noqa: E402 - nach dem Pfad-Bootstrap

FIXTURES = ROOT / "tests" / "fixtures"


IMAGE_PLACEHOLDER = "<gekuerzt: base64-webp, siehe scripts/record_hotel_fixtures.py>"


def strip_images(result: object) -> object:
    """Die Bilddaten kuerzen, den Aufbau behalten.

    Eine Antwort mit 25 Unterkuenften traegt rund 600 kB base64-webp. Der
    Adapter liest davon nichts: er sucht den ersten `text`-Block. Die Bloecke
    bleiben deshalb an Ort und Stelle stehen, damit die Testdaten weiter
    beweisen, dass zwischen Text und Bild unterschieden wird, nur ihr Inhalt
    wird ersetzt.
    """
    if isinstance(result, dict):
        if result.get("type") == "image" and "data" in result:
            return {**result, "data": IMAGE_PLACEHOLDER}
        return {key: strip_images(value) for key, value in result.items()}
    if isinstance(result, list):
        return [strip_images(item) for item in result]
    return result


async def record_trivago(destination: str, day: date, nights: int) -> Path:
    from flightopt.hotels.sources.trivago_mcp import TOOL_SEARCH, TrivagoMcpSource, build_arguments

    source = TrivagoMcpSource()
    query = HotelQuery(destination=destination, arrival=day, nights=nights)
    try:
        await source.handshake()
        result = await source._rpc(
            "tools/call", {"name": TOOL_SEARCH, "arguments": build_arguments(query)}
        )
    finally:
        source.close()

    target = FIXTURES / f"trivago_mcp_{destination.lower()}.json"
    target.write_text(
        json.dumps(strip_images(result), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


APOLLO_TAG = '<script data-capla-store-data="apollo" type="application/json">'

KEEP_FILTER_GROUPS = ("price", "class", "review_score", "ht_id", "fc", "oos")
"""Die Filtergruppen, die der Adapter kennt. Die uebrigen neunzehn Gruppen
(Ausstattung, Ketten, Stadtteile) sind zusammen mehr als 150 kB und beweisen
nichts, was `ht_id` nicht schon beweist."""

PAGE_HEAD = (
    '<!doctype html>\n<html lang="de">\n<head><meta charset="utf-8">'
    "<title>booking searchresults, gekuerzt</title></head>\n<body>\n"
)
PAGE_FOOT = "</body>\n</html>\n"


def _wrap(note: str, body: str) -> str:
    return f"{PAGE_HEAD}<!--\n{note}\n-->\n{body}\n{PAGE_FOOT}"


def _as_script_body(store: object) -> str:
    # Ein "</script>" in einer Zeichenkette wuerde das Skript der Fixture
    # vorzeitig schliessen. Booking selbst maskiert genau so.
    return json.dumps(store, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def reduce_store(html: str, keep: int) -> str:
    """Den Apollo-Cache auf die ersten `keep` Treffer eindampfen.

    Geschnitten wird nur die Laenge der Listen, nie der Inhalt eines Eintrags:
    jeder verbleibende Treffer steht Feld fuer Feld so da, wie Booking ihn
    ausgeliefert hat.
    """
    from flightopt.hotels.sources.booking import apollo_store, search_node

    store = apollo_store(html)
    node = search_node(store)
    if store is None or node is None:
        return ""
    results = node.get("results")
    if isinstance(results, list):
        node["results"] = results[:keep]
    groups = node.get("filters")
    if isinstance(groups, list):
        node["filters"] = [
            group
            for group in groups
            if isinstance(group, dict) and group.get("field") in KEEP_FILTER_GROUPS
        ]
    return _as_script_body(store)


def reduce_search_page(html: str, keep: int = 6) -> str:
    """Aus der Ergebnisseite den Apollo-Cache und die ersten Karten schneiden.

    Die Originalseite ist knapp zwei Megabyte gross und besteht fast nur aus
    Skripten, Stilen und Inline-Bildern, die der Parser nie anfasst. Behalten
    werden der Suchknoten des Apollo-Caches und der Quelltext der ersten
    Karten, Zeichen fuer Zeichen: eine nachgebaute Karte wuerde den Parser nur
    gegen sich selbst pruefen.
    """
    from flightopt.hotels.dom import parse_html, testid

    cards = parse_html(html).find_all(testid("property-card"))[:keep]
    body = "\n".join(card.source(html) for card in cards)
    store = reduce_store(html, keep)
    script = f"{APOLLO_TAG}{store}</script>\n" if store else ""
    return _wrap(
        "Aufgezeichnet mit scripts/record_hotel_fixtures.py. Uebernommen sind\n"
        f"der Apollo-Cache und die ersten {keep} Ergebniskarten der\n"
        "Originalseite, wortgetreu. Aus dem Cache sind nur die Trefferliste\n"
        "und die Filtergruppen gekuerzt, kein einzelnes Feld veraendert.\n"
        "Skripte, Stile und Bilder der Seite fehlen.",
        script + body,
    )


def break_page(html: str) -> str:
    """Dieselbe Seite, aber unkenntlich: kein Cache, andere Testids.

    Das ist der Fall, der frueher still null Karten ergab. Er gehoert in die
    Testdaten, weil er sonst erst in einem Durchlauf auffaellt - und dort wie
    "heute gab es keine Hotels" aussieht.
    """
    from flightopt.hotels.sources.booking import APOLLO_SCRIPT

    inner = html.split("<body>", 1)[-1].split("</body>", 1)[0]
    inner = APOLLO_SCRIPT.sub("", inner)
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    return _wrap(
        "Kuenstlich beschaedigt, abgeleitet aus der echten Aufzeichnung:\n"
        "der Apollo-Knoten ist entfernt und jedes data-testid umbenannt.\n"
        "Gebaut von scripts/record_hotel_fixtures.py ableiten.",
        inner.replace("data-testid=", "data-test-marker=").strip(),
    )


def empty_page(html: str) -> str:
    """Dieselbe Seite ohne einen einzigen Treffer, wie Booking sie ausliefert.

    Der Cache bleibt vollstaendig, nur die Trefferliste ist leer und die
    Gesamtzahl null. Genau daran soll der Adapter "nichts gefunden" von
    "nicht wiedererkannt" unterscheiden.
    """
    from flightopt.hotels.sources.booking import apollo_store, search_node

    store = apollo_store(html)
    node = search_node(store)
    if store is None or node is None:
        raise SystemExit("Vorlage ohne Apollo-Cache, daraus laesst sich nichts ableiten")
    node["results"] = []
    pagination = node.get("pagination")
    if isinstance(pagination, dict):
        pagination["nbResultsTotal"] = 0
    if isinstance(node.get("filters"), list):
        node["filters"] = []
    return _wrap(
        "Abgeleitet aus der echten Aufzeichnung: derselbe Apollo-Cache,\n"
        "aber ohne Treffer und mit nbResultsTotal 0. Keine Ergebniskarte.\n"
        "Gebaut von scripts/record_hotel_fixtures.py ableiten.",
        f"{APOLLO_TAG}{_as_script_body(store)}</script>",
    )


def derive(destination: str) -> list[Path]:
    """Die beiden Sonderfaelle aus der Aufzeichnung bauen. Ohne Netz."""
    template = FIXTURES / f"booking_apollo_{destination.lower()}.html"
    html = template.read_text(encoding="utf-8")
    written = []
    for name, builder in (
        (f"booking_kaputt_{destination.lower()}.html", break_page),
        (f"booking_leer_{destination.lower()}.html", empty_page),
    ):
        target = FIXTURES / name
        target.write_text(builder(html), encoding="utf-8", newline="\n")
        written.append(target)
    return written


async def record_booking(destination: str, day: date, nights: int) -> Path:
    from flightopt.hotels.sources.booking import BookingSource

    source = BookingSource()
    query = HotelQuery(destination=destination, arrival=day, nights=nights)
    try:
        html = await source.fetch_html(query)
    finally:
        source.close()

    # Eigener Name: `booking_searchresults_athen.html` ist eine aeltere
    # Aufzeichnung ohne Apollo-Knoten und bleibt genau deshalb liegen, sie ist
    # der Beweis fuer die Rueckfallebene.
    target = FIXTURES / f"booking_apollo_{destination.lower()}.html"
    target.write_text(reduce_search_page(html), encoding="utf-8", newline="\n")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["trivago", "booking", "ableiten"])
    parser.add_argument("--destination", default="Athen")
    parser.add_argument("--arrival", default=None, help="YYYY-MM-DD, sonst in 60 Tagen")
    parser.add_argument("--nights", type=int, default=1)
    args = parser.parse_args()

    if args.source == "ableiten":
        for target in derive(args.destination):
            print(f"geschrieben: {target}")
        return 0

    day = (
        date.fromisoformat(args.arrival)
        if args.arrival
        else date.today() + timedelta(days=60)
    )
    recorder = record_trivago if args.source == "trivago" else record_booking
    target = asyncio.run(recorder(args.destination, day, args.nights))
    print(f"geschrieben: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
