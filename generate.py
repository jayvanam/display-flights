#!/usr/bin/env python3
"""Render recent fare alerts as one self-contained, phone-first HTML page.

Reads the `deal_alerts` table written by the private `flights` scraper and emits a
single static file. No JS framework, no external requests, no inbound network path
to the Pi: the Pi generates this after each sweep and pushes it to a public repo,
GitHub Pages serves it, the phone opens a URL.

The central transform is ALERT -> SALE. The scraper keys a route on
(origin, destination-airport), and config.py expands every airport code of a
destination into its own route, so one regional fare sale fans out into dozens of
independent alerts: a Morocco sale is 3 codes x 19 origins = 57 possible routes,
and on 2026-08-12 a single 2026-11-10 departure produced 16 separate alerts. That
is correct behaviour for notification (you want the alert for *your* airport) and
unreadable as a list. Here they collapse to one card per
(destination, outbound date, return date), with each origin as a chip carrying its
own price, so 27 Morocco rows become 5 cards.

Presentation lives OUTSIDE this file: `theme.css` holds every design token and
component rule, `template.html` holds the page shell. Both are inlined at build
time. This module only decides what the data means.

Usage:
    ./generate.py --db ../flights/data/fares.db --hours 24 --out index.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import string
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import catalog

HERE = Path(__file__).resolve().parent
THEME_CSS = HERE / "theme.css"
TEMPLATE_HTML = HERE / "template.html"

# Supabase, for the History view only. Both values are PUBLIC by design: the
# publishable key is meant to ship in client code, and RLS grants `anon` SELECT on
# fare_alerts and nothing else — no insert, update or delete policy exists. The
# SECRET key (sb_secret_..., which bypasses RLS) is used only by sync_supabase.py on
# the Pi and must never appear here; this repo is public.
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://iceqjfmokjwwcindfuyk.supabase.co").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_9givag6OhjI1sDzPty-Usg_lc6m6KwU")

# Alert tiers, keyed on the PERCENTILE the scraper ranked the fare at (0 = cheapest
# observation in its comparable bucket; lower is better). Vendored from
# analyzer.TIERS plus the GOOD floor at config.ALERT_PERCENTILE;
# tests/test_catalog.py asserts these still match upstream, because the scraper
# already has one silently-duplicated copy of these boundaries in db._tier_rank and
# a third un-checked copy is how a display quietly disagrees with its own alerts.
TIERS = (
    (0.5, "t1", "Exceptional"),
    (1.5, "t2", "Excellent"),
    (3.0, "t3", "Great"),
    (5.0, "t4", "Good"),
)

# deal_alerts stores `quality` = 100 - percentile * 20 (higher is better) because
# digest.py sorts on it descending. Invert to recover the percentile the tiers speak.
QUALITY_PER_PERCENTILE = 20.0


def percentile_from_quality(quality: int) -> float:
    return (100.0 - quality) / QUALITY_PER_PERCENTILE


def tier_for_quality(quality: int) -> tuple[str, str]:
    """(css class, label) for a persisted quality score.

    Anything at or past the bottom boundary is still "Good": every row in
    deal_alerts already passed analyzer's gate, and quality clamps to 0 both at the
    bar and far below it, so a stricter reading here would render real alerts as
    untiered. Bonuses can also widen a fare's own threshold above ALERT_PERCENTILE.
    """
    pct = percentile_from_quality(quality)
    for bound, css, label in TIERS:
        if pct <= bound:
            return css, label
    return TIERS[-1][1], TIERS[-1][2]


def tier_rank(quality: int) -> int:
    """0 = best tier. Used to sort cards so the strongest deals lead."""
    pct = percentile_from_quality(quality)
    for i, (bound, _, _) in enumerate(TIERS):
        if pct <= bound:
            return i
    return len(TIERS)


# ---------------------------------------------------------------- catalog lookups

def _build_lookups():
    code_to_dest, code_to_region = {}, {}
    for name, region, codes in catalog.DESTINATIONS:
        for code in codes:
            # First writer wins: a code can appear as both an origin-city
            # destination and elsewhere, and the scraper resolves in list order.
            code_to_dest.setdefault(code, name)
            code_to_region.setdefault(code, region)
    return code_to_dest, code_to_region


CODE_TO_DEST, CODE_TO_REGION = _build_lookups()
REGION_LABELS = dict(catalog.REGIONS)


def city(code: str) -> str:
    """'RAK' -> 'Marrakesh'. Drops the country/state; the code is shown alongside."""
    label = catalog.AIRPORTS.get(code)
    return label.split(",")[0].strip() if label else code


def dest_name(code: str) -> str:
    return CODE_TO_DEST.get(code) or city(code)


def region_of(code: str) -> str:
    # A code that is no longer any destination (the scraper retired several hub
    # destination rows but kept them as origins) still renders, under "Other".
    return CODE_TO_REGION.get(code, "other")


# ------------------------------------------------------------------------- model

class Leg(NamedTuple):
    """One alert inside a sale: a single origin's price for that date pair."""
    origin: str
    code: str
    price: int
    quality: int
    url: str  # the scraper's stored booking_url; "" when it was never captured
    airline: str  # free text, "" when the parser captured none; may be comma-joined
    sent_at: str  # when this alert fired (UTC ISO), for the "found Xh ago" line
    kind: str = "deal"  # "deal" = percentile-scored; "mistake" = region-hunter hit


def search_url(origin: str, code: str, outbound: str, return_date: str | None) -> str:
    """Fallback Google Flights link when a row has no stored booking_url.

    The scraper's own links carry a `tfs=` protobuf blob built by
    http_client.build_tfs, which is not worth reimplementing here — a plain query
    search resolves to the same itinerary and needs nothing but the columns we have.
    """
    q = f"Flights from {origin} to {code} on {outbound}"
    if return_date:
        q += f" through {return_date}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


class Sale:
    """One shopping decision: a destination on a specific date pair.

    Groups every origin the scraper alerted on for that pairing, cheapest first.
    """

    __slots__ = ("dest", "region", "outbound", "return_date", "legs")

    def __init__(self, dest, region, outbound, return_date):
        self.dest = dest
        self.region = region
        self.outbound = outbound
        self.return_date = return_date
        self.legs = []

    @property
    def best_price(self) -> int:
        return self.legs[0].price

    @property
    def max_price(self) -> int:
        return self.legs[-1].price

    @property
    def best_quality(self) -> int:
        return max(leg.quality for leg in self.legs)

    @property
    def is_mistake(self) -> bool:
        """True for a region-hunter card. build_sales keys on kind, so a sale's legs
        are all one kind and the first leg settles it."""
        return self.legs[0].kind == "mistake"

    @property
    def cities(self) -> list[str]:
        seen = []
        for leg in self.legs:
            name = city(leg.code)
            if name not in seen:
                seen.append(name)
        return seen

    @property
    def best_airline(self) -> str:
        """Carrier on the cheapest leg — the one the headline price belongs to.

        Airline is per-leg, so a single card-level value can only ever describe one
        of them; the rest ride along in each chip's title. Relies on legs being
        price-sorted, as best_price does.
        """
        return self.legs[0].airline

    @property
    def newest_sent(self) -> str:
        """Most recent alert in this sale, for the "found Xh ago" line.

        Max rather than the cheapest leg's: the card is fresh if ANY of its origins
        just fired, and taking the cheapest leg's stamp would age a card that picked
        up a new origin seconds ago.
        """
        return max((leg.sent_at for leg in self.legs), default="")

    def link_for(self, leg: Leg) -> str:
        return leg.url or search_url(leg.origin, leg.code,
                                     self.outbound, self.return_date)

    @property
    def nights(self) -> int | None:
        if not self.return_date:
            return None
        try:
            a = datetime.strptime(self.outbound, "%Y-%m-%d")
            b = datetime.strptime(self.return_date, "%Y-%m-%d")
        except ValueError:
            return None
        return (b - a).days

    def sort_key(self):
        return (tier_rank(self.best_quality), self.best_price)


def load_alerts(db_path: Path, hours: int):
    """Recent deal_alerts rows. sent_at is UTC (db._utcnow_iso)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # deal_alerts.airline arrived 2026-08-12 and this repo deploys separately from
        # flights, so publish.sh must keep rendering against a fares.db that has not
        # been migrated yet — with no airline rather than a hard failure every 20 min.
        has_airline = any(
            row[1] == "airline"
            for row in con.execute("PRAGMA table_info(deal_alerts)")
        )
        airline_col = "airline" if has_airline else "NULL AS airline"
        return con.execute(
            f"""
            SELECT sent_at, origin, destination, outbound_date, return_date,
                   price, deal_score, booking_url, {airline_col}, 'deal' AS kind
              FROM deal_alerts
             WHERE sent_at >= ?
             ORDER BY sent_at DESC
            """,
            (cutoff,),
        ).fetchall() + _load_mistakes(con, cutoff)
    finally:
        con.close()


def _load_mistakes(con, cutoff: str) -> list:
    """Region-hunter rows, shaped like the deal_alerts SELECT above. [] if absent.

    Absent is normal — publish.sh runs every 20 minutes and this repo deploys
    independently of the scraper, so it must render against a fares.db predating the
    hunter rather than failing. deal_score is NULL because a hunter hit has no
    percentile; it cleared an absolute floor, not a comparison against history.
    """
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                       " AND name='mistake_alerts'").fetchone():
        return []
    return con.execute(
        """
        SELECT sent_at, origin, destination, outbound_date, return_date,
               price, NULL AS deal_score, booking_url, airline, 'mistake' AS kind
          FROM mistake_alerts
         WHERE sent_at >= ?
         ORDER BY sent_at DESC
        """,
        (cutoff,),
    ).fetchall()


def build_sales(rows) -> list[Sale]:
    grouped: dict[tuple, Sale] = {}
    for row in rows:
        (sent, origin, code, outbound, return_date,
         price, quality, url, airline) = row[:9]
        # A row without a kind is a deal — mirrors the fare_alerts column default, and
        # keeps every existing caller (and any fares.db predating the hunter) working.
        kind = row[9] if len(row) > 9 else "deal"
        dest = dest_name(code)
        # kind is part of the key ON PURPOSE. A hunter hit and a scored deal for the
        # same destination and dates are different claims; merging them would let
        # best_quality come from the scored leg and badge the whole card as tiered.
        key = (dest, outbound, return_date, kind)
        sale = grouped.get(key)
        if sale is None:
            sale = Sale(dest, region_of(code), outbound, return_date)
            grouped[key] = sale
        sale.legs.append(Leg(origin, code, int(price),
                             int(quality) if quality is not None else 0,
                             url or "", airline or "", sent or "", kind or "deal"))

    sales = list(grouped.values())
    for sale in sales:
        # Cheapest first: the price you'd actually book, and the chip we highlight.
        sale.legs.sort(key=lambda leg: (leg.price, leg.origin))
    sales.sort(key=Sale.sort_key)
    return sales


# ------------------------------------------------------------------------ render

# Origins beyond this many collapse behind a "Show all N" toggle.
CHIP_COLLAPSE_AFTER = 6

# Region-hunter cards. MUST match TIER_LABELS/MISTAKE_CSS in template.html and the
# .tier-mistake rules in theme.css — the Python fallback and the JS live view are
# visible in sequence as the fetch resolves, so any divergence reads as a glitch.
MISTAKE_CSS = "mistake"
MISTAKE_LABEL = "Error fare · unverified"


def fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %-d %b")
    except ValueError:
        return iso


def fmt_stamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%-d %b, %H:%M UTC")


def fmt_ago(iso: str, now: datetime | None = None) -> str:
    """How long ago an alert fired: "just now", "34m ago", "3h ago", "2d ago".

    ⚠️ Mirrored in the page's JS `fmtAgo`. The thresholds and wording must match, or
    the static snapshot and the Supabase render disagree about the same alert — and
    the two are visible in sequence as the fetch resolves. `tests/test_generate.py`
    pins the boundaries.

    A naive stamp is read as UTC, matching db._utcnow_iso, which writes naive UTC.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = ((now or datetime.now(timezone.utc)) - dt).total_seconds()

    # Clamp instead of rendering "-3m ago": clock skew between the Pi and a phone is
    # real, and a negative age reads as a bug rather than as freshness.
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def render_card(sale: Sale) -> str:
    # A hunter card gets its own class and label rather than a tier. It cleared an
    # absolute floor, not a percentile against this route's history, and this page is
    # public — rendering it as "Exceptional" would assert a comparison never made.
    if sale.is_mistake:
        tier_css, tier_label = MISTAKE_CSS, MISTAKE_LABEL
    else:
        tier_css, tier_label = tier_for_quality(sale.best_quality)
    cities = " · ".join(sale.cities)
    codes = sorted({leg.code for leg in sale.legs})

    dates = html.escape(fmt_date(sale.outbound))
    if sale.return_date:
        dates += " &rarr; " + html.escape(fmt_date(sale.return_date))
    nights = sale.nights
    if nights:
        dates += f'<span class="dot">·</span>{nights} night{"s" if nights != 1 else ""}'

    price = f"${sale.best_price}"
    spread = ""
    if sale.max_price != sale.best_price:
        spread = f'<span class="spread">to ${sale.max_price}</span>'

    # The city line earns its space only when it says something the headline does
    # not: a multi-airport destination (Miami/Fort Lauderdale) or a country whose
    # cities are the actual news (Morocco -> Marrakesh, Casablanca). For "Denver /
    # Denver DEN" it is pure repetition, so it collapses to the bare code.
    code_str = html.escape(" ".join(codes))
    if sale.cities == [sale.dest]:
        subtitle = f'<span class="codes">{code_str}</span>'
    else:
        subtitle = f'{html.escape(cities)} <span class="codes">{code_str}</span>'

    n = len(sale.legs)
    if n == 1:
        # One origin needs no chip list — the chip would just restate the headline
        # price. Put the route inline, still tappable, and save the row.
        leg = sale.legs[0]
        href = html.escape(sale.link_for(leg), quote=True)
        foot_note = (
            f'<a class="origin-note solo" href="{href}"'
            f' target="_blank" rel="noopener"'
            f' aria-label="Open {html.escape(leg.origin)} to {html.escape(leg.code)}'
            f' on Google Flights">{html.escape(leg.origin)}'
            f'&nbsp;&rarr;&nbsp;{html.escape(leg.code)}</a>'
        )
        chips_block = ""
    else:
        foot_note = f'<span class="origin-note">{n} origins</span>'
        chips = []
        for i, leg in enumerate(sale.legs):
            best = " is-best" if i == 0 else ""
            href = html.escape(sale.link_for(leg), quote=True)
            arrow = f"{html.escape(leg.origin)}&nbsp;&rarr;&nbsp;{html.escape(leg.code)}"
            # Airline rides in the label rather than the visible capsule: it varies per
            # leg, and printing it inline would double every chip's width.
            carrier = f", {leg.airline}" if leg.airline else ""
            chips.append(
                f'<li><a class="chip{best}" href="{href}" target="_blank"'
                f' rel="noopener" title="{html.escape(leg.airline, quote=True)}"'
                f' aria-label="Open {html.escape(leg.origin)} to'
                f' {html.escape(leg.code)}, ${leg.price}'
                f'{html.escape(carrier, quote=True)}, on Google Flights">'
                f'<span class="chip-route">{arrow}</span>'
                f'<span class="chip-price">${leg.price}</span></a></li>'
            )
        chips_block = ('\n        <ul class="chips">\n'
                       + "\n".join("          " + c for c in chips)
                       + "\n        </ul>")
        if n > CHIP_COLLAPSE_AFTER:
            chips_block += (f'\n        <button class="more" type="button" hidden'
                            f' aria-expanded="true">Show all {n}</button>')

    # Airline of the cheapest leg — the carrier the headline price belongs to. Em dash
    # rather than an empty span when the parser captured none, matching the Discord
    # embed's own fallback (flights/notifier.py:160).
    airline_str = html.escape(sale.best_airline) if sale.best_airline else "&mdash;"
    when = sale.newest_sent
    when_block = ""
    if when:
        when_block = (f'<span class="when" title="{html.escape(fmt_stamp(when), quote=True)}">'
                      f'{html.escape(fmt_ago(when))}</span>')

    # data-sent drives the client-side "Latest" sort without re-parsing the rendered
    # text, and data-price does the same for the price tiebreak.
    return f"""      <article class="card tier-{tier_css}" data-region="{sale.region}"\
 data-sent="{html.escape(when, quote=True)}" data-price="{sale.best_price}"\
 data-tier="{tier_css}">
        <header class="card-head">
          <div class="card-id">
            <h2>{html.escape(sale.dest)}</h2>
            <p class="cities">{subtitle}</p>
          </div>
          <div class="card-price">
            <span class="price">{price}</span>{spread}
          </div>
        </header>
        <p class="dates">{dates}<span class="dot">·</span><span class="airline">{airline_str}</span></p>
        <div class="card-foot">
          <span class="badge">{tier_label}</span>
          {foot_note}{when_block}
        </div>{chips_block}
      </article>
"""


def render_tabs(sales: list[Sale]) -> str:
    counts = defaultdict(int)
    for sale in sales:
        counts[sale.region] += 1

    tabs = [
        '      <button class="tab" type="button" role="tab" data-region="all"'
        ' aria-selected="true">All<span class="n">%d</span></button>' % len(sales)
    ]
    for region_id, label in catalog.REGIONS:
        if not counts.get(region_id):
            continue  # a region with nothing in it is noise, not information
        tabs.append(
            f'      <button class="tab" type="button" role="tab" data-region="{region_id}"'
            f' aria-selected="false">{html.escape(label)}'
            f'<span class="n">{counts[region_id]}</span></button>'
        )
    return "\n".join(tabs)


def render_page(sales: list[Sale], alert_count: int, hours: int, newest: str) -> str:
    """Substitute the shell. Card HTML and CSS arrive as VALUES, so their literal
    `$` (every price) is never scanned as a placeholder."""
    template = string.Template(TEMPLATE_HTML.read_text())
    return template.substitute(
        css=THEME_CSS.read_text().rstrip(),
        sale_count=len(sales),
        alert_count=alert_count,
        hours=hours,
        newest=html.escape(fmt_stamp(newest)) if newest else "n/a",
        generated=datetime.now(timezone.utc).strftime("%-d %b, %H:%M UTC"),
        tabs=render_tabs(sales),
        cards="".join(render_card(s) for s in sales),
        supabase_url=html.escape(SUPABASE_URL, quote=True),
        supabase_key=html.escape(SUPABASE_PUBLISHABLE_KEY, quote=True),
        region_labels=_region_labels_json(),
        # High-water mark for the "Just in" fetch. Explicit UTC, because sent_at is
        # a naive UTC string locally and PostgREST would compare it in the server's
        # zone otherwise — silently hiding or duplicating an hour of alerts.
        newest_iso=html.escape(_as_utc(newest), quote=True) if newest else "",
    )


def _as_utc(iso: str) -> str:
    return iso if (iso.endswith("Z") or "+" in iso[10:]) else iso + "Z"


def _region_labels_json() -> str:
    """Region id -> label, for the client-rendered tab bar. Emitted from the same
    catalog the static snapshot uses, so the two cannot disagree — including the
    ORDER, since JS object keys iterate in insertion order and that is what puts the
    tabs in catalog order rather than count order.

    Labels are emitted RAW (json.dumps handles JS string escaping) and the page's
    esc() does the single HTML escape at render time. They used to be html.escaped
    here as well, so "Caribbean & Mexico" became "Caribbean &amp;amp; Mexico" by the
    time esc() had run over it a second time, and rendered with the entity visible.
    """
    return json.dumps({rid: label for rid, label in catalog.REGIONS},
                      ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path,
                    default=HERE.parent / "flights" / "data" / "fares.db",
                    help="path to the scraper's fares.db (opened read-only)")
    ap.add_argument("--hours", type=int, default=24,
                    help="how far back to include alerts (default 24)")
    ap.add_argument("--out", type=Path, default=HERE / "index.html")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"error: no database at {args.db} — pass --db")

    rows = load_alerts(args.db, args.hours)
    sales = build_sales(rows)
    newest = rows[0][0] if rows else ""
    args.out.write_text(render_page(sales, len(rows), args.hours, newest))

    print(f"wrote {args.out} — {len(rows)} alerts collapsed into {len(sales)} sales")


if __name__ == "__main__":
    main()
