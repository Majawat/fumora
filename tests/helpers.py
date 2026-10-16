import json
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMAS = Path(__file__).parent / "schemas"


def metron_issue(metron_id: int) -> dict:
    return json.loads((FIXTURES / "metron" / f"issue-{metron_id}.json").read_text())


def make_cbz(path: Path, comicinfo: str | None = None, pages: int = 3) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n in range(1, pages + 1):
            zf.writestr(
                f"page{n:03}.jpg", b"\xff\xd8\xff" + bytes([n]) * 500, compress_type=zipfile.ZIP_STORED
            )
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
    return path


def validate(xml: bytes, schema: str) -> None:
    from lxml import etree

    tree = etree.parse(str(SCHEMAS / schema))
    # MetronInfo uses XSD 1.1 <xs:assert> (at most one primary ID/URL); lxml only
    # does XSD 1.0, so drop the asserts. Fumora writes a single primary anyway.
    for node in tree.iter("{http://www.w3.org/2001/XMLSchema}assert"):
        node.getparent().remove(node)
    xsd = etree.XMLSchema(tree)
    doc = etree.fromstring(xml)
    if not xsd.validate(doc):
        raise AssertionError(str(xsd.error_log))
