from types import SimpleNamespace as NS

from fumora.ratings import TO_COMICINFO, issue_rating, series_rating, strictest


def test_strictest():
    assert strictest(["Teen", "Unknown", None, "Mature", "Everyone"]) == "Mature"
    assert strictest(["Unknown", None]) is None


def test_issue_rating_precedence():
    s = NS(age_rating_override=None, issues=[])
    a = NS(rating="Teen", rating_override=None, series=s)
    b = NS(rating="Unknown", rating_override=None, series=s)
    s.issues = [a, b]
    assert issue_rating(a) == "Teen"
    assert issue_rating(b) == "Teen"  # falls back to the series' calculated rating
    s.age_rating_override = "Adult"
    assert issue_rating(a) == "Adult"  # manual series rating beats Metron's
    a.rating_override = "Everyone"
    assert issue_rating(a) == "Everyone"  # issue override beats everything
    assert series_rating(s) == "Adult"


def test_comicinfo_values_are_schema_values():
    assert set(TO_COMICINFO.values()) <= {
        "Everyone", "Teen", "MA15+", "Mature 17+", "Adults Only 18+", "X18+",
    }  # fmt: skip
