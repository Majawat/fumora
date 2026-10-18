import shutil
import struct
import zipfile
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from fumora import convert
from fumora.cbz import ArchiveError
from fumora.grabbing import check_downloads
from fumora.models import File, Grab
from fumora.releases import parse_release
from tests.helpers import FIXTURES

pytestmark = pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("bsdtar") is None, reason="poppler/bsdtar not installed"
)
PDFS = FIXTURES / "pdf"


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i < len(data):
        if data[i + 1] in (0xC0, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    raise ValueError


def _pages(cbz: Path) -> list[bytes]:
    with zipfile.ZipFile(cbz) as zf:
        return [zf.read(n) for n in sorted(zf.namelist()) if n.endswith(".jpg")]


def test_one_image_per_page_is_extracted_losslessly(tmp_path):
    pdf = tmp_path / "Scanned 001 (1990).pdf"
    shutil.copy(PDFS / "scanned.pdf", pdf)
    embedded = PDFS.joinpath("scanned.pdf").read_bytes()
    out = convert.convert_to_cbz(pdf, tmp_path / "recycle")
    pages = _pages(out)
    assert len(pages) == 3
    assert all(_jpeg_size(p) == (209, 320) for p in pages)  # original size, not re-rendered
    assert all(p in embedded for p in pages)  # the very same JPEG bytes
    assert list((tmp_path / "recycle").rglob("*.pdf"))  # original kept in the recycle folder
    assert not pdf.exists()


def test_composed_layout_is_rendered_at_300_dpi(tmp_path):
    pdf = tmp_path / "Composed 001 (2024).pdf"
    shutil.copy(PDFS / "composed.pdf", pdf)
    pages = _pages(convert.convert_to_cbz(pdf, tmp_path / "recycle"))
    assert len(pages) == 3
    w, h = _jpeg_size(pages[0])
    assert (abs(w - 1250) <= 2, abs(h - 1917) <= 2) == (True, True)  # 300 x 460 pt at 300 dpi


def test_broken_pdf_is_refused_untouched(tmp_path):
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"%PDF-1.4\nthis is not really a pdf\n")
    before = pdf.read_bytes()
    with pytest.raises(ArchiveError):
        convert.convert_to_cbz(pdf, tmp_path / "recycle")
    assert pdf.read_bytes() == before and not list(tmp_path.glob(".*fumora*"))


def test_pdf_releases_are_comics_not_ebooks():
    assert not parse_release("Some Comic #1 by Someone [ENG / PDF]").ebook
    assert parse_release("Some Novel by Someone [ENG / EPUB]").ebook
    assert convert.needs_conversion("Issue 001.pdf")


@respx.mock
async def test_downloaded_pdf_is_converted_on_import(app, tmp_path):
    from tests.test_downloads import SAB, _setup
    from tests.test_downloads_review import _grab16, _sab_done

    _setup(app, tmp_path)
    job = tmp_path / "usenet" / "complete" / "fumora" / "Absolute Batman 016 PDF"
    job.mkdir(parents=True)
    shutil.copy(PDFS / "scanned.pdf", job / "Absolute Batman 016 (2026).pdf")
    _sab_done(job)
    await _grab16(app)
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        g = db.scalar(select(Grab))
        assert g.state == "imported", g.message
        f = db.scalar(select(File))
        assert f.path.endswith("Absolute Batman 016 (2026).cbz") and len(_pages(Path(f.path))) == 3
    assert SAB
