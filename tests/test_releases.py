"""Release-name parser, on real titles from Prowlarr (NZBgeek, MyAnonamouse, The Pirate Bay)."""

import pytest

from fumora.releases import matches_issue, matches_volume, parse_release

CASES = [
    # title, series, kind, number, range, year, source, group
    ("Absolute Batman 016 [2026] [Digital] [Shan-Empire]", "Absolute Batman", "issue", "16", None, 2026, "digital", "Shan-Empire"),
    ("Absolute.Batman.009.2025.Digital.Shan-Empire", "Absolute Batman", "issue", "9", None, 2025, "digital", "Shan-Empire"),
    ("DC.Comics-Absolute.Batman.No.02.2024.HYBRID.COMIC", "Absolute Batman", "issue", "2", None, 2024, "digital", None),
    ("Absolute Batman #19 by Scott Snyder, Nick Dragotta, DC Comics [ENG / CBZ]", "Absolute Batman", "issue", "19", None, None, "unknown", None),
    ("Absolute Batman 014 [2026] [Webrip] [The Last Kryptonian-DCP]", "Absolute Batman", "issue", "14", None, 2026, "digital", "The Last Kryptonian-DCP"),
    ("Saga 001-003 (2012) (Digital-1920) (Zone-Empire)", "Saga", "range", "1", (1, 3), 2012, "digital", "Zone-Empire"),
    ("Saga TPB 07 by Brian K Vaughan, Fiona Staples [ENG / CBR] [VIP]", "Saga", "volume", "7", None, None, "unknown", None),
    ("Saga 024 (2014) (Digital) (Zone-Empire).cbr", "Saga", "issue", "24", None, 2014, "digital", "Zone-Empire"),
    ("SAGA # 02", "SAGA", "issue", "2", None, None, "unknown", None),
    ("Saga of the Swamp Thing v02 [2009] [digital] [Marika-Empire]", "Saga of the Swamp Thing", "volume", "2", None, 2009, "digital", "Marika-Empire"),
    ("LOCKE AND KEY - OMEGA 03 (of 07) (2013) (Digital) (Nahga-Empire)", "LOCKE AND KEY - OMEGA", "issue", "3", None, 2013, "digital", "Nahga-Empire"),
    ("Locke &amp; Key - Omega 03 (2 cvrs c2c+noAds) (theProletariat-No", "Locke & Key - Omega", "issue", "3", None, None, "scan", None),
    ("Uncle Scrooge 005 [Legacy 465]-First Aiders of Dawson [2026] [2 covers] [digital] [Salem-Empire]", "Uncle Scrooge", "issue", "5", None, 2026, "digital", "Salem-Empire"),
    ("Uncle Scrooge (001-016) (2015-2016) (Ongoing) by IDW Publishing [ENG / CBR CBZ]", "Uncle Scrooge", "range", "1", (1, 16), 2015, "unknown", None),
    ("Disney.Uncle.Scrooge.No.10.2023.HYBRiD.COMiC.eBook-LiBRiCiDE", "Disney Uncle Scrooge", "issue", "10", None, 2023, "digital", "LiBRiCiDE"),
    ("Black.Hole.[2000-12].008.Fantagraphics", "Black Hole", "issue", "8", None, 2000, "unknown", None),
    ("Private Dance 006 [2025] [ADULT] [Digital] [ASO] [Cheeky]", "Private Dance", "issue", "6", None, 2025, "digital", None),
    ("Soul Saga (01 - 06) (2000-2003) (Digital) (Ookla-Empire)", "Soul Saga", "range", "1", (1, 6), 2000, "digital", "Ookla-Empire"),
    ("Marvel-Giant-Size.Dark.Phoenix.Saga.2025.No.01.2025.HYBRID.COMIC", "Giant-Size Dark Phoenix Saga", "issue", "1", None, 2025, "digital", None),
    ("Absolute Batman 1.5 (2025) (Digital)", "Absolute Batman", "issue", "1.5", None, 2025, "digital", None),
    ("Batman v3 016 (2016) (Digital) (Zone-Empire)", "Batman", "issue", "16", None, 2016, "digital", "Zone-Empire"),
    ("Spider-Man 2099 001 (2019) (Digital)", "Spider-Man 2099", "issue", "1", None, 2019, "digital", None),
    ("X-Men 92 001 (2016) (Digital)", "X-Men 92", "issue", "1", None, 2016, "digital", None),
    ("Saga Deluxe Edition 2012-2018", "Saga Deluxe Edition", "unknown", None, None, 2012, "unknown", None),
    ("Absolute Batman 01 (2024) (Webrip) (The Last Kryptonian-DCP) (HD-Upscaled)", "Absolute Batman", "issue", "1", None, 2024, "upscaled", "The Last Kryptonian-DCP"),
]  # fmt: skip


@pytest.mark.parametrize("title,series,kind,number,rng,year,source,group", CASES)
def test_parse_real_titles(title, series, kind, number, rng, year, source, group):
    r = parse_release(title)
    assert (r.series, r.kind, r.number, r.year, r.source, r.group) == (
        series,
        kind,
        number,
        year,
        source,
        group,
    )
    if rng:
        assert (r.range_start, r.range_end) == rng


def test_ebooks_and_other_languages_are_flagged():
    assert parse_release("Saga Vol. 3, Kindle Edition by Brian K Vaughan, Fiona Staples [ENG / AZW3]").ebook
    assert parse_release("Thorgal.Saga.T05.La.cite.mouvante.2025.FRENCH.HYBRiD.COMiC.CBZ").language == "fr"
    assert not parse_release("Saga #68 by Brian K Vaughan, Fiona Staples, Image Comics [ENG / CBR]").ebook


def test_variant_markers_do_not_make_another_issue():
    r = parse_release("Saga 7 (2012) (2 Covers)(1440px &amp; 2048px-HD)(Tarutaru-Novus)")
    assert r.variant and r.kind == "issue" and r.number == "7"


def test_matching_issue_number_series_and_year():
    names = ["Absolute Batman"]
    years = (2024, 2025)
    assert matches_issue(parse_release("Absolute.Batman.009.2025.Digital.Shan-Empire"), names, "9", years)
    assert matches_issue(parse_release("Absolute Batman #9 by Scott Snyder [ENG / CBZ]"), names, "009", years)
    assert not matches_issue(parse_release("Absolute Batman 010 (2025) (Digital)"), names, "9", years)
    assert not matches_issue(
        parse_release("Absolute Batman 009 (1999) (Digital)"), names, "9", years
    )  # other decade
    assert not matches_issue(parse_release("Absolute Batman Vol. 1 (2025)"), names, "1", years)  # a TPB
    assert matches_issue(
        parse_release("Saga 001-003 (2012) (Digital-1920) (Zone-Empire)"), ["Saga"], "2", (2012,)
    )
    assert not matches_issue(parse_release("Saga of the Swamp Thing 002 (1982)"), ["Saga"], "2", (2012,))
    # Language is the profile's decision (search.evaluate), not matching's.
    assert matches_issue(parse_release("Thorgal Saga 005 (2025) FRENCH"), ["Thorgal Saga"], "5", (2025,))


def test_matching_volumes_only_for_collected_editions():
    rel = parse_release("Saga TPB 07 by Brian K Vaughan, Fiona Staples [ENG / CBR] [VIP]")
    assert matches_volume(rel, ["Saga TPB"], "7")
    assert not matches_volume(rel, ["Saga TPB"], "8")
    assert not matches_volume(parse_release("Saga 007 (2012)"), ["Saga TPB"], "7")
