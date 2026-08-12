#!/usr/bin/env python3
"""Render index.html from the recorded sample instead of the live Pi database.

Two uses:
  - Iterate on the design from the laptop, where data/fares.db does not exist.
  - Seed the published page before the Pi's first push, so GitHub Pages has a real
    (if fixed) window to serve rather than a 404.

Unlike tests/conftest.py this keeps the sample's ORIGINAL timestamps, so the page
honestly reports the window it came from (12 Aug 2026) rather than claiming to be
current.

    ./dev/preview.py [--out ../index.html]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import generate  # noqa: E402
from conftest import LINE, SAMPLE, SCHEMA  # noqa: E402


def build_db(path: Path) -> int:
    rows = []
    for line in SAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            raise SystemExit(f"unparsed sample line: {line}")
        rows.append((m["sent"], m["origin"], m["dest"], m["out"],
                     None if m["ret"] == "?" else m["ret"],
                     int(m["price"]), int(m["score"])))

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        """INSERT INTO deal_alerts
           (sent_at, origin, destination, outbound_date, return_date, price, deal_score)
           VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    con.commit()
    con.close()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "index.html")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sample.db"
        n = build_db(db)
        # The sample is a fixed historical window, so reach far enough back to
        # include all of it rather than the live 24h cutoff.
        rows = generate.load_alerts(db, hours=24 * 365 * 20)
        sales = generate.build_sales(rows)
        args.out.write_text(
            generate.render_page(sales, len(rows), 24, rows[0][0] if rows else "")
        )

    print(f"wrote {args.out} from the sample — {n} alerts, {len(sales)} sales")


if __name__ == "__main__":
    main()
