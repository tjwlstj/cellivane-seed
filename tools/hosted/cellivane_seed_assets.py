#!/usr/bin/env python3
"""Download one pinned official Qwen GGUF; verify bytes; never execute it.

No token, credential, remote model name, or arbitrary download URL is accepted.
Failed exclusive partials and all pre-existing artifacts are preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.request
from uuid import uuid4

CATALOG = {
    "0.6b": {"repo": "Qwen/Qwen3-0.6B-GGUF", "revision": "23749fefcc72300e3a2ad315e1317431b06b590a",
        "filename": "Qwen3-0.6B-Q8_0.gguf", "size": 639446688,
        "sha256": "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"},
    "1.7b": {"repo": "Qwen/Qwen3-1.7B-GGUF", "revision": "90862c4b9d2787eaed51d12237eafdfe7c5f6077",
        "filename": "Qwen3-1.7B-Q8_0.gguf", "size": 1834426016,
        "sha256": "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"},
    "4b": {"repo": "Qwen/Qwen3-4B-GGUF", "revision": "bc640142c66e1fdd12af0bd68f40445458f3869b",
        "filename": "Qwen3-4B-Q8_0.gguf", "size": 4280404704,
        "sha256": "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"},
}
CHUNK_BYTES = 1024 * 1024


def safe_path(path: Path, *, regular=False):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("symlink-path-refused")
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("reparse-path-refused")
    if regular and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("regular-file-required")


def digest_file(path: Path, expected_size: int):
    safe_path(path, regular=True)
    if path.stat().st_size != expected_size:
        raise ValueError("existing-artifact-size-mismatch-preserved")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire(spec: dict, root: Path, *, opener=None):
    """The caller selects a catalog row; opener is injectable for local tests."""
    root = root.absolute()
    safe_path(root)
    directory = root / "models"
    directory.mkdir(parents=True, exist_ok=True)
    safe_path(directory)
    target = directory / spec["filename"]
    run_id = str(uuid4())
    partial = directory / (spec["filename"] + "." + run_id + ".part")
    url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{spec['filename']}"
    receipt = {"schema_version": 1, "run_id": run_id, **spec, "url": url,
        "license_metadata": "apache-2.0", "path": str(target), "partial_path": str(partial),
        "model_executed": False, "authentication_used": False, "status": "FAIL"}
    started = time.monotonic()
    try:
        safe_path(target)
        if target.exists():
            actual = digest_file(target, spec["size"])
            if actual != spec["sha256"]:
                raise ValueError("existing-artifact-hash-mismatch-preserved")
            receipt.update(status="PASS", action="verified-existing", actual_size=spec["size"], actual_sha256=actual)
        else:
            request = urllib.request.Request(url + "?download=true&cellivane_request=" + run_id,
                headers={"User-Agent": "CellivaneSeed-assets/0.1", "Accept-Encoding": "identity"})
            digest, size, last_report = hashlib.sha256(), 0, started
            # Do not inherit proxy URLs which could contain implicit credentials.
            fetch = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
            with partial.open("xb") as sink:
                with fetch(request, timeout=60) as source:
                    if source.status != 200:
                        raise ValueError("unexpected-http-status")
                    declared = source.headers.get("Content-Length")
                    if declared is not None and int(declared) != spec["size"]:
                        raise ValueError("http-content-length-mismatch")
                    if source.headers.get("Content-Encoding", "identity") != "identity":
                        raise ValueError("encoded-body-refused")
                    while True:
                        if time.monotonic() - started > 1800:
                            raise TimeoutError("download-wall-budget-exceeded")
                        chunk = source.read(min(CHUNK_BYTES, spec["size"] - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > spec["size"]:
                            raise ValueError("artifact-size-overflow")
                        sink.write(chunk)
                        digest.update(chunk)
                        if time.monotonic() - last_report >= 30:
                            print(json.dumps({"file": spec["filename"], "bytes": size, "expected": spec["size"]}),
                                  file=sys.stderr, flush=True)
                            last_report = time.monotonic()
                sink.flush()
                os.fsync(sink.fileno())
            actual = digest.hexdigest()
            receipt.update(actual_size=size, actual_sha256=actual)
            if size != spec["size"] or actual != spec["sha256"]:
                raise ValueError("artifact-size-or-hash-mismatch")
            safe_path(target)
            # Exclusive same-volume publication; unlike replace(), this cannot overwrite.
            os.link(partial, target)
            partial.unlink()
            receipt.update(status="PASS", action="downloaded-and-verified")
    except Exception as exc:
        receipt.update(error=f"{type(exc).__name__}: {exc}", partial_preserved=partial.exists())
    receipt["elapsed_s"] = round(time.monotonic() - started, 3)
    receipts = root / "downloads"
    safe_path(receipts)
    receipts.mkdir(exist_ok=True)
    safe_path(receipts)
    receipt_path = receipts / ("model-download-" + run_id + ".json")
    receipt["receipt_path"] = str(receipt_path)
    with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(CATALOG), required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = acquire(CATALOG[args.model], args.storage_root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc), "model_executed": False}))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
