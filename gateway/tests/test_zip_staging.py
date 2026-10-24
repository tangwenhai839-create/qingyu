import zipfile
from pathlib import Path

import pytest
from app.importers.zip_staging import UnsafeZipError, ZipLimits, safe_extract


def make_zip(path: Path, name: str, data: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)

def test_zip_slip_and_limits(tmp_path: Path):
    z = tmp_path / "bad.zip"
    make_zip(z, "../escape.txt", b"x")
    with pytest.raises(UnsafeZipError): safe_extract(z, tmp_path / "out")
    z2 = tmp_path / "big.zip"
    make_zip(z2, "x.txt", b"x" * 20)
    with pytest.raises(UnsafeZipError): safe_extract(z2, tmp_path / "out2", ZipLimits(max_file_bytes=10))

def test_zip_extracts_inside_destination(tmp_path: Path):
    z = tmp_path / "ok.zip"
    make_zip(z, "folder/file.txt", b"ok")
    report = safe_extract(z, tmp_path / "out")
    assert report["file_count"] == 1
    assert (tmp_path / "out/folder/file.txt").read_bytes() == b"ok"
