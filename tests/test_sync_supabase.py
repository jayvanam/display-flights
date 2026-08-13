"""The local half of the Supabase mirror: row selection and enrichment.

The network half needs a service key, so it is not covered here. What IS covered is
the part that can silently corrupt the mirror: which rows get selected, and whether
the derived fields agree with what the page renders.
"""

from __future__ import annotations

import os
import sqlite3

import generate
from datetime import date, datetime, timedelta, timezone

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
    backfill needs to be able to start at the overhaul.

    Cutoffs are derived from the fixture's OWN timestamps, not hardcoded. conftest's
    parse_sample deliberately shifts the sample so its newest row lands a minute ago
    (otherwise the 24h tests would pass against zero rows a day after recording), so
    the fixture is dated TODAY — not the 2026-08-12 it was captured on. A literal
    "2026-08-13" therefore matched everything from 2026-08-13 onward, and this test
    began failing on that date for reasons that had nothing to do with the code."""
    all_rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    newest = max(r["sent_at"][:10] for r in all_rows)
    day = date.fromisoformat(newest)
    before_all = (day - timedelta(days=1)).isoformat()
    after_all = (day + timedelta(days=1)).isoformat()

    assert len(sync_supabase.read_new_alerts(
        sample_db, since_id=0, since_date=before_all)) == len(all_rows)
    assert sync_supabase.read_new_alerts(
        sample_db, since_id=0, since_date=after_all) == []


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


def test_airline_is_carried_through_and_empty_becomes_null(tmp_path, schema_sql):
    """Empty string and NULL must collapse to one empty case, so the page has a single
    "no airline" branch rather than rendering a blank label for "" and a dash for None."""
    path = tmp_path / "airlines.db"
    con = sqlite3.connect(path)
    con.executescript(schema_sql)
    con.executemany(
        "INSERT INTO deal_alerts (origin,destination,outbound_date,return_date,"
        "price,deal_score,sent_at,airline) VALUES (?,?,?,?,?,?,?,?)",
        [("ORD", "KEF", "2026-11-10", "2026-11-20", 400, 95, "2026-08-12T11:20:00", "Icelandair"),
         ("BOS", "KEF", "2026-11-10", "2026-11-20", 420, 95, "2026-08-12T11:20:00", ""),
         ("JFK", "KEF", "2026-11-10", "2026-11-20", 430, 95, "2026-08-12T11:20:00", None),
         # Multi-carrier itineraries arrive comma-joined and must not be split or trimmed.
         ("MIA", "KEF", "2026-11-10", "2026-11-20", 440, 95, "2026-08-12T11:20:00",
          "JetBlue, Icelandair")],
    )
    con.commit()
    con.close()

    got = {r["origin"]: r["airline"] for r in sync_supabase.read_new_alerts(path, since_id=0)}
    assert got == {"ORD": "Icelandair", "BOS": None, "JFK": None,
                   "MIA": "JetBlue, Icelandair"}


def test_missing_airline_column_degrades_instead_of_breaking_the_sync(
        tmp_path, schema_pre_airline_sql):
    """The ordering hazard: this sync runs every 5 minutes and flights deploys
    separately, so an un-migrated fares.db must yield NULL airlines rather than
    "no such column: airline" on every cron tick until the other repo ships."""
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(schema_pre_airline_sql)
    con.execute(
        "INSERT INTO deal_alerts (origin,destination,outbound_date,return_date,"
        "price,deal_score,sent_at) VALUES ('ORD','KEF','2026-11-10','2026-11-20',400,95,"
        "'2026-08-12T11:20:00')")
    con.commit()
    con.close()

    rows = sync_supabase.read_new_alerts(path, since_id=0)
    assert len(rows) == 1
    assert rows[0]["airline"] is None
    assert rows[0]["city_name"] == "Reykjavik", "every other field must still populate"


def test_city_name_is_per_airport_not_per_market(sample_db):
    """The card subtitle needs the CITY for a code. destination_name cannot serve:
    RAK/CMN/FEZ all carry "Morocco", but the subtitle reads "Marrakesh · Casablanca"."""
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    by_code = {r["destination"]: r for r in rows}

    assert by_code["RAK"]["city_name"] == generate.city("RAK") == "Marrakesh"
    assert by_code["RAK"]["destination_name"] == "Morocco"

    # Same market, different city — this is the distinction the column exists for.
    if "CMN" in by_code:
        assert by_code["CMN"]["city_name"] != by_code["RAK"]["city_name"]
        assert by_code["CMN"]["destination_name"] == by_code["RAK"]["destination_name"]

    for row in rows:
        assert row["city_name"] == generate.city(row["destination"])


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


# ── region-hunter hits share the table with a NEGATIVE key ────────────────────

def _add_mistakes(db_path, n=2):
    con = sqlite3.connect(db_path)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS mistake_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, region TEXT NOT NULL,
        origin TEXT NOT NULL, destination TEXT NOT NULL, outbound_date TEXT NOT NULL,
        return_date TEXT, price INTEGER NOT NULL, threshold INTEGER NOT NULL,
        airline TEXT, booking_url TEXT, sent_at TEXT NOT NULL);""")
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    con.executemany(
        """INSERT INTO mistake_alerts (region,origin,destination,outbound_date,
             return_date,price,threshold,airline,booking_url,sent_at)
           VALUES ('Europe',?,?,'2026-11-10','2026-11-17',?,250,'AF','http://x',?)""",
        [(f"OR{i}", "CDG", 180 + i, stamp) for i in range(n)])
    con.commit(); con.close()


def test_a_missing_mistake_alerts_table_is_not_an_error(sample_db):
    """This sync runs every 5 minutes and the repos deploy independently, so it must
    keep working against a fares.db that predates the hunter."""
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    assert rows and all(r["kind"] == "deal" for r in rows)


def test_hunter_hits_get_a_negative_source_id(sample_db):
    """source_id is the PRIMARY KEY and mistake_alerts.id starts at 1 exactly like
    deal_alerts.id, so unsigned ids would collide and each table would overwrite the
    other's rows on upsert."""
    _add_mistakes(sample_db, n=2)
    rows = sync_supabase.read_new_alerts(sample_db, since_id=0)
    hits = [r for r in rows if r["kind"] == "mistake"]
    assert len(hits) == 2
    assert all(r["source_id"] < 0 for r in hits)
    deals = {r["source_id"] for r in rows if r["kind"] == "deal"}
    assert not deals & {r["source_id"] for r in hits}, "keys must not collide"


def test_hunter_hits_carry_no_score_and_no_tier(sample_db):
    """deal_score is what tier derives from, and a hunter hit has no percentile.
    Inventing one would render an unverified fare with an earned-looking tier."""
    _add_mistakes(sample_db, n=1)
    hit = [r for r in sync_supabase.read_new_alerts(sample_db, since_id=0)
           if r["kind"] == "mistake"][0]
    assert hit["deal_score"] is None and hit["tier"] is None
    # ...but the display fields the page needs are still derived.
    assert hit["destination_name"] and hit["region"]


def test_hunter_hits_are_enriched_like_deals(sample_db):
    _add_mistakes(sample_db, n=1)
    hit = [r for r in sync_supabase.read_new_alerts(sample_db, since_id=0)
           if r["kind"] == "mistake"][0]
    assert hit["destination_name"] == generate.dest_name("CDG")
    assert hit["city_name"] == generate.city("CDG")
    assert hit["region"] == generate.region_of("CDG")
    assert hit["sent_at"].endswith("Z"), "naive UTC must be made explicit for timestamptz"
