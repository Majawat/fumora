"""Builds the small PDF fixtures used by tests/test_convert_pdf.py.

  python tests/fixtures/pdf/make_fixtures.py

- composed.pdf: 3 pages of vector text and shapes (like an InDesign export):
  has to be rendered.
- scanned.pdf: 3 pages, each one full-page JPEG (like a scan or most digital
  PDFs): the images can be extracted as they are.
The JPEGs come from rendering composed.pdf with pdftoppm, so only poppler is needed.
"""

import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent


def write_pdf(path: Path, pages: list[tuple[bytes, dict[str, bytes]]], size=(300, 460)) -> None:
    """pages: [(content stream, {name: image JPEG bytes})]."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    pages_id = len(objs) + 1 + sum(1 + len(imgs) + 1 for _, imgs in pages)  # placeholder, fixed below
    kids = []
    for content, images in pages:
        xobjs = []
        for name, jpeg in images.items():
            w, h = _jpeg_size(jpeg)
            oid = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                % (w, h, len(jpeg))
                + jpeg
                + b"\nendstream"
            )
            xobjs.append(b"/%s %d 0 R" % (name.encode(), oid))
        cid = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        res = b"<< /Font << /F1 %d 0 R >> /XObject << %s >> >>" % (font, b" ".join(xobjs))
        kids.append((cid, res))
    pages_id = len(objs) + len(kids) + 1
    for cid, res in kids:
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R /Resources %s >>"
                % (pages_id, size[0], size[1], cid, res)
            )
        )
    add(
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % p for p in page_ids), len(page_ids))
    )
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    path.write_bytes(bytes(out))


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i < len(data):
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            return w, h
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    raise ValueError("no JPEG size")


def main() -> None:
    texts = []
    for n in range(1, 4):
        texts.append(
            (
                b"0.9 0.3 0.2 rg 20 20 260 420 re f 0 0 0 rg BT /F1 28 Tf 40 380 Td (PAGE %d) Tj ET "
                b"BT /F1 14 Tf 40 340 Td (Fumora PDF test) Tj ET" % n,
                {},
            )
        )
    write_pdf(HERE / "composed.pdf", texts)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "50", "-jpeg", str(HERE / "composed.pdf"), f"{tmp}/p"], check=True)
        jpegs = [p.read_bytes() for p in sorted(Path(tmp).glob("p-*.jpg"))]
    write_pdf(
        HERE / "scanned.pdf",
        [(b"q 300 0 0 460 0 0 cm /Im0 Do Q", {"Im0": j}) for j in jpegs],
    )


if __name__ == "__main__":
    main()
