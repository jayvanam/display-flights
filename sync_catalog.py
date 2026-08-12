#!/usr/bin/env python3
"""Regenerate `catalog.py` from the private `flights` repo's config.

This repo is PUBLIC and the dashboard has to render on a schedule without
importing anything from the private scraper, so the destination/region/airport
tables are *vendored* into `catalog.py` rather than imported at runtime. This
script is the one place that reads the private repo, and `tests/test_catalog.py`
re-runs the same derivation to fail loudly when the two drift apart.

Why vendored instead of imported:
  - `flights.config` calls `load_dotenv()` and `flights.analyzer` imports `db`,
    so importing them drags secrets-loading and a SQLite module into a public
    static-site generator for two lookup tables.
  - The dashboard must keep rendering if the scraper's imports break.

Regions come from two sources, in this order:
  1. The scraper's own tier sets (`_CARIBBEAN_TIER`, `_EUROPE_SA_TIER`,
     `_SOUTH_AMERICA_TIER`, `DOMESTIC_DESTINATIONS`). These already encode the
     ceiling + origin policy, so reusing them means a destination cannot be
     filed under a region that contradicts how it is actually scraped.
  2. `_LONGHAUL_REGIONS` below, for the 22 long-haul names that sit in no tier
     (Asia, Middle East, Africa, Oceania). The scraper has no notion of these —
     they only exist for display — so they are listed explicitly, and anything
     missing is reported instead of silently bucketed.

Usage:
    ./sync_catalog.py [--flights-repo ../flights] [--out catalog.py]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Long-haul destinations that belong to no scraper tier. Seeded from the region
# comments in the scraper's INTERNATIONAL_DESTINATIONS block, which is the
# author's own grouping. A name absent from here lands in "other" AND is printed
# as a warning, so adding a destination upstream surfaces here rather than
# quietly appearing under the wrong tab.
_LONGHAUL_REGIONS = {
    # South & East Asia
    "Japan": "asia",
    "Southeast Asia": "asia",
    "India": "asia",
    "Nepal": "asia",
    "Bali": "asia",
    "Maldives": "asia",
    "Sri Lanka": "asia",
    "Angkor Wat": "asia",
    "South Korea": "asia",
    "Hong Kong": "asia",
    "China": "asia",
    "Taiwan": "asia",
    # Middle East
    "Dubai": "mideast",
    "Egypt": "mideast",
    # Africa
    "Morocco": "africa",
    "South Africa": "africa",
    "Kenya Safari": "africa",
    "Tanzania": "africa",
    # Oceania
    "New Zealand": "oceania",
    "Australia": "oceania",
    "Fiji": "oceania",
    "Tahiti": "oceania",
}

# Hawaii lives in DOMESTIC_DESTINATIONS (it is a domestic flight) but reads as its
# own thing on a deals page, and its $400 ceiling makes it a west-coast-origin feed
# in practice — so it gets its own tab instead of diluting Domestic.
_SPLIT_FROM_DOMESTIC = {"Hawaii": "hawaii"}


def derive(flights_repo: Path):
    """Return (destinations, airports, provenance) derived from the flights repo.

    destinations: tuple of (name, region_id, (codes...)) in the scraper's own order.
    airports:     dict of every referenced code -> "City, Country" label.
    """
    repo = flights_repo.resolve()
    if not (repo / "config.py").exists():
        sys.exit(f"error: no config.py in {repo} — pass --flights-repo")

    sys.path.insert(0, str(repo))
    try:
        import airports as flights_airports
        import config
    except Exception as exc:  # pragma: no cover - environment problem, not logic
        sys.exit(
            f"error: could not import the flights repo at {repo}: {exc}\n"
            "hint: run this with that repo's interpreter, e.g.\n"
            f"      {repo}/.venv/bin/python sync_catalog.py"
        )

    unmapped: list[str] = []
    destinations: list[tuple[str, str, tuple[str, ...]]] = []

    def region_for(name: str, domestic: bool) -> str:
        if domestic:
            return _SPLIT_FROM_DOMESTIC.get(name, "domestic")
        # South America first: every name in it is also in _EUROPE_SA_TIER, which
        # mirrors the order _ceiling_for uses upstream for exactly this reason.
        if name in config._SOUTH_AMERICA_TIER:
            return "southamerica"
        if name in config._EUROPE_SA_TIER:
            return "europe"
        if name in config._CARIBBEAN_TIER:
            return "caribbean"
        # The scraper's SECOND short-haul tier (added 2026-08-12). Read via getattr
        # so this tool still runs against a checkout that predates it — anything
        # that falls through is reported as unmapped rather than mis-filed.
        if name in getattr(config, "_CANADA_TIER", frozenset()):
            return "canada"
        if name in _LONGHAUL_REGIONS:
            return _LONGHAUL_REGIONS[name]
        unmapped.append(name)
        return "other"

    for dest_list, domestic in (
        (config.DOMESTIC_DESTINATIONS, True),
        (config.INTERNATIONAL_DESTINATIONS, False),
    ):
        for dest in dest_list:
            destinations.append(
                (dest.name, region_for(dest.name, domestic), tuple(dest.airports))
            )

    # Every destination code plus every origin code — origins show up on the
    # per-origin chips, so their labels are needed too.
    codes = {c for _, _, cs in destinations for c in cs}
    codes |= set(config.ORIGINS) | set(config.HOME_ORIGINS)
    codes |= set(config.WEST_COAST_ORIGINS)
    airports = {c: flights_airports.label(c) for c in sorted(codes)}

    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        sha = "unknown"

    return destinations, airports, sha, unmapped


def render(destinations, airports, sha) -> str:
    lines = [
        '"""Vendored destination / region / airport tables. GENERATED — do not edit.',
        "",
        f"Regenerate with:  ./sync_catalog.py    (source: flights@{sha})",
        "",
        "Hand edits are overwritten and will fail tests/test_catalog.py. To change how",
        "a destination is filed, edit _LONGHAUL_REGIONS in sync_catalog.py and re-run.",
        '"""',
        "",
        f'GENERATED_FROM = "flights@{sha}"',
        "",
        "# Tab order on the page. Leading four are the high-volume regions.",
        "REGIONS = (",
        '    ("domestic", "Domestic"),',
        '    ("caribbean", "Caribbean & Mexico"),',
        '    ("canada", "Canada"),',
        '    ("europe", "Europe"),',
        '    ("asia", "Asia"),',
        '    ("mideast", "Middle East"),',
        '    ("africa", "Africa"),',
        '    ("southamerica", "South America"),',
        '    ("hawaii", "Hawaii"),',
        '    ("oceania", "Oceania"),',
        '    ("other", "Other"),',
        ")",
        "",
        "# (destination name, region id, airport codes)",
        "DESTINATIONS = (",
    ]
    for name, region, codes in destinations:
        codes_src = ", ".join(f'"{c}"' for c in codes)
        trailing = "," if len(codes) == 1 else ""
        lines.append(f'    ("{name}", "{region}", ({codes_src}{trailing})),')
    lines += [")", "", "AIRPORTS = {"]
    for code, label in airports.items():
        lines.append(f'    "{code}": "{label}",')
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flights-repo", type=Path, default=here.parent / "flights")
    ap.add_argument("--out", type=Path, default=here / "catalog.py")
    args = ap.parse_args()

    destinations, airports, sha, unmapped = derive(args.flights_repo)
    args.out.write_text(render(destinations, airports, sha))

    print(f"wrote {args.out.name}: {len(destinations)} destinations, "
          f"{len(airports)} airports (flights@{sha})")
    if unmapped:
        print("\nWARNING: no region for these destinations — they will show under "
              "'Other'.\nAdd them to _LONGHAUL_REGIONS in sync_catalog.py:")
        for name in unmapped:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
