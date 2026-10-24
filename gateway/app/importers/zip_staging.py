import hashlib
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1,10)), *(f"lpt{i}" for i in range(1,10))}


@dataclass(frozen=True)
class ZipLimits:
    max_total_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 128 * 1024 * 1024
    max_files: int = 10_000
    max_compression_ratio: float = 100.0


class UnsafeZipError(ValueError): pass


class UnsupportedZipError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_member(info):
    raw=info.filename
    if raw.startswith(("/","\\")) or re.match(r"^[A-Za-z]:",raw): raise UnsafeZipError("ZIP absolute/drive/UNC path")
    name=raw.replace("\\","/"); parts=name.split("/"); clean=[]
    for part in parts:
        if not part: continue
        if part in {".",".."}: raise UnsafeZipError("ZIP path traversal")
        if ":" in part: raise UnsafeZipError("ZIP ADS/colon path")
        if part.endswith(("."," ")): raise UnsafeZipError("ZIP trailing dot/space")
        if part.split(".",1)[0].casefold() in WINDOWS_RESERVED: raise UnsafeZipError("ZIP Windows reserved name")
        clean.append(part)
    if not clean: raise UnsafeZipError("ZIP empty member path")
    mode=(info.external_attr >> 16) & 0xFFFF
    kind=stat.S_IFMT(mode)
    if kind not in {0,stat.S_IFREG,stat.S_IFDIR}: raise UnsafeZipError("ZIP unsupported member type")
    return "/".join(clean), "/".join(clean).casefold()


def _prevalidate(archive, limits):
    infos=archive.infolist()
    if len(infos)>limits.max_files: raise UnsafeZipError("ZIP file count exceeded")
    seen={}; total=0; normalized=[]
    for info in infos:
        if info.flag_bits & 0x1:
            raise UnsupportedZipError("encrypted_zip_unsupported")
        name,key=_normalized_member(info)
        if key in seen: raise UnsafeZipError("ZIP duplicate or case-insensitive path collision")
        seen[key]=name; normalized.append((info,name,key))
        if info.is_dir(): continue
        if info.file_size>limits.max_file_bytes: raise UnsafeZipError("ZIP single file size exceeded")
        total += info.file_size
        if total>limits.max_total_bytes: raise UnsafeZipError("ZIP total size exceeded")
        if info.compress_size and info.file_size/info.compress_size>limits.max_compression_ratio: raise UnsafeZipError("ZIP suspicious compression ratio")
        if name.casefold().endswith(".zip"): raise UnsafeZipError("nested ZIP prohibited")
    file_keys={key for info,_,key in normalized if not info.is_dir()}
    for _,_,key in normalized:
        parts=key.split("/")
        if any("/".join(parts[:i]) in file_keys for i in range(1,len(parts))): raise UnsafeZipError("ZIP file/directory collision")
    return normalized,total


_DEFAULT_ZIP_LIMITS = ZipLimits()


def safe_extract(zip_path: Path, destination: Path, limits: ZipLimits = _DEFAULT_ZIP_LIMITS) -> dict:
    with zipfile.ZipFile(zip_path) as archive:
        normalized,total=_prevalidate(archive,limits)
        destination.mkdir(parents=True,exist_ok=True); root=destination.resolve()
        for info,name,_ in normalized:
            target=(destination/name).resolve()
            if root not in target.parents and target != root: raise UnsafeZipError("ZIP escaped destination")
            if info.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output: shutil.copyfileobj(source,output,1024*1024)
    return {"file_count":len(normalized),"total_uncompressed_bytes":total,"sha256":_sha256_file(zip_path)}
