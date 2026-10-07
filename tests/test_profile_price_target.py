"""Optional price targets use minor units and preserve existing profiles."""

from datetime import date

import pytest

from flightopt.domain.models import LegSpec, SearchSpec
from flightopt.jobs.daily import list_profiles, save_profile, update_profile
from flightopt.storage import db


def spec():
    return SearchSpec(legs=(LegSpec("BER", "ATH"),), stays=(),
                      window_start=date(2027, 1, 1), window_end=date(2027, 1, 10))


def test_new_profile_target_is_optional_and_listed_in_its_currency(tmp_path):
    conn = db.connect(tmp_path / "targets.db")
    profile_id = save_profile(conn, "Athen", [spec()])
    row = list_profiles(conn)[0]
    assert row["id"] == profile_id
    assert row["price_target_minor"] is None
    assert row["currency"] == "EUR"


def test_target_can_be_saved_changed_and_removed(tmp_path):
    conn = db.connect(tmp_path / "targets.db")
    profile_id = save_profile(conn, "Athen", [spec()], price_target_minor=25099)
    assert list_profiles(conn)[0]["price_target_minor"] == 25099
    update_profile(conn, profile_id, price_target_minor=19900)
    assert list_profiles(conn)[0]["price_target_minor"] == 19900
    update_profile(conn, profile_id, name="Athen neu")
    assert list_profiles(conn)[0]["price_target_minor"] == 19900
    update_profile(conn, profile_id, price_target_minor=None)
    assert list_profiles(conn)[0]["price_target_minor"] is None


@pytest.mark.parametrize("target", [0, -1, True, 1.5, "25000", 100000001])
def test_target_rejects_invalid_minor_amounts(tmp_path, target):
    conn = db.connect(tmp_path / "invalid.db")
    profile_id = save_profile(conn, "Athen", [spec()])
    with pytest.raises(ValueError):
        update_profile(conn, profile_id, price_target_minor=target)
    assert list_profiles(conn)[0]["price_target_minor"] is None


def test_existing_database_gets_nullable_target_without_backfill(tmp_path):
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    profile_id = save_profile(conn, "Alt", [spec()])
    conn.execute("ALTER TABLE search_profile DROP COLUMN price_target_minor")
    conn.close()
    conn = db.connect(path)
    assert list_profiles(conn)[0]["price_target_minor"] is None
    assert list_profiles(conn)[0]["id"] == profile_id
    conn.close()
    conn = db.connect(path)
    assert list_profiles(conn)[0]["price_target_minor"] is None
