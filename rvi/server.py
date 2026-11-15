"""Upload backend: chunked, resumable, hash-verified, with an explicit state machine.

States
------
UPLOADING  -> chunks are arriving
STALLED    -> no progress for `stall_timeout` seconds (returns to UPLOADING on progress)
VERIFYING  -> all chunks present, whole-file hash being checked
COMPLETE   -> whole file verified byte-perfect; the ONLY state that allows analysis
REJECTED   -> assembled file failed size / SHA-256 verification
EXPIRED    -> abandoned for `expire_timeout` seconds; partial data discarded

Analysis states (only reachable from COMPLETE): ANALYZING -> ANALYZED / ANALYSIS_FAILED

Endpoints
---------
POST /uploads                      create or resume a session (idempotent)
PUT  /uploads/{id}/chunks/{n}      upload one chunk (idempotent, hash-verified)
GET  /uploads/{id}                 session status + missing chunk list
POST /uploads/{id}/complete        assemble + verify whole file
GET  /uploads/{id}/report          integrity report (JSON)
GET  /uploads/{id}/report.html     integrity report (HTML)
GET  /health
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import atomic_write_bytes, atomic_write_json, iso, now, sha256_bytes

HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_CHUNK = 8 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024


class Session:
    def __init__(self, root, meta):
        self.root = root
        self.meta = meta
        self.lock = threading.Lock()

    # -- paths -------------------------------------------------------------
    @property
    def dir(self):
        return os.path.join(self.root, "sessions", self.meta["upload_id"])

    def chunk_path(self, n):
        return os.path.join(self.dir, "chunks", "%06d.part" % n)

    @property
    def verified_path(self):
        return os.path.join(self.root, "verified", self.meta["upload_id"] + "_" + self.meta["safe_name"])

    @property
    def report_path(self):
        return os.path.join(self.dir, "report.json")

    # -- persistence -------------------------------------------------------
    def save(self):
        atomic_write_json(os.path.join(self.dir, "session.json"), self.meta)

    def event(self, kind, **kw):
        rec = {"t": iso(), "event": kind}
        rec.update(kw)
        with open(os.path.join(self.dir, "events.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def set_state(self, state, **kw):
        old = self.meta["state"]
        if old != state:
            self.meta["state"] = state
            self.meta["state_history"].append({"t": iso(), "from": old, "to": state})
            self.event("state", **{"from": old, "to": state}, **kw)
        self.save()

    # -- views -------------------------------------------------------------
    def missing(self):
        got = set(self.meta["received"])
        return [i for i in range(len(self.meta["chunks"])) if i not in got]

    def status(self):
        m = self.meta
        return {
            "upload_id": m["upload_id"],
            "state": m["state"],
            "filename": m["filename"],
            "size": m["size"],
            "sha256": m["sha256"],
            "chunk_size": m["chunk_size"],
            "total_chunks": len(m["chunks"]),
            "received_chunks": len(m["received"]),
            "missing": self.missing(),
            "bytes_received": m["bytes_received"],
            "last_progress": iso(m["last_progress"]),
            "reject_reason": m.get("reject_reason"),
            "counters": m["counters"],
            "state_history": m["state_history"],
        }


class Store:
    def __init__(self, root, stall_timeout=10.0, expire_timeout=600.0, analyze=True):
        self.root = os.path.abspath(root)
        self.stall_timeout = stall_timeout
        self.expire_timeout = expire_timeout
        self.analyze = analyze
        self.sessions = {}
        self.lock = threading.Lock()
        os.makedirs(os.path.join(self.root, "sessions"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "verified"), exist_ok=True)
        self._load()
        self._stop = threading.Event()
        self._mon = threading.Thread(target=self._monitor, daemon=True)
        self._mon.start()

    def _load(self):
        """Recover sessions after a server restart (state is on disk, not in RAM)."""
        base = os.path.join(self.root, "sessions")
        for uid in os.listdir(base):
            p = os.path.join(base, uid, "session.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        self.sessions[uid] = Session(self.root, json.load(f))
                except (OSError, ValueError):
                    pass

    def stop(self):
        self._stop.set()

    def _monitor(self):
        while not self._stop.wait(0.5):
            t = now()
            for s in list(self.sessions.values()):
                with s.lock:
                    st = s.meta["state"]
                    idle = t - s.meta["last_progress"]
                    if st == "UPLOADING" and idle > self.stall_timeout:
                        s.set_state("STALLED", idle_seconds=round(idle, 1))
                    elif st in ("UPLOADING", "STALLED") and idle > self.expire_timeout:
                        shutil.rmtree(os.path.join(s.dir, "chunks"), ignore_errors=True)
                        s.meta["received"] = []
                        s.meta["bytes_received"] = 0
                        s.set_state("EXPIRED", idle_seconds=round(idle, 1))

    # -- operations --------------------------------------------------------
    def create(self, body):
        try:
            size = int(body["size"])
            chunk_size = int(body["chunk_size"])
            sha = str(body["sha256"]).lower()
            chunks = [str(c).lower() for c in body["chunks"]]
            filename = str(body.get("filename") or "video.bin")
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "bad_manifest"}
        if not HEX64.match(sha) or not all(HEX64.match(c) for c in chunks):
            return 400, {"error": "bad_hash_format"}
        if not (0 < size <= MAX_FILE) or not (0 < chunk_size <= MAX_CHUNK):
            return 400, {"error": "bad_size"}
        if len(chunks) != -(-size // chunk_size):
            return 400, {"error": "chunk_count_mismatch"}
        # Deterministic id: the same file + chunking always maps to the same
        # session, so a client that crashed can resume with no local state.
        uid = hashlib.sha256(("%s:%d:%d" % (sha, size, chunk_size)).encode()).hexdigest()[:20]
        with self.lock:
            s = self.sessions.get(uid)
            if s is not None and s.meta["chunks"] != chunks:
                return 409, {"error": "manifest_conflict"}
            if s is None or s.meta["state"] in ("REJECTED", "EXPIRED"):
                if s is not None:
                    shutil.rmtree(s.dir, ignore_errors=True)
                meta = {
                    "upload_id": uid, "filename": filename,
                    "safe_name": re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))[:80] or "video.bin",
                    "size": size, "sha256": sha, "chunk_size": chunk_size, "chunks": chunks,
                    "received": [], "bytes_received": 0, "state": "UPLOADING",
                    "created": now(), "last_progress": now(), "state_history": [],
                    "source_manifest": body.get("source_manifest"),
                    "counters": {"chunk_ok": 0, "chunk_duplicate": 0, "chunk_hash_mismatch": 0,
                                 "chunk_incomplete_body": 0, "finalize_refused": 0},
                }
                s = Session(self.root, meta)
                os.makedirs(os.path.join(s.dir, "chunks"), exist_ok=True)
                s.save()
                s.event("created", size=size, chunks=len(chunks))
                self.sessions[uid] = s
                return 201, s.status()
            with s.lock:
                s.event("resumed", received=len(s.meta["received"]))
                return 200, s.status()

    def get(self, uid):
        return self.sessions.get(uid)

    def put_chunk(self, s, n, data, claimed):
        m = s.meta
        if n < 0 or n >= len(m["chunks"]):
            return 404, {"error": "no_such_chunk"}
        with s.lock:
            if m["state"] not in ("UPLOADING", "STALLED"):
                if n in m["received"]:
                    return 200, {"ok": True, "duplicate": True, "state": m["state"]}
                return 409, {"error": "not_accepting_chunks", "state": m["state"]}
        expected_len = m["chunk_size"] if n < len(m["chunks"]) - 1 else m["size"] - m["chunk_size"] * (len(m["chunks"]) - 1)
        digest = sha256_bytes(data)
        with s.lock:
            if len(data) != expected_len or digest != m["chunks"][n] or (claimed and claimed != digest):
                m["counters"]["chunk_hash_mismatch"] += 1
                s.event("chunk_rejected", n=n, got_len=len(data), expected_len=expected_len)
                s.save()
                return 422, {"error": "chunk_integrity_mismatch", "n": n}
            if n in m["received"]:
                m["counters"]["chunk_duplicate"] += 1
                s.save()
                return 200, {"ok": True, "duplicate": True}
            atomic_write_bytes(s.chunk_path(n), data)
            m["received"].append(n)
            m["bytes_received"] += len(data)
            m["last_progress"] = now()
            m["counters"]["chunk_ok"] += 1
            s.event("chunk_ok", n=n, bytes=len(data))
            if m["state"] == "STALLED":
                s.set_state("UPLOADING", reason="progress_resumed")
            else:
                s.save()
            return 200, {"ok": True, "received_chunks": len(m["received"])}

    def note_incomplete(self, s, n, got, expected):
        with s.lock:
            s.meta["counters"]["chunk_incomplete_body"] += 1
            s.event("chunk_incomplete_body", n=n, got=got, expected=expected)
            s.save()

    def complete(self, s):
        m = s.meta
        with s.lock:
            if m["state"] in ("COMPLETE", "ANALYZING", "ANALYZED", "ANALYSIS_FAILED"):
                return 200, s.status()
            if m["state"] not in ("UPLOADING", "STALLED"):
                return 409, {"error": "cannot_complete", "state": m["state"]}
            missing = s.missing()
            if missing:
                m["counters"]["finalize_refused"] += 1
                s.event("finalize_refused", missing=len(missing))
                s.save()
                return 409, {"error": "incomplete", "missing": missing, "state": m["state"]}
            s.set_state("VERIFYING")
            # Assemble into a temp file, hash while writing, rename only if verified.
            os.makedirs(os.path.dirname(s.verified_path), exist_ok=True)
            tmp = s.verified_path + ".assembling"
            h = hashlib.sha256()
            total = 0
            try:
                with open(tmp, "wb") as out:
                    for i in range(len(m["chunks"])):
                        with open(s.chunk_path(i), "rb") as f:
                            b = f.read()
                        h.update(b)
                        total += len(b)
                        out.write(b)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as e:
                m["reject_reason"] = "assembly_io_error: %s" % e
                s.set_state("REJECTED", reason=m["reject_reason"])
                return 500, s.status()
            if total != m["size"] or h.hexdigest() != m["sha256"]:
                os.unlink(tmp)
                m["reject_reason"] = "whole_file_mismatch: size %d/%d, sha256 %s" % (
                    total, m["size"], "ok" if h.hexdigest() == m["sha256"] else "MISMATCH")
                s.set_state("REJECTED", reason=m["reject_reason"])
                return 422, s.status()
            os.replace(tmp, s.verified_path)
            shutil.rmtree(os.path.join(s.dir, "chunks"), ignore_errors=True)
            m["verified_at"] = now()
            s.set_state("COMPLETE", sha256=m["sha256"])
            status = s.status()
        if self.analyze:
            threading.Thread(target=self._analyze, args=(s,), daemon=True).start()
        return 200, status

    def _analyze(self, s):
        from .analyze import analyze_video
        from .report import build_report, render_html
        with s.lock:
            if s.meta["state"] != "COMPLETE":  # hard gate: never decode unverified data
                return
            s.set_state("ANALYZING")
        try:
            analysis = analyze_video(s.verified_path, s.meta.get("source_manifest"))
            with s.lock:
                rep = build_report(s.status(), analysis)
                atomic_write_json(s.report_path, rep)
                atomic_write_bytes(os.path.join(s.dir, "report.html"), render_html(rep).encode())
                s.set_state("ANALYZED", verdict=rep["verdict"])
        except Exception as e:  # analysis must never take the server down
            with s.lock:
                s.meta["analysis_error"] = repr(e)
                s.set_state("ANALYSIS_FAILED", error=repr(e))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rvi/0.1"
    store = None  # set by make_server

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit):
        """Read exactly Content-Length bytes; return (data, expected_len)."""
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None, -1
        if n < 0 or n > limit:
            return None, n
        buf = bytearray()
        while len(buf) < n:
            b = self.rfile.read(min(65536, n - len(buf)))
            if not b:
                break
            buf.extend(b)
        return bytes(buf), n

    def _route(self):
        return [p for p in self.path.split("?")[0].split("/") if p]

    def do_GET(self):
        p = self._route()
        if p == ["health"]:
            return self._send(200, {"ok": True})
        if len(p) >= 2 and p[0] == "uploads":
            s = self.store.get(p[1])
            if s is None:
                return self._send(404, {"error": "unknown_upload"})
            if len(p) == 2:
                with s.lock:
                    return self._send(200, s.status())
            if len(p) == 3 and p[2] in ("report", "report.html"):
                path = s.report_path if p[2] == "report" else os.path.join(s.dir, "report.html")
                if not os.path.exists(path):
                    return self._send(404, {"error": "report_not_ready", "state": s.meta["state"]})
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "application/json" if p[2] == "report" else "text/html; charset=utf-8")
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        p = self._route()
        data, n = self._body(16 * 1024 * 1024)
        if data is None or len(data) != n:
            self.close_connection = True
            return self._send(400, {"error": "bad_body"})
        if p == ["uploads"]:
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad_json"})
            return self._send(*self.store.create(body))
        if len(p) == 3 and p[0] == "uploads" and p[2] == "complete":
            s = self.store.get(p[1])
            if s is None:
                return self._send(404, {"error": "unknown_upload"})
            return self._send(*self.store.complete(s))
        self._send(404, {"error": "not_found"})

    def do_PUT(self):
        p = self._route()
        if not (len(p) == 4 and p[0] == "uploads" and p[2] == "chunks" and p[3].isdigit()):
            self.close_connection = True
            return self._send(404, {"error": "not_found"})
        s = self.store.get(p[1])
        if s is None:
            self.close_connection = True
            return self._send(404, {"error": "unknown_upload"})
        data, n = self._body(MAX_CHUNK)
        if data is None:
            self.close_connection = True
            return self._send(413, {"error": "bad_length"})
        if len(data) != n:
            # Connection died mid-body: nothing is stored, the client must resend.
            self.store.note_incomplete(s, int(p[3]), len(data), n)
            self.close_connection = True
            return
        claimed = (self.headers.get("X-Chunk-SHA256") or "").lower()
        self._send(*self.store.put_chunk(s, int(p[3]), data, claimed))


def make_server(root, host="127.0.0.1", port=8000, **store_kw):
    store = Store(root, **store_kw)
    handler = type("BoundHandler", (Handler,), {"store": store})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.store = store
    return httpd


def main():
    ap = argparse.ArgumentParser(description="Reliable video upload backend")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="server_data")
    ap.add_argument("--stall-timeout", type=float, default=10.0)
    ap.add_argument("--expire-timeout", type=float, default=600.0)
    a = ap.parse_args()
    httpd = make_server(a.data, a.host, a.port, stall_timeout=a.stall_timeout, expire_timeout=a.expire_timeout)
    print("backend listening on http://%s:%d  data=%s" % (a.host, a.port, os.path.abspath(a.data)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
