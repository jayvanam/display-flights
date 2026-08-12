"""Drift guard: the vendored catalog + tiers must still match the private scraper.

`catalog.py` and `generate.TIERS` are copies, taken so a public static-site
generator does not import a private repo (which loads dotenv and pulls in SQLite)
at render time. Copies rot. These tests are where that rot is caught.

They SKIP when the flights repo is not importable — on a machine that only has this
repo there is nothing to compare against, and the page still renders correctly from
the vendored tables. They must NOT skip on the laptop or the Pi, where both repos
are checked out; that is the whole point.

Set FLIGHTS_REPO to point somewhere other than ../flights.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import catalog
import generate
import sync_catalog

ROOT = Path(__file__).resolve().parent.parent
FLIGHTS = Path(os.environ.get("FLIGHTS_REPO", ROOT.parent / "flights"))


def _flights_or_skip():
    if not (FLIGHTS / "config.py").exists():
        pytest.skip(f"flights repo not found at {FLIGHTS}")
    sys.path.insert(0, str(FLIGHTS))
    try:
        import config  # noqa: F401
    except Exception as exc:
        pytest.skip(f"flights repo present but not importable ({exc}); "
                    "run pytest with that repo's interpreter to enable drift checks")
    import analyzer
    import config
    return config, analyzer


def test_catalog_matches_the_scrapers_destinations():
    _flights_or_skip()
    destinations, airports, _sha, unmapped = sync_catalog.derive(FLIGHTS)

    assert not unmapped, (
        "these destinations have no region and would render under 'Other': "
        f"{unmapped}. Add them to _LONGHAUL_REGIONS in sync_catalog.py."
    )
    assert tuple(destinations) == catalog.DESTINATIONS, (
        "catalog.py is stale — re-run ./sync_catalog.py"
    )
    assert airports == catalog.AIRPORTS, (
        "catalog.py airport labels are stale — re-run ./sync_catalog.py"
    )


def test_every_region_id_has_a_tab_label():
    used = {region for _, region, _ in catalog.DESTINATIONS}
    labelled = dict(catalog.REGIONS)
    assert used <= set(labelled), f"no tab label for {used - set(labelled)}"


def test_tier_boundaries_match_the_analyzer():
    config, analyzer = _flights_or_skip()

    # The three named tiers come straight from analyzer.TIERS...
    upstream = [bound for bound, _emoji, _label in analyzer.TIERS]
    assert [bound for bound, _css, _label in generate.TIERS][:len(upstream)] == upstream, (
        "generate.TIERS disagrees with analyzer.TIERS — the page would label deals "
        "with a different tier than the alert that produced them"
    )
    # ...and the bottom rung is the alert gate itself.
    assert generate.TIERS[-1][0] == config.ALERT_PERCENTILE


def test_quality_scale_matches_how_the_scraper_persists_it():
    config, analyzer = _flights_or_skip()
    # analyzer stores quality = 100 - percentile * 20. If that ever changes, every
    # tier and every sort on this page inverts silently.
    assert generate.QUALITY_PER_PERCENTILE == 20.0
    assert generate.percentile_from_quality(100 - config.ALERT_PERCENTILE * 20) == \
        pytest.approx(config.ALERT_PERCENTILE)
