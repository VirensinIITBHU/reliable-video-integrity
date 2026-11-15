"""Shared helpers: hashing, atomic writes, timestamps."""
import hashlib
import json
import os
import tempfile
import time

CHUNK_SIZE_DEFAULT = 256 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write to a temp file in the same directory, fsync, then rename.

    A reader can therefore never observe a half-written file under `path`.
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=2, sort_keys=True).encode())


def now() -> float:
    return time.time()


def iso(ts: float = None) -> str:
    ts = now() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)
