"""Monitoring, Sonarr-style.

`Issue.monitored` is the only stored choice. Everything else is derived:

- Downloaded:  the issue has a file
- Unreleased:  no file, release date in the future (or unknown)
- Wanted:      monitored, released, no file
- Missing:     not monitored, released, no file

(`Issue.status` is Mylar's old status, kept for reference only.)
"""

from datetime import date

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import Session

from .models import Issue, Series
from .numbers import sort_number

OPTIONS = {
    "all": "All issues",
    "future": "Future issues only",
    "missing": "Missing + future",
    "existing": "Ones I have + future",
    "gaps": "Fill gaps + future",
    "none": "None",
}


def release_date(issue: Issue) -> date | None:
    return issue.store_date or issue.cover_date


def released(issue: Issue, today: date | None = None) -> bool:
    d = release_date(issue)
    return d is not None and d <= (today or date.today())


def state(issue: Issue, today: date | None = None) -> str:
    if issue.files:
        return "Downloaded"
    if not released(issue, today):
        return "Unreleased"
    return "Wanted" if issue.monitored and issue.series.monitored else "Missing"


def wanted_clause(today: date | None = None):
    """SQL condition for Wanted issues (join Series first)."""
    today = today or date.today()
    return and_(
        Issue.monitored.is_(True),
        Series.monitored.is_(True),
        not_(Issue.files.any()),
        func.coalesce(Issue.store_date, Issue.cover_date) <= today,
    )


def missing_clause(today: date | None = None):
    """SQL condition for Missing issues: released, no file, not monitored (join Series first)."""
    today = today or date.today()
    return and_(
        or_(Issue.monitored.is_(False), Series.monitored.is_(False)),
        not_(Issue.files.any()),
        func.coalesce(Issue.store_date, Issue.cover_date) <= today,
    )


def released_before_clause(when: date):
    return func.coalesce(Issue.store_date, Issue.cover_date) < when


def apply_option(series: Series, option: str, today: date | None = None) -> None:
    """Set which issues are monitored, like Sonarr's "Monitor" choice."""
    if option not in OPTIONS:
        raise ValueError(option)
    today = today or date.today()
    owned = [sort_number(i.number) for i in series.issues if i.files and sort_number(i.number) is not None]
    low, high = (min(owned), max(owned)) if owned else (None, None)
    series.monitored = option != "none"
    for issue in series.issues:
        future = not released(issue, today)
        has_file = bool(issue.files)
        if option == "all":
            on = True
        elif option == "future":
            on = future
        elif option == "missing":
            on = future or not has_file
        elif option == "existing":
            on = future or has_file
        elif option == "gaps":
            n = sort_number(issue.number)
            between = low is not None and n is not None and low <= n <= high
            on = future or has_file or between
        else:
            on = False
        issue.monitored = on


def new_issue_monitored(series: Series, issue_release: date | None, added_on: date | None) -> bool:
    """Monitoring for an issue a Metron sync just added to `series`.

    Issues released before the series was added are old gaps, not new issues:
    they start unmonitored (pick them up with 'Fill gaps' or 'Missing').
    """
    if not (series.monitored and series.monitor_new):
        return False
    if issue_release is not None and added_on is not None and issue_release < added_on:
        return False
    return True


def counts(db: Session, series_id: int | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    q = db.query(Issue).join(Series)
    if series_id is not None:
        q = q.filter(Series.id == series_id)
    wanted = q.filter(wanted_clause(today)).count()
    missing = q.filter(missing_clause(today)).count()
    return {"wanted": wanted, "missing": missing}
