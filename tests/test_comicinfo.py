import xml.etree.ElementTree as ET
from datetime import date

import pytest

from fumora.comicinfo import IssueMeta, build_metroninfo, comicinfo_fields, merge_comicinfo
from fumora.models import Issue, Publisher, Series, Tag
from fumora.tagging import issue_meta
from tests.helpers import metron_issue, validate

EXISTING = """<?xml version="1.0"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Series>Old Name</Series>
  <Number>1</Number>
  <Volume>4</Volume>
  <Notes>Tagged with ComicTagger 1.6 [CVDB123]</Notes>
  <Tags>Explicit, Favorite</Tags>
  <ScanInformation>Digital-Empire</ScanInformation>
  <Pages><Page Image="0" Type="FrontCover" ImageWidth="1988" ImageHeight="3056" /></Pages>
  <SomethingCustom>keep me</SomethingCustom>
</ComicInfo>"""


def meta_from_fixture(app, metron_id: int, **series_kw):
    d = metron_issue(metron_id)
    with app.state.session_factory() as db:
        pub = Publisher(metron_id=d["publisher"]["id"], name=d["publisher"]["name"])
        s = Series(
            name=d["series"]["name"],
            year_began=d["series"]["year_began"],
            volume=d["series"]["volume"],
            series_type=d["series"]["series_type"]["name"],
            metron_id=d["series"]["id"],
            publisher=pub,
            genres=[g["name"] for g in d["series"]["genres"]],
            locked_fields=[],
            **series_kw,
        )
        # As ensure_detail would after fetching Metron's record.
        i = Issue(
            series=s,
            number=d["number"],
            metron_id=d["id"],
            metron_detail=d,
            isbn=d["isbn"] or None,
            upc=d["upc"] or None,
            page_count=d["page"] or None,
            rating=d["rating"]["name"],
            locked_fields=[],
        )
        db.add_all([pub, s, i])
        db.flush()
        return issue_meta(i)


def test_watchmen_fields_and_credits(app):
    meta = meta_from_fixture(app, 4743)
    f = comicinfo_fields(meta)
    assert f["Series"] == "Watchmen" and f["Number"] == "1"
    assert f["Volume"] == "1986"  # start year, which Kavita expects
    assert f["Title"] == "At Midnight, All The Agents..."
    assert "Alan Moore" in f["Writer"]
    # Dave Gibbons is Illustrator -> Penciller and Inker; also Letterer and Cover.
    assert "Dave Gibbons" in f["Penciller"] and "Dave Gibbons" in f["Inker"]
    assert "Dave Gibbons" in f["Letterer"] and "Dave Gibbons" in f["CoverArtist"]
    assert f["Format"] == "Limited Series"
    assert f["Web"].startswith("https://metron.cloud/issue/")
    assert "[Metron:4743]" in f["Notes"] and "[CVDB" in f["Notes"]


@pytest.mark.parametrize("metron_id", [186450, 4743, 156154])
def test_outputs_validate_against_schemas(app, metron_id):
    meta = meta_from_fixture(app, metron_id)
    ci, _ = merge_comicinfo(EXISTING.encode(), meta)
    # The schema doesn't know <SomethingCustom>; validate a copy without it.
    root = ET.fromstring(ci)
    root.remove(root.find("SomethingCustom"))
    validate(ET.tostring(root), "ComicInfo-v2.1.xsd")
    validate(build_metroninfo(meta), "MetronInfo-v1.1.xsd")


def test_merge_keeps_what_fumora_does_not_own(app):
    meta = meta_from_fixture(app, 186450)
    meta.tags = ["Mature-Themes", "Explicit"]
    xml, changes = merge_comicinfo(EXISTING.encode(), meta)
    root = ET.fromstring(xml)
    assert root.findtext("Series") == "Private Dance"
    assert root.findtext("Volume") == "2023"
    assert root.findtext("ScanInformation") == "Digital-Empire"
    assert root.find("Pages/Page").get("ImageWidth") == "1988"
    assert root.findtext("SomethingCustom") == "keep me"
    assert root.findtext("Tags") == "Explicit, Favorite, Mature-Themes"
    assert changes["Series"] == ("Old Name", "Private Dance")
    assert "ScanInformation" not in changes


def test_merge_into_file_without_comicinfo(app):
    meta = meta_from_fixture(app, 186450)
    xml, changes = merge_comicinfo(None, meta)
    assert ET.fromstring(xml).findtext("Number") == "1"
    assert changes["Series"] == (None, "Private Dance")


def test_tpb_metroninfo_lists_reprints(app):
    meta = meta_from_fixture(app, 156154)
    root = ET.fromstring(build_metroninfo(meta))
    assert root.findtext("Series/Format") == "Trade Paperback"
    assert "Batman (2016) #112" in [r.text for r in root.findall("Reprints/Reprint")]
    assert root.findtext("GTIN/ISBN") == "9781779520036"
    assert comicinfo_fields(meta)["Format"] == "TPB"


def test_single_issue_metroninfo_has_no_reprints(app):
    root = ET.fromstring(build_metroninfo(meta_from_fixture(app, 4743)))
    assert root.find("Reprints") is None


def test_metroninfo_keeps_existing_tags():
    meta = IssueMeta(series_name="S", number="1", tags=["New"])
    old = b"<MetronInfo><Series><Name>S</Name></Series><Tags><Tag>Old</Tag></Tags></MetronInfo>"
    root = ET.fromstring(build_metroninfo(meta, old))
    assert [t.text for t in root.findall("Tags/Tag")] == ["Old", "New"]


def test_age_rating_and_tags_flow_from_series(app):
    with app.state.session_factory() as db:
        s = Series(name="S", year_began=2000, age_rating_override="Teen Plus", locked_fields=[])
        s.tags = [Tag(name="Explicit")]
        i = Issue(series=s, number="1", rating="Everyone", cover_date=date(2000, 5, 1), locked_fields=[])
        db.add_all([s, i])
        db.flush()
        f = comicinfo_fields(issue_meta(i))
        # The manual series rating beats Metron's issue rating.
        assert f["AgeRating"] == "MA15+"
        assert issue_meta(i).tags == ["Explicit"]
        assert (f["Year"], f["Month"]) == ("2000", "5") and "Day" not in f


def test_number_formatting_is_not_a_change(app):
    meta = meta_from_fixture(app, 186450)
    first, _ = merge_comicinfo(None, meta)
    padded = first.replace(b"<Month>9</Month>", b"<Month>09</Month>")
    assert b"<Month>09</Month>" in padded
    assert "Month" not in merge_comicinfo(padded, meta)[1]


def test_xsi_attributes_in_existing_file_stay_well_formed(app):
    meta = meta_from_fixture(app, 186450)
    existing = EXISTING.replace("<Number>1</Number>", '<Number>1</Number><Count xsi:nil="true"/>')
    xml, _ = merge_comicinfo(existing.encode(), meta)
    root = ET.fromstring(xml)  # duplicate xmlns:xsi would fail here
    assert root.find("Count").get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
    assert xml.count(b"xmlns:xsi") == 1


def test_metroninfo_keeps_elements_fumora_does_not_produce(app):
    meta = meta_from_fixture(app, 186450)
    old = (
        b"<MetronInfo><Series><Name>Old</Name></Series><MangaVolume>3</MangaVolume>"
        b"<Locations><Location>Gotham</Location></Locations><Tags><Tag>Old</Tag></Tags></MetronInfo>"
    )
    xml = build_metroninfo(meta, old)
    root = ET.fromstring(xml)
    assert root.findtext("Series/Name") == "Private Dance"
    assert root.findtext("MangaVolume") == "3"
    assert root.findtext("Locations/Location") == "Gotham"
    tags = [c.tag for c in root]
    assert tags.index("MangaVolume") < tags.index("Number") < tags.index("Locations")
    validate(xml, "MetronInfo-v1.1.xsd")


def test_locked_isbn_is_not_replaced_by_metron(app):
    d = metron_issue(156154)
    with app.state.session_factory() as db:
        s = Series(name="T", locked_fields=[])
        i = Issue(series=s, number="1", metron_id=d["id"], metron_detail=d, isbn="1234567890",
                  locked_fields=["isbn"])  # fmt: skip
        db.add_all([s, i])
        db.flush()
        assert issue_meta(i).isbn == "1234567890"
