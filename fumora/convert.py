"""Converting CBR / CB7 / CBT comics to CBZ.

A file whose content is already a zip (a common mislabel) is just renamed.
RAR, 7z and tar archives are unpacked with bsdtar (libarchive: RAR4, RAR5,
7z, tar) into a temporary folder next to the file, repacked as a zip, checked,
and only then put in place. The original is moved to a recycle folder, never
deleted. Symlinks and special files inside an archive are ignored.
"""

import os
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path

from .cbz import IMAGE_EXTS, ArchiveError

CONVERTIBLE_EXTS = {".cbr", ".cb7", ".cbt", ".rar", ".7z"}
_MAGIC = [
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),  # empty zip
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"%PDF", "pdf"),
]
_JUNK = {"__macosx", ".ds_store", "thumbs.db", "desktop.ini"}


def sniff(path: str | Path) -> str | None:
    """The archive's real format from its first bytes: zip, rar, 7z, tar, pdf."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def needs_conversion(path: str | Path) -> bool:
    p = Path(path)
    return p.suffix.lower() in CONVERTIBLE_EXTS or (
        p.suffix.lower() == ".cbz" and sniff(p) not in ("zip", None)
    )


def _bsdtar(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    if shutil.which("bsdtar") is None:
        raise ArchiveError("bsdtar (libarchive-tools) is not installed")
    try:
        return subprocess.run(["bsdtar", *args], capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError("bsdtar timed out") from exc


def read_member(path: str | Path, name: str) -> bytes | None:
    """One root-level member (case-insensitive) of a non-zip archive, e.g. ComicInfo.xml."""
    listing = _bsdtar("-tf", str(path))
    if listing.returncode != 0:
        raise ArchiveError(
            f"Cannot read {Path(path).name}: {listing.stderr.decode(errors='replace').strip()}"
        )
    for member in listing.stdout.decode(errors="replace").splitlines():
        if member.removeprefix("./").lower() == name.lower():
            out = _bsdtar("-xOf", str(path), member)
            return out.stdout if out.returncode == 0 else None
    return None


def _recycle(path: Path, recycle_dir: Path) -> Path:
    target_dir = recycle_dir / date.today().isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    n = 1
    while target.exists():
        target = target_dir / f"{path.stem} ({n}){path.suffix}"
        n += 1
    try:
        os.rename(path, target)
    except OSError:
        shutil.move(str(path), str(target))  # recycle folder on another mount
    return target


def convert_to_cbz(path: str | Path, recycle_dir: str | Path) -> Path:
    """Convert one file to .cbz next to it; returns the new path."""
    path = Path(path)
    target = path.with_suffix(".cbz")
    if target != path and target.exists():
        raise ArchiveError(f"{target.name} already exists")
    kind = sniff(path)
    if kind == "zip":
        if target != path:
            os.rename(path, target)  # mislabelled zip: same file, right extension
        return target
    if kind not in ("rar", "7z", "tar"):
        raise ArchiveError(
            f"{path.name}: not a comic archive Fumora can convert ({kind or 'unknown format'})"
        )

    work = path.with_name(f".{path.stem}.fumora-convert")
    tmp_zip = path.with_name(f".{path.stem}.fumora-tmp.cbz")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        out = _bsdtar("-xf", str(path), "-C", str(work), "--no-same-owner", "--no-same-permissions")
        if out.returncode != 0:
            raise ArchiveError(f"Cannot unpack {path.name}: {out.stderr.decode(errors='replace').strip()}")
        members = []
        for root, dirs, files in os.walk(work, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d.lower() not in _JUNK)
            for name in sorted(files):
                full = Path(root) / name
                if name.lower() in _JUNK or full.is_symlink() or not full.is_file():
                    continue
                members.append(full)
        if not any(m.suffix.lower() in IMAGE_EXTS for m in members):
            raise ArchiveError(f"{path.name} contains no images")
        with zipfile.ZipFile(tmp_zip, "w") as zf:
            for m in members:
                arc = m.relative_to(work).as_posix()
                stored = m.suffix.lower() in IMAGE_EXTS  # images are already compressed
                zf.write(m, arc, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(tmp_zip) as check:
            if check.testzip() is not None or len(check.infolist()) != len(members):
                raise ArchiveError(f"Verification failed converting {path.name}")
        os.chmod(tmp_zip, path.stat().st_mode & 0o7777)
        if target == path:
            # A ".cbz" that is really a RAR: move the original away first, so it
            # is never overwritten.
            _recycle(path, Path(recycle_dir))
            os.replace(tmp_zip, target)
        else:
            os.replace(tmp_zip, target)
            _recycle(path, Path(recycle_dir))
        return target
    except OSError as exc:
        raise ArchiveError(f"Cannot convert {path.name}: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
        tmp_zip.unlink(missing_ok=True)
