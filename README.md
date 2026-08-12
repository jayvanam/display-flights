# display-flights

A phone-readable page for the fare alerts produced by the (private) `flights`
scraper. One static file, no server, no inbound network path to the Pi.

**Live:** https://jayvanam.github.io/display-flights/

## Why a static page

The view is a *snapshot*, not a query: 24 hours of alerts is ~150 rows and a few KB
of HTML. So the Pi renders the page after each sweep and pushes it here; GitHub
Pages serves it. That means no port forwarding, no tunnel, no VPN client on the
phone, nothing to keep running — and the page still works when the Pi is off, it
just shows the last snapshot. The 6-hourly sweep is the only thing that changes the
data, so there is nothing to gain from live queries.

Everything is inline. The host CSP blocks external subresources, and a linked
webfont that silently falls back looks worse than none, so the type is a system
stack and there are zero external requests (the only outbound links are the
per-origin Google Flights links you tap).

## One card per sale, not per alert

The scraper keys a route on `(origin, destination-airport)`, and `config.py` expands
every airport code of a destination into its own route. One regional fare sale
therefore fans out into dozens of independent alerts — Morocco is 3 codes × 19
origins = **57 possible routes**, and on 2026-08-12 a single 2026-11-10 departure
produced **16 separate alerts**, 27 across the whole window.

That is right for notification (you want the alert for *your* airport) and
unreadable as a list. Here alerts collapse to one card per
`(destination, outbound date, return date)`, with every origin as a chip carrying
its own price, cheapest first and highlighted. Those 27 Morocco alerts become 5
cards. Tapping any chip opens that origin's itinerary on Google Flights.

Cards are ordered by alert tier, then price, so the strongest deals lead. The region
strip filters; both the region and the light/dark choice persist locally.

## Layout

| Path | Role |
|---|---|
| `generate.py` | The generator. Reads `deal_alerts` read-only, writes `index.html`. |
| `catalog.py` | **Generated.** Vendored destination / region / airport tables. |
| `sync_catalog.py` | Regenerates `catalog.py` from the `flights` repo. |
| `publish.sh` | Pi-side cron entry: pull, generate, commit, push. |
| `dev/preview.py` | Render from the recorded sample, for design work off the Pi. |
| `dev/make_icons.py` | Rasterise the mark to PNG (iOS ignores SVG for home-screen icons). |
| `tests/sample_alerts.txt` | A real 144-alert window from the Pi, 2026-08-12. |

## Why the catalog is vendored rather than imported

`flights.config` calls `load_dotenv()` and `flights.analyzer` imports `db`, so
importing them would drag secrets-loading and SQLite into a public static-site
generator for two lookup tables — and would break this page whenever the scraper's
imports break. So the tables are copied into `catalog.py`, and the tier boundaries
into `generate.TIERS`.

Copies rot, so `tests/test_catalog.py` re-derives both from the real repo and fails
when they drift — including a check that the tier boundaries still match
`analyzer.TIERS`, since a display that labels a deal differently than the alert that
produced it is worse than no label. Those tests **skip** when `flights` is not
checked out (the page still renders fine from the vendored tables) and must **not**
skip on the laptop or the Pi.

After changing destinations upstream:

```sh
../flights/.venv/bin/python sync_catalog.py     # regenerate catalog.py
../flights/.venv/bin/python -m pytest tests/ -q # confirm no drift
```

Regions come from the scraper's own tier sets (`_CARIBBEAN_TIER`,
`_EUROPE_SA_TIER`, `_SOUTH_AMERICA_TIER`, `DOMESTIC_DESTINATIONS`) wherever they
exist, so a destination cannot be filed under a region that contradicts how it is
actually priced and scraped — Colombia shows under Caribbean because that is the
tier it is scraped in. The 22 long-haul names in no tier (Asia, Middle East,
Africa, Oceania) are listed explicitly in `sync_catalog.py`; anything missing is
reported rather than silently bucketed.

## Running it

```sh
# from the laptop, against the recorded sample
../flights/.venv/bin/python dev/preview.py

# against a real database
./generate.py --db ../flights/data/fares.db --hours 24 --out index.html

pytest tests/ -q
```

The database is only ever opened read-only (`mode=ro`). `.gitignore` blocks `*.db`
and `.env` — this repo is public and the scraper's is not.

## On the phone

Open the URL, then Share → Add to Home Screen. It installs standalone with its own
icon via `manifest.webmanifest`.
