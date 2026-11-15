"""Upload client: chunked, resumable, retrying, with endpoint failover.

Reliability mechanisms
----------------------
* whole-file SHA-256 + per-chunk SHA-256 sent up front (the manifest)
* every request has a timeout; a stalled request is abandoned and retried
* exponential backoff with full jitter between retries
* after any failure the client re-asks the server which chunks it holds
  (the server is the source of truth, so resume needs no local state)
* multiple endpoints: repeated failures on one path fail over to the next
  (models Wi-Fi -> cellular connection switching)
* success is declared ONLY when the server reports the whole file verified
"""
import argparse
import http.client
import json
import os
import random
import subprocess
import time
from urllib.parse import urlparse

from .common import CHUNK_SIZE_DEFAULT, iso, sha256_bytes, sha256_file

VERIFIED_STATES = ("COMPLETE", "ANALYZING", "ANALYZED", "ANALYSIS_FAILED")


class TransferFailed(Exception):
    pass


def probe_source(path):
    """Reference evidence captured at the source, before any transfer."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,duration",
             "-of", "json", path], capture_output=True, text=True, timeout=120)
        st = (json.loads(out.stdout or "{}").get("streams") or [None])[0]
        if not st:
            return {"available": False, "reason": (out.stderr or "no video stream").strip()[:300]}
        return {"available": True, "codec": st.get("codec_name"), "width": st.get("width"),
                "height": st.get("height"), "r_frame_rate": st.get("r_frame_rate"),
                "avg_frame_rate": st.get("avg_frame_rate"),
                "frame_count": int(st["nb_read_frames"]) if str(st.get("nb_read_frames", "")).isdigit() else None,
                "duration": float(st["duration"]) if st.get("duration") not in (None, "N/A") else None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"available": False, "reason": repr(e)[:300]}


class Uploader:
    def __init__(self, endpoints, chunk_size=CHUNK_SIZE_DEFAULT, timeout=4.0, max_seconds=300.0,
                 backoff_base=0.2, backoff_cap=3.0, failover_after=3, seed=None, log=None):
        self.endpoints = [e.rstrip("/") for e in endpoints]
        self.ep = 0
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.max_seconds = max_seconds
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.failover_after = failover_after
        self.rng = random.Random(seed)
        self.log = log or (lambda *a: None)
        self.stats = {"requests": 0, "request_failures": 0, "timeouts": 0, "chunk_retries": 0,
                      "chunks_rejected_by_server": 0, "bytes_sent": 0, "bytes_resent": 0,
                      "resumes": 0, "failovers": 0, "endpoint_used": {}}
        self.events = []
        self._consecutive = 0

    # -- low level ---------------------------------------------------------
    def _note(self, kind, **kw):
        rec = {"t": iso(), "event": kind}
        rec.update(kw)
        self.events.append(rec)
        self.log(kind, kw)

    def _request(self, method, path, body=None, headers=None):
        """One HTTP request on a fresh connection. Returns (status, json)."""
        base = self.endpoints[self.ep]
        u = urlparse(base)
        self.stats["requests"] += 1
        self.stats["endpoint_used"][base] = self.stats["endpoint_used"].get(base, 0) + 1
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        try:
            h = {"Connection": "close"}
            h.update(headers or {})
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            raw = r.read()
            self._consecutive = 0
            try:
                return r.status, json.loads(raw or b"{}")
            except ValueError:
                return r.status, {"raw": raw[:200].decode("latin1")}
        except (OSError, http.client.HTTPException) as e:
            self.stats["request_failures"] += 1
            if isinstance(e, TimeoutError) or "timed out" in str(e):
                self.stats["timeouts"] += 1
            self._consecutive += 1
            self._note("request_failed", method=method, path=path, endpoint=base, error=type(e).__name__)
            if self._consecutive >= self.failover_after and len(self.endpoints) > 1:
                self.ep = (self.ep + 1) % len(self.endpoints)
                self._consecutive = 0
                self.stats["failovers"] += 1
                self._note("failover", to=self.endpoints[self.ep])
            raise
        finally:
            conn.close()

    def _backoff(self, attempt):
        d = self.rng.uniform(0, min(self.backoff_cap, self.backoff_base * (2 ** attempt)))
        time.sleep(d)

    def _retrying(self, deadline, fn):
        attempt = 0
        while True:
            try:
                return fn()
            except (OSError, http.client.HTTPException):
                if time.time() > deadline:
                    raise TransferFailed("deadline exceeded: network unavailable")
                self._backoff(attempt)
                attempt += 1

    # -- upload ------------------------------------------------------------
    def upload(self, path):
        t0 = time.time()
        deadline = t0 + self.max_seconds
        size = os.path.getsize(path)
        sha = sha256_file(path)
        chunks = []
        with open(path, "rb") as f:
            while True:
                b = f.read(self.chunk_size)
                if not b:
                    break
                chunks.append(sha256_bytes(b))
        manifest = {"filename": os.path.basename(path), "size": size, "sha256": sha,
                    "chunk_size": self.chunk_size, "chunks": chunks,
                    "source_manifest": probe_source(path)}
        body = json.dumps(manifest).encode()
        result = {"file": path, "size": size, "sha256": sha, "total_chunks": len(chunks),
                  "started": iso(t0), "final_status": "FAILED", "server_state": None}
        try:
            code, st = self._retrying(deadline, lambda: self._request(
                "POST", "/uploads", body, {"Content-Type": "application/json"}))
            if code not in (200, 201):
                raise TransferFailed("session refused: %s %s" % (code, st))
            uid = st["upload_id"]
            result["upload_id"] = uid
            if st.get("received_chunks"):
                self.stats["resumes"] += 1
                self._note("resume", already_on_server=st["received_chunks"])
            sent_once = set()
            with open(path, "rb") as f:
                while st["state"] not in VERIFIED_STATES:
                    if st["state"] in ("REJECTED", "EXPIRED"):
                        raise TransferFailed("server state %s: %s" % (st["state"], st.get("reject_reason")))
                    missing = st["missing"]
                    if not missing:
                        code, st2 = self._retrying(deadline, lambda: self._request(
                            "POST", "/uploads/%s/complete" % uid, b""))
                        if code == 200:
                            st = st2
                            continue
                        if code == 422:
                            raise TransferFailed("server rejected file: %s" % st2.get("reject_reason"))
                        st = self._retrying(deadline, lambda: self._request("GET", "/uploads/" + uid))[1]
                        continue
                    need_resync = False
                    for n in missing:
                        f.seek(n * self.chunk_size)
                        data = f.read(self.chunk_size)
                        attempt = 0
                        while True:
                            if time.time() > deadline:
                                raise TransferFailed("deadline exceeded after %d/%d chunks" % (
                                    len(chunks) - len(missing), len(chunks)))
                            try:
                                if n in sent_once:
                                    self.stats["bytes_resent"] += len(data)
                                sent_once.add(n)
                                self.stats["bytes_sent"] += len(data)
                                code, r = self._request(
                                    "PUT", "/uploads/%s/chunks/%d" % (uid, n), data,
                                    {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": chunks[n]})
                            except (OSError, http.client.HTTPException):
                                self.stats["chunk_retries"] += 1
                                self._backoff(attempt)
                                attempt += 1
                                continue
                            if code == 200:
                                break
                            if code == 422:  # damaged in transit: resend the same chunk
                                self.stats["chunks_rejected_by_server"] += 1
                                self.stats["chunk_retries"] += 1
                                self._note("chunk_rejected", n=n)
                                self._backoff(attempt)
                                attempt += 1
                                continue
                            need_resync = True
                            break
                        if need_resync:
                            break
                    # Always re-sync with the server: it decides what is still missing.
                    st = self._retrying(deadline, lambda: self._request("GET", "/uploads/" + uid))[1]
                    if "state" not in st:
                        raise TransferFailed("unexpected status response: %s" % st)
            result["final_status"] = "COMPLETE"
            result["server_state"] = st["state"]
        except TransferFailed as e:
            result["error"] = str(e)
            self._note("transfer_failed", error=str(e))
            try:
                if result.get("upload_id"):
                    result["server_state"] = self._request("GET", "/uploads/" + result["upload_id"])[1].get("state")
            except (OSError, http.client.HTTPException):
                result["server_state"] = "UNREACHABLE"
        result["elapsed_seconds"] = round(time.time() - t0, 2)
        result["stats"] = self.stats
        result["events"] = self.events[-200:]
        return result

    def wait_report(self, uid, timeout=120.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code, st = self._request("GET", "/uploads/" + uid)
                if st.get("state") in ("ANALYZED", "ANALYSIS_FAILED"):
                    code, rep = self._request("GET", "/uploads/%s/report" % uid)
                    return rep if code == 200 else {"error": "analysis_failed", "state": st.get("state")}
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.3)
        return {"error": "report_timeout"}


def main():
    ap = argparse.ArgumentParser(description="Reliable video upload client")
    ap.add_argument("file")
    ap.add_argument("--server", action="append", required=True,
                    help="backend URL; repeat to add failover paths")
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE_DEFAULT)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--max-seconds", type=float, default=300.0)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--out", help="write transfer log JSON here")
    a = ap.parse_args()
    up = Uploader(a.server, a.chunk_size, a.timeout, a.max_seconds,
                  log=lambda k, kw: print("  [%s] %s" % (k, kw)))
    res = up.upload(a.file)
    print("TRANSFER %s  server_state=%s  %.1fs  retries=%d  failovers=%d" % (
        res["final_status"], res["server_state"], res["elapsed_seconds"],
        res["stats"]["chunk_retries"], res["stats"]["failovers"]))
    if res.get("error"):
        print("  reason:", res["error"])
    if res["final_status"] == "COMPLETE" and not a.no_report:
        rep = up.wait_report(res["upload_id"])
        res["report"] = rep
        print("INTEGRITY VERDICT:", rep.get("verdict", rep.get("error")))
        print("  report: %s/uploads/%s/report.html" % (up.endpoints[up.ep], res["upload_id"]))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)
    raise SystemExit(0 if res["final_status"] == "COMPLETE" else 2)


if __name__ == "__main__":
    main()
