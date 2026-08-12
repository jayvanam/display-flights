#!/usr/bin/env python3
"""Mirror new rows from the Pi's `deal_alerts` into Supabase `fare_alerts`.

This is the off-host backup of every alert the scraper has ever sent, and the query
surface the web page uses for history beyond the baked-in 24h snapshot.

WHAT IT DOES NOT DO: mirror `fare_history`. That table is ~8.85M rows / ~2.4 GB and
grows until retention binds, which does not fit Supabase's free tier (500 MB) and
would not fit comfortably on Pro (8 GB) either, let alone move through a REST API in
"one big write". `deal_alerts` is the small, irreplaceable part — the alerts you
acted on — and that is what ships here. For the bulk observational history, use the
scraper's own compact `.db.gz` snapshots plus `BACKUP_RSYNC_DEST` in maintenance.py.

REQUEST BUDGET, since that was the point: one GET for the high-water mark, one POST
per 500 new rows, one POST to log the run. A quiet 15-minute cycle is 2 requests.

Auth: the SERVICE key, which bypasses RLS. It must never reach this public repo —
keep it in an untracked `.env` beside this script or in the crontab environment:

    SUPABASE_URL=https://iceqjfmokjwwcindfuyk.supabase.co
    SUPABASE_SERVICE_KEY=<secret, from Settings > API in the dashboard>

Usage:
    ./sync_supabase.py --db ../flights/data/fares.db [--dry-run] [--max-batches N]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import generate

HERE = Path(__file__).resolve().parent
BATCH = 500          # rows per POST; PostgREST handles far more, this keeps retries cheap
TIMEOUT = 30


# ------------------------------------------------------------------------ config

def load_env() -> tuple[str, str]:
    """Read Supabase credentials from the environment, falling back to a local
    untracked .env. Fails loudly rather than silently syncing nowhere."""
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        sys.exit(
            "error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (env or "
            f"{env_file}).\n"
            "The SERVICE key is required — the publishable/anon key is read-only by\n"
            "design (RLS grants it SELECT and nothing else), so a sync with it would\n"
            "fail on every insert."
        )
    if "service" not in key and key.startswith("eyJ"):
        # Legacy JWTs encode the role; a quick sanity check beats a 401 per batch.
        print("warning: SUPABASE_SERVICE_KEY does not look like a service key",
              file=sys.stderr)
    return url, key


def request(url: str, key: str, method: str, path: str,
            body: object | None = None, extra_headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise SystemExit(f"error: {method} {path} -> HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}")


# -------------------------------------------------------------------- the mirror

def high_water(url: str, key: str) -> int:
    """Largest source_id already stored. 0 when the table is empty.

    Syncing strictly above this is what keeps the run incremental AND guards the
    one hazard of using the Pi's autoincrement id as a primary key: if fares.db is
    ever rebuilt its ids restart at 1, and without this an old id would silently
    overwrite a newer alert.
    """
    rows = request(url, key, "GET",
                   "fare_alerts?select=source_id&order=source_id.desc&limit=1")
    return int(rows[0]["source_id"]) if rows else 0


def read_new_alerts(db_path: Path, since_id: int, limit: int | None = None):
    """Local deal_alerts rows above `since_id`, enriched for the mirror."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT id, origin, destination, outbound_date, return_date, price,"
               "       deal_score, booking_url, sent_at"
               "  FROM deal_alerts WHERE id > ? ORDER BY id")
        params: tuple = (since_id,)
        if limit:
            sql += " LIMIT ?"
            params = (since_id, limit)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    out = []
    for (rid, origin, code, outbound, ret, price, score, booking, sent_at) in rows:
        # sent_at is a naive UTC ISO string (db._utcnow_iso). Postgres timestamptz
        # would read a naive value in the server's zone, so make the UTC explicit.
        stamp = sent_at if (sent_at.endswith("Z") or "+" in sent_at[10:]) else sent_at + "Z"
        out.append({
            "source_id": rid,
            "origin": origin,
            "destination": code,
            "outbound_date": outbound,
            "return_date": ret,
            "price": int(price),
            "deal_score": int(score),
            "booking_url": booking or None,
            "sent_at": stamp,
            # Derived with the same helpers the page uses, so the mirror cannot
            # disagree with the rendered snapshot.
            "destination_name": generate.dest_name(code),
            "region": generate.region_of(code),
            "tier": generate.tier_for_quality(int(score))[0],
        })
    return out


def push(url: str, key: str, rows: list[dict]) -> None:
    """Upsert a batch on the primary key (source_id)."""
    request(url, key, "POST", "fare_alerts?on_conflict=source_id", rows,
            {"Prefer": "resolution=merge-duplicates,return=minimal"})


def log_run(url: str, key: str, added: int, water: int, note: str | None) -> None:
    request(url, key, "POST", "sync_runs",
            [{"rows_added": added, "high_water": water, "note": note}],
            {"Prefer": "return=minimal"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path,
                    default=HERE.parent / "flights" / "data" / "fares.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be pushed, contact Supabase read-only")
    ap.add_argument("--max-batches", type=int, default=0,
                    help="cap batches per run (0 = no cap). Useful for the first "
                         "backfill, which may hold every alert ever sent.")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"error: no database at {args.db}")

    url, key = load_env()
    water = high_water(url, key)
    limit = args.max_batches * BATCH if args.max_batches else None
    rows = read_new_alerts(args.db, water, limit)

    if not rows:
        print(f"nothing new (high-water source_id={water})")
        if not args.dry_run:
            log_run(url, key, 0, water, None)
        return

    print(f"{len(rows)} new alerts above source_id={water}"
          f" (ids {rows[0]['source_id']}..{rows[-1]['source_id']})")
    if args.dry_run:
        print(json.dumps(rows[0], indent=2))
        print("dry run — nothing written")
        return

    sent = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        push(url, key, batch)
        sent += len(batch)
        print(f"  pushed {sent}/{len(rows)}")

    new_water = rows[-1]["source_id"]
    log_run(url, key, sent, new_water,
            f"batches={(sent + BATCH - 1) // BATCH}")
    print(f"synced {sent} rows; high-water now {new_water}")


if __name__ == "__main__":
    main()
