"""Baut flightopt/data/airports.json aus den OurAirports-Rohdaten.

    uv run python scripts/build_airports.py

Nicht Teil des Pakets und nicht im Image: das Ergebnis wird eingecheckt, das
Skript laeuft von Hand, wenn die Basis erneuert werden soll.

Quelle: https://davidmegginson.github.io/ourairports-data/airports.csv
Lizenz: Public Domain (CC0), Namensnennung im Kommentar reicht.

Gefiltert wird auf Flughaefen, die ein Reisender ueberhaupt buchen kann:
Linienverkehr, IATA-Code, gross oder mittelgross. Die bereits gepflegten
Eintraege gewinnen bei Namenskonflikten, weil sie deutsche Namen und die
Airline-Zuordnung tragen, die es upstream nicht gibt.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "flightopt" / "data"
SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
KEEP_TYPES = {"large_airport", "medium_airport"}

# Deutsche Landesnamen fuer jeden ISO-3166-1-alpha-2-Code, den OurAirports
# vergibt, plus XK fuer den Kosovo (dort in Gebrauch, offiziell nicht vergeben).
# Vollstaendig, weil ein fehlender Eintrag auf den blanken ISO-Code zurueckfaellt:
# nie falsch, aber unsuchbar - eine Suche nach "Kongo" fand die Flughaefen nicht,
# solange dort "CD" stand. Kurznamen, keine Protokollnamen ("Bolivien", nicht
# "Plurinationaler Staat Bolivien"), weil sie in der Trefferliste stehen.
COUNTRIES_DE: dict[str, str] = {
    "AD": "Andorra", "AE": "Vereinigte Arabische Emirate", "AF": "Afghanistan",
    "AG": "Antigua und Barbuda", "AI": "Anguilla", "AL": "Albanien", "AM": "Armenien",
    "AO": "Angola", "AQ": "Antarktis", "AR": "Argentinien", "AS": "Amerikanisch-Samoa",
    "AT": "Österreich", "AU": "Australien", "AW": "Aruba", "AX": "Åland",
    "AZ": "Aserbaidschan", "BA": "Bosnien und Herzegowina", "BB": "Barbados",
    "BD": "Bangladesch", "BE": "Belgien", "BF": "Burkina Faso", "BG": "Bulgarien",
    "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin", "BL": "Saint-Barthélemy",
    "BM": "Bermuda", "BN": "Brunei", "BO": "Bolivien", "BQ": "Karibische Niederlande",
    "BR": "Brasilien", "BS": "Bahamas", "BT": "Bhutan", "BV": "Bouvetinsel",
    "BW": "Botsuana", "BY": "Belarus", "BZ": "Belize", "CA": "Kanada",
    "CC": "Kokosinseln", "CD": "Kongo (Demokratische Republik)",
    "CF": "Zentralafrikanische Republik", "CG": "Kongo (Republik)", "CH": "Schweiz",
    "CI": "Elfenbeinküste", "CK": "Cookinseln", "CL": "Chile", "CM": "Kamerun",
    "CN": "China", "CO": "Kolumbien", "CR": "Costa Rica", "CU": "Kuba",
    "CV": "Kap Verde", "CW": "Curaçao", "CX": "Weihnachtsinsel", "CY": "Zypern",
    "CZ": "Tschechien", "DE": "Deutschland", "DJ": "Dschibuti", "DK": "Dänemark",
    "DM": "Dominica", "DO": "Dominikanische Republik", "DZ": "Algerien",
    "EC": "Ecuador", "EE": "Estland", "EG": "Ägypten", "EH": "Westsahara",
    "ER": "Eritrea", "ES": "Spanien", "ET": "Äthiopien", "FI": "Finnland",
    "FJ": "Fidschi", "FK": "Falklandinseln", "FM": "Mikronesien", "FO": "Färöer",
    "FR": "Frankreich", "GA": "Gabun", "GB": "Großbritannien", "GD": "Grenada",
    "GE": "Georgien", "GF": "Französisch-Guayana", "GG": "Guernsey", "GH": "Ghana",
    "GI": "Gibraltar", "GL": "Grönland", "GM": "Gambia", "GN": "Guinea",
    "GP": "Guadeloupe", "GQ": "Äquatorialguinea", "GR": "Griechenland",
    "GS": "Südgeorgien und die Südlichen Sandwichinseln", "GT": "Guatemala",
    "GU": "Guam", "GW": "Guinea-Bissau", "GY": "Guyana", "HK": "Hongkong",
    "HM": "Heard und McDonaldinseln", "HN": "Honduras", "HR": "Kroatien", "HT": "Haiti",
    "HU": "Ungarn", "ID": "Indonesien", "IE": "Irland", "IL": "Israel",
    "IM": "Isle of Man", "IN": "Indien",
    "IO": "Britisches Territorium im Indischen Ozean", "IQ": "Irak", "IR": "Iran",
    "IS": "Island", "IT": "Italien", "JE": "Jersey", "JM": "Jamaika", "JO": "Jordanien",
    "JP": "Japan", "KE": "Kenia", "KG": "Kirgisistan", "KH": "Kambodscha",
    "KI": "Kiribati", "KM": "Komoren", "KN": "St. Kitts und Nevis", "KP": "Nordkorea",
    "KR": "Südkorea", "KW": "Kuwait", "KY": "Kaimaninseln", "KZ": "Kasachstan",
    "LA": "Laos", "LB": "Libanon", "LC": "St. Lucia", "LI": "Liechtenstein",
    "LK": "Sri Lanka", "LR": "Liberia", "LS": "Lesotho", "LT": "Litauen",
    "LU": "Luxemburg", "LV": "Lettland", "LY": "Libyen", "MA": "Marokko",
    "MC": "Monaco", "MD": "Moldau", "ME": "Montenegro", "MF": "Saint-Martin",
    "MG": "Madagaskar", "MH": "Marshallinseln", "MK": "Nordmazedonien", "ML": "Mali",
    "MM": "Myanmar", "MN": "Mongolei", "MO": "Macau", "MP": "Nördliche Marianen",
    "MQ": "Martinique", "MR": "Mauretanien", "MS": "Montserrat", "MT": "Malta",
    "MU": "Mauritius", "MV": "Malediven", "MW": "Malawi", "MX": "Mexiko",
    "MY": "Malaysia", "MZ": "Mosambik", "NA": "Namibia", "NC": "Neukaledonien",
    "NE": "Niger", "NF": "Norfolkinsel", "NG": "Nigeria", "NI": "Nicaragua",
    "NL": "Niederlande", "NO": "Norwegen", "NP": "Nepal", "NR": "Nauru", "NU": "Niue",
    "NZ": "Neuseeland", "OM": "Oman", "PA": "Panama", "PE": "Peru",
    "PF": "Französisch-Polynesien", "PG": "Papua-Neuguinea", "PH": "Philippinen",
    "PK": "Pakistan", "PL": "Polen", "PM": "Saint-Pierre und Miquelon",
    "PN": "Pitcairninseln", "PR": "Puerto Rico", "PS": "Palästina", "PT": "Portugal",
    "PW": "Palau", "PY": "Paraguay", "QA": "Katar", "RE": "Réunion", "RO": "Rumänien",
    "RS": "Serbien", "RU": "Russland", "RW": "Ruanda", "SA": "Saudi-Arabien",
    "SB": "Salomonen", "SC": "Seychellen", "SD": "Sudan", "SE": "Schweden",
    "SG": "Singapur", "SH": "St. Helena", "SI": "Slowenien",
    "SJ": "Spitzbergen und Jan Mayen", "SK": "Slowakei", "SL": "Sierra Leone",
    "SM": "San Marino", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "SS": "Südsudan", "ST": "São Tomé und Príncipe", "SV": "El Salvador",
    "SX": "Sint Maarten", "SY": "Syrien", "SZ": "Eswatini",
    "TC": "Turks- und Caicosinseln", "TD": "Tschad",
    "TF": "Französische Süd- und Antarktisgebiete", "TG": "Togo", "TH": "Thailand",
    "TJ": "Tadschikistan", "TK": "Tokelau", "TL": "Timor-Leste", "TM": "Turkmenistan",
    "TN": "Tunesien", "TO": "Tonga", "TR": "Türkei", "TT": "Trinidad und Tobago",
    "TV": "Tuvalu", "TW": "Taiwan", "TZ": "Tansania", "UA": "Ukraine", "UG": "Uganda",
    "UM": "Amerikanische Überseeinseln", "US": "Vereinigte Staaten", "UY": "Uruguay",
    "UZ": "Usbekistan", "VA": "Vatikanstadt", "VC": "St. Vincent und die Grenadinen",
    "VE": "Venezuela", "VG": "Britische Jungferninseln",
    "VI": "Amerikanische Jungferninseln", "VN": "Vietnam", "VU": "Vanuatu",
    "WF": "Wallis und Futuna", "WS": "Samoa", "XK": "Kosovo", "YE": "Jemen",
    "YT": "Mayotte", "ZA": "Südafrika", "ZM": "Sambia", "ZW": "Simbabwe",
}


@dataclass
class BuildReport:
    """Was der Lauf verworfen hat. Wird am Ende gedruckt, nicht geloggt."""

    skipped_coordinates: int = 0
    """Upstream-Zeilen ohne lesbare Koordinaten."""
    duplicate_codes: int = 0
    """Wie oft derselbe IATA-Code upstream mehrfach vorkam."""
    curated_missing_upstream: list[str] = field(default_factory=list)
    """Gepflegte Codes, die upstream gar nicht auftauchen - bleiben erhalten."""
    curated_closed: list[str] = field(default_factory=list)
    """Gepflegte Codes, die upstream als geschlossen gelten - fliegen raus."""


def closed_codes(text: str) -> set[str]:
    """IATA-Codes, die upstream als geschlossen gefuehrt werden.

    Der Filter in `parse_csv` wirft geschlossene Flughaefen weg, und der Merge
    wuerde sie aus dem gepflegten Bestand wieder hereinholen. Diese Menge sagt
    ihm, welche Codes er dabei auslassen muss.

    Upstream raeumt `iata_code` einer geschlossenen Zeile irgendwann leer und
    hebt den Code nur noch in `keywords` auf - Tegel steht dort als
    "TXL, EDDT, Otto Lilienthal". Ohne diese zweite Spalte kaeme Tegel bei
    jedem Neubau der Basis zurueck. Die Menge wird nur fuer Codes befragt, die
    upstream gar nicht mehr vorkommen, also schadet ein zu weit gefasster
    Treffer aus den Stichworten nicht.
    """
    out: set[str] = set()
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("type") or "").strip() != "closed":
            continue
        code = (row.get("iata_code") or "").strip().upper()
        if len(code) == 3 and code.isalpha():
            out.add(code)
            continue
        for token in (row.get("keywords") or "").split(","):
            token = token.strip().upper()
            if len(token) == 3 and token.isalpha():
                out.add(token)
    return out


def parse_csv(text: str, report: BuildReport | None = None) -> list[dict]:
    """Filtert die Rohdaten auf buchbare Flughaefen, sortiert nach IATA-Code."""
    report = report or BuildReport()
    best: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("type") or "").strip() not in KEEP_TYPES:
            continue
        if (row.get("scheduled_service") or "").strip().lower() != "yes":
            continue
        code = (row.get("iata_code") or "").strip().upper()
        if len(code) != 3 or not code.isalpha():
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            report.skipped_coordinates += 1
            continue
        cc = (row.get("iso_country") or "").strip().upper()
        name = (row.get("name") or "").strip()
        size = "large" if row["type"].strip() == "large_airport" else "medium"
        previous = best.get(code)
        if previous is not None:
            # Upstream vergibt einen IATA-Code gelegentlich zweimal. Ohne
            # Entscheidung gewinnt die letzte Zeile, und das ist mal der
            # Grossflughafen, mal der Regionalplatz nebenan.
            report.duplicate_codes += 1
            if not (previous["size"] == "medium" and size == "large"):
                continue
        best[code] = {
            "code": code,
            "name": name,
            "city": (row.get("municipality") or "").strip() or name,
            "country": COUNTRIES_DE.get(cc, cc),
            "cc": cc,
            "lat": lat,
            "lon": lon,
            "size": size,
            "carriers": [],
            "aliases": [],
        }
    return sorted(best.values(), key=lambda r: r["code"])


def merge(
    existing: list[dict],
    upstream: list[dict],
    aliases: dict[str, list[str]],
    closed: set[str] | frozenset[str] = frozenset(),
    report: BuildReport | None = None,
) -> list[dict]:
    """Vereinigt beide Listen. Der gepflegte Bestand gewinnt bei Namen.

    Koordinaten und Groesse kommen aus OurAirports, weil sie dort gepflegt
    werden; Name, Stadt und die Airline-Zuordnung stammen aus dem Bestand, weil
    sie dort von Hand auf Deutsch gesetzt wurden.

    Das Land kommt immer aus `COUNTRIES_DE`, nie aus dem Bestand: der traegt
    englische Namen aus seiner ersten Fassung ("Germany"), und eine Basis mit
    zwei Sprachen laesst die Landessuche je nach Flughafen ins Leere laufen.

    `closed` sind Codes, die upstream als geschlossen gelten. Tegel steht noch
    im Bestand, fliegt aber seit 2020 nicht mehr: ein solcher Eintrag darf hier
    nicht wieder auferstehen, sonst bietet die Suche ihn weiter an.
    """
    report = report or BuildReport()
    by_code: dict[str, dict] = {r["code"]: dict(r) for r in upstream}

    for old in existing:
        code = old["code"]
        row = by_code.get(code)
        if row is None:
            if code in closed:
                report.curated_closed.append(code)
                continue
            report.curated_missing_upstream.append(code)
            cc = (old.get("cc") or "").strip().upper()
            row = {
                "code": code,
                "name": old.get("name", code),
                "city": old.get("city") or old.get("name", code),
                "country": COUNTRIES_DE.get(cc, cc),
                "cc": cc,
                "lat": old.get("lat"),
                "lon": old.get("lon"),
                "size": old.get("size") or "medium",
                "carriers": list(old.get("carriers") or []),
                "aliases": [],
            }
            by_code[code] = row
            continue
        for key in ("name", "city"):
            if old.get(key):
                row[key] = old[key]
        if old.get("carriers"):
            row["carriers"] = list(old["carriers"])
        for key in ("lat", "lon"):
            if row.get(key) is None and old.get(key) is not None:
                row[key] = old[key]

    for code, names in aliases.items():
        row = by_code.get(code)
        if row is not None:
            row["aliases"] = list(names)

    return sorted(by_code.values(), key=lambda r: r["code"])


def write_json(rows: list[dict], path: Path) -> None:
    """Ein Flughafen pro Zeile: kompakt genug und trotzdem diffbar."""
    body = ",\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
    )
    path.write_text(f"[\n{body}\n]\n", encoding="utf-8", newline="\n")


def load_source(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        request = Request(source, headers={"User-Agent": "flightopt-build/1.0"})
        with urlopen(request, timeout=120) as resp:  # noqa: S310 - fixed https URL
            return resp.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def _codes(values: list[str]) -> str:
    return ", ".join(sorted(values)) if values else "-"


def print_audit(report: BuildReport) -> None:
    """Was der Lauf still verworfen haette, steht vor der Zusammenfassung.

    Ohne diesen Block merkt niemand, dass ein gepflegter Flughafen aus der
    Basis verschwunden ist, bis eine Suche ins Leere laeuft.
    """
    print("audit:")
    print(f"  uebersprungen ohne Koordinaten: {report.skipped_coordinates}")
    print(f"  doppelte IATA-Codes: {report.duplicate_codes}")
    print(f"  Bestand ohne Upstream ({len(report.curated_missing_upstream)}): "
          f"{_codes(report.curated_missing_upstream)}")
    print(f"  Bestand upstream geschlossen ({len(report.curated_closed)}): "
          f"{_codes(report.curated_closed)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", default=SOURCE_URL,
                        help="URL oder Pfad der OurAirports-CSV")
    parser.add_argument("--existing", default=str(DATA / "airports.json"),
                        help="Bisherige Basis; ihre deutschen Namen und "
                             "Airline-Zuordnungen gewinnen beim Merge")
    parser.add_argument("--aliases", default=str(DATA / "airport_aliases_de.json"),
                        help="Deutsche Aliase je IATA-Code; werden in jede "
                             "Zeile eingebacken")
    parser.add_argument("--out", default=str(DATA / "airports.json"),
                        help="Zieldatei; ueberschreibt per Vorgabe die Basis")
    args = parser.parse_args(argv)

    report = BuildReport()
    text = load_source(args.source)
    upstream = parse_csv(text, report=report)
    existing_path = Path(args.existing)
    existing = (
        json.loads(existing_path.read_text(encoding="utf-8"))
        if existing_path.exists()
        else []
    )
    aliases = json.loads(Path(args.aliases).read_text(encoding="utf-8"))

    rows = merge(existing, upstream, aliases,
                 closed=closed_codes(text), report=report)
    out = Path(args.out)
    write_json(rows, out)
    size_kb = out.stat().st_size / 1024
    print_audit(report)
    print(f"upstream {len(upstream)}, bestand {len(existing)}, ergebnis {len(rows)}")
    print(f"{out} -> {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
