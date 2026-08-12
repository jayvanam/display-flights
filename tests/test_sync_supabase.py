"""The local half of the Supabase mirror: row selection and enrichment.

The network half needs a service key, so it is not covered here. What IS covered is
the part that can silently corrupt the mirror: which rows get selected, and whether
the derived fields agree with what the page renders.
"""

from __future__ import annotations

import sqlite3

import generate
import sync_supabase


def test_reads_only_rows_above_the_high_water_mark(sample_db, sample_rows):
    all_rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    assert len(all_rows) == len(sample_rows) == 144

    ids = [r["source_id"] for r in all_rows]
    assert ids == sorted(ids), "must be id-ordered so the last id is a safe high-water"

    # Resuming from halfway returns exactly the tail.
    midpoint = ids[len(ids) // 2]
    tail = sync_supabase.read_new_alerts(sample_db, since_id=midpoint)
    assert [r["source_id"] for r in tail] == [i for i in ids if i > midpoint]

    # Nothing left once caught up — the quiet-cycle case, which must cost no writes.
    assert sync_supabase.read_new_alerts(sample_db, since_id=ids[-1]) == []


def test_limit_caps_the_first_backfill(sample_db):
    capped = sync_supabase.read_new_alerts(sample_db, since_id=0, limit=10)
    assert len(capped) == 10


def test_derived_fields_match_what_the_page_renders(sample_db):
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    by_code = {r["destination"]: r for r in rows}

    rak = by_code["RAK"]
    assert rak["destination_name"] == generate.dest_name("RAK") == "Morocco"
    assert rak["region"] == generate.region_of("RAK") == "africa"

    # tier must be the same css class the card gets, or the mirror and the snapshot
    # would label the same alert differently.
    for row in rows:
        assert row["tier"] == generate.tier_for_quality(row["deal_score"])[0]
        assert row["tier"] in {"t1", "t2", "t3", "t4"}


def test_naive_utc_timestamps_are_made_explicit(sample_db):
    """deal_alerts.sent_at is naive UTC. Postgres timestamptz would interpret a
    naive value in the SERVER's zone, silently shifting every alert."""
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    for row in rows:
        stamp = row["sent_at"]
        assert stamp.endswith("Z") or "+" in stamp[10:], stamp


def test_already_offset_timestamps_are_not_double_suffixed(tmp_path):
    path = tmp_path / "tz.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE deal_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL,
            destination TEXT NOT NULL, outbound_date TEXT NOT NULL, return_date TEXT,
            price INTEGER NOT NULL, deal_score INTEGER NOT NULL, booking_url TEXT,
            sent_at TEXT NOT NULL);
    """)
    con.executemany(
        "INSERT INTO deal_alerts (origin,destination,outbound_date,return_date,"
        "price,deal_score,sent_at) VALUES (?,?,?,?,?,?,?)",
        [("ORD", "RAK", "2026-11-10", "2026-11-20", 700, 100, "2026-08-12T11:20:00Z"),
         ("BOS", "RAK", "2026-11-10", "2026-11-20", 650, 100, "2026-08-12T11:20:00+00:00")],
    )
    con.commit()
    con.close()

    stamps = [r["sent_at"] for r in sync_supabase.read_new_alerts(path, since_id=0)]
    assert stamps == ["2026-08-12T11:20:00Z", "2026-08-12T11:20:00+00:00"]
    assert not any(s.endswith("ZZ") for s in stamps)


def test_null_booking_url_becomes_json_null_not_empty_string(sample_db):
    # The sample has no booking_url. Empty string would be stored as a real value and
    # then treated as a usable link by any consumer.
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    assert all(r["booking_url"] is None for r in rows)


def test_batch_size_is_sane():
    assert 100 <= sync_supabase.BATCH <= 1000
