from datetime import date

from fumora.mylar import read_mylar, snapshot
from tests.fake_mylar import build

LIB = "/data/media/Comics/Private Dance (2023)"


def make_db(tmp_path):
    return build(
        tmp_path / "mylar.db",
        comics=[
            (
                "162288",
                "Private Dance",
                "Private Dance",
                "2023",
                "Space Between",
                LIB,
                "Active",
                "Print",
                "Adult",
            ),
            ("999", "Paused Thing", None, "2001", "X", "/data/media/Comics/Paused", "Paused", "TPB", None),
        ],
        issues=[
            (
                "1001",
                "162288",
                "1",
                None,
                "2023-09-00",
                "2023-09-13",
                "Downloaded",
                "Private Dance 001 (2023).cbz",
            ),
            ("1002", "162288", "2", None, "0000-00-00", None, "Wanted", None),
            ("1003", "162288", "3", None, "2024-08-01", None, "Skipped", None),
        ],
        annuals=[
            (
                "5001",
                "162288",
                "1",
                None,
                "2024-12-01",
                None,
                "Downloaded",
                "Private Dance Annual 001.cbz",
                "777",
                "Private Dance Annual",
            ),
        ],
    )


def test_read_mylar(tmp_path):
    series = {s.cv_id: s for s in read_mylar(make_db(tmp_path))}
    pd = series[162288]
    assert pd.active and pd.mylar_type == "Print"
    assert pd.age_rating == "Adult"
    by_num = {i.number: i for i in pd.issues}
    assert by_num["1"].path == f"{LIB}/Private Dance 001 (2023).cbz"
    assert by_num["1"].status == "Downloaded"
    assert by_num["1"].cover_date == date(2023, 9, 1)
    assert by_num["2"].status == "Wanted" and by_num["2"].cover_date is None
    assert by_num["3"].status == "Skipped"
    assert not series[999].active

    annual = series[777]
    assert annual.is_annual and annual.year == 2024
    assert annual.issues[0].path == f"{LIB}/Private Dance Annual 001.cbz"


def test_snapshot_leaves_source_untouched(tmp_path):
    src = make_db(tmp_path)
    before = src.read_bytes()
    dest = tmp_path / "copy" / "snap.db"
    snapshot(src, dest)
    assert src.read_bytes() == before
    assert len(read_mylar(dest)) == 3
