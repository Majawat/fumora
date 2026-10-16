import os
from datetime import date

from fumora import naming
from fumora.models import File, Issue, Publisher, Series
from fumora.scanner import parse_issue_number, scan_folders


def test_parse_issue_number():
    assert parse_issue_number("Private Dance 005 (2025).cbz", "Private Dance") == "005"
    assert (
        parse_issue_number(
            "The Magnificent Ms. Marvel Annual 001 (2019).cbz", "The Magnificent Ms. Marvel Annual"
        )
        == "001"
    )
    assert parse_issue_number("Uncle Scrooge 153 (1978).cbz", "Walt Disney Uncle Scrooge") == "153"
    assert parse_issue_number("X-Men #-1 (1997).cbz", "X-Men") == "-1"
    assert parse_issue_number("Locke & Key 003 (2008).cbz", "Locke and Key") == "003"
    assert parse_issue_number("Saga v01 (2012).cbz", "Saga") is None  # a volume, not an issue


def test_scan_links_untracked_files_and_keeps_annuals_apart(app, tmp_path):
    folder = tmp_path / "Ms Marvel (2019)"
    folder.mkdir()
    for name in [
        "The Magnificent Ms. Marvel 001 (2019).cbz",
        "The Magnificent Ms. Marvel 002 (2019).cbz",
        "The Magnificent Ms. Marvel Annual 001 (2019).cbz",
        "The Magnificent Ms. Marvel 099 (2019).cbz",
        "notes.txt",
    ]:
        (folder / name).write_bytes(b"x")
    with app.state.session_factory() as db:
        main = Series(name="The Magnificent Ms. Marvel", path=str(folder), locked_fields=[])
        annual = Series(name="The Magnificent Ms. Marvel Annual", path=str(folder), locked_fields=[])
        i1 = Issue(series=main, number="1", locked_fields=[])
        i2 = Issue(series=main, number="2", locked_fields=[])
        a1 = Issue(series=annual, number="1", locked_fields=[])
        db.add_all([main, annual, i1, i2, a1])
        db.flush()
        db.add(File(issue_id=i2.id, path=str(folder / "The Magnificent Ms. Marvel 002 (2019).cbz")))
        db.flush()

        r = scan_folders(db)
        assert r.linked == 2
        assert [f.path.rsplit("/", 1)[-1] for f in i1.files] == ["The Magnificent Ms. Marvel 001 (2019).cbz"]
        assert [f.path.rsplit("/", 1)[-1] for f in a1.files] == [
            "The Magnificent Ms. Marvel Annual 001 (2019).cbz"
        ]
        assert [os.path.basename(p) for p in r.unlinked] == ["The Magnificent Ms. Marvel 099 (2019).cbz"]
        assert scan_folders(db).linked == 0  # already known files are skipped


def _series(db, folder, name="Umbrella Academy: Dallas", year=2008):
    s = Series(name=name, year_began=year, path=str(folder), publisher=Publisher(name="Dark Horse"),
               series_type="Limited Series", locked_fields=[])  # fmt: skip
    db.add(s)
    db.flush()
    return s


def test_default_template_reproduces_mylar_names(app, tmp_path):
    with app.state.session_factory() as db:
        s = _series(db, tmp_path / "x", name="Uncle Scrooge", year=1953)
        i = Issue(series=s, number="153", cover_date=date(1978, 5, 1), locked_fields=[])
        db.add(i)
        db.flush()
        assert naming.render(naming.DEFAULT_FOLDER, naming.context(s)) == "Uncle Scrooge (1953)"
        assert naming.render(naming.DEFAULT_FILE, naming.context(s, i)) == "Uncle Scrooge 153 (1978)"


def test_render_tokens_and_colons(app, tmp_path):
    with app.state.session_factory() as db:
        s = _series(db, tmp_path / "x", name="The Umbrella Academy: Dallas")
        i = Issue(series=s, number="1.5", locked_fields=[])
        db.add(i)
        db.flush()
        ctx = naming.context(s, i)
        assert naming.render("{Series Title}", ctx) == "The Umbrella Academy Dallas"
        assert naming.render("{Series Title}", ctx, colon="space_dash") == "The Umbrella Academy - Dallas"
        assert naming.render("{Series TitleThe}", ctx, colon="dash") == "Umbrella Academy- Dallas, The"
        assert naming.render("{Series Title} #{Issue:000}", ctx) == "The Umbrella Academy Dallas #001.5"
        assert naming.render("{Publisher}/{Issue Title}", ctx) == "Dark Horse"  # "/" removed, empty title
        assert naming.render("{Series Title} ({Issue Year})", ctx) == "The Umbrella Academy Dallas (2008)"


def test_plan_and_apply_rename(app, tmp_path):
    folder = tmp_path / "Dallas old"
    folder.mkdir()
    (folder / "dallas1.cbz").write_bytes(b"one")
    (folder / "dallas2.cbz").write_bytes(b"two")
    with app.state.session_factory() as db:
        s = _series(db, folder)
        for n in ("1", "2"):
            i = Issue(series=s, number=n, cover_date=date(2008, n == "1" and 10 or 11, 1), locked_fields=[])
            i.files = [File(path=str(folder / f"dallas{n}.cbz"))]
            db.add(i)
        db.flush()
        inode = os.stat(folder / "dallas1.cbz").st_ino
        plan = naming.plan_series(s)
        assert plan.folder.new.endswith("/Umbrella Academy Dallas (2008)")
        assert [os.path.basename(r.new) for r in plan.files] == [
            "Umbrella Academy Dallas 001 (2008).cbz",
            "Umbrella Academy Dallas 002 (2008).cbz",
        ]
        assert naming.apply_plan(db, s, plan) == []
        new = tmp_path / "Umbrella Academy Dallas (2008)" / "Umbrella Academy Dallas 001 (2008).cbz"
        assert new.read_bytes() == b"one" and os.stat(new).st_ino == inode  # same file, just renamed
        assert s.path == str(tmp_path / "Umbrella Academy Dallas (2008)")
        assert s.issues[0].files[0].path == str(new)
        assert naming.plan_series(s).files == [] and naming.plan_series(s).folder is None


def test_shared_folder_is_not_renamed(app, tmp_path):
    with app.state.session_factory() as db:
        s = _series(db, tmp_path / "Shared")
        plan = naming.plan_series(s, shared_folder=True)
        assert plan.folder is None and plan.skipped
