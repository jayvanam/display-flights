#!/usr/bin/env python3
"""Push newly alerted deals from the Pi's `deal_alerts` into Supabase `fare_alerts`.

This is the live feed behind the page's "Just in" section: alerts reach the phone
within one sync interval instead of waiting for the next static rebuild.

SCOPE: alerted deals only. `fare_history` is deliberately never touched — it is not a
backup target here and this is not a disaster-recovery tool. (For the record, it also
could not be: ~8.85M rows / ~2.4 GB against a 500 MB free tier, and no REST API moves
that in one write.)

REQUEST BUDGET, since that was the point: one GET for the high-water mark, one POST
per 500 new rows, one POST to log the run. A quiet 15-minute cycle is 2 requests.

Auth: the SECRET key, which bypasses RLS. It must never reach this public repo —
keep it in an untracked `.env` beside this script, or in the crontab environment:

    SUPABASE_URL=https://iceqjfmokjwwcindfuyk.supabase.co
    SUPABASE_SECRET_KEY=sb_secret_...        # Settings > API Keys

The publishable key will NOT work: RLS grants it SELECT and nothing else, so every
insert would 401. classify_key() checks for that mistake before the first request.

Usage:
    ./sync_supabase.py --db ../flights/data/fares.db [--dry-run] [--max-batches N]
"""

from __future__ import annotations

import argparse
import base64
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

def classify_key(key: str) -> str:
    """'secret' | 'publishable' | 'unknown'.

    Worth doing up front: only a secret key bypasses RLS. A publishable/anon key
    reads fine and then fails on every insert, which surfaces as a wall of 401s
    rather than "you pasted the wrong key".
    """
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "publishable"
    if key.startswith("eyJ"):
        # Legacy JWT: the role lives unsigned in the payload, so just read it. No
        # verification needed — we only want to know which key the user pasted.
        try:
            payload = key.split(".")[1]
            payload += "=" * (-len(payload) % 4)          # restore base64 padding
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role", "")
        except Exception:
            return "unknown"
        if role == "service_role":
            return "secret"
        if role in ("anon", "authenticated"):
            return "publishable"
    return "unknown"


def load_env() -> tuple[str, str]:
    """Read Supabase credentials from the environment, falling back to a local
    untracked .env. Fails loudly rather than silently syncing nowhere."""
    env_file = HERE / ".env"
    found: list[str] = []
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip().lstrip("﻿")       # tolerate a UTF-8 BOM
            if not line or line.startswith("#") or "=" not in line:
                continue
            # `export FOO=bar` is how most people write an env file by hand; without
            # this the name parses as "export FOO" and the value is silently lost.
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]                   # matched surrounding quotes
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()   # trailing comment
            if name:
                found.append(name)
                os.environ.setdefault(name, value)

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    # SUPABASE_SECRET_KEY is the current name (matching Supabase's own
    # sb_secret_... keys). SUPABASE_SERVICE_KEY is accepted as an alias for the
    # legacy service_role JWT so an older .env keeps working.
    key = (os.environ.get("SUPABASE_SECRET_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY")
           or "")

    if not url or not key:
        # Report the NAMES this build actually parsed (never the values). If the
        # file defines exactly what the error asks for, the checkout is stale — which
        # is otherwise a genuinely baffling five minutes.
        if found:
            seen = "read from .env: " + ", ".join(sorted(set(found)))
        elif env_file.exists():
            seen = f"{env_file} exists but no NAME=VALUE lines parsed from it"
        else:
            seen = f"no {env_file}"
        sys.exit(
            "error: SUPABASE_URL and SUPABASE_SECRET_KEY must be set.\n\n"
            f"  {seen}\n"
            f"  this build reads: SUPABASE_URL, SUPABASE_SECRET_KEY"
            " (or SUPABASE_SERVICE_KEY)\n\n"
            "If the names above already look right, this checkout is older than the\n"
            "rename — run `git pull` and try again.\n\n"
            "  SUPABASE_URL=https://<ref>.supabase.co\n"
            "  SUPABASE_SECRET_KEY=sb_secret_...   # Settings > API Keys\n\n"
            "A secret key is required. The publishable key is read-only by design —\n"
            "RLS grants it SELECT and nothing else — so a sync with it would fail on\n"
            "every insert."
        )

    kind = classify_key(key)
    if kind == "publishable":
        sys.exit(
            "error: that is a PUBLISHABLE (anon) key, which cannot write.\n"
            "RLS grants it SELECT only, so every batch would 401. Use the secret\n"
            "key from Settings > API Keys — it bypasses RLS."
        )
    if kind == "unknown":
        print("warning: could not identify the key type; expected sb_secret_... "
              "or a service_role JWT. Continuing.", file=sys.stderr)
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


def read_new_alerts(db_path: Path, since_id: int, limit: int | None = None,
                    since_date: str | None = None):
    """Local deal_alerts rows above `since_id`, enriched for the feed.

    `since_date` (YYYY-MM-DD) additionally skips older alerts. Worth using for the
    initial backfill: alerts sent before the 2026-08-11 deal-logic overhaul were
    scored against the page-pooled baseline that inflated every percentile, so a
    June row labelled "Exceptional" does not mean what an August one does. Syncing
    from the overhaul forward keeps every row in the feed comparable.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT id, origin, destination, outbound_date, return_date, price,"
               "       deal_score, booking_url, sent_at"
               "  FROM deal_alerts WHERE id > ?")
        params: list = [since_id]
        if since_date:
            # sent_at is an ISO string, so a lexical compare is a date compare.
            sql += " AND sent_at >= ?"
            params.append(since_date)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = con.execute(sql, tuple(params)).fetchall()
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
    ap.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                    help="skip alerts sent before this date. Use for the initial "
                         "backfill: pre-2026-08-11 alerts were scored on the old "
                         "inflated baseline and are not comparable to current ones.")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"error: no database at {args.db}")

    url, key = load_env()
    water = high_water(url, key)
    limit = args.max_batches * BATCH if args.max_batches else None
    rows = read_new_alerts(args.db, water, limit, args.since)

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
