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

Everything is inline — CSS, script, icons. GitHub Pages does not forbid external
subresources, so this is a choice rather than a constraint: one file loads instantly
on a phone, works from cache when offline, and can't break because a CDN did. The
type is a system stack for the same reason (a webfont that silently falls back is
worse than one that never does). The only outbound links are the per-origin Google
Flights links you tap.

### How fresh it is

`publish.sh` runs on its **own** cron every 15 minutes, decoupled from the scraper.
It is not "once at the end of a run": the runner writes alerts continuously
(`db.record_alert` per route) and a sweep routinely spans more than one 6h slot, so
there is no single end-of-run moment to hook. The script exits without committing
when nothing changed, so frequent runs are nearly free and produce no commit noise.

Two floors make anything finer pointless: GitHub Pages serves `cache-control:
max-age=600`, so 10 minutes of CDN caching is the limit on perceived freshness; and
the data is coarser still, since a route is rescraped every 6h at best and revisited
every 1–3 days. This design detects sustained sales, not error fares.

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
strip filters; both the region and the light/dark choice persist locally. A sale with
more than six origins shows six and a **Show all** toggle — the markup ships expanded
and is collapsed by script on load, so with JS off you get every option rather than a
button that does nothing.

The look is Apple's: SF via `-apple-system` with tabular figures throughout, iOS
grouped-list cards on `systemGroupedBackground`, capsule filters, and the iOS system
palette in both themes. Tier colours keep the scraper's own ordering so a card agrees
with the Discord alert that produced it (Exceptional red, Excellent purple, Great
indigo, Good green) — Great is systemIndigo rather than systemBlue on purpose, since
blue is reserved for things you can tap.

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

## Supabase: the live deal feed

`sync_supabase.py` pushes newly alerted deals into the `fare_alerts` table, which the
page's **Just in** section reads so a fare found mid-sweep appears without waiting for
the next static rebuild. Scope is exactly that: **deals that actually alerted**.

This is *not* a backup. `fare_history` is never touched — it isn't a target here, and
couldn't be anyway (~8.85M rows / ~2.4 GB against a 500 MB free tier).

⚠️ **Only alerts sent after 2026-08-12 18:00 UTC reflect the current gate.** Two
separate eras of incomparable rows sit before that line, and a date-only `--since` is
not enough to exclude the second:

- **Before 2026-08-11** — scored against the page-pooled baseline that the deal-logic
  overhaul fixed, which inflated every percentile. One day in June fired 1,965 alerts
  with fares up to $2,732.
- **2026-08-12, up to ~18:00 UTC** — the anchor-rotation flood. The rotating anchor
  moved to lead 90 while history was still ~50% lead-31-45, so every route's anchor
  scored as an all-time low on the lead-blind `wide` rung: **142 alerts in the 11:00
  UTC run, 36 of them above the $700 long-haul ceiling** (to $798). Same calendar day
  as the fix, so `--since 2026-08-12` keeps all of it.

A first backfill run without `--since` pulled both eras (5,500 of 5,654 rows); they were
deleted from `fare_alerts` on 2026-08-12. Because `source_id` is chronological and the
high-water mark is `max(source_id)`, deleting old rows does **not** cause a re-pull.
`deal_alerts` on the Pi remains the system of record, so any of this is recoverable by
re-running without `--since`.

Two keys, and the distinction matters:

| key | where it lives | can it write? |
|---|---|---|
| `sb_publishable_...` | inlined in `index.html`, public | **No.** RLS grants `anon` SELECT only |
| `sb_secret_...` | `.env` on the Pi, gitignored | Yes — bypasses RLS |

Verified rather than assumed: an anon `DELETE` returns `200 []`, which looks like
success. Seeding a real row and re-running it confirmed the row survives untouched —
RLS filters the rows to zero rather than deleting them.

Pi setup:

```sh
cat > .env <<'EOF'
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
EOF
chmod 600 .env

./sync_supabase.py --dry-run                    # reads only, prints the first row
./sync_supabase.py --since 2026-08-13           # backfill the comparable era only
./sync_supabase.py --max-batches 4              # cap a large first run
```

Cron — the two run on **separate clocks** on purpose. The sync is cheap (one GET, plus
a POST only when there is something new, no git operation), so it runs often; the
publish makes a git commit, so it runs less often. The "Just in" fetch goes straight
to Supabase and bypasses the Pages CDN, so sync frequency — not publish frequency —
is what sets how fresh the page feels.

```
*/5  * * * *  /home/jay/Documents/display-flights/sync_supabase.py
*/20 * * * *  /home/jay/Documents/display-flights/publish.sh
```

`SUPABASE_SERVICE_KEY` still works as an alias for a legacy `service_role` JWT.
Pasting the publishable key exits immediately with an explanation rather than
401-ing on every batch. Request cost per sync: one GET for the high-water mark, one
POST per 500 new rows, one POST to log the run — so a quiet cycle is two requests.

## On the phone

Open the URL, then Share → Add to Home Screen. It installs standalone with its own
icon via `manifest.webmanifest`.
