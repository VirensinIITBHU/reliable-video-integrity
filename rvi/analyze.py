"""Video validation and 30 FPS frame-integrity analysis (ffprobe + ffmpeg only).

Every anomaly carries the EVIDENCE that supports it, a confidence level and an
attributed cause, because different evidence supports different conclusions:

  evidence tier                what it can prove
  ---------------------------  ------------------------------------------------
  container timestamps (PTS)   frames are missing between two source timestamps
  decoder errors               the bitstream is damaged / truncated / not a video
  frame content comparison     two consecutive frames are (near-)identical
  source manifest (reference)  the file equals what the client had before upload

A byte-perfect transfer proves only that backend bytes == client bytes. It says
nothing about frames lost at capture or encoding; those are judged from the
tiers above and reported separately from transfer status.
"""
import bisect
import json
import statistics
import subprocess

TARGET_FPS = 30
EXPECTED_DURATION = 30.0
TW, TH = 32, 18                 # thumbnail size used for content comparison
DUP_MAD = 0.20                  # mean abs gray-level difference below this = duplicate
JUMP_MIN, JUMP_MAX = 1.75, 6.0   # motion jump vs local median (weak evidence)
GAP_FACTOR = 1.5                # PTS delta > 1.5x nominal interval = missing frame(s)


def _run(cmd, timeout=300, binary=False):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout if binary else r.stdout.decode("utf-8", "replace")), r.stderr.decode("utf-8", "replace")


def _ratio(s):
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path):
    code, out, err = _run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    info = {"ok": False, "errors": [l for l in err.splitlines() if l.strip()][:20]}
    try:
        j = json.loads(out or "{}")
    except ValueError:
        j = {}
    vs = [s for s in j.get("streams", []) if s.get("codec_type") == "video"]
    if code != 0 or not vs:
        info["reason"] = "not a parseable media container" if code != 0 or not j.get("streams") else "no video stream"
        return info
    v, fmt = vs[0], j.get("format", {})
    dur = v.get("duration") or fmt.get("duration")
    info.update({
        "ok": True, "container": fmt.get("format_name"), "codec": v.get("codec_name"),
        "width": v.get("width"), "height": v.get("height"),
        "declared_fps": _ratio(v.get("r_frame_rate")), "average_fps": _ratio(v.get("avg_frame_rate")),
        "header_frame_count": int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None,
        "duration": float(dur) if dur not in (None, "N/A") else None,
        "size_bytes": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
    })
    return info


def read_timestamps(path):
    """Presentation timestamps of every decodable frame, in presentation order."""
    code, out, err = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "frame=pts_time,best_effort_timestamp_time,pict_type", "-of", "json", path])
    try:
        frames = json.loads(out or "{}").get("frames", [])
    except ValueError:
        frames = []
    pts, types = [], []
    for f in frames:
        t = f.get("pts_time") or f.get("best_effort_timestamp_time")
        try:
            pts.append(float(t))
            types.append(f.get("pict_type", "?"))
        except (TypeError, ValueError):
            continue
    return pts, types, [l for l in err.splitlines() if l.strip()]


def decode_thumbnails(path):
    """Fully decode the video once: collect decoder errors + tiny gray frames."""
    base = ["ffmpeg", "-hide_banner", "-v", "error", "-i", path, "-map", "0:v:0"]
    tail = ["-vf", "scale=%d:%d,format=gray" % (TW, TH), "-f", "rawvideo", "-"]
    code, out, err = _run(base + ["-fps_mode", "passthrough"] + tail, binary=True)
    if "fps_mode" in err and not out:  # older ffmpeg
        code, out, err = _run(base + ["-vsync", "0"] + tail, binary=True)
    n = TW * TH
    frames = [out[i:i + n] for i in range(0, len(out) - n + 1, n)]
    return frames, [l for l in err.splitlines() if l.strip()], code


def _mad(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _runs(flags):
    """Group consecutive True indices into (start, end) runs."""
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def resample_map(pts, fps=TARGET_FPS):
    """Map a 30 FPS grid onto the source frames (nearest timestamp).

    Returns one entry per 30 FPS slot and classifies WHY a slot repeats a frame:
      unique                -> a distinct source frame is available for the slot
      conversion_duplicate  -> the source simply has fewer than 30 FPS here
      gap_fill              -> no source frame within half a slot: a real gap
    and which source frames a 30 FPS conversion would omit (source > 30 FPS).
    """
    if not pts:
        return {"slots": [], "summary": {}}
    t0, t_end = pts[0], pts[-1]
    n = int(round((t_end - t0) * fps)) + 1
    half = 0.5 / fps + 1e-6
    slots, used, prev, prev_ok = [], set(), None, False
    for k in range(n):
        t = t0 + k / fps
        i = bisect.bisect_left(pts, t)
        cand = [c for c in (i - 1, i) if 0 <= c < len(pts)]
        j = min(cand, key=lambda c: abs(pts[c] - t))
        if abs(pts[j] - t) > half:
            status = "gap_fill"
        elif j == prev and prev_ok:
            status = "conversion_duplicate"
        else:
            status = "unique"
        used.add(j)
        prev, prev_ok = j, status != "gap_fill"
        slots.append({"i": k, "t": round(t, 6), "src": j, "src_pts": round(pts[j], 6), "status": status})
    omitted = [i for i in range(len(pts)) if i not in used]
    counts = {}
    for s in slots:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"slots": slots, "omitted_source_frames": omitted,
            "summary": {"target_fps": fps, "output_frames": n, "source_frames": len(pts),
                        "unique": counts.get("unique", 0),
                        "conversion_duplicate": counts.get("conversion_duplicate", 0),
                        "gap_fill": counts.get("gap_fill", 0),
                        "omitted_by_conversion": len(omitted)}}


def analyze_video(path, source_manifest=None):
    res = {"file": path, "target_fps": TARGET_FPS, "anomalies": [], "warnings": [], "limitations": []}
    info = probe(path)
    res["probe"] = info
    if not info["ok"]:
        res["validation"] = {"status": "INVALID", "reason": info.get("reason"), "errors": info["errors"]}
        return res

    pts, types, ts_err = read_timestamps(path)
    thumbs, dec_err, dec_code = decode_thumbnails(path)
    errors = list(dict.fromkeys(dec_err + ts_err))
    res["decode"] = {"frames_decoded": len(thumbs), "frames_with_timestamps": len(pts),
                     "error_count": len(dec_err), "errors": errors[:40], "exit_code": dec_code}
    if not thumbs or not pts:
        res["validation"] = {"status": "INVALID", "reason": "container parsed but no frame could be decoded",
                             "errors": errors[:40]}
        return res
    res["validation"] = {"status": "DEGRADED" if dec_err else "VALID",
                         "reason": "decoder reported %d error line(s)" % len(dec_err) if dec_err else "decoded cleanly"}
    damaged = bool(dec_err)

    # ---- timestamp order ---------------------------------------------------
    backwards = [i for i in range(len(pts) - 1) if pts[i + 1] <= pts[i]]
    if backwards:
        res["anomalies"].append({
            "type": "non_monotonic_timestamps", "count": len(backwards),
            "start_time": round(min(pts[i + 1] for i in backwards), 4),
            "end_time": round(max(pts[i] for i in backwards), 4),
            "source_frames": [backwards[0], backwards[-1] + 1],
            "evidence": "container timestamps: PTS goes backwards or repeats at %d point(s), first %.4f -> %.4f"
                        % (len(backwards), pts[backwards[0]], pts[backwards[0] + 1]),
            "confidence": "high",
            "cause": "bitstream damage (decoder errors present)" if dec_err else "encoding/muxing fault"})
        pts = sorted(set(pts))

    # ---- duration / frame-count sanity -----------------------------------
    span = pts[-1] - pts[0]
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    nominal = statistics.median(deltas) if deltas else 1.0 / TARGET_FPS
    res["timing"] = {"first_pts": pts[0], "last_pts": pts[-1], "span_seconds": round(span + nominal, 4),
                     "nominal_frame_interval": round(nominal, 6),
                     "nominal_fps": round(1 / nominal, 3) if nominal > 0 else None,
                     "max_interval": round(max(deltas), 6) if deltas else None}
    if abs((span + nominal) - EXPECTED_DURATION) > 0.5:
        res["warnings"].append({"type": "duration_out_of_spec",
                                "detail": "video spans %.2fs, expected %.0fs" % (span + nominal, EXPECTED_DURATION)})
    if info.get("declared_fps") and info.get("average_fps") and abs(info["declared_fps"] - info["average_fps"]) > 0.05:
        res["warnings"].append({"type": "variable_frame_rate",
                                "detail": "declared %.3f FPS but average %.3f FPS: frames are not evenly spaced"
                                          % (info["declared_fps"], info["average_fps"])})

    hdr_n, hdr_d = info.get("header_frame_count"), info.get("duration")
    short_n = hdr_n and len(thumbs) < hdr_n - 1
    short_d = hdr_d and (span + nominal) < hdr_d - max(0.25, 3 * nominal)
    if short_d:
        res["anomalies"].append({
            "type": "truncated_stream", "count": (hdr_n - len(thumbs)) if hdr_n else None,
            "start_time": round(pts[-1] + nominal, 4), "end_time": round(hdr_d, 4),
            "source_frames": [len(thumbs) - 1, (hdr_n - 1) if hdr_n else None],
            "evidence": "container metadata vs decode: header declares %s frames / %.2fs, but only %d frames (%.2fs) are decodable"
                        % (hdr_n, hdr_d, len(thumbs), span + nominal),
            "confidence": "high",
            "cause": "the source file was already truncated or damaged before upload "
                     "(the transfer was byte-verified against the client's own hash)"})
    elif short_n:
        res["anomalies"].append({
            "type": "undecodable_frames", "count": hdr_n - len(thumbs),
            "start_time": None, "end_time": None, "source_frames": None,
            "evidence": "container metadata vs decode: header declares %d frames but only %d could be decoded; "
                        "see missing_frames entries for where the timestamps show holes" % (hdr_n, len(thumbs)),
            "confidence": "high",
            "cause": "bitstream damage in the source file (the transfer was byte-verified)"})

    cause_gap = ("bitstream damage: decoder errors are present, so frames here were likely undecodable"
                 if damaged else
                 "capture/encoding: the source file itself has no frame at these timestamps "
                 "(transfer was byte-verified, so this cannot be a transfer loss)")

    # ---- tier A: timestamp evidence --------------------------------------
    for i, d in enumerate(deltas):
        if d > GAP_FACTOR * nominal:
            miss = int(round(d / nominal)) - 1
            res["anomalies"].append({
                "type": "missing_frames", "count": miss,
                "start_time": round(pts[i] + nominal, 4), "end_time": round(pts[i + 1], 4),
                "source_frames": [i, i + 1],
                "slots_30fps": [int(round((pts[i] + nominal - pts[0]) * TARGET_FPS)),
                                int(round((pts[i + 1] - pts[0]) * TARGET_FPS)) - 1],
                "evidence": "container timestamps: gap of %.4fs between source frames %d and %d (nominal interval %.4fs)"
                            % (d, i, i + 1, nominal),
                "confidence": "high", "cause": cause_gap})

    # ---- tier C: content evidence ----------------------------------------
    mads = [_mad(a, b) for a, b in zip(thumbs, thumbs[1:])]
    med = statistics.median(mads) if mads else 0.0
    res["content"] = {"median_frame_difference": round(med, 4), "duplicate_threshold": DUP_MAD}
    static_scene = med < 1.0
    for a, b in _runs([m <= DUP_MAD for m in mads]):
        first, last = a + 1, b + 1          # frames first..last repeat frame `a`
        ta = pts[min(first, len(pts) - 1)]
        tb = pts[min(last, len(pts) - 1)]
        res["anomalies"].append({
            "type": "duplicated_frames", "count": last - first + 1,
            "start_time": round(ta, 4), "end_time": round(tb + nominal, 4), "source_frames": [first, last],
            "evidence": "frame content: frames %d-%d are identical to frame %d (mean abs difference <= %.2f; "
                        "typical motion in this video is %.2f)" % (first, last, a, DUP_MAD, med),
            "confidence": "low" if static_scene else "medium",
            "cause": "capture freeze or frame repetition during encoding; "
                     + ("the scene is nearly static, so this may be genuine still content"
                        if static_scene else "surrounding frames show motion, so a real still scene is unlikely")})
    # Weak evidence: a sudden jump in motion with no timestamp gap. This is the only
    # hint left when drops were hidden by re-encoding to constant frame rate.
    gap_frames = {a["source_frames"][1] for a in res["anomalies"] if a["type"] == "missing_frames"}
    for i, m in enumerate(mads):
        win = mads[max(0, i - 15):i] + mads[i + 1:i + 16]
        local = statistics.median(win) if win else 0.0
        if local < 0.3 or (i + 1) in gap_frames:
            continue
        ratio = m / local
        if JUMP_MIN <= ratio <= JUMP_MAX:
            t = pts[min(i + 1, len(pts) - 1)]
            res["anomalies"].append({
                "type": "motion_discontinuity", "count": max(1, int(round(ratio)) - 1),
                "start_time": round(t, 4), "end_time": round(t, 4), "source_frames": [i, i + 1],
                "evidence": "frame content: change between frames %d and %d is %.1fx the local typical motion, "
                            "but timestamps show no gap" % (i, i + 1, ratio),
                "confidence": "low",
                "cause": "possible frames dropped before a constant-rate re-encode (timestamps regenerated), "
                         "or simply fast motion / a scene change; cannot be confirmed without a reference"})
    if static_scene:
        res["limitations"].append("The video is nearly static, so duplicated frames cannot be told apart from real still content.")

    # ---- 30 FPS resampling map -------------------------------------------
    rm = resample_map(pts)
    res["resample_30fps"] = rm
    s = rm["summary"]
    if s.get("conversion_duplicate") or s.get("omitted_by_conversion"):
        res["warnings"].append({"type": "conversion_artifacts",
                                "detail": "sampling at 30 FPS repeats %d slot(s) and omits %d source frame(s) purely "
                                          "because the source is not 30 FPS; these are NOT counted as anomalies"
                                          % (s["conversion_duplicate"], s["omitted_by_conversion"])})

    # ---- tier D: reference evidence --------------------------------------
    ref = {"available": bool(source_manifest and source_manifest.get("available"))}
    if ref["available"]:
        ref["source_frame_count"] = source_manifest.get("frame_count")
        ref["backend_frame_count"] = len(thumbs)
        ref["match"] = source_manifest.get("frame_count") == len(thumbs)
        ref["conclusion"] = ("backend decodes the same number of frames the client had before upload: "
                             "every anomaly above pre-dates the transfer" if ref["match"] else
                             "frame count differs from the client's pre-upload probe")
    res["reference"] = ref

    res["limitations"] += [
        "Byte-perfect transfer proves the backend file equals the client file; it cannot prove the original recording had no dropped frames.",
        "If frames were dropped and the video was then re-encoded with regenerated constant-rate timestamps, the gap evidence is erased; "
        "without an external reference (e.g. capture-side frame counter) such drops are not detectable from timestamps.",
        "Duplicate detection compares 32x18 grayscale thumbnails; very subtle motion can look identical.",
    ]
    return res


if __name__ == "__main__":
    import sys
    r = analyze_video(sys.argv[1])
    r.get("resample_30fps", {}).pop("slots", None)
    print(json.dumps(r, indent=2))
