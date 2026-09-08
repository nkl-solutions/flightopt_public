"""Zahlen aus formatierten Zeichenketten lesen.

Die Hotelquellen liefern keine Zahlen, sondern Anzeigetext: "$287", "€ 129",
"1.234 €", "6,276" Bewertungen, "8.6" Punkte. Wer das mit `float(...)` angeht,
macht aus 1.234 Euro eine Euro-dreiundzwanzig.

Die Waehrung wird nie aus dem Symbol geraten. "$" steht fuer USD, CAD, AUD, SGD
und ein Dutzend weitere; die Quelle nennt die Waehrung in einem eigenen Feld,
und nur das zaehlt.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flightopt.domain.models import Money

# Alles ausser Ziffern und den beiden Trennzeichen faellt weg. Damit sind auch
# Waehrungssymbole, Buchstaben und jede Art von Leerzeichen erledigt, ein
# geschuetztes Leerzeichen als Tausendertrenner ("1 234 EUR") eingeschlossen.
_KEEP = re.compile(r"[^0-9.,]")


def _to_decimal(text: str) -> Decimal | None:
    """Eine bereinigte Ziffernfolge zu einer Zahl, ohne das Format zu raten.

    Stehen beide Trennzeichen im Text, ist das hintere das Dezimaltrennzeichen
    und das vordere der Tausendertrenner: das gilt in beiden Schreibweisen.
    Steht nur eines da, entscheidet die Laenge der letzten Gruppe. Genau drei
    Ziffern dahinter sind ein Tausenderblock ("1.234", "1,234"), alles andere
    ist eine Nachkommastelle ("12,50", "9.99").
    """
    last_dot = text.rfind(".")
    last_comma = text.rfind(",")

    if last_dot >= 0 and last_comma >= 0:
        decimal_sep = "." if last_dot > last_comma else ","
        thousands_sep = "," if decimal_sep == "." else "."
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif last_dot >= 0 or last_comma >= 0:
        sep = "." if last_dot >= 0 else ","
        parts = text.split(sep)
        if len(parts) > 2 or len(parts[-1]) == 3:
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".")

    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_price(raw: object, currency: str) -> Money | None:
    """"$287" plus "USD" zu Money(28700, "USD"). `None`, wenn nichts zu lesen ist.

    `None` heisst ausdruecklich "unlesbar", nicht "null". Der Aufrufer
    ueberspringt die Zeile und vermerkt sie im Bericht, statt einen Preis zu
    erfinden.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value: Decimal | None = Decimal(str(raw))
    else:
        cleaned = _KEEP.sub("", str(raw))
        if not any(ch.isdigit() for ch in cleaned):
            return None
        value = _to_decimal(cleaned)
    if value is None or value <= 0:
        return None
    minor = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if minor <= 0:
        return None
    return Money(minor, currency.upper())


def parse_count(raw: object) -> int | None:
    """"6,276" zu 6276. Trennzeichen sind hier nie Dezimalstellen."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    digits = re.sub(r"[^0-9]", "", str(raw))
    return int(digits) if digits else None


def parse_rating(raw: object) -> float | None:
    """"8.6" oder "8,6" zu 8.6. Ausserhalb von 0 bis 10 gilt als unlesbar."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        cleaned = _KEEP.sub("", str(raw)).replace(",", ".")
        try:
            value = float(cleaned)
        except ValueError:
            return None
    return value if 0.0 <= value <= 10.0 else None


def parse_stars(raw: object) -> int | None:
    """1 bis 5 Sterne. Alles andere ist keine Sternekategorie."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(float(str(raw).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 5 else None
