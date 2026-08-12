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
own price, so 24 Morocco rows become 5 cards.

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
        dates += f'<span class="sep">·</span>{nights} night{"s" if nights != 1 else ""}'

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
            f'<a class="origin-note mono solo" href="{href}"'
            f' target="_blank" rel="noopener"'
            f' aria-label="Open {html.escape(leg.origin)} to {html.escape(leg.code)}'
            f' on Google Flights">{html.escape(leg.origin)}'
            f'&thinsp;&rarr;&thinsp;{html.escape(leg.code)}</a>'
        )
        chips_block = ""
    else:
        foot_note = f'<span class="origin-note">{n} origins &middot; tap to open</span>'
        chips = []
        for i, leg in enumerate(sale.legs):
            best = " is-best" if i == 0 else ""
            href = html.escape(sale.link_for(leg), quote=True)
            arrow = f"{html.escape(leg.origin)}&thinsp;&rarr;&thinsp;{html.escape(leg.code)}"
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="description" content="$alert_count fare alerts from the last $hours hours, grouped by sale.">
<link rel="manifest" href="manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Fares">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23171612'/><path d='M6 19.5l20-7.5-3.2 8.2-7-1.2-4.3 4.1-.6-4.2z' fill='%23D8A94F'/></svg>">
<style>
/* ------------------------------------------------------------------ tokens
   Light is the full palette on bare :root. Dark redefines ONLY tokens, twice:
   once behind prefers-color-scheme (guarded so an explicit light choice beats a
   dark OS) and once behind the [data-theme] stamp (so the toggle wins both ways).
   No component color is ever declared inside a media or [data-theme] block. */
:root {
  --paper:    #FBF9F5;
  --card:     #FFFFFF;
  --ink:      #1C1B18;
  --ink-2:    #5A5751;
  --ink-3:    #8A857C;
  --rule:     #E4DFD5;
  --rule-2:   #F0EBE1;
  --brass:    #8F6620;
  --brass-dim:#EFE4CE;
  --t1:       #B3382C;
  --t2:       #71449B;
  --t3:       #2A6394;
  --t4:       #3B7248;
  --shadow:   0 1px 2px rgba(28,27,24,.06), 0 6px 16px -10px rgba(28,27,24,.18);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper:    #171612;
    --card:     #1F1E19;
    --ink:      #F2EEE4;
    --ink-2:    #ADA698;
    --ink-3:    #7C7568;
    --rule:     #322E26;
    --rule-2:   #272520;
    --brass:    #D8A94F;
    --brass-dim:#3A3021;
    --t1:       #E8695A;
    --t2:       #A98BD1;
    --t3:       #6BA3D6;
    --t4:       #6FB283;
    --shadow:   0 1px 2px rgba(0,0,0,.4), 0 6px 18px -10px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"] {
  --paper:    #171612;
  --card:     #1F1E19;
  --ink:      #F2EEE4;
  --ink-2:    #ADA698;
  --ink-3:    #7C7568;
  --rule:     #322E26;
  --rule-2:   #272520;
  --brass:    #D8A94F;
  --brass-dim:#3A3021;
  --t1:       #E8695A;
  --t2:       #A98BD1;
  --t3:       #6BA3D6;
  --t4:       #6FB283;
  --shadow:   0 1px 2px rgba(0,0,0,.4), 0 6px 18px -10px rgba(0,0,0,.6);
}

/* Type. No @font-face and no font CDN link: the host CSP blocks external font
   requests, so a linked webfont would silently fall back to a default face. These
   stacks resolve to real faces on the target devices (Iowan Old Style ships on
   macOS/iOS, Palatino elsewhere) and degrade to Georgia. */
:root {
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --sans:  system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono:  ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.45;
  -webkit-text-size-adjust: 100%;
}

.wrap { max-width: 940px; margin: 0 auto; padding: 0 16px 64px; }

/* ------------------------------------------------------------------ header */
.top {
  position: sticky; top: 0; z-index: 10;
  background: var(--paper);
  border-bottom: 1px solid var(--rule);
}
.top-inner {
  max-width: 940px; margin: 0 auto; padding: 14px 16px 0;
  display: flex; flex-direction: column; gap: 12px;
}
.masthead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.masthead h1 {
  font-family: var(--serif);
  font-size: 1.5rem; font-weight: 600; letter-spacing: -.01em;
  margin: 0; text-wrap: balance;
}
.meta {
  font-family: var(--mono); font-size: .72rem; color: var(--ink-3);
  font-variant-numeric: tabular-nums; letter-spacing: .02em;
  margin: 0; margin-inline-start: auto;
}
.meta b { color: var(--ink-2); font-weight: 600; }

.themer {
  appearance: none; border: 1px solid var(--rule); background: var(--card);
  color: var(--ink-2); border-radius: 999px; width: 34px; height: 34px;
  font-size: .95rem; cursor: pointer; line-height: 1; flex: none;
}
.themer:hover { border-color: var(--brass); color: var(--brass); }
.themer:focus-visible { outline: 2px solid var(--brass); outline-offset: 2px; }

/* Region strip. Scrolls horizontally on a phone rather than wrapping into a
   stack that pushes the first card off-screen. */
.tabs {
  display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none;
  margin: 0 -16px; padding: 0 16px;
  list-style: none;
}
.tabs::-webkit-scrollbar { display: none; }
.tab {
  appearance: none; background: none; border: 0; cursor: pointer;
  font-family: var(--sans); font-size: .82rem; color: var(--ink-2);
  padding: 9px 12px 11px; white-space: nowrap;
  border-bottom: 2px solid transparent;
  min-height: 44px; /* thumb target */
}
.tab:focus-visible { outline: 2px solid var(--brass); outline-offset: -2px; }
.tab .n {
  font-family: var(--mono); font-size: .72rem; color: var(--ink-3);
  font-variant-numeric: tabular-nums; margin-inline-start: 5px;
}
.tab[aria-selected="true"] {
  color: var(--ink); border-bottom-color: var(--brass); font-weight: 600;
}
.tab[aria-selected="true"] .n { color: var(--brass); }

/* ------------------------------------------------------------------- cards */
.list { display: flex; flex-direction: column; gap: 10px; padding-top: 18px; }

.card {
  background: var(--card);
  border: 1px solid var(--rule);
  border-inline-start: 3px solid var(--tier);
  border-radius: 4px;
  padding: 13px 15px 12px;
  box-shadow: var(--shadow);
}
.tier-t1 { --tier: var(--t1); }
.tier-t2 { --tier: var(--t2); }
.tier-t3 { --tier: var(--t3); }
.tier-t4 { --tier: var(--t4); }
.card[hidden] { display: none; }

.card-head { display: flex; align-items: flex-start; gap: 12px; }
.card-id { min-width: 0; flex: 1; }
.card-id h2 {
  font-family: var(--serif); font-size: 1.22rem; font-weight: 600;
  margin: 0; letter-spacing: -.01em; line-height: 1.2;
}
.cities {
  margin: 2px 0 0; font-size: .8rem; color: var(--ink-2);
  overflow-wrap: anywhere;
}
.codes {
  font-family: var(--mono); font-size: .7rem; color: var(--ink-3);
  letter-spacing: .04em; margin-inline-start: 4px;
}

.card-price { text-align: end; flex: none; }
.price {
  font-family: var(--mono); font-size: 1.35rem; font-weight: 600;
  font-variant-numeric: tabular-nums; letter-spacing: -.02em;
  display: block; line-height: 1.1;
}
.spread {
  font-family: var(--mono); font-size: .68rem; color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}

.dates {
  font-family: var(--mono); font-size: .78rem; color: var(--ink-2);
  font-variant-numeric: tabular-nums;
  margin: 9px 0 0;
}
.dates .sep { color: var(--ink-3); margin: 0 7px; }

.card-foot { display: flex; align-items: center; gap: 9px; margin-top: 9px; }
.badge {
  font-size: .64rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: var(--tier);
  border: 1px solid var(--tier); border-radius: 3px; padding: 2px 6px;
}
.origin-note { font-size: .72rem; color: var(--ink-3); }
.origin-note.mono {
  font-family: var(--mono); font-size: .72rem; letter-spacing: .02em;
  color: var(--ink-2);
}

.chips {
  list-style: none; margin: 10px 0 0; padding: 10px 0 0;
  border-top: 1px solid var(--rule-2);
  display: flex; flex-wrap: wrap; gap: 5px;
}
/* Chips are links to that exact origin's itinerary on Google Flights, so they get
   a real tap target (min 32px) and visible pressed/focus states. */
.chips li { display: flex; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 32px;
  font-family: var(--mono); font-size: .72rem;
  font-variant-numeric: tabular-nums;
  border: 1px solid var(--rule); border-radius: 3px;
  padding: 4px 8px; color: var(--ink-2);
  text-decoration: none;
  transition: border-color .12s ease, background-color .12s ease;
}
.chip:hover { border-color: var(--brass); color: var(--ink); }
.chip:active { background: var(--rule-2); }
.chip:focus-visible { outline: 2px solid var(--brass); outline-offset: 1px; }
.chip-price { color: var(--ink); font-weight: 600; }
.chip.is-best {
  border-color: var(--brass); background: var(--brass-dim);
  color: var(--ink);
}
.chip.is-best .chip-price { color: var(--brass); }

.origin-note.solo {
  text-decoration: none; border-bottom: 1px dotted var(--ink-3);
  padding-bottom: 1px;
}
.origin-note.solo:hover { color: var(--brass); border-bottom-color: var(--brass); }
.origin-note.solo:focus-visible { outline: 2px solid var(--brass); outline-offset: 2px; }

@media (prefers-reduced-motion: reduce) {
  .chip { transition: none; }
}

.empty {
  font-family: var(--mono); font-size: .8rem; color: var(--ink-3);
  padding: 40px 0; text-align: center;
}
.empty[hidden] { display: none; }

.foot {
  margin-top: 30px; padding-top: 14px; border-top: 1px solid var(--rule);
  font-family: var(--mono); font-size: .68rem; color: var(--ink-3);
  line-height: 1.7;
}
.foot b { color: var(--ink-2); font-weight: 600; }

@media (min-width: 620px) {
  .masthead h1 { font-size: 1.8rem; }
  .card { padding: 15px 18px 14px; }
  .card-id h2 { font-size: 1.32rem; }
  .price { font-size: 1.5rem; }
}
</style>

<header class="top">
  <div class="top-inner">
    <div class="masthead">
      <h1>Bucket List Fares</h1>
      <p class="meta"><b>$sale_count</b> sales &middot; <b>$alert_count</b> alerts &middot; $hours h</p>
      <button class="themer" type="button" id="themer" aria-label="Switch between light and dark">&#9682;</button>
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
  <p class="empty" hidden id="empty">No alerts in this region.</p>

  <footer class="foot">
    <div>Newest alert <b>$newest</b> &middot; generated <b>$generated</b></div>
    <div>$alert_count alerts collapsed into $sale_count sales. One card per
      destination and date pair; each chip is one origin the scraper alerted on,
      cheapest first. Prices are round trip, as seen at scrape time.</div>
  </footer>
</main>

<script>
(function () {
  "use strict";
  // Theme toggle. Stamps data-theme on <html>, which both dark blocks key on, so
  // an explicit choice beats the OS setting in either direction.
  var root = document.documentElement;
  var btn = document.getElementById("themer");
  try {
    var saved = localStorage.getItem("fares-theme");
    if (saved) { root.setAttribute("data-theme", saved); }
  } catch (e) {}
  function currentTheme() {
    // Read the stamp when present, otherwise ask the OS. Deliberately does NOT
    // sniff a computed color: that would put a copy of a palette hex in the JS,
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

  // Region filter.
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var empty = document.getElementById("empty");

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
    // The strip scrolls horizontally, so a restored region further along it would
    // otherwise leave the page filtered with no visible active tab.
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
