# Notes for working on this repo

A single static page (`index.html`) showing fare alerts from the private `flights`
scraper. Cards render client-side from Supabase; the baked-in HTML is the no-JS /
offline fallback. Nothing reaches into the Pi — it pushes out via git, and the browser
reads Supabase directly.

## Three constraints that will bite

**1. `template.html` is a `string.Template`.** A literal `$` must be doubled, and
`${...}` parses as a placeholder. So there is **not one JS template literal** in the
page script, and there must not be — add one and `.substitute` fails the build. It
fails loudly rather than shipping broken markup, which is the only reason this is
survivable.

**2b. There are two KINDS of card.** `kind='deal'` cleared analyzer's percentile gate
against six months of that route's own history. `kind='mistake'` is a region-hunter hit
(`flights/mistake_hunter.py`) that only cleared a hand-set ABSOLUTE floor — no
statistics, no comparison, and error fares are routinely pulled. They share one feed, so
the distinction has to survive in three places at once: `generate.MISTAKE_CSS` /
`MISTAKE_LABEL`, `MISTAKE_CSS` / `TIER_LABELS.mistake` in `template.html`, and
`.tier-mistake` in `theme.css`. `deal_score` and `tier` are **NULL** for them — deriving
a tier would put an unverified fare behind an earned badge, on a public page.

`kind` is part of the grouping key in BOTH renderers. Without that, a hunter hit and a
scored deal for the same destination and dates merge, `best_quality` comes from the
scored leg, and the whole card renders as tiered.

Their `source_id` is **NEGATIVE** (`-mistake_alerts.id`). It is the primary key and
`mistake_alerts.id` starts at 1 exactly like `deal_alerts.id`, so unsigned ids would
collide and each table would overwrite the other's rows on upsert.

**2. Card rendering exists twice.** `generate.render_card` (Python, fallback) and
`cardHtml` in `template.html` (JS, live view). Both are visible in sequence as the
fetch resolves, so any divergence reads as the page glitching. In particular `fmtDate`
is hand-rolled rather than using `toLocaleDateString` — it must match Python's
`%a %-d %b` — and `fmtAgo` mirrors `generate.fmt_ago`'s thresholds exactly. Change one
renderer, change the other.

**3. The Supabase publishable key ships in `index.html`, on a public repo.** That is
intentional and unavoidable for a static page: whatever the page displays is obtainable
by anyone who opens devtools. What is closed off is the COLUMNS, not the time range:
table-level `SELECT` is revoked in favour of a column-level grant on the display columns
(15 as of 2026-08-13 — `kind` needed its own `grant select`, since a new column is
invisible to the page without one), so `deal_score` and `source_id` stay unreachable.

RLS no longer bounds the window. Policy `"anon reads all history"` on `fare_alerts` is
`USING (true)` as of 2026-08-21 — it was `"anon reads last 24h"`, and the History view
needs the whole mirror. It exposed nothing the repo itself did not already expose (see
below). Writes are still refused — anon `DELETE` returns 401, verified, not assumed.

⚠️ A column-revoked request returns `42501 permission denied for table fare_alerts`,
whose hint reads *"GRANT SELECT ON public.fare_alerts TO anon"*. Following that hint
would undo the column-level grant this design is built on. The hint is wrong here.

The bigger exposure was always the repo itself, which is why widening RLS changed little:
every fare price is committed in plaintext, one snapshot per publish, in git history.
Don't "fix" the key by moving it; genuinely private would mean a private repo.

## Don't import from `flights`

`catalog.py` is **generated** — vendored destination / region / airport tables, plus the
tier boundaries in `generate.TIERS`. `flights.config` calls `load_dotenv()` and
`flights.analyzer` imports `db`, so importing them would drag secrets-loading and SQLite
into a public static-site generator, and would break this page whenever the scraper's
imports break.

Copies rot, so `tests/test_catalog.py` re-derives both from the real repo and fails on
drift — including that tier boundaries still match `analyzer.TIERS`. Those tests **skip**
when `flights` isn't checked out, and must not skip on the laptop or the Pi.

After changing destinations upstream:

```sh
../flights/.venv/bin/python sync_catalog.py     # regenerate catalog.py
../flights/.venv/bin/python -m pytest tests/ -q # confirm no drift
```

## Layout

| Path | Role |
|---|---|
| `generate.py` | Builds the FALLBACK page. Reads `deal_alerts` + `mistake_alerts` read-only, writes `index.html`. |
| `template.html` | The shell, the live card renderer (`buildSales`/`cardHtml`/`loadFeed`), **and** the History view (`rowHtml`/`renderHistory`/`fetchHistoryPage`). |
| `sync_supabase.py` | Pi-side: pushes newly alerted deals **and region-hunter hits** into Supabase `fare_alerts`. |
| `catalog.py` | **Generated.** Vendored destination / region / airport tables. |
| `sync_catalog.py` | Regenerates `catalog.py` from the `flights` repo. |
| `publish.sh` | Pi-side cron entry: pull, generate, commit, push. |
| `dev/preview.py` | Render from the recorded sample, for design work off the Pi. |
| `tests/sample_alerts.txt` | A real 144-alert window from the Pi, 2026-08-12. |

## Running it

```sh
# from the laptop, against the recorded sample
../flights/.venv/bin/python dev/preview.py

# against a real database
./generate.py --db ../flights/data/fares.db --hours 24 --out index.html

pytest tests/ -q
```

The database is only ever opened read-only (`mode=ro`). `.gitignore` blocks `*.db` and
`.env` — this repo is public and the scraper's is not.

## Two clocks, on purpose

- `sync_supabase.py` every **5 min** (Pi → Supabase). Cheap, so it runs often. **This
  sets how fresh the page feels**, because the browser fetches Supabase directly and
  bypasses the Pages CDN.
- `publish.sh` every **20 min** (regenerate, commit, push). Makes a git commit, so it
  runs less often; exits without committing when nothing changed. Only bounds how stale
  the no-JS fallback can be (floor: Pages' `max-age=600`).

Underneath both, a route is rescraped every 6h at best, so this design detects sustained
sales, not error fares.

## Design decisions that were made deliberately

- Everything inline (CSS, script, icons) — a choice, not a constraint. One file loads
  instantly on a phone and can't break because a CDN did. System font stack for the same
  reason.
- One card per `(destination, outbound, return)`, not per alert — one regional sale fans
  out into dozens of alerts upstream (Morocco: 3 codes × 19 origins). Origins become
  chips, cheapest first.
- Tier colours keep the scraper's ordering so a card agrees with the Discord alert that
  produced it (Exceptional red, Excellent purple, Great indigo, Good green). Great is
  systemIndigo not systemBlue on purpose — blue is reserved for things you can tap. A
  grayscale palette was built and reverted on 2026-08-12.
- Sorting **reorders** nodes; the region filter only toggles `hidden`. Keeping those
  separate is what lets them compose ("Europe + Latest").
- Region tabs are navigation, not filter buttons — no background on any tab, accent
  underline on the selected one.

## Supabase data caveat

Only alerts sent after **2026-08-12 18:00 UTC** reflect the current deal gate. Two eras
of incomparable rows sit before that, and a date-only `--since` won't exclude the second
(the anchor-rotation flood was the same calendar day as the fix). `deal_alerts` on the Pi
is the system of record. `source_id` is chronological and the high-water mark is
`max(source_id)`, so deleting old rows does not cause a re-pull.

The mirror was backfilled cleanly and holds **zero** pre-gate rows: verified 2026-08-21,
its oldest row is `2026-08-12T18:47:32`, which is exactly the first comparable run. So
every tier badge the History view renders is earned. `GATE_MS` in `template.html` greys
out pre-gate rows anyway — dormant insurance, and the one thing that must not rot if an
older backfill is ever run, because it is what stops the old inflated percentiles from
rendering behind an earned-looking badge.

Do NOT size the archive from `tests/sample_alerts.txt`. That 144-alert day is the Morocco
fan-out worst case; the real rate is ~4/day (43 rows over the 10 days to 2026-08-21), so
`HIST_PAGE = 500` means one fetch and no visible "Load more" for months. The paging is
verified to 1,200 rows regardless.
