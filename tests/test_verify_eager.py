"""Die ersten fuenf Kandidaten sind als erste bestaetigt.

Kein eigenes eager_limit noetig: die Rangfolge aus pairs_by_rank erledigt das
schon. Ein Paar, das nur zu Kandidat sechs gehoert, wird erst beim sechsten
Durchlauf zum ersten Mal gesehen und kann deshalb gar nicht vor einem Paar
stehen, das Kandidat eins bereits gebraucht hat. Der Test haelt genau diese
Eigenschaft fest, damit eine spaetere Umsortierung sie nicht still verliert.
"""

from __future__ import annotations

from datetime import date

from flightopt.search.verify import pairs_by_rank, verify
from tests.test_verify_order import RecordingSource, combo, make_spec


def test_every_pair_of_the_first_five_precedes_the_later_ones():
    """Faellt aus der Rangfolge heraus, ohne ein eigenes eager_limit."""
    shortlist = [
        combo((2026, 10, 1 + i), (2026, 10, 15 + i), (2026, 11, 1 + i))
        for i in range(8)
    ]

    ordered = pairs_by_rank(shortlist)
    position = {pair: i for i, pair in enumerate(ordered)}

    early: set[tuple[int, date]] = set()
    for c in shortlist[:5]:
        early.update((i, d) for i, d in enumerate(c.dates))
    late: set[tuple[int, date]] = set()
    for c in shortlist[5:]:
        late.update((i, d) for i, d in enumerate(c.dates))
    late -= early

    assert early and late
    assert max(position[p] for p in early) < min(position[p] for p in late)


async def test_the_first_five_candidates_resolve_before_the_rest():
    spec = make_spec()
    shortlist = [
        combo((2026, 10, 1 + i), (2026, 10, 15 + i), (2026, 11, 1 + i), price=9000 + i)
        for i in range(8)
    ]
    src = RecordingSource()

    verified, report = await verify(spec, shortlist, [src], limit=10)

    early_days = {d for c in shortlist[:5] for d in c.dates}
    late_only = {d for c in shortlist[5:] for d in c.dates} - early_days

    positions = {pair: i for i, pair in enumerate(src.asked)}
    last_early = max(i for (_, day), i in positions.items() if day in early_days)
    first_late = min(i for (_, day), i in positions.items() if day in late_only)

    assert last_early < first_late
    assert report.confirmed == 8
    assert len(verified) == 8


async def test_the_pair_set_is_unchanged_by_the_new_order():
    """Die Reihenfolge aendert sich, die Menge der Abrufe nicht."""
    spec = make_spec()
    shortlist = [
        combo((2026, 10, 1), (2026, 10, 5), (2026, 10, 9)),
        combo((2026, 10, 1), (2026, 10, 6), (2026, 10, 9)),
    ]
    src = RecordingSource()

    _, report = await verify(spec, shortlist, [src], limit=10)

    assert len(src.asked) == 4
    assert report.calls == 4
