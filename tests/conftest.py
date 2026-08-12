"""Shared fixtures: turn the recorded sample into a throwaway fares.db."""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = Path(__file__).resolve().parent / "sample_alerts.txt"

LINE = re.compile(
    r"^(?P<sent>\S+)\s+(?P<origin>[A-Z]{3})->(?P<dest>[A-Z]{3})\s+"
    r"(?P<out>\d{4}-\d{2}-\d{2})\s+to\s+(?P<ret>\d{4}-\d{2}-\d{2}|\?)\s+"
    r"\$(?P<price>\d+)\s+score\s+(?P<score>\d+)/100\s*$"
)

# Mirrors the scraper's deal_alerts table (db.py). Only the columns the page reads.
SCHEMA = """
CREATE TABLE deal_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    return_date TEXT,
    price INTEGER NOT NULL,
    deal_score INTEGER NOT NULL,
    booking_url TEXT,
    sent_at TEXT NOT NULL
);
"""


def parse_sample():
    """Rows as (sent_at, origin, dest, outbound, return, price, quality).

    Timestamps are shifted so the newest sample alert lands ~1 minute ago, keeping
    the original spacing. Without this the fixture would silently fall outside
    load_alerts' 24h cutoff a day after it was recorded and every test would pass
    against zero rows.
    """
    raw = []
    for line in SAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        assert m, f"unparsed sample line: {line}"
        raw.append(m)

    stamps = [datetime.fromisoformat(m["sent"]) for m in raw]
    newest = max(stamps)
    anchor = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)

    rows = []
    for m, stamp in zip(raw, stamps):
        shifted = anchor - (newest - stamp)
        rows.append((
            shifted.isoformat(),
            m["origin"],
            m["dest"],
            m["out"],
            None if m["ret"] == "?" else m["ret"],
            int(m["price"]),
            int(m["score"]),
        ))
    return rows


@pytest.fixture(scope="session")
def sample_rows():
    return parse_sample()


@pytest.fixture()
def sample_db(tmp_path, sample_rows):
    path = tmp_path / "fares.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        """INSERT INTO deal_alerts
           (sent_at, origin, destination, outbound_date, return_date, price, deal_score)
           VALUES (?,?,?,?,?,?,?)""",
        sample_rows,
    )
    con.commit()
    con.close()
    return path
