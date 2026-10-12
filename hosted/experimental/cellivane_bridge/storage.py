"""Single-writer run records on a local disk; no automatic replay or task restart."""
from __future__ import annotations

import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from uuid import uuid4


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def read_json(path, maximum=1024 * 1024):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate-json-key")
            result[key] = value
        return result

    with Path(path).open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("json-size")
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("json-number")))


def local_root(value):
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("storage-local-absolute-required")
    for parent in (path, *path.parents):
        if parent.exists():
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("storage-reparse-point")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
        raise ValueError("storage-fixed-drive-required")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


class Journal:
    def __init__(self, storage_root):
        runs = local_root(local_root(storage_root) / "runs")
        self.root = runs / str(uuid4())
        self.root.mkdir(parents=True, exist_ok=False)
        self._writer = threading.get_ident()
        self._sequence = 0
        self._poisoned = False
        self._previous = "0" * 64
        self.directory_fsync = os.name != "nt"

    def append(self, kind, payload):
        if threading.get_ident() != self._writer or self._poisoned:
            raise RuntimeError("journal-writer-unavailable")
        sequence = self._sequence + 1
        record = {"schema_version": 1, "sequence": sequence, "kind": kind,
                  "previous_sha256": self._previous, "payload": payload}
        raw = encoded(record)
        target = self.root / f"{sequence:04d}-{kind}.json"
        temporary = self.root / f".{sequence:04d}-{uuid4()}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise RuntimeError("journal-duplicate-record")
            os.replace(temporary, target)
            if self.directory_fsync:
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except BaseException:
            self._poisoned = True
            raise
        self._sequence = sequence
        self._previous = sha256(raw).hexdigest()
        return target
