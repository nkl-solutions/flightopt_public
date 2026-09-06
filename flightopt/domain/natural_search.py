"""Small natural-language layer for pre-filling the normal search form."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flightopt.domain import airports

KEYWORD_RE = re.compile(
    r"\b(zurück nach|zurueck nach|wieder nach|von|ab|aus|nach|über|ueber|via)\s+"
    r"(.+?)(?=\s+(?:und\s+)?(?:zurück nach|zurueck nach|wieder nach|von|ab|aus|nach|über|ueber|via)\b|[,.;]|$)",
    re.IGNORECASE,
)
FILLER_RE = re.compile(
    r"\b(flug|flüge|fluege|reise|route|bitte|ich|möchte|moechte|will|suche|einen|eine|"
    r"den|die|das|dem|der|von|nach|ab|aus|über|ueber|via|wieder|zurück|zurueck|hin)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NaturalSearchIntent:
    trip: str
    airports: list[str]
    labels: list[str]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "trip": self.trip,
            "airports": self.airports,
            "labels": self.labels,
            "hops": [
                {"code": code, "label": label}
                for code, label in zip(self.airports, self.labels, strict=True)
            ],
            "warnings": self.warnings,
        }


def _clean_place(raw: str) -> str:
    text = re.split(r"\bund\s+(?:zurück|zurueck|retour)\b", raw, flags=re.IGNORECASE)[0]
    text = re.sub(r"\b(?:zum|zur|ins|in die|in den|in das|nach)\b", " ", text, flags=re.IGNORECASE)
    text = FILLER_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.-")


def _resolve(raw: str):
    cleaned = _clean_place(raw)
    if not cleaned:
        return None, raw
    hits = airports.search(cleaned, limit=8)
    folded = airports._fold(cleaned)
    for hit in hits:
        data = hit.as_dict()
        if data.get("kind") == "group":
            continue
        if folded in {
            airports._fold(data["code"]),
            airports._fold(data["city"]),
            airports._fold(data["name"]),
        }:
            return hit, cleaned
    return (hits[0] if hits else None), cleaned


def _trip_type(text: str, codes: list[str]) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("nur hin", "one way", "einfacher flug")):
        return "one_way"
    if len(codes) == 2 and any(marker in lowered for marker in ("zurück", "zurueck", "retour", "hin und zurück")):
        return "return"
    if len(codes) > 2:
        return "multi"
    return "one_way"


def parse_search_text(text: str) -> NaturalSearchIntent:
    entries = []
    warnings: list[str] = []
    for match in KEYWORD_RE.finditer(text):
        keyword = match.group(1).lower()
        place, cleaned = _resolve(match.group(2))
        if place is None:
            warnings.append(f"Nicht erkannt: {cleaned.strip() or match.group(2).strip()}")
            continue
        entries.append((keyword, place))

    codes: list[str] = []
    labels: list[str] = []
    for i, (keyword, place) in enumerate(entries):
        next_keyword = entries[i + 1][0] if i + 1 < len(entries) else ""
        data = place.as_dict()
        if keyword == "von" and next_keyword in {"aus", "ab"} and data.get("kind") == "group":
            continue
        code = data["code"]
        if codes and codes[-1] == code:
            continue
        codes.append(code)
        labels.append(data["city"])

    if len(codes) < 2:
        raise ValueError("Bitte mindestens Start und Ziel nennen.")

    return NaturalSearchIntent(
        trip=_trip_type(text, codes),
        airports=codes,
        labels=labels,
        warnings=warnings,
    )
