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
