"""Die beiden neuen Tabellen entstehen wie alle anderen beim Verbinden."""

from __future__ import annotations

from flightopt.storage import db


def columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_the_hotel_tables_are_created_with_the_columns_the_design_names(tmp_path):
    conn = db.connect(tmp_path / "hotels.db")

    assert columns(conn, "hotel_property") >= {
        "property_key", "source", "name", "city", "country", "country_code",
        "stars", "lat", "lon", "review_rating", "review_count", "url",
        "first_seen", "last_seen",
    }
    # current_day traegt die Wiederaufnahme, ohne sie faengt ein Lauf von vorn an.
    assert columns(conn, "hotel_scan") >= {
        "id", "destination", "window_start", "window_end", "nights", "adults",
        "children", "rooms", "filters", "status", "current_day", "days_done",
        "days_total", "offers_found", "error", "created_at", "updated_at",
        "finished_at",
    }
    conn.close()


def test_connecting_twice_does_not_fall_over_the_existing_tables(tmp_path):
    path = tmp_path / "hotels.db"
    first = db.connect(path)
    first.execute(
        "INSERT INTO hotel_property(property_key, source, name, first_seen, last_seen) "
        "VALUES('trivago:x', 'trivago', 'Hotel X', '2026-09-08', '2026-09-08')"
    )
    first.close()

    second = db.connect(path)

    assert second.execute("SELECT COUNT(*) c FROM hotel_property").fetchone()["c"] == 1
    second.close()
