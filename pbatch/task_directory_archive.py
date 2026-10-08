"""Canonical uncompressed USTAR byte copies and bounded non-extracting verification."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tarfile

from .task_center_io import checked_path, _open_regular
from .task_directory_inventory import inventory
from .task_directory_schema import MAX_ENTRIES, member_path, summarize
from .task_preservation_io import copy_stream, stat_fields
from .task_preservation_schema import MAX_PAYLOAD_BYTES

BLOCK = tarfile.BLOCKSIZE
RECORD = tarfile.RECORDSIZE


class _Writer:
    def __init__(self, output):
        self.output, self.bytes = output, 0
        self.hash = hashlib.sha256()

    def write(self, data: bytes) -> None:
        if self.bytes + len(data) > MAX_PAYLOAD_BYTES:
            raise ValueError("directory container byte budget exhausted; no truncation")
        self.output.write(data)
        self.hash.update(data)
        self.bytes += len(data)


def _header(row: dict) -> tarfile.TarInfo:
    info = row["stat"]
    header = tarfile.TarInfo(row["path"])
    header.mode = stat.S_IMODE(info["mode"])
    header.type = tarfile.DIRTYPE if stat.S_ISDIR(info["mode"]) else tarfile.REGTYPE
    header.size = 0 if header.isdir() else info["bytes"]
    return header  # uid/gid/mtime zero; no owner labels, executable hooks or extensions


def _member(header: tarfile.TarInfo, digest: str | None) -> dict:
    return {"path": header.name, "kind": "directory" if header.isdir() else "file",
            "mode": header.mode, "bytes": header.size, "sha256": digest}


def copy_directory(path: Path, output, expected: dict) -> tuple[dict, dict]:
    rows = inventory(path, expected)
    writer, members = _Writer(output), []
    for row in rows:
        header = _header(row)
        writer.write(header.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict"))
        payload = None
        if header.isfile():
            payload = copy_stream(path / row["path"], writer, row["stat"])
            writer.write(bytes((-header.size) % BLOCK))
        members.append(_member(header, payload["sha256"] if payload else None))
    writer.write(bytes(2 * BLOCK))
    writer.write(bytes((-writer.bytes) % RECORD))
    if inventory(path, expected) != rows:
        raise ValueError("directory names/stats changed during bounded copy; no receipt published")
    return {"bytes": writer.bytes, "sha256": writer.hash.hexdigest()}, summarize(members)


def _decode(block: bytes, members: list, kinds: dict) -> tarfile.TarInfo:
    if len(block) != BLOCK:
        raise ValueError("directory copy has an incomplete USTAR header")
    try:
        header = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        if header.type not in {tarfile.REGTYPE, tarfile.DIRTYPE}:
            raise ValueError("directory copy links/extensions/special types refused")
        member_path(header.name)
        _order(header, members, kinds)
        if (header.mode < 0 or header.mode > 0o777 or header.size < 0 or header.size > MAX_PAYLOAD_BYTES or
                (header.isdir() and header.size != 0) or header.linkname or header.uid or header.gid or
                header.mtime or header.uname or header.gname or header.devmajor or header.devminor or
                header.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict") != block):
            raise ValueError("directory copy header conflicts with its closed canonical profile")
        return header
    except (tarfile.HeaderError, UnicodeError, OverflowError) as exc:
        raise ValueError("invalid directory copy USTAR header") from exc


def _order(header: tarfile.TarInfo, members: list, kinds: dict) -> None:
    name = header.name
    if len(members) >= MAX_ENTRIES or name in kinds:
        raise ValueError("directory copy has too many or duplicate members")
    if not members:
        if name != "." or not header.isdir():
            raise ValueError("directory copy requires exactly one selected directory root")
    elif (name == "." or (len(members) > 1 and name <= members[-1]["path"]) or
          kinds.get(Path(name).parent.as_posix()) != "directory"):
        raise ValueError("directory copy member order/parent is missing or conflicting")


def _body(handle, size: int) -> str:
    digest, remaining = hashlib.sha256(), size
    while remaining:
        data = handle.read(min(65536, remaining))
        if not data:
            raise ValueError("directory copy file body is incomplete")
        digest.update(data)
        remaining -= len(data)
    padding = (-size) % BLOCK
    if handle.read(padding) != bytes(padding):
        raise ValueError("directory copy file padding is incomplete or nonzero")
    return digest.hexdigest()


def _trailer(handle, offset: int) -> None:
    expected = ((offset + 2 * BLOCK + RECORD - 1) // RECORD) * RECORD - offset - BLOCK
    trailer = handle.read(RECORD + BLOCK + 1)
    if trailer != bytes(expected):
        raise ValueError("directory copy trailing bytes/padding are incomplete or unsupported")


def verify_directory(path: Path, expected: dict) -> dict:
    """Verify only the saved container's full members. Never extract or read originals."""
    members, kinds = [], {}
    with _open_regular(path) as handle:
        if stat_fields(os.fstat(handle.fileno())) != expected:
            raise ValueError("directory copy changed before member verification")
        while True:
            offset = handle.tell()
            if offset >= MAX_PAYLOAD_BYTES:
                raise ValueError("directory copy verification byte budget exhausted")
            block = handle.read(BLOCK)
            if block == bytes(BLOCK):
                _trailer(handle, offset)
                break
            header = _decode(block, members, kinds)
            digest = _body(handle, header.size) if header.isfile() else None
            members.append(_member(header, digest))
            kinds[header.name] = members[-1]["kind"]
        if stat_fields(os.fstat(handle.fileno())) != expected:
            raise ValueError("directory copy changed during member verification")
    if stat_fields(checked_path(path).stat()) != expected:
        raise ValueError("directory copy pathname changed during member verification")
    if not members:
        raise ValueError("directory copy root/member inventory is empty")
    return summarize(members)
