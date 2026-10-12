#!/usr/bin/env python3
"""Prepare the CPU binary selected by llama.cpp's stable v0.6.0 release.

The official stable release's SHA-pinned nightly-tag.txt selects b11429.
Only binary assets are downloaded; no upstream source is copied into this repo.
Archive/member hashes are integrity identities, not endpoint authentication.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import urllib.request
from uuid import uuid4
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted" / "experimental"))
from cellivane_bridge.storage import encoded, local_root, read_json

TAG = "b11429"
COMMIT = "d81235049384534c167caea52b85a694f6103d14"
RELEASE_ID = 403936548
ASSET_ID = 613152134
ASSET_NAME = "llama-b11429-bin-win-cpu-x64.zip"
ASSET_BYTES = 19398918
ASSET_SHA256 = "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe"
ASSET_URL = "https://github.com/ggml-org/llama.cpp/releases/download/" + TAG + "/" + ASSET_NAME
STABLE_POINTER_SHA256 = "5677c5a4561fad44f2a58fc14187dd9cde253874584e958b37094c723ea8e8a5"
MAX_EXPANDED_BYTES = 512 * 1024**2


def file_sha256(path):
    h = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def _save(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _fetch(url, maximum):
    request = urllib.request.Request(url, headers={"User-Agent": "Cellivane-local-lab/0.1"})
    # HTTPS release-assets redirects are expected for GitHub binary downloads.
    # Proxy settings are disabled; no credential or parent-environment dump.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("asset-https-required")
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("asset-download-size")
    return raw


def archive_members(package):
    """Bounded ordinary ZIP files only; reject Windows path aliases and links."""
    result, names, total = [], set(), 0
    items = package.infolist()
    if not 1 <= len(items) <= 2000:
        raise ValueError("archive-member-count")
    for item in items:
        value = item.filename
        path = PurePosixPath(value)
        parts = path.parts
        mode = (item.external_attr >> 16) & 0o170000
        if (not parts or value.startswith("/") or "\\" in value or path.is_absolute()
                or any(p in (".", "..") or not p or ":" in p or p.endswith((".", " "))
                       or any(ord(c) < 32 for c in p) for p in parts)
                or path.as_posix() != value.rstrip("/")
                or mode not in (0, 0o040000, 0o100000) or item.flag_bits & 1):
            raise ValueError("unsafe-archive-entry")
        key = path.as_posix().casefold()
        if key in names:
            raise ValueError("duplicate-archive-path")
        names.add(key)
        total += item.file_size
        if item.file_size < 0 or total > MAX_EXPANDED_BYTES:
            raise ValueError("archive-expanded-size")
        if not item.is_dir():
            result.append((item, path.as_posix()))
    if not any(PurePosixPath(name).name == "llama-server.exe" for _item, name in result):
        raise ValueError("llama-server-missing")
    return result


def prepare_assets(asset_root, *, download=False):
    if type(download) is not bool:
        raise ValueError("download-boolean-required")
    root = local_root(asset_root)
    downloads = local_root(root / "downloads")
    archive = downloads / ASSET_NAME
    if archive.exists():
        local_root(archive.parent)
        if archive.is_symlink() or getattr(archive.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("asset-reparse-point")
    else:
        if not download:
            raise ValueError("asset-not-prepared-use-download-assets")
        # Save the pinned release and tag provenance, with no dependence on a
        # moving latest release during cache reuse or experiment execution.
        release_raw = _fetch("https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/" + TAG, 4 * 1024**2)
        release = json.loads(release_raw)
        candidates = [a for a in release["assets"] if a["name"] == ASSET_NAME]
        if (release["id"] != RELEASE_ID or release["tag_name"] != TAG or len(candidates) != 1
                or any(candidates[0].get(k) != v for k, v in {
                    "id": ASSET_ID, "size": ASSET_BYTES, "digest": "sha256:" + ASSET_SHA256,
                    "browser_download_url": ASSET_URL}.items())):
            raise ValueError("release-metadata-pin-mismatch")
        pointer = _fetch("https://github.com/ggml-org/llama.cpp/releases/download/v0.6.0/nightly-tag.txt", 128)
        if sha256(pointer).hexdigest() != STABLE_POINTER_SHA256 or pointer != (TAG + "\n").encode():
            raise ValueError("stable-pointer-pin-mismatch")
        tag_raw = _fetch("https://api.github.com/repos/ggml-org/llama.cpp/git/ref/tags/" + TAG, 65536)
        if json.loads(tag_raw).get("object") != {"sha": COMMIT, "type": "commit",
                "url": "https://api.github.com/repos/ggml-org/llama.cpp/git/commits/" + COMMIT}:
            raise ValueError("release-commit-pin-mismatch")
        raw = _fetch(ASSET_URL, ASSET_BYTES)
        if len(raw) != ASSET_BYTES or sha256(raw).hexdigest() != ASSET_SHA256:
            raise ValueError("asset-download-digest")
        token = str(uuid4())
        for name, content in (("release-" + token + ".json", release_raw),
                              ("tag-" + token + ".json", tag_raw),
                              ("stable-pointer-" + token + ".txt", pointer)):
            _save(downloads / name, content)
        _save(archive, raw)
    if archive.stat().st_size != ASSET_BYTES or file_sha256(archive) != ASSET_SHA256:
        raise ValueError("cached-archive-digest")
    directory = root / ("llama.cpp-" + TAG + "-cpu")
    marker = directory / "cellivane-assets.json"
    with zipfile.ZipFile(archive) as package:
        members = archive_members(package)
        expected = {}
        for item, name in members:
            with package.open(item) as stream:
                expected[name] = sha256(stream.read(MAX_EXPANDED_BYTES + 1)).hexdigest()
        if not marker.exists():
            if directory.exists():
                raise ValueError("incomplete-assets-use-fresh-asset-root")
            directory.mkdir()
            for item, name in members:
                target = directory.joinpath(*PurePosixPath(name).parts)
                local_root(target.parent)
                with package.open(item) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024**2)
                    dst.flush()
                    os.fsync(dst.fileno())
            _save(marker, encoded({"schema_version": 1, "tag": TAG, "commit": COMMIT,
                                   "archive_sha256": ASSET_SHA256, "member_sha256": expected}))
        local_root(directory)
        if marker.is_symlink() or getattr(marker.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("asset-manifest-reparse")
        saved = read_json(marker)
        if saved != {"schema_version": 1, "tag": TAG, "commit": COMMIT,
                     "archive_sha256": ASSET_SHA256, "member_sha256": expected}:
            raise ValueError("asset-manifest-mismatch")
        actual = set()
        for target in directory.rglob("*"):
            if target.is_symlink() or getattr(target.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("asset-tree-reparse")
            if target.is_file():
                actual.add(target.relative_to(directory).as_posix())
        if actual != set(expected) | {marker.name}:
            raise ValueError("asset-tree-extra-or-missing")
        for item, name in members:
            target = directory / name
            if target.stat().st_size != item.file_size or file_sha256(target) != expected[name]:
                raise ValueError("asset-member-digest")
    executables = {name: value for name, value in expected.items()
                   if PurePosixPath(name).suffix.lower() in (".exe", ".dll")}
    if not executables:
        raise ValueError("asset-binary-tree-empty")
    server = next(directory / name for name in expected if PurePosixPath(name).name == "llama-server.exe")
    return {"schema_version": 1, "backend": "llama.cpp", "stable_release": "v0.6.0",
            "release_tag": TAG, "source_commit": COMMIT, "release_id": RELEASE_ID,
            "asset_id": ASSET_ID, "asset_name": ASSET_NAME, "asset_url": ASSET_URL,
            "archive_bytes": ASSET_BYTES, "archive_sha256": ASSET_SHA256,
            "stable_pointer_sha256": STABLE_POINTER_SHA256,
            "member_sha256": expected, "binary_sha256": executables,
            "binary_tree_sha256": sha256(encoded(executables)).hexdigest(),
            "engine_identity": sha256(encoded({"backend": "llama.cpp", "commit": COMMIT,
                "archive_sha256": ASSET_SHA256, "binary_sha256": executables})).hexdigest(),
            "directory": str(directory), "server": str(server)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", required=True)
    parser.add_argument("--download-assets", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(prepare_assets(args.asset_root, download=args.download_assets), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
