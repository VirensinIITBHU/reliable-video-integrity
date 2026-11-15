"""Integrity report: combines transfer status, validation and frame analysis."""
import html

from .common import iso

COLORS = {"missing_frames": "#d62728", "duplicated_frames": "#1f77b4", "truncated_stream": "#7f3b08",
          "motion_discontinuity": "#ff9f1c", "non_monotonic_timestamps": "#9467bd", "undecodable_frames": "#8c564b"}


def build_report(transfer_status, analysis):
    val = analysis["validation"]["status"]
    an = analysis.get("anomalies", [])
    strong = [a for a in an if a["confidence"] in ("high", "medium")]
    if val == "INVALID":
        verdict = "INVALID_VIDEO"
    elif strong or val == "DEGRADED":
        verdict = "ANOMALIES_DETECTED"
    elif an:
        verdict = "SUSPECTED_ANOMALIES"
    else:
        verdict = "NO_ANOMALIES_DETECTED"
    by_type = {}
    for a in an:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    return {
        "generated": iso(),
        "verdict": verdict,
        "transfer": {
            "status": "VERIFIED_BYTE_PERFECT",
            "upload_id": transfer_status["upload_id"], "filename": transfer_status["filename"],
            "size_bytes": transfer_status["size"], "sha256": transfer_status["sha256"],
            "chunks": transfer_status["total_chunks"], "server_counters": transfer_status["counters"],
            "state_history": transfer_status["state_history"],
            "meaning": "Every chunk and the whole file matched the client's SHA-256. "
                       "Any issue reported below therefore existed in the source file and is NOT a transfer error.",
        },
        "validation": analysis["validation"],
        "probe": analysis.get("probe"),
        "decode": analysis.get("decode"),
        "timing": analysis.get("timing"),
        "content": analysis.get("content"),
        "reference": analysis.get("reference"),
        "anomaly_summary": by_type,
        "anomalies": an,
        "warnings": analysis.get("warnings", []),
        "resample_30fps": analysis.get("resample_30fps"),
        "limitations": analysis.get("limitations", []),
    }


def _e(x):
    return html.escape(str(x))


def render_html(rep):
    an = rep["anomalies"]
    dur = max(30.0, (rep.get("timing") or {}).get("span_seconds") or 30.0)
    W = 900
    marks = []
    for a in an:
        if a.get("start_time") is None:
            continue
        x = a["start_time"] / dur * W
        w = max(3.0, ((a.get("end_time") or a["start_time"]) - a["start_time"]) / dur * W)
        marks.append('<rect x="%.1f" y="6" width="%.1f" height="28" fill="%s"><title>%s @ %.3fs</title></rect>'
                     % (x, w, COLORS.get(a["type"], "#555"), _e(a["type"]), a["start_time"]))
    ticks = "".join('<text x="%d" y="52" font-size="10" fill="#666" text-anchor="%s">%ds</text>'
                    % (i / dur * W, "start" if i == 0 else "end" if i / dur * W > W - 20 else "middle", i)
                    for i in range(0, int(dur) + 1, 5))
    legend = " ".join('<span class="chip" style="background:%s"></span>%s' % (c, _e(t)) for t, c in COLORS.items())
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            _e(a["type"]), _e(a.get("count", "")),
            "" if a.get("start_time") is None else "%.3f – %.3f" % (a["start_time"], a.get("end_time") or a["start_time"]),
            _e(a.get("source_frames") or ""), _e(a["confidence"]), _e(a["evidence"]), _e(a["cause"]))
        for a in an) or '<tr><td colspan="7">No anomalies detected.</td></tr>'
    t, v, d, s = rep["transfer"], rep["validation"], rep.get("decode") or {}, (rep.get("resample_30fps") or {}).get("summary") or {}
    ref = rep.get("reference") or {}
    vc = {"NO_ANOMALIES_DETECTED": "#2ca02c", "SUSPECTED_ANOMALIES": "#ff9f1c"}.get(rep["verdict"], "#d62728")
    return """<!doctype html><html><head><meta charset="utf-8"><title>Integrity report</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{font-family:system-ui,sans-serif;margin:24px;color:#222;max-width:1000px}
h1{font-size:22px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}
table{border-collapse:collapse;width:100%%;font-size:13px}td,th{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
th{background:#f5f5f5}.v{display:inline-block;padding:6px 12px;border-radius:6px;color:#fff;font-weight:600;background:%s}
.chip{display:inline-block;width:10px;height:10px;margin:0 4px 0 12px}.kv td:first-child{width:220px;color:#555}
svg{width:100%%;height:auto;background:#fafafa;border:1px solid #ddd}li{margin:4px 0;font-size:13px}code{font-size:12px}
</style></head><body>
<h1>Video integrity report</h1><p><span class="v">%s</span> &nbsp; generated %s</p>
<h2>1. Transfer</h2><table class="kv">
<tr><td>Status</td><td><b>%s</b></td></tr><tr><td>File</td><td>%s (%s bytes, %s chunks)</td></tr>
<tr><td>SHA-256</td><td><code>%s</code></td></tr><tr><td>Server counters</td><td>%s</td></tr>
<tr><td>What this proves</td><td>%s</td></tr></table>
<h2>2. Validation and decode</h2><table class="kv">
<tr><td>Validation</td><td><b>%s</b> — %s</td></tr><tr><td>Container / codec</td><td>%s</td></tr>
<tr><td>Frames decoded</td><td>%s</td></tr><tr><td>Decoder error lines</td><td>%s</td></tr>
<tr><td>Reference (client pre-upload probe)</td><td>%s</td></tr></table>
<h2>3. 30 FPS sampling</h2><table class="kv">
<tr><td>30 FPS slots</td><td>%s</td></tr><tr><td>Slots with their own source frame</td><td>%s</td></tr>
<tr><td>Slots with no source frame (real gaps)</td><td>%s</td></tr>
<tr><td>Repeats / omissions caused only by conversion</td><td>%s repeated, %s omitted (not anomalies)</td></tr></table>
<h2>4. Anomaly timeline</h2><svg viewBox="0 0 %d 58"><rect x="0" y="6" width="%d" height="28" fill="#e8f5e9"/>%s%s</svg>
<p style="font-size:12px">%s</p>
<h2>5. Anomalies (%d)</h2><table><tr><th>Type</th><th>Count</th><th>Time (s)</th><th>Source frames</th><th>Confidence</th><th>Evidence</th><th>Attributed cause</th></tr>%s</table>
<h2>6. Warnings</h2><ul>%s</ul><h2>7. Limitations</h2><ul>%s</ul>
</body></html>""" % (
        vc, _e(rep["verdict"]), _e(rep["generated"]),
        _e(t["status"]), _e(t["filename"]), t["size_bytes"], t["chunks"], _e(t["sha256"]),
        _e(", ".join("%s=%s" % (k.replace("_", " "), v) for k, v in (t["server_counters"] or {}).items()) if isinstance(t["server_counters"], dict) else t["server_counters"]), _e(t["meaning"]),
        _e(v["status"]), _e(v.get("reason", "")),
        _e("%s / %s %sx%s" % ((rep.get("probe") or {}).get("container"), (rep.get("probe") or {}).get("codec"),
                              (rep.get("probe") or {}).get("width"), (rep.get("probe") or {}).get("height"))),
        _e(d.get("frames_decoded", "–")), _e(d.get("error_count", "–")),
        _e(ref.get("conclusion", "not available")),
        _e(s.get("output_frames", "–")), _e(s.get("unique", "–")), _e(s.get("gap_fill", "–")),
        _e(s.get("conversion_duplicate", "–")), _e(s.get("omitted_by_conversion", "–")),
        W, W, "".join(marks), ticks, legend, len(an), rows,
        "".join("<li><b>%s</b>: %s</li>" % (_e(w["type"]), _e(w["detail"])) for w in rep["warnings"]) or "<li>None</li>",
        "".join("<li>%s</li>" % _e(l) for l in rep["limitations"]) or "<li>None</li>")
