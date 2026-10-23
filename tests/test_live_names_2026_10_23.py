"""Release names from the live workflow test (2026-10-23) that Fumora missed."""

import pytest

from fumora.releases import matches_issue, parse_release


@pytest.mark.parametrize(
    ("title", "names", "number", "years"),
    [
        # A writer in front of a scene name.
        ("Kelly.Thompson-Absolute.Wonder.Woman.017", ["Absolute Wonder Woman"], "17", (2024, 2026)),
        # The year after "Annual" in the release, before it in Metron's name.
        ("Absolute Wonder Woman Annual 2026 001 (2026) (Denys Cowan Variant) (Digital) (MikeNY76-Empire)",
         ["Absolute Wonder Woman 2026 Annual"], "1", (2026,)),
        # A dotted series number: 2099 is the name, 025 the issue.
        ("Spider-Man.2099.025.2017.Digital.Zone-Empire", ["Spider-Man 2099"], "25", (2015, 2017)),
        # GetComics' own tag isn't part of the name.
        ("Big Hero 6 (01 - 05 + Extras) (2008 - 2009) GetComics.INFO.zip", ["Big Hero 6"], "3", (2008,)),
    ],
)  # fmt: skip
def test_found_now(title, names, number, years):
    assert matches_issue(parse_release(title), names, number, years)


def test_still_kept_apart():
    # Another year's annual is another series.
    rel = parse_release("Absolute Wonder Woman Annual 2027 001 (2027) (Digital)")
    assert not matches_issue(rel, ["Absolute Wonder Woman 2026 Annual"], "1", (2026,))
    # Decimal issues keep their dot.
    assert parse_release("Batman.1.5.2016").number == "1.5"
    assert parse_release("Uncanny.X-Men.0.1.2014.Digital").number == "0.1"
    # One word before the dash is part of the name, not a writer.
    assert parse_release("Spider-Man.2099.2015.No.25.2018.HYBRID.COMIC").alt_series is None


# --- From comparing the parser with Mylar's reading of 10,743 of its RSS titles ---------------


@pytest.mark.parametrize(
    ("title", "series", "kind", "number", "alt"),
    [
        ("Miles Morales – Spider-Man by Cody Ziglar Vol. 6 – Webs of Wakanda (TPB) (2025)",
         "Miles Morales - Spider-Man by Cody Ziglar", "volume", "6", "Miles Morales - Spider-Man"),
        ("Batman – Gotham by Gaslight – A League for Justice #1 (2025)",
         "Batman - Gotham by Gaslight - A League for Justice", "issue", "1", None),
        ("Werewolf by Night – In the Blood (TPB) (2009)",
         "Werewolf by Night - In the Blood", "unknown", None, None),
        ("Touched by a Demon #2 (2026)", "Touched by a Demon", "issue", "2", None),
        ("Marvel – DC – Spider-Man – Superman #1 (2026)",
         "Marvel - DC - Spider-Man - Superman", "issue", "1", "DC - Spider-Man - Superman"),
        ("Fairest.18", "Fairest", "issue", "18", None),
        ("UNCANNY.X-MEN.#.540", "UNCANNY X-MEN", "issue", "540", None),
        # A TPB, never issue #1234:
        ("Fantastic Four – 1234 (TPB) (2011)", "Fantastic Four", "volume", "1234", None),
        ("X-Men ’97 Annual (1997)", "X-Men ’97 Annual", "unknown", None, None),  # ’97 is the name
        ("Absolute Batman #19 by Scott Snyder, Nick Dragotta, DC Comics [ENG / CBZ]",
         "Absolute Batman", "issue", "19", None),  # MyAnonamouse: authors dropped
    ],
)  # fmt: skip
def test_names_from_the_mylar_comparison(title, series, kind, number, alt):
    r = parse_release(title)
    assert (r.series, r.kind, r.number, r.alt_series) == (series, kind, number, alt)


def test_a_tpb_tagged_release_never_satisfies_a_single_issue():
    assert not matches_issue(
        parse_release("The Magic Order 3 (TPB) (2023)"), ["The Magic Order"], "3", (2023,)
    )


# --- Found by labelling 3,450 hard titles with a local model (qwen3:14b) ---------------------


def test_accents_fold_to_plain_letters():
    from fumora.releases import series_key

    assert series_key("Le Bateau de Thésée") == series_key("Le Bateau de Thesee")
    assert matches_issue(parse_release("Bludwire 001 (2026) (Digital)"), ["Blüdwire"], "1", (2026,))


def test_issue_number_in_brackets_and_lists_of_volumes():
    r = parse_release("G.I. Joe: A Real American Hero - Saturday Morning Adventures (#01) (2022) (digital)")
    assert (r.kind, r.number) == ("issue", "1")
    r = parse_release("Amazing Spider-Man: Epic Collection Vol. 1,2,5,8,9,15,23,24,25,27 [ENG / CBR]")
    assert (r.series, r.kind, r.number) == ("Amazing Spider-Man: Epic Collection", "list", None)
    assert (
        parse_release("Hellboy in Love  Black Eyes (2026) (digital) Torrent").series
        == "Hellboy in Love Black Eyes"
    )


# --- Gemini review of the parser commits in #34 ------------------------------------------------


def test_a_match_through_the_second_reading_is_held_for_an_ok():
    from datetime import date

    from fumora.models import Issue, Series
    from fumora.search import Profile, evaluate

    def candidate(title, series_name, number, year=2024):
        s = Series(name=series_name, year_began=2024, series_type="Single Issue")
        rel = {"title": title, "size": 40 * 2**20, "protocol": "usenet"}
        return evaluate(rel, Issue(series=s, number=number, cover_date=date(year, 3, 1)), Profile())

    # "Miles Morales" is a character, not a writer: never grabbed by itself for Spider-Man #17.
    c = candidate("Miles.Morales-Spider-Man.017.2024.Digital.Zone-Empire", "Spider-Man", "17")
    assert c.identified and not c.accepted and c.holdable and "Spider-Man" in c.hold
    # The full name still matches the series it really is.
    assert candidate(
        "Miles.Morales-Spider-Man.017.2024.Digital.Zone-Empire", "Miles Morales: Spider-Man", "17"
    ).accepted
    assert candidate(
        "Absolute Wonder Woman 017 (2026) (Digital)", "Absolute Wonder Woman", "17", 2026
    ).accepted


def test_by_in_an_issue_title_is_not_a_writer():
    r = parse_release("Werewolf by Night 001 (2020) (Digital)")
    assert r.alt_series is None and not matches_issue(r, ["Werewolf"], "1", (2020,))


def test_zero_padded_decimal_issues_and_tagged_one_dot_names():
    assert parse_release("Batman.001.5.2016.Digital.Zone-Empire").number == "1.5"
    assert not matches_issue(parse_release("Batman.001.5.2016.Digital.Zone-Empire"), ["Batman"], "1", (2016,))
    r = parse_release("Fairest.18 (2012) (Digital).cbr")
    assert (r.series, r.kind, r.number) == ("Fairest", "issue", "18")


def test_a_list_of_volumes_matches_nothing_but_keeps_its_fields():
    from fumora.releases import matches_single_volume, matches_volume

    r = parse_release("Marvel – Amazing Spider-Man: Epic Collection Vol. 1,2,5,8 2023 [ENG / CBR]")
    assert (r.kind, r.year, r.alt_series) == ("list", 2023, "Amazing Spider-Man: Epic Collection")
    names = ["Amazing Spider-Man: Epic Collection"]
    assert not matches_single_volume(r, names, (2023,)) and not matches_volume(r, names, "2")
