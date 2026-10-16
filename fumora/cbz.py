"""Reading and rewriting metadata inside CBZ (zip) archives.

A rewrite copies every entry into a temporary archive next to the original,
adds the new metadata files, checks the result, then atomically replaces the
original. If anything fails the original is untouched.
"""

import os
import zipfile
from pathlib import Path

COMICINFO = "ComicInfo.xml"
METRONINFO = "MetronInfo.xml"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".jxl", ".bmp"}


class ArchiveError(Exception):
    pass


def is_cbz(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".cbz"


def _root_entry(zf: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    for info in zf.infolist():
        if info.filename.lower() == name.lower():
            return info
    return None


def read_metadata(path: str | Path) -> tuple[bytes | None, bytes | None, int]:
    """(ComicInfo.xml, MetronInfo.xml, number of image pages)."""
    try:
        with zipfile.ZipFile(path) as zf:
            ci = _root_entry(zf, COMICINFO)
            mi = _root_entry(zf, METRONINFO)
            pages = sum(
                1 for i in zf.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() in IMAGE_EXTS
            )
            return (zf.read(ci) if ci else None, zf.read(mi) if mi else None, pages)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"Cannot read {Path(path).name}: {exc}") from exc


def write_metadata(path: str | Path, files: dict[str, bytes]) -> None:
    """Replace (or add) root-level metadata files, e.g. {"ComicInfo.xml": b"..."}."""
    path = Path(path)
    if not is_cbz(path):
        raise ArchiveError(f"Only .cbz can be written, not {path.suffix}")
    tmp = path.with_name(f".{path.name}.fumora-tmp")
    replaced = {name.lower() for name in files}
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
            zout.comment = zin.comment
            copied = 0
            for info in zin.infolist():
                if info.filename.lower() in replaced:
                    continue
                # Reading verifies each entry's CRC, so a damaged archive aborts here.
                zout.writestr(info, zin.read(info))
                copied += 1
            for name, data in files.items():
                zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

        with zipfile.ZipFile(tmp) as check:
            bad = check.testzip()
            if bad is not None:
                raise ArchiveError(f"Verification failed on {bad}")
            if len(check.infolist()) != copied + len(files):
                raise ArchiveError("Verification failed: entry count differs")

        st = path.stat()
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass  # not allowed to give it away; it keeps our PUID/PGID
        os.replace(tmp, path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as exc:
        raise ArchiveError(f"Cannot rewrite {path.name}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
