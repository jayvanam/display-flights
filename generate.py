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

Usage:
    ./generate.py --db ../flights/data/fares.db --hours 24 --out index.html
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import string
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import catalog

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
    def cities(self) -> list[str]:
        seen = []
        for leg in self.legs:
            name = city(leg.code)
            if name not in seen:
                seen.append(name)
        return seen

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
        return con.execute(
            """
            SELECT sent_at, origin, destination, outbound_date, return_date,
                   price, deal_score, booking_url
              FROM deal_alerts
             WHERE sent_at >= ?
             ORDER BY sent_at DESC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        con.close()


def build_sales(rows) -> list[Sale]:
    grouped: dict[tuple, Sale] = {}
    for row in rows:
        _sent, origin, code, outbound, return_date, price, quality, url = row
        dest = dest_name(code)
        key = (dest, outbound, return_date)
        sale = grouped.get(key)
        if sale is None:
            sale = Sale(dest, region_of(code), outbound, return_date)
            grouped[key] = sale
        sale.legs.append(Leg(origin, code, int(price), int(quality), url or ""))

    sales = list(grouped.values())
    for sale in sales:
        # Cheapest first: the price you'd actually book, and the chip we highlight.
        sale.legs.sort(key=lambda leg: (leg.price, leg.origin))
    sales.sort(key=Sale.sort_key)
    return sales


# ------------------------------------------------------------------------ render

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


def render_card(sale: Sale) -> str:
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
            chips.append(
                f'<li><a class="chip{best}" href="{href}" target="_blank"'
                f' rel="noopener" aria-label="Open {html.escape(leg.origin)} to'
                f' {html.escape(leg.code)}, ${leg.price}, on Google Flights">'
                f'<span class="chip-route">{arrow}</span>'
                f'<span class="chip-price">${leg.price}</span></a></li>'
            )
        chips_block = ('\n        <ul class="chips">\n'
                       + "\n".join("          " + c for c in chips)
                       + "\n        </ul>")
        if n > 6:
            chips_block += (f'\n        <button class="more" type="button" hidden'
                            f' aria-expanded="true">Show all {n}</button>')

    return f"""      <article class="card tier-{tier_css}" data-region="{sale.region}">
        <header class="card-head">
          <div class="card-id">
            <h2>{html.escape(sale.dest)}</h2>
            <p class="cities">{subtitle}</p>
          </div>
          <div class="card-price">
            <span class="price">{price}</span>{spread}
          </div>
        </header>
        <p class="dates">{dates}</p>
        <div class="card-foot">
          <span class="badge">{tier_label}</span>
          {foot_note}
        </div>{chips_block}
      </article>
"""


PAGE = string.Template("""<title>Bucket List Fares</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="description" content="$alert_count fare alerts from the last $hours hours, grouped by sale.">
<link rel="manifest" href="manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Fares">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<style>
/* ------------------------------------------------------------------ tokens
   The iOS system palette. Light is the full set on bare :root; dark redefines
   ONLY tokens, twice: once behind prefers-color-scheme (guarded so an explicit
   light choice beats a dark OS) and once behind the [data-theme] stamp (so the
   toggle wins in both directions). No component color is ever declared inside a
   media or [data-theme] block.

   Tier hues keep the scraper's own ordering, so a card agrees with the Discord
   alert that produced it: Exceptional red, Excellent purple, Great indigo, Good
   green. Great is systemIndigo rather than systemBlue on purpose — blue is
   reserved for things you can tap. */
:root {
  --bg:          #F2F2F7;                  /* systemGroupedBackground */
  --surface:     #FFFFFF;                  /* secondarySystemGroupedBackground */
  --fill:        rgba(118,118,128,0.12);   /* tertiarySystemFill */
  --fill-press:  rgba(118,118,128,0.20);
  --label:       #1D1D1F;
  --label-2:     rgba(60,60,67,0.60);      /* secondaryLabel */
  --label-3:     rgba(60,60,67,0.35);      /* tertiaryLabel */
  --separator:   rgba(60,60,67,0.14);
  --accent:      #007AFF;                  /* systemBlue — interactive only */
  --on-accent:   #FFFFFF;
  --t1:          #FF3B30;
  --t1-tint:     rgba(255,59,48,0.12);
  --t2:          #AF52DE;
  --t2-tint:     rgba(175,82,222,0.12);
  --t3:          #5856D6;
  --t3-tint:     rgba(88,86,214,0.12);
  --t4:          #34C759;
  --t4-tint:     rgba(52,199,89,0.16);
  --hairline:    rgba(60,60,67,0.12);
  --blur-bg:     rgba(242,242,247,0.82);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:          #000000;
    --surface:     #1C1C1E;
    --fill:        rgba(118,118,128,0.24);
    --fill-press:  rgba(118,118,128,0.36);
    --label:       #FFFFFF;
    --label-2:     rgba(235,235,245,0.60);
    --label-3:     rgba(235,235,245,0.30);
    --separator:   rgba(84,84,88,0.55);
    --accent:      #0A84FF;
    --on-accent:   #FFFFFF;
    --t1:          #FF453A;
    --t1-tint:     rgba(255,69,58,0.20);
    --t2:          #BF5AF2;
    --t2-tint:     rgba(191,90,242,0.20);
    --t3:          #5E5CE6;
    --t3-tint:     rgba(94,92,230,0.24);
    --t4:          #30D158;
    --t4-tint:     rgba(48,209,88,0.22);
    --hairline:    rgba(84,84,88,0.45);
    --blur-bg:     rgba(0,0,0,0.72);
  }
}
:root[data-theme="dark"] {
  --bg:          #000000;
  --surface:     #1C1C1E;
  --fill:        rgba(118,118,128,0.24);
  --fill-press:  rgba(118,118,128,0.36);
  --label:       #FFFFFF;
  --label-2:     rgba(235,235,245,0.60);
  --label-3:     rgba(235,235,245,0.30);
  --separator:   rgba(84,84,88,0.55);
  --accent:      #0A84FF;
  --on-accent:   #FFFFFF;
  --t1:          #FF453A;
  --t1-tint:     rgba(255,69,58,0.20);
  --t2:          #BF5AF2;
  --t2-tint:     rgba(191,90,242,0.20);
  --t3:          #5E5CE6;
  --t3-tint:     rgba(94,92,230,0.24);
  --t4:          #30D158;
  --t4-tint:     rgba(48,209,88,0.22);
  --hairline:    rgba(84,84,88,0.45);
  --blur-bg:     rgba(0,0,0,0.72);
}

/* One family throughout — SF on Apple devices via -apple-system. Figures are
   tabular everywhere digits align, which is what SF is for; a monospace face
   would be the wrong instrument. */
:root {
  --ui: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui,
        "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
}

* { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--label);
  font-family: var(--ui);
  font-size: 17px;
  line-height: 1.4;
  letter-spacing: -0.01em;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 720px; margin: 0 auto;
  padding: 0 max(16px, env(safe-area-inset-left)) 56px;
}

/* ------------------------------------------------------------------ header
   iOS large title, then a scrolling row of capsule filters. */
.top {
  position: sticky; top: 0; z-index: 10;
  background: var(--blur-bg);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 0.5px solid var(--hairline);
  padding-top: env(safe-area-inset-top);
}
.top-inner {
  max-width: 720px; margin: 0 auto;
  padding: 10px max(16px, env(safe-area-inset-left)) 0;
}
.titlebar { display: flex; align-items: center; gap: 12px; }
.titlebar h1 {
  font-size: 30px; font-weight: 700; letter-spacing: -0.022em;
  margin: 0; flex: 1; text-wrap: balance;
}
.sub {
  margin: 1px 0 0; font-size: 13px; color: var(--label-2);
  font-variant-numeric: tabular-nums; letter-spacing: -0.004em;
}

.themer {
  appearance: none; border: 0; background: var(--fill);
  color: var(--accent); border-radius: 50%;
  width: 34px; height: 34px; flex: none;
  font-size: 15px; line-height: 1; cursor: pointer;
  display: grid; place-items: center;
}
.themer:active { background: var(--fill-press); }
.themer:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }

.tabs {
  display: flex; gap: 8px; list-style: none;
  overflow-x: auto; scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
  margin: 12px -16px 0; padding: 0 16px 12px;
}
.tabs::-webkit-scrollbar { display: none; }
.tab {
  appearance: none; cursor: pointer; border: 0;
  font-family: var(--ui); font-size: 14px; font-weight: 500;
  letter-spacing: -0.01em; white-space: nowrap;
  color: var(--label); background: var(--fill);
  border-radius: 999px; padding: 7px 14px; min-height: 34px;
}
.tab:active { background: var(--fill-press); }
.tab:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.tab .n {
  font-variant-numeric: tabular-nums; color: var(--label-2);
  margin-inline-start: 6px;
}
.tab[aria-selected="true"] {
  background: var(--accent); color: var(--on-accent); font-weight: 600;
}
.tab[aria-selected="true"] .n { color: var(--on-accent); opacity: .7; }

/* ------------------------------------------------------------------- cards
   Inset grouped-list panels: rounded, filled, hairline-free, no shadow. */
.list { display: flex; flex-direction: column; gap: 12px; padding-top: 16px; }

.card {
  background: var(--surface);
  border-radius: 14px;
  padding: 14px 16px 15px;
}
.tier-t1 { --tier: var(--t1); --tier-tint: var(--t1-tint); }
.tier-t2 { --tier: var(--t2); --tier-tint: var(--t2-tint); }
.tier-t3 { --tier: var(--t3); --tier-tint: var(--t3-tint); }
.tier-t4 { --tier: var(--t4); --tier-tint: var(--t4-tint); }
.card[hidden] { display: none; }

.card-head { display: flex; align-items: flex-start; gap: 12px; }
.card-id { min-width: 0; flex: 1; }
.card-id h2 {
  font-size: 19px; font-weight: 600; letter-spacing: -0.018em;
  margin: 0; line-height: 1.2;
}
.cities {
  margin: 2px 0 0; font-size: 14px; color: var(--label-2);
  letter-spacing: -0.006em; overflow-wrap: anywhere;
}
.codes { color: var(--label-3); font-variant-numeric: tabular-nums; }

.card-price { text-align: end; flex: none; }
.price {
  display: block; font-size: 24px; font-weight: 600;
  letter-spacing: -0.022em; line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.spread {
  font-size: 12px; color: var(--label-3);
  font-variant-numeric: tabular-nums;
}

.dates {
  margin: 10px 0 0; font-size: 14px; color: var(--label-2);
  font-variant-numeric: tabular-nums; letter-spacing: -0.006em;
}
.dates .dot { color: var(--label-3); margin: 0 6px; }

.card-foot { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.badge {
  font-size: 12px; font-weight: 600; letter-spacing: -0.005em;
  color: var(--tier); background: var(--tier-tint);
  border-radius: 999px; padding: 3px 9px;
}
.origin-note {
  font-size: 13px; color: var(--label-2);
  font-variant-numeric: tabular-nums;
}

/* Chips are links to that exact origin's itinerary on Google Flights. The blue
   price is the affordance — in this language, blue means tappable. */
/* An even grid rather than a ragged wrap, so the prices line up in a column and a
   sixteen-origin sale still scans. */
.chips {
  list-style: none; margin: 12px 0 0; padding: 12px 0 0;
  border-top: 0.5px solid var(--separator);
  display: grid; gap: 8px;
  grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
}
.chips li { display: flex; }
.chip {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; width: 100%;
  min-height: 36px; padding: 6px 12px;
  background: var(--fill); border-radius: 999px;
  font-size: 14px; letter-spacing: -0.008em;
  font-variant-numeric: tabular-nums;
  color: var(--label-2); text-decoration: none;
  transition: background-color .15s ease;
}
.chip:active { background: var(--fill-press); }
.chip:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.chip-route { color: var(--label); }
.chip-price { color: var(--accent); font-weight: 600; }
.chip.is-best { background: var(--tier-tint); }
.chip.is-best .chip-route { color: var(--label); font-weight: 500; }

/* Long origin lists collapse. Rendered expanded and collapsed by script on load,
   so with no JS you still get every option rather than a dead "Show all". */
.chips.collapsed li:nth-child(n+7) { display: none; }
.more {
  appearance: none; border: 0; background: none; cursor: pointer;
  font-family: var(--ui); font-size: 14px; font-weight: 500;
  letter-spacing: -0.008em; color: var(--accent);
  padding: 9px 0 2px; min-height: 34px;
}
.more[hidden] { display: none; }
.more:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }

.origin-note.solo {
  color: var(--accent); text-decoration: none; font-weight: 500;
}
.origin-note.solo:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }

.empty {
  font-size: 15px; color: var(--label-2);
  padding: 48px 0; text-align: center;
}
.empty[hidden] { display: none; }

.foot {
  margin-top: 28px; font-size: 12px; color: var(--label-3);
  line-height: 1.6; letter-spacing: -0.003em;
}
.foot p { margin: 0 0 6px; }
.foot b { color: var(--label-2); font-weight: 600; font-variant-numeric: tabular-nums; }

@media (min-width: 600px) {
  .titlebar h1 { font-size: 34px; }
  .card { padding: 16px 18px 17px; }
  .card-id h2 { font-size: 20px; }
  .price { font-size: 26px; }
}

@media (prefers-reduced-motion: reduce) {
  .chip { transition: none; }
  * { scroll-behavior: auto !important; }
}
</style>

<header class="top">
  <div class="top-inner">
    <div class="titlebar">
      <div>
        <h1>Fares</h1>
        <p class="sub">$sale_count sales from $alert_count alerts &middot; last $hours h</p>
      </div>
      <button class="themer" type="button" id="themer" aria-label="Switch between light and dark">&#9788;</button>
    </div>
    <div class="tabs" role="tablist" aria-label="Region">
$tabs
    </div>
  </div>
</header>

<main class="wrap">
  <div class="list" id="list">
$cards
  </div>
  <p class="empty" hidden id="empty">Nothing here in the last $hours hours.</p>

  <footer class="foot">
    <p>Newest alert <b>$newest</b> &middot; built <b>$generated</b></p>
    <p>One card per destination and date pair; each pill is one origin that
      alerted, cheapest first. Tap a pill to open it on Google Flights. Prices are
      round trip, as seen at scrape time.</p>
  </footer>
</main>

<script>
(function () {
  "use strict";
  var root = document.documentElement;
  var btn = document.getElementById("themer");

  try {
    var saved = localStorage.getItem("fares-theme");
    if (saved) { root.setAttribute("data-theme", saved); }
  } catch (e) {}

  function currentTheme() {
    // Read the stamp when present, otherwise ask the OS. Deliberately does NOT
    // sniff a computed color: that would put a copy of a palette value in the JS,
    // which then silently disagrees the first time a token changes.
    var stamp = root.getAttribute("data-theme");
    if (stamp === "dark" || stamp === "light") { return stamp; }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  btn.addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("fares-theme", next); } catch (e) {}
  });

  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var empty = document.getElementById("empty");

  // Collapse long origin lists now that we know script is running. The markup ships
  // expanded so a no-JS reader sees every option instead of a button that does
  // nothing.
  Array.prototype.forEach.call(document.querySelectorAll(".more"), function (btn) {
    var list = btn.previousElementSibling;
    var total = list.children.length;
    list.classList.add("collapsed");
    btn.hidden = false;
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "Show all " + total;
    btn.addEventListener("click", function () {
      var collapsed = list.classList.toggle("collapsed");
      btn.setAttribute("aria-expanded", String(!collapsed));
      btn.textContent = collapsed ? "Show all " + total : "Show fewer";
    });
  });

  function show(region) {
    var shown = 0;
    cards.forEach(function (card) {
      var match = (region === "all") || (card.dataset.region === region);
      card.hidden = !match;
      if (match) { shown++; }
    });
    empty.hidden = shown > 0;
    var active = null;
    tabs.forEach(function (t) {
      var on = t.dataset.region === region;
      t.setAttribute("aria-selected", String(on));
      if (on) { active = t; }
    });
    // The filter row scrolls horizontally, so a restored region further along it
    // would otherwise leave the page filtered with no visible active pill.
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ inline: "center", block: "nearest" });
    }
    try { localStorage.setItem("fares-region", region); } catch (e) {}
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { show(tab.dataset.region); });
  });

  var start = "all";
  try {
    var savedRegion = localStorage.getItem("fares-region");
    if (savedRegion && tabs.some(function (t) { return t.dataset.region === savedRegion; })) {
      start = savedRegion;
    }
  } catch (e) {}
  show(start);
})();
</script>
""")


def render_page(sales: list[Sale], alert_count: int, hours: int, newest: str) -> str:
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

    return PAGE.substitute(
        sale_count=len(sales),
        alert_count=alert_count,
        hours=hours,
        newest=html.escape(fmt_stamp(newest)) if newest else "n/a",
        generated=datetime.now(timezone.utc).strftime("%-d %b, %H:%M UTC"),
        tabs="\n".join(tabs),
        cards="".join(render_card(s) for s in sales),
    )


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path,
                    default=here.parent / "flights" / "data" / "fares.db",
                    help="path to the scraper's fares.db (opened read-only)")
    ap.add_argument("--hours", type=int, default=24,
                    help="how far back to include alerts (default 24)")
    ap.add_argument("--out", type=Path, default=here / "index.html")
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
