"""#16: issue numbers vs volume numbers, packs of volumes, year volumes, variants (real titles)."""

import pytest

from fumora.grabbing import covered_issues
from fumora.models import Issue, Series
from fumora.releases import matches_issue, matches_volume, parse_release
from fumora.search import Profile, evaluate

PACKS = [
    # title, series, first, last
    ("Saga Vol.1 - Vol.9 by Brian K Vaughan, Fiona Staples, Image Comics [ENG / CBR] [VIP]", "Saga", 1, 9),
    ("The Walking Dead TPBs v.01-29 by Robert Kirkman [ENG / CBR]", "The Walking Dead", 1, 29),
    ("The Walking Dead v01-v32 by Robert Kirkman, Charlie Adlard [ENG / CBR CBZ]", "The Walking Dead", 1, 32),
    ("Mieruko-chan v01-11 (2020-2025) (Digital) (1r0n)", "Mieruko-chan", 1, 11),
    ("All New X-Men TPBs vol.1-7 by Brian Michael Bendis [ENG / CBR]", "All New X-Men", 1, 7),
    ("The Saga of Tanya the Evil Vol. 1-10 by Carlo Zen [ENG / CBZ]", "The Saga of Tanya the Evil", 1, 10),
    ("Saga, Book 1-2 (2014-2017) (Image Comics) by Brian K Vaughan [ENG / CBR]", "Saga", 1, 2),
    ("B.P.R.D. Hell on Earth Book 01-05 (2017-2019) [ENG / CBR]", "B.P.R.D. Hell on Earth", 1, 5),
    ("X-Men Gold Vol.0 - Vol.7 (2017-2019) (Marvel Comics) by Chris Claremont", "X-Men Gold", 0, 7),
    ("Batman Vol 1 through 5 (2020)", "Batman", 1, 5),
    ("Saga TPB 1-4", "Saga", 1, 4),
]  # fmt: skip


@pytest.mark.parametrize("title,series,first,last", PACKS)
def test_a_pack_of_volumes_is_not_an_issue(title, series, first, last):
    r = parse_release(title)
    got = (r.series, r.kind, r.range_start, r.range_end, r.number)
    assert got == (series, "volumes", first, last, str(first))


def test_issues_of_a_volume_stay_issues():
    r = parse_release("Batman v2 01-12 (2012)")  # issues 1-12 of volume 2, not volumes 1-12
    assert (r.series, r.kind, r.range_start, r.range_end, r.volume) == ("Batman", "range", 1, 12, "2")
    r = parse_release("Batman v3 016 (2017)")
    assert (r.series, r.kind, r.number, r.volume) == ("Batman", "issue", "16", "3")
    r = parse_release("Ultimate Spider-Man Vol. 1 (1-133 + Extras) by Brian Michael Bendis [ENG / CBR]")
    got = (r.series, r.kind, r.range_start, r.range_end, r.volume)
    assert got == ("Ultimate Spider-Man", "range", 1, 133, "1")
    r = parse_release("The Walking Dead (01-138+extras) by Robert Kirkman [ENG / CBR CBZ]")
    assert (r.kind, r.range_start, r.range_end) == ("range", 1, 138)


def test_year_volumes():
    r = parse_release("Batman v2016 001 (2016) (Digital) (Zone-Empire)")
    assert (r.series, r.kind, r.number, r.volume, r.year) == ("Batman", "issue", "1", "2016", 2016)
    r = parse_release("G.I. Joe - A Real American Hero (v2010) (IDW) by Larry Hama [ENG / CBR]")
    assert (r.series, r.volume, r.year) == ("G.I. Joe - A Real American Hero", "2010", 2010)
    # The year volume guards against the same name from another decade.
    assert not matches_issue(parse_release("Batman (v1940) 001"), ["Batman"], "1", (2016,))


@pytest.mark.parametrize(
    "title",
    [
        "Batman 001 Cover B (2016)",
        "Batman 001 Jim Lee Variant (2016) (digital)",
        "Batman 001 1:25 (2016)",
        "Batman 001 (2016) (Virgin Variant)",
    ],
)
def test_variant_covers_are_the_same_issue(title):
    r = parse_release(title)
    assert (r.series, r.kind, r.number, r.variant) == ("Batman", "issue", "1", True)
    assert matches_issue(r, ["Batman"], "1", (2016,))


def test_half_issue():
    r = parse_release("Batman 1/2 (2016)")
    assert (r.series, r.kind, r.number) == ("Batman", "issue", "0.5")


def test_volume_pack_matches_the_volumes_it_holds():
    rel = parse_release("Saga Vol.1 - Vol.9 by Brian K Vaughan [ENG / CBR]")
    assert matches_volume(rel, ["Saga TPB"], "3")
    assert matches_volume(rel, ["Saga"], "9")
    assert not matches_volume(rel, ["Saga"], "10")
    assert not matches_issue(rel, ["Saga"], "3", (2012,))  # a TPB pack never satisfies a single issue
    tpbs = parse_release("The Walking Dead TPBs v.01-29 by Robert Kirkman [ENG / CBR]")
    assert matches_volume(tpbs, ["The Walking Dead"], "12")


def _series(series_type, numbers, **kw):
    s = Series(name="Saga", year_began=2012, series_type=series_type, **kw)
    for i, n in enumerate(numbers, start=1):
        Issue(id=i, series=s, number=n)
    return s


PACK = {"title": "Saga Vol.1 - Vol.9 (Digital) [ENG / CBR]", "size": 900 * 2**20, "protocol": "usenet"}


def test_collected_series_takes_a_volume_pack_and_it_covers_those_volumes():
    s = _series("Trade Paperback", [str(n) for n in range(1, 12)])
    c = evaluate(PACK, s.issues[2], Profile())
    assert c.accepted, c.rejections
    assert covered_issues(c, s.issues[2]) == list(range(1, 10))  # volumes 1-9, not 10 and 11


def test_single_issue_series_rejects_a_volume_pack():
    s = _series("Single Issue", ["1", "2", "3"])
    c = evaluate(PACK, s.issues[2], Profile())
    assert not c.accepted and "a collected volume, not this issue" in c.rejections


def test_manga_series_whose_issues_are_volumes_takes_a_pack():
    s = _series("Single Issue", [str(n) for n in range(1, 12)], numbering="volumes")
    c = evaluate(PACK, s.issues[0], Profile())
    assert c.accepted, c.rejections


def test_packs_disabled_rejects_volume_packs_too():
    s = _series("Trade Paperback", ["1", "2"])
    c = evaluate(PACK, s.issues[0], Profile(allow_packs=False))
    assert "packs disabled" in c.rejections
