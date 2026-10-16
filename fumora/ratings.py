"""Age ratings.

Stored in Metron's vocabulary; translated to ComicInfo's when writing files.
"""

# Metron's values, least to most restrictive.
METRON_RATINGS = ["Everyone", "Teen", "Teen Plus", "Mature", "Adult", "Explicit"]
_ORDER = {name: i for i, name in enumerate(METRON_RATINGS)}

# ComicInfo 2.1 AgeRating values; Komga maps them to 0/13/15/17/18/18.
TO_COMICINFO = {
    "Everyone": "Everyone",
    "Teen": "Teen",
    "Teen Plus": "MA15+",
    "Mature": "Mature 17+",
    "Adult": "Adults Only 18+",
    "Explicit": "X18+",
}


def known(rating: str | None) -> str | None:
    return rating if rating in _ORDER else None


def strictest(ratings) -> str | None:
    """The most restrictive known rating, e.g. a series' rating from its issues."""
    found = [r for r in ratings if r in _ORDER]
    return max(found, key=_ORDER.__getitem__) if found else None


def series_rating(series) -> str | None:
    """Manual override, else calculated from the issues' Metron ratings."""
    return known(series.age_rating_override) or strictest(i.rating for i in series.issues)


def issue_rating(issue) -> str | None:
    """What a file gets: issue override > series override > Metron issue rating >
    the series' calculated rating. Manual choices always beat Metron's."""
    return (
        known(issue.rating_override)
        or known(issue.series.age_rating_override)
        or known(issue.rating)
        or strictest(i.rating for i in issue.series.issues)
    )
