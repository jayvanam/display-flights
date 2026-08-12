"""The local half of the Supabase mirror: row selection and enrichment.

The network half needs a service key, so it is not covered here. What IS covered is
the part that can silently corrupt the mirror: which rows get selected, and whether
the derived fields agree with what the page renders.
"""

from __future__ import annotations

import os
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


def test_since_date_skips_the_pre_overhaul_era(sample_db):
    """Alerts before 2026-08-11 were scored on the inflated baseline, so the initial
    backfill needs to be able to start at the overhaul."""
    all_rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    # The whole fixture is 2026-08-12, so a cutoff before it keeps everything...
    assert len(sync_supabase.read_new_alerts(
        sample_db, since_id=0, since_date="2026-08-01")) == len(all_rows)
    # ...and one after it keeps nothing.
    assert sync_supabase.read_new_alerts(
        sample_db, since_id=0, since_date="2026-08-13") == []


def test_since_date_and_limit_compose(sample_db):
    rows = sync_supabase.read_new_alerts(
        sample_db, since_id=0, since_date="2026-08-01", limit=5)
    assert len(rows) == 5


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


# ------------------------------------------------------------------ key handling

def _jwt(role: str) -> str:
    """A legacy-style Supabase JWT with the given role. Unsigned — classify_key only
    reads the payload, and these never leave the test."""
    import base64
    import json as _json

    def seg(obj):
        raw = base64.urlsafe_b64encode(_json.dumps(obj).encode()).decode()
        return raw.rstrip("=")   # real tokens are unpadded; the classifier re-pads

    return f"{seg({'alg': 'HS256'})}.{seg({'iss': 'supabase', 'role': role})}.sig"


def test_modern_key_prefixes_are_classified():
    assert sync_supabase.classify_key("sb_secret_abc123") == "secret"
    assert sync_supabase.classify_key("sb_publishable_abc123") == "publishable"


def test_legacy_jwt_role_is_read_from_the_payload():
    assert sync_supabase.classify_key(_jwt("service_role")) == "secret"
    assert sync_supabase.classify_key(_jwt("anon")) == "publishable"
    assert sync_supabase.classify_key(_jwt("authenticated")) == "publishable"


def test_the_real_publishable_key_is_recognised_as_unwritable():
    """The exact key the page ships. Pasting it into the sync must be caught before
    the first request, not discovered as a wall of 401s."""
    import generate as g
    assert sync_supabase.classify_key(g.SUPABASE_PUBLISHABLE_KEY) == "publishable"


def test_garbage_is_unknown_not_misread_as_secret():
    for bad in ("", "hunter2", "eyJ-not-base64", "eyJhbGciOiJIUzI1NiJ9.!!!.sig"):
        assert sync_supabase.classify_key(bad) == "unknown"


def test_secret_key_env_name_is_preferred_and_service_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_supabase, "HERE", tmp_path)  # no stray .env
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/")

    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_new")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_old")
    url, key = sync_supabase.load_env()
    assert key == "sb_secret_new", "SECRET must win when both are set"
    assert url == "https://example.supabase.co", "trailing slash must be stripped"

    monkeypatch.delenv("SUPABASE_SECRET_KEY")
    _, key = sync_supabase.load_env()
    assert key == "sb_secret_old", "SERVICE_KEY stays a working alias"


def test_env_file_parsing_tolerates_how_people_actually_write_them(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "﻿# leading BOM and a comment\n"
        "\n"
        "export SUPABASE_URL=https://example.supabase.co\n"
        '  SUPABASE_SECRET_KEY = "sb_secret_quoted"  \n'
        "IGNORED_NO_EQUALS\n"
        "OTHER=value # trailing comment\n"
    )
    monkeypatch.setattr(sync_supabase, "HERE", tmp_path)
    for name in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY", "OTHER"):
        monkeypatch.delenv(name, raising=False)

    url, key = sync_supabase.load_env()
    assert url == "https://example.supabase.co", "`export ` prefix must be stripped"
    assert key == "sb_secret_quoted", "quotes and padding must be stripped"
    assert os.environ["OTHER"] == "value", "trailing comment must not join the value"


def test_stale_checkout_error_names_what_it_read(monkeypatch, tmp_path, capsys):
    """The failure that actually happened: .env defined SECRET_KEY while the running
    code only read SERVICE_KEY. The message must show the mismatch."""
    (tmp_path / ".env").write_text("SUPABASE_URL=https://x.supabase.co\n")
    monkeypatch.setattr(sync_supabase, "HERE", tmp_path)
    for name in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY"):
        monkeypatch.delenv(name, raising=False)

    import pytest
    with pytest.raises(SystemExit) as exc:
        sync_supabase.load_env()
    msg = str(exc.value)
    assert "read from .env: SUPABASE_URL" in msg
    assert "git pull" in msg
    # Never echo a value, even a partial one.
    assert "x.supabase.co" not in msg.split("SUPABASE_URL=https://<ref>")[0]


def test_publishable_key_in_the_env_exits_rather_than_401ing(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_supabase, "HERE", tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_publishable_oops")

    import pytest
    with pytest.raises(SystemExit) as exc:
        sync_supabase.load_env()
    assert "PUBLISHABLE" in str(exc.value)
