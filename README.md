# display-flights

A phone-readable page for the fare alerts produced by the (private) `flights`
scraper. One file, no server, no inbound network path to the Pi.

**Live:** https://jayvanam.github.io/display-flights/

## How the page gets its data

**Cards are rendered client-side from Supabase, with the baked-in HTML as a
fallback.** On load, script reads the last 24h of `fare_alerts` and replaces the card
list wholesale. `publish.sh` still bakes cards into `index.html` from the scraper's own
database — that copy is the floor when JS is off, when the phone is offline on a cached
page, or when Supabase is unreachable, and it is what makes the page work at all
without a round trip.

This was a hybrid that only *looked* Supabase-backed until 2026-08-12: every card was
static and a single fetch filled a small "Just in" strip of anything newer than the
build. That strip is **gone** — it existed only because the list was static, and two
live views would show every new alert twice.

Nothing reaches into the Pi. It pushes out (git) and the browser reads Supabase, so
there is no port forwarding, no tunnel, and no VPN client on the phone.

Everything is inline — CSS, script, icons. GitHub Pages does not forbid external
subresources, so this is a choice rather than a constraint: one file loads instantly
on a phone and can't break because a CDN did. The type is a system stack for the same
reason (a webfont that silently falls back is worse than one that never does).

⚠️ Because `template.html` is a `string.Template`, a literal `$` must be doubled and
`${...}` parses as a placeholder — so there is **not one JS template literal** in the
page script, and there must not be. `.substitute` fails the build loudly rather than
shipping broken markup, which is the only reason this is survivable.

### How fresh it is

Two clocks, on purpose:

- `sync_supabase.py` every **5 min** — Pi → Supabase. Cheap (one GET, plus a POST only
  when there is something new), so it runs often. **This is what sets how fresh the
  page feels**, because the browser's fetch goes straight to Supabase and bypasses the
  Pages CDN.
- `publish.sh` every **20 min** — regenerates `index.html`, commits, pushes. Makes a git
  commit, so it runs less often, and exits without committing when nothing changed.

Neither is "once at the end of a run": the runner writes alerts continuously
(`db.record_alert` per route) and a sweep spans more than one 6h slot, so there is no
end-of-run moment to hook.

The *fallback* copy is still bounded by GitHub Pages' `cache-control: max-age=600`, so
10 minutes of CDN caching is the floor on how stale the no-JS view can be. The live
cards are not subject to that. Underneath both, the data is coarser still — a route is
rescraped every 6h at best and revisited every 1–3 days — so this design detects
sustained sales, not error fares.

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

Each card also carries the **airline** of its cheapest leg on the meta line (an em dash
when the parser captured none, matching the Discord embed) and a relative **posting
date** in the foot — "3h ago", with the absolute UTC in a `title`. Per-leg airlines
differ, so the rest ride in each chip's `title`/`aria-label` rather than doubling every
capsule's width.

**Sort** is `Best` (alert tier, then price — the default, so the page opens strongest
first) or `Latest` (newest alert first). Sorting **reorders** nodes while the region
filter only toggles `hidden`; keeping those separate is what lets them compose, so
"Europe + Latest" works without either knowing about the other. Region, sort and the
light/dark choice all persist locally.

A sale with more than six origins shows six and a **Show all** toggle. In the fallback
copy the markup ships expanded and is collapsed by script, so with JS off you get every
option rather than a button that does nothing.

The look is Apple's: SF via `-apple-system` with tabular figures throughout, iOS
grouped-list cards on `systemGroupedBackground`, and the iOS system palette in both
themes. Region tabs are **navigation, not filter buttons** — no background on any tab
including the selected one, which carries an accent underline against the strip's
baseline hairline. Tier colours keep the scraper's own ordering so a card agrees with
the Discord alert that produced it (Exceptional red, Excellent purple, Great indigo,
Good green) — Great is systemIndigo rather than systemBlue on purpose, since blue is
reserved for things you can tap.

⚠️ Card rendering exists **twice**: Python (`generate.render_card`) for the fallback and
JS (`cardHtml` in `template.html`) for the live view. They are visible in sequence as
the fetch resolves, so any divergence reads as the page glitching. In particular
`fmtDate` is hand-rolled rather than using `toLocaleDateString`, which yields
"Wed, Dec 2" where Python's `%a %-d %b` yields "Wed 2 Dec"; and `fmtAgo` mirrors
`generate.fmt_ago`'s thresholds exactly. A grayscale palette was built and reverted on
2026-08-12 — the tier hues above are deliberate.

## Layout

| Path | Role |
|---|---|
| `generate.py` | Builds the FALLBACK page. Reads `deal_alerts` read-only, writes `index.html`. |
| `template.html` | The shell **and** the live client renderer (`buildSales`/`cardHtml`/`loadFeed`). |
| `sync_supabase.py` | Pi-side: pushes newly alerted deals into Supabase `fare_alerts`. |
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

`sync_supabase.py` pushes newly alerted deals into the `fare_alerts` table, which is
what the page **renders from**, so a fare found mid-sweep appears within one sync
interval instead of waiting for a rebuild. Scope is exactly that: **deals that actually
alerted**.

Two columns exist only for the page and are derived on the Pi from the vendored catalog
rather than in the browser: `airline`, and `city_name` — which is **not** a duplicate of
`destination_name`. The latter is the market ("Miami", "Hawaii"); `city_name` is the
airport's city ("Fort Lauderdale", "Maui"), which is what the card subtitle needs. When
they match, the subtitle collapses to the bare code.

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

### What the public key can and cannot reach

| key | where it lives | reach |
|---|---|---|
| `sb_publishable_...` | inlined in `index.html`, public | SELECT on the **last 24h**, **12 display columns only** |
| `sb_secret_...` | `.env` on the Pi, gitignored | Full; bypasses RLS. Never in this repo. |

Since a static page has no server, the credential must ship in the HTML — so **whatever
the page displays is obtainable by anyone who opens devtools, and that cannot be fixed.**
What *can* be closed is everything beyond the current view, and as of 2026-08-12 it is:
an RLS policy limits `anon` to `sent_at >= now() - interval '24 hours'`, and table-level
`SELECT` is revoked in favour of a **column-level** grant. Verified with the real
publishable key — `deal_score`, `source_id`, `select=*`, any older row, and `sync_runs`
all return `42501 permission denied` or empty, while the page's exact 12-column select
works. `service_role` is untouched, so the Pi's sync (which reads `source_id` for its
high-water mark) is unaffected.

Column grants rather than a view on purpose: a definer view achieves the same thing but
trips Supabase's security-definer lint.

Writes were verified rather than assumed: an anon `DELETE` returns `200 []`, which looks
like success. Seeding a real row and re-running it confirmed the row survives untouched —
RLS filters the rows to zero rather than deleting them.

⚠️ **The public repo is a bigger exposure than the key.** `index.html` is committed with
every fare price in plaintext, readable with no credential, and git history keeps a
snapshot per publish. That is the cost of keeping a no-JS fallback; genuinely private
would mean a private repo, not a code change.

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
publish makes a git commit, so it runs less often. The page's fetch goes straight to
Supabase and bypasses the Pages CDN, so **sync** frequency — not publish frequency —
is what sets how fresh the page feels. Publish frequency only bounds how stale the
no-JS fallback can be.

Both were installed on 2026-08-12; before that neither existed and the mirror had only
ever been filled by hand. Two ways to tell a dead sync remotely, no Pi access needed:
`sync_runs` logs **every** run including `rows_added: 0` no-ops, so a gap there means
the cron is not firing; and `publish.sh` commits as `pi-publisher`, so an absence of
commits by that author means it has never run from cron.

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
