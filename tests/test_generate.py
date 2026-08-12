"""The alert -> sale collapse, region filing, and page rendering.

Anchored on a recorded 24h window from the live Pi (tests/sample_alerts.txt) rather
than invented rows, so the assertions describe behaviour that actually happened.
"""

from __future__ import annotations

import re

import generate


# ------------------------------------------------------------------ the collapse

def test_sample_is_the_recorded_window(sample_rows):
    assert len(sample_rows) == 144


def test_morocco_sale_collapses_to_one_card_per_date_pair(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    sales = generate.build_sales(rows)

    morocco = [s for s in sales if s.dest == "Morocco"]
    # 27 individual alerts (13 to Marrakesh, 14 to Casablanca) across five date
    # pairs. One Royal-Air-Maroc-shaped sale, twenty-seven Discord notifications.
    assert sum(len(s.legs) for s in morocco) == 27
    assert len(morocco) == 5

    # The pile that motivated the page: one departure, sixteen origins.
    big = max(morocco, key=lambda s: len(s.legs))
    assert big.outbound == "2026-11-10"
    assert big.return_date == "2026-11-20"
    assert len(big.legs) == 16
    assert big.nights == 10


def test_sale_reports_cheapest_leg_first_and_price_spread(sample_db):
    sales = generate.build_sales(generate.load_alerts(sample_db, hours=24))
    big = max((s for s in sales if s.dest == "Morocco"), key=lambda s: len(s.legs))

    prices = [leg.price for leg in big.legs]
    assert prices == sorted(prices)
    assert big.best_price == 650   # BOS and EWR, both to Casablanca
    assert big.max_price == 795    # SFO to Marrakesh
    assert big.legs[0].origin in {"BOS", "EWR"}

    # Both Moroccan cities appear on the one card, so collapsing does not hide
    # which airport a given origin is actually priced to.
    assert set(big.cities) == {"Casablanca", "Marrakesh"}


def test_total_collapse_is_a_real_reduction(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    sales = generate.build_sales(rows)
    assert len(rows) == 144
    assert len(sales) < 100


# -------------------------------------------------------------------- filing

def test_regions_follow_the_scrapers_own_tiers():
    assert generate.region_of("RAK") == "africa"       # untiered long-haul
    assert generate.region_of("CAI") == "mideast"
    assert generate.region_of("DXB") == "mideast"
    assert generate.region_of("KEF") == "europe"       # _EUROPE_SA_TIER
    assert generate.region_of("ARN") == "europe"
    assert generate.region_of("SJU") == "caribbean"    # _CARIBBEAN_TIER
    assert generate.region_of("CUN") == "caribbean"
    assert generate.region_of("BNA") == "domestic"
    assert generate.region_of("HNL") == "hawaii"       # split out of domestic
    assert generate.region_of("LIH") == "hawaii"
    assert generate.region_of("DPS") == "asia"
    assert generate.region_of("SGN") == "asia"


def test_colombia_files_under_caribbean_like_the_scraper_prices_it():
    # Colombia sits in _CARIBBEAN_TIER upstream (the $350 ceiling + home origins),
    # so it must not drift to South America here just because of geography.
    assert generate.region_of("BOG") == "caribbean"


def test_unknown_destination_code_still_renders():
    # The scraper retired several hub destination rows but kept them as origins, so
    # historical alerts can name a code that is no longer any destination.
    assert generate.region_of("ZZZ") == "other"
    assert generate.dest_name("ZZZ") == "ZZZ"


def test_city_label_drops_the_country():
    assert generate.city("RAK") == "Marrakesh"
    assert generate.city("BNA") == "Nashville"


# --------------------------------------------------------------------- tiers

def test_quality_inverts_to_the_percentile_the_tiers_speak():
    assert generate.percentile_from_quality(100) == 0.0
    assert generate.percentile_from_quality(90) == 0.5
    assert generate.percentile_from_quality(0) == 5.0


def test_tier_boundaries():
    assert generate.tier_for_quality(100)[1] == "Exceptional"
    assert generate.tier_for_quality(90)[1] == "Exceptional"
    assert generate.tier_for_quality(89)[1] == "Excellent"
    assert generate.tier_for_quality(70)[1] == "Excellent"
    assert generate.tier_for_quality(69)[1] == "Great"
    assert generate.tier_for_quality(40)[1] == "Great"
    assert generate.tier_for_quality(39)[1] == "Good"
    # Every deal_alerts row already passed analyzer's gate, and quality clamps to 0
    # both at the bar and far below it, so the floor must stay tiered rather than
    # rendering a real alert as untiered.
    assert generate.tier_for_quality(0)[1] == "Good"


def test_best_tier_leads_the_page(sample_db):
    sales = generate.build_sales(generate.load_alerts(sample_db, hours=24))
    ranks = [generate.tier_rank(s.best_quality) for s in sales]
    assert ranks == sorted(ranks)


# ---------------------------------------------------------------------- render

def test_page_is_self_contained_and_themed(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    sales = generate.build_sales(rows)
    page = generate.render_page(sales, len(rows), 24, rows[0][0])

    # No external SUBRESOURCES: a font CDN or remote stylesheet would be blocked and
    # fall back silently. Anchors to Google Flights are fine — a link is navigation
    # the reader chooses, not something the page fetches to render itself. (An SVG
    # xmlns is likewise a namespace identifier, so a bare "http" test would also
    # false-positive.)
    for fetch in ('<link rel="stylesheet"', "<script src=", "<img src=",
                  "url(http", "@import", "<iframe"):
        assert fetch not in page, f"external subresource: {fetch}"

    # Both dark paths exist: the un-stamped prefers-color-scheme case (guarded so an
    # explicit light choice wins) and the [data-theme] stamp the toggle sets.
    assert ':root:not([data-theme="light"])' in page
    assert ':root[data-theme="dark"]' in page
    # The body ground must come from a token; a transparent body borrows the host's.
    assert "background: var(--paper)" in page


def test_every_card_carries_a_region_and_a_tier(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    page = generate.render_page(generate.build_sales(rows), len(rows), 24, rows[0][0])

    cards = re.findall(r'<article class="card tier-(t[1-4])" data-region="(\w+)"', page)
    assert len(cards) == len(generate.build_sales(rows))
    assert all(region in dict(generate.catalog.REGIONS) for _, region in cards)


def test_tabs_omit_empty_regions(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    page = generate.render_page(generate.build_sales(rows), len(rows), 24, rows[0][0])

    tabbed = set(re.findall(r'class="tab" type="button" role="tab" data-region="(\w+)"', page))
    assert "africa" in tabbed          # Morocco is in the sample
    assert "oceania" not in tabbed     # nothing to Australia/Fiji in this window
    assert "all" in tabbed


def _row(origin, code, out, ret, price, quality, url=None):
    return ("2026-08-12T11:20", origin, code, out, ret, price, quality, url)


def test_every_origin_is_tappable(sample_db):
    """Each origin on a multi-origin card opens that origin's own itinerary."""
    rows = generate.load_alerts(sample_db, hours=24)
    sales = generate.build_sales(rows)
    big = max((s for s in sales if s.dest == "Morocco"), key=lambda s: len(s.legs))
    card = generate.render_card(big)

    links = re.findall(r'<a class="chip[^"]*" href="([^"]+)"', card)
    assert len(links) == 16
    assert len(set(links)) == 16, "each origin must link somewhere distinct"
    for origin in ("BOS", "SFO", "IAD"):
        assert any(f"from+{origin}" in u or f"from%20{origin}" in u for u in links)
    # New tab, and never handing the opener to a third-party page.
    assert card.count('rel="noopener"') == 16


def test_stored_booking_url_wins_over_the_constructed_one():
    stored = "https://www.google.com/travel/flights/search?tfs=REALBLOB&hl=en"
    sales = generate.build_sales([
        _row("ORD", "RAK", "2026-11-10", "2026-11-20", 700, 100, stored),
        _row("BOS", "RAK", "2026-11-10", "2026-11-20", 650, 100, None),
    ])
    sale = sales[0]
    by_origin = {leg.origin: sale.link_for(leg) for leg in sale.legs}

    # The scraper's own link points at the exact multi-airport search the deal came
    # from, so it is preferred whenever it was captured.
    assert by_origin["ORD"] == stored
    # A row that never got one still gets a working link rather than a dead chip.
    assert by_origin["BOS"].startswith("https://www.google.com/travel/flights?q=")
    assert "BOS" in by_origin["BOS"] and "2026-11-10" in by_origin["BOS"]


def test_single_origin_card_has_no_chip_list_but_stays_tappable():
    sales = generate.build_sales([
        _row("ORD", "PHL", "2026-09-03", "2026-09-07", 94, 100),
    ])
    card = generate.render_card(sales[0])
    assert "<ul class=\"chips\">" not in card
    assert 'class="origin-note mono solo"' in card
    assert "href=" in card


def test_fallback_url_is_escaped_for_a_one_way():
    url = generate.search_url("ORD", "RAK", "2026-11-10", None)
    assert "through" not in url
    assert " " not in url


def test_prices_are_escaped_and_tabular(sample_db):
    rows = generate.load_alerts(sample_db, hours=24)
    page = generate.render_page(generate.build_sales(rows), len(rows), 24, rows[0][0])
    assert "$650" in page
    assert "font-variant-numeric: tabular-nums" in page
