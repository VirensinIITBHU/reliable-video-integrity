#!/usr/bin/env python3
"""End-to-end evidence run: every transfer and integrity scenario, one command.

    python run_all.py            # full run, writes ./evidence/
                                 # (+ section C for any real clips placed in ./samples/)
    python run_all.py --quick    # skip the slowest network scenarios

Each scenario starts its own backend (fresh storage) and, where needed, a seeded
chaos proxy, runs the real client against it and checks the outcome.
"""
import argparse
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

from rvi import testvideos
from rvi.chaos import ChaosProxy, Profile
from rvi.client import Uploader
from rvi.common import sha256_bytes, sha256_file
from rvi.server import make_server

ROOT = os.path.dirname(os.path.abspath(__file__))
EVID = os.path.join(ROOT, "evidence")
VID = os.path.join(ROOT, "test_videos")
SAMPLES = os.path.join(ROOT, "samples")   # optional: real footage, not shipped in the repo
SEED = 2026
RESULTS = []


class Env:
    """A fresh backend (+ optional proxies) for one scenario."""

    def __init__(self, analyze=False, stall_timeout=2.0):
        self.data = tempfile.mkdtemp(prefix="rvi-")
        self.httpd = make_server(self.data, port=0, analyze=analyze, stall_timeout=stall_timeout)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.proxies = []

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def proxy(self, **kw):
        px = ChaosProxy(0, ("127.0.0.1", self.port), seed=SEED, **kw).start()
        self.proxies.append(px)
        return px, "http://127.0.0.1:%d" % px.port

    def verified_files(self):
        return [f for f in os.listdir(os.path.join(self.data, "verified")) if not f.endswith(".assembling")]

    def verified_hash(self):
        fs = self.verified_files()
        return sha256_file(os.path.join(self.data, "verified", fs[0])) if fs else None

    def close(self):
        for px in self.proxies:
            px.stop()
        self.httpd.store.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.data, ignore_errors=True)


def save_log(res, name):
    """Write a transfer log with repo-relative paths (no local directory names)."""
    res = dict(res, file=os.path.relpath(res["file"], ROOT))
    json.dump(res, open(os.path.join(EVID, "transfer_logs", name), "w"), indent=2)


def raw(url, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
    c.request(method, path, body=body)
    r = c.getresponse()
    out = (r.status, json.loads(r.read() or b"{}"))
    c.close()
    return out


def manifest(path, chunk=256 * 1024):
    data = open(path, "rb").read()
    parts = [data[i:i + chunk] for i in range(0, len(data), chunk)]
    return parts, {"filename": os.path.basename(path), "size": len(data), "sha256": sha256_bytes(data),
                   "chunk_size": chunk, "chunks": [sha256_bytes(p) for p in parts]}


def record(sid, name, expected, ok, observed, **extra):
    row = {"id": sid, "scenario": name, "expected": expected, "observed": observed, "pass": bool(ok)}
    row.update(extra)
    RESULTS.append(row)
    print("  [%s] %-4s %-46s %s" % ("PASS" if ok else "FAIL", sid, name, observed))
    return row


def transfer_case(sid, name, expected, proxy_kw=None, second_kw=None, client_kw=None, check=None, file="clean.mp4"):
    env = Env()
    try:
        eps = [env.url]
        pxs = []
        if proxy_kw is not None:
            px, u = env.proxy(**proxy_kw)
            pxs, eps = [px], [u]
            if second_kw is not None:
                px2, u2 = env.proxy(**second_kw)
                pxs.append(px2)
                eps.append(u2)
        kw = {"timeout": 2.0, "max_seconds": 90.0, "seed": SEED}
        kw.update(client_kw or {})
        up = Uploader(eps, **kw)
        path = os.path.join(VID, file)
        res = up.upload(path)
        st = res["stats"]
        intact = env.verified_hash() == res["sha256"]
        ok = res["final_status"] == "COMPLETE" and intact
        if check:
            ok = ok and check(res, pxs)
        obs = "%s, byte-perfect=%s, %.1fs, retries=%d, timeouts=%d, failovers=%d, resent=%dKB" % (
            res["final_status"], intact, res["elapsed_seconds"], st["chunk_retries"], st["timeouts"],
            st["failovers"], st["bytes_resent"] // 1024)
        res["proxy_stats"] = [p.stats for p in pxs]
        res["signal_trace"] = [p.trace[:400] for p in pxs]
        save_log(res, sid + ".json")
        return record(sid, name, expected, ok, obs)
    finally:
        env.close()


def t_premature_finalize():
    env = Env()
    try:
        parts, man = manifest(os.path.join(VID, "clean.mp4"))
        _, st = raw(env.url, "POST", "/uploads", json.dumps(man).encode())
        uid = st["upload_id"]
        for n in range(len(parts) // 2):
            raw(env.url, "PUT", "/uploads/%s/chunks/%d" % (uid, n), parts[n])
        code, r = raw(env.url, "POST", "/uploads/%s/complete" % uid, b"")
        _, st = raw(env.url, "GET", "/uploads/" + uid)
        ok = code == 409 and r.get("error") == "incomplete" and st["state"] == "UPLOADING" and not env.verified_files()
        record("T7", "Partial upload, finalize attempted", "refused, nothing accepted", ok,
               "HTTP %d %s, %d chunks missing, state=%s, accepted files=%d" % (
                   code, r.get("error"), len(r.get("missing", [])), st["state"], len(env.verified_files())))
    finally:
        env.close()


def t_tampered():
    env = Env()
    try:
        parts, man = manifest(os.path.join(VID, "clean.mp4"))
        _, st = raw(env.url, "POST", "/uploads", json.dumps(man).encode())
        uid = st["upload_id"]
        for n, p in enumerate(parts):
            raw(env.url, "PUT", "/uploads/%s/chunks/%d" % (uid, n), p)
        # a chunk is damaged AFTER it passed its own check (disk fault / tampering)
        cp = os.path.join(env.data, "sessions", uid, "chunks", "000003.part")
        b = bytearray(open(cp, "rb").read())
        b[1000] ^= 0xFF
        open(cp, "wb").write(bytes(b))
        code, r = raw(env.url, "POST", "/uploads/%s/complete" % uid, b"")
        ok = code == 422 and r["state"] == "REJECTED" and not env.verified_files()
        record("T8", "Altered data at rest before finalize", "REJECTED by whole-file SHA-256", ok,
               "HTTP %d state=%s (%s), accepted files=%d" % (code, r["state"], r.get("reject_reason"), len(env.verified_files())))
        # a wrong chunk sent directly is refused at the door
        _, st2 = raw(env.url, "POST", "/uploads", json.dumps(man).encode())  # REJECTED -> fresh session
        code, r = raw(env.url, "PUT", "/uploads/%s/chunks/0" % st2["upload_id"], parts[0][:-1] + b"\x00")
        record("T8b", "Altered chunk sent by client", "chunk refused (422)", code == 422,
               "HTTP %d %s" % (code, r.get("error")))
    finally:
        env.close()


def t_outage():
    env = Env(stall_timeout=1.5)
    try:
        px, u = env.proxy(profile=Profile([(1.2, 1.0), (999, 0.0)], loop=False), max_bw=500_000)
        up = Uploader([u], timeout=1.0, max_seconds=5.0, seed=SEED)
        res = up.upload(os.path.join(VID, "clean.mp4"))
        time.sleep(2.2)
        _, st = raw(env.url, "GET", "/uploads/" + res["upload_id"])
        ok = (res["final_status"] == "FAILED" and st["state"] == "STALLED" and not env.verified_files()
              and 0 < st["received_chunks"] < st["total_chunks"])
        save_log(res, "T9.json")
        record("T9", "Network never returns (permanent outage)", "client FAILED, server STALLED, nothing accepted", ok,
               "client=%s, server=%s with %d/%d chunks, accepted files=%d" % (
                   res["final_status"], st["state"], st["received_chunks"], st["total_chunks"], len(env.verified_files())))
        # ...and the same upload resumes later from where it stopped
        up2 = Uploader([env.url], seed=SEED)
        res2 = up2.upload(os.path.join(VID, "clean.mp4"))
        ok2 = (res2["final_status"] == "COMPLETE" and up2.stats["resumes"] == 1
               and up2.stats["bytes_sent"] < res2["size"] and env.verified_hash() == res2["sha256"])
        record("T10", "New client process resumes the stalled upload", "resume, only missing chunks sent", ok2,
               "%s, resumed=%s, sent %dKB of %dKB, byte-perfect=%s" % (
                   res2["final_status"], up2.stats["resumes"] == 1, up2.stats["bytes_sent"] // 1024,
                   res2["size"] // 1024, env.verified_hash() == res2["sha256"]))
    finally:
        env.close()


def video_case(sid, file, name, expected, check, max_bw=4_000_000, max_seconds=90.0):
    env = Env(analyze=True)
    try:
        px, u = env.proxy(profile=Profile.fluctuating(SEED, lo=0.5), max_bw=max_bw, reset_rate=0.2)
        up = Uploader([u], timeout=2.0, max_seconds=max_seconds, seed=SEED)
        res = up.upload(os.path.join(VID, file))
        rep = up.wait_report(res["upload_id"]) if res["final_status"] == "COMPLETE" else {}
        ok, obs, extra = check(res, rep)
        stem = os.path.basename(file).rsplit(".", 1)[0]
        json.dump(rep, open(os.path.join(EVID, "reports", stem + ".report.json"), "w"), indent=2)
        hp = os.path.join(env.data, "sessions", res.get("upload_id", "x"), "report.html")
        if os.path.exists(hp):
            shutil.copy(hp, os.path.join(EVID, "reports", stem + ".report.html"))
        record(sid, name, expected, ok and res["final_status"] == "COMPLETE",
               "transfer=%s; verdict=%s; %s" % (res["final_status"], rep.get("verdict"), obs), **extra)
    finally:
        env.close()


def score(found, truth, tol=0.05):
    """Match detections to ground truth by (time, count). Returns tp, fp, fn."""
    truth = list(truth)
    tp = 0
    for t, c in found:
        hit = next((g for g in truth if abs(g[0] - t) <= tol and g[1] == c), None)
        if hit:
            truth.remove(hit)
            tp += 1
    return tp, len(found) - tp, len(truth)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    shutil.rmtree(EVID, ignore_errors=True)
    os.makedirs(os.path.join(EVID, "transfer_logs"))
    os.makedirs(os.path.join(EVID, "reports"))
    print("Generating test videos with known ground truth ...")
    truth = testvideos.generate(VID)

    print("\nA. Transfer reliability")
    transfer_case("T1", "Stable connection (100%)", "COMPLETE, no retries", {"profile": Profile.constant(1.0)},
                  check=lambda r, p: r["stats"]["chunk_retries"] == 0)
    transfer_case("T2", "Fluctuating signal 20%-100% + resets + stalls", "COMPLETE despite failures",
                  {"profile": Profile.fluctuating(SEED), "max_bw": 2_500_000, "stall_rate": 0.4, "reset_rate": 0.8,
                   "fault_window": 50_000},
                  client_kw={"chunk_size": 64 * 1024},  # more requests -> more exposure to the bad link
                  check=lambda r, p: (r["stats"]["chunk_retries"] >= 3 and p[0].stats["resets"] > 0
                                      and min(x[1] for x in p[0].trace) <= 0.35))
    transfer_case("T3", "Temporary disconnection (3 s blackout) + reconnect", "COMPLETE after reconnect",
                  {"profile": Profile([(1.0, 1.0), (3.0, 0.0), (999, 1.0)], loop=False), "max_bw": 500_000},
                  check=lambda r, p: p[0].stats["blackout_drops"] > 0 and r["stats"]["request_failures"] > 0)
    if not a.quick:
        transfer_case("T4", "Stalled requests -> client timeouts", "COMPLETE, timeouts recovered",
                      {"profile": Profile.constant(0.4), "max_bw": 6_000_000, "stall_rate": 0.8},
                      client_kw={"timeout": 1.5}, check=lambda r, p: r["stats"]["timeouts"] > 0)
    transfer_case("T5", "Connection switching (path A dies -> path B)", "COMPLETE via failover",
                  {"profile": Profile([(1.0, 1.0), (999, 0.0)], loop=False), "max_bw": 500_000},
                  second_kw={"profile": Profile.constant(0.7)},
                  check=lambda r, p: r["stats"]["failovers"] >= 1 and len(r["stats"]["endpoint_used"]) == 2)
    transfer_case("T6", "Bytes corrupted in transit", "bad chunks refused + resent, COMPLETE",
                  {"profile": Profile.constant(1.0), "corrupt_rate": 0.03},
                  check=lambda r, p: p[0].stats["corrupted_bytes"] > 0 and r["stats"]["chunks_rejected_by_server"] > 0)
    t_premature_finalize()
    t_tampered()
    t_outage()

    print("\nB. Video validation and frame integrity (uploaded over a fluctuating link)")

    def clean(res, rep):
        return rep.get("verdict") == "NO_ANOMALIES_DETECTED", "anomalies=%d" % len(rep.get("anomalies", [])), {}

    def dropped(res, rep):
        found = [(x["start_time"], x["count"]) for x in rep["anomalies"] if x["type"] == "missing_frames"]
        gt = [(g["start_time"], g["missing"]) for g in truth["dropped_vfr.mp4"]["gaps"]]
        tp, fp, fn = score(found, gt)
        other = [x for x in rep["anomalies"] if x["type"] != "missing_frames"]
        return (tp == len(gt) and fp == 0 and fn == 0 and not other,
                "gaps found %d/%d, false alarms %d, missed %d" % (tp, len(gt), fp + len(other), fn),
                {"detection": {"tp": tp, "fp": fp + len(other), "fn": fn}})

    def duplicated(res, rep):
        found = [(x["start_time"], x["count"]) for x in rep["anomalies"] if x["type"] == "duplicated_frames"]
        gt = [((g["first_index"] + 1) / testvideos.FPS, g["repeats"]) for g in truth["duplicated.mp4"]["runs"]]
        tp, fp, fn = score(found, gt)
        other = [x for x in rep["anomalies"] if x["type"] != "duplicated_frames"]
        return (tp == len(gt) and fp == 0 and fn == 0 and not other,
                "runs found %d/%d, false alarms %d, missed %d" % (tp, len(gt), fp + len(other), fn),
                {"detection": {"tp": tp, "fp": fp + len(other), "fn": fn}})

    def hidden(res, rep):
        hi = [x for x in rep["anomalies"] if x["confidence"] != "low"]
        lo = [x for x in rep["anomalies"] if x["type"] == "motion_discontinuity"]
        return (rep.get("verdict") == "SUSPECTED_ANOMALIES" and not hi,
                "no timestamp evidence (as expected); %d low-confidence motion jumps flagged" % len(lo), {})

    def corrupt(res, rep):
        v = rep.get("validation", {})
        return (v.get("status") == "DEGRADED" and rep.get("verdict") == "ANOMALIES_DETECTED",
                "validation=%s, decoder errors=%s" % (v.get("status"), (rep.get("decode") or {}).get("error_count")), {})

    def truncated(res, rep):
        tr = [x for x in rep.get("anomalies", []) if x["type"] == "truncated_stream"]
        return (bool(tr) and rep.get("verdict") == "ANOMALIES_DETECTED",
                "truncated_stream: %s" % (("%d frames missing from %.1fs" % (tr[0]["count"], tr[0]["start_time"])) if tr else "none"), {})

    def invalid(res, rep):
        return rep.get("verdict") == "INVALID_VIDEO", "validation=%s (%s)" % (
            rep.get("validation", {}).get("status"), rep.get("validation", {}).get("reason")), {}

    video_case("V1", "clean.mp4", "Clean 30 s / 30 FPS video", "no anomalies", clean)
    video_case("V2", "dropped_vfr.mp4", "Frames dropped, timestamps preserved", "every gap located", dropped)
    video_case("V3", "duplicated.mp4", "Frozen / duplicated frames", "every run located", duplicated)
    video_case("V4", "dropped_cfr.mp4", "Frames dropped then re-encoded to constant rate",
               "only low-confidence hints (documented limit)", hidden)
    video_case("V5", "corrupt_source.mp4", "Video altered before upload (bitstream damage)",
               "transfer OK, decode errors reported", corrupt)
    video_case("V6", "truncated.mp4", "Truncated video file", "transfer OK, truncation reported", truncated)
    video_case("V7", "not_a_video.mp4", "Invalid file (random bytes named .mp4)", "INVALID_VIDEO, no crash", invalid)

    real = sorted(f for f in os.listdir(SAMPLES) if f.lower().endswith((".mp4", ".mov"))) if os.path.isdir(SAMPLES) else []
    if real:
        print("\nC. Real footage from ./samples (uploaded over a fluctuating link)")

        # No ground truth for real footage: we cannot assert "no anomalies". We assert the
        # pipeline is sound: decodable, and every CONFIRMED anomaly rests on hard evidence
        # (container timestamps, metadata or decoder output), never on frame content alone.
        HARD = ("container timestamps", "container metadata", "decoder")

        def real_check(res, rep):
            v = rep.get("validation", {})
            strong = [x for x in rep.get("anomalies", []) if x["confidence"] != "low"]
            weak = [x for x in rep.get("anomalies", []) if x["confidence"] == "low"]
            backed = all(x["evidence"].startswith(HARD) or x["type"] == "duplicated_frames" for x in strong)
            t = rep.get("timing", {})
            where = "; ".join("%s x%s at %.2f-%.2fs" % (x["type"], x["count"], x["start_time"], x["end_time"])
                              for x in strong if x.get("start_time") is not None)
            return (v.get("status") in ("VALID", "DEGRADED") and backed,
                    "validation=%s, %s frames at %.2f FPS over %.1fs, confirmed anomalies=%d%s, low-confidence hints=%d, warnings=%s" % (
                        v.get("status"), (rep.get("decode") or {}).get("frames_decoded"), t.get("nominal_fps", 0),
                        t.get("span_seconds", 0), len(strong), " (%s)" % where if where else "", len(weak),
                        [w["type"] for w in rep.get("warnings", [])] or "none"),
                    {"retries": res["stats"]["chunk_retries"], "size_bytes": res["size"]})

        for i, f in enumerate(real, 1):
            video_case("R%d" % i, os.path.join(SAMPLES, f), "Real clip %s" % f,
                       "byte-perfect transfer; every confirmed anomaly backed by timestamp/decoder evidence", real_check,
                       max_bw=12_000_000, max_seconds=300.0)

    passed = sum(r["pass"] for r in RESULTS)
    json.dump({"seed": SEED, "results": RESULTS}, open(os.path.join(EVID, "results.json"), "w"), indent=2)
    with open(os.path.join(EVID, "SUMMARY.md"), "w") as f:
        f.write("# Test evidence\n\nSeed `%d` · %d/%d scenarios passed · generated by `python run_all.py`\n\n" % (SEED, passed, len(RESULTS)))
        f.write("| ID | Scenario | Expected | Observed | Result |\n|---|---|---|---|---|\n")
        for r in RESULTS:
            f.write("| %s | %s | %s | %s | %s |\n" % (r["id"], r["scenario"], r["expected"], r["observed"], "PASS" if r["pass"] else "**FAIL**"))
    print("\n%d/%d scenarios passed in %.0fs. Evidence written to %s" % (passed, len(RESULTS), time.time() - t0, EVID))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
