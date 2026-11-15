# Reliable Video Upload & Frame Integrity Detection

Confluentia 2026 · nSEPIA Technical Challenge (IIT BHU)

A fault-tolerant client → backend video transfer that survives a network swinging
between 20% and 100% signal, accepts a file **only** when it is proven byte-perfect,
and then analyses the video at 30 FPS for missing, duplicated and abnormal frames,
stating the evidence behind every conclusion.

**No dependencies:** Python 3.9+ standard library and `ffmpeg`/`ffprobe` on PATH.

## Quick start

```bash
python run_all.py          # 18 scenarios (~90 s), writes ./evidence/
                           # + one extra scenario per real clip in ./samples/
```

Manual three-terminal demo:

```bash
python -m rvi.server --port 8000                              # backend
python -m rvi.chaos  --listen 9000 --upstream 127.0.0.1:8000  # bad network
python -m rvi.client test_videos/dropped_vfr.mp4 --server http://127.0.0.1:9000
# then open the printed report.html URL
```

## Architecture

```
            manifest: file SHA-256 + SHA-256 of every chunk
 ┌────────┐      ┌──────────────┐      ┌─────────────────────────────────────┐
 │ client │ ───► │ chaos proxy  │ ───► │ backend                             │
 │        │ ◄─── │ 20–100%      │ ◄─── │  chunks/ (each hash-checked, atomic)│
 └────────┘      │ signal model │      │  └► assemble ► whole-file SHA-256   │
  retry, backoff └──────────────┘      │        │ pass            │ fail     │
  timeout, resume                      │     COMPLETE          REJECTED     │
  endpoint failover                    │        ▼                           │
                                       │  validate ► decode ► 30 FPS ► report│
                                       └─────────────────────────────────────┘
```

| File | Role |
|---|---|
| `rvi/server.py` | Upload API, session state machine, stall detection, verification gate |
| `rvi/client.py` | Chunked uploader: timeouts, backoff + jitter, resume, failover |
| `rvi/chaos.py` | Seeded network impairment proxy (signal strength → bandwidth, latency, stalls, resets, blackouts, corruption) |
| `rvi/analyze.py` | Validation, full decode, timestamp / content analysis, 30 FPS map |
| `rvi/report.py` | Integrity report (JSON + HTML timeline) |
| `rvi/testvideos.py` | Test videos with known ground truth |
| `run_all.py` | One-command evidence run |

## How the transfer stays reliable

| Problem | Mechanism |
|---|---|
| Low bandwidth, variable speed | Small independent chunks (256 KB); progress is never lost |
| Packet loss, resets, interruptions | Per-chunk retry with exponential backoff and jitter |
| Stalls, request timeouts | Every request has a timeout; the server discards half-received bodies |
| Temporary disconnection | Client re-asks the server which chunks it holds and sends only the rest |
| Client crash / restart | Upload id is derived from the file hash, so resume needs no local state |
| Connection switching | Multiple endpoints; repeated failures fail over to the next path |
| Corruption in transit | Each chunk is checked against its SHA-256 and refused if different |
| Partial or damaged file | Finalize is refused while chunks are missing; the assembled file must match the whole-file SHA-256 or the upload is `REJECTED` |
| Stalled / abandoned upload | Server marks `STALLED` after no progress, `EXPIRED` later |

Server states: `UPLOADING → STALLED → VERIFYING → COMPLETE | REJECTED | EXPIRED`,
then `ANALYZING → ANALYZED`. Analysis can start **only** from `COMPLETE`.
All writes are temp-file + fsync + rename, so a half-written file is never visible.

## How frame integrity is judged

The backend never mixes up *transfer* problems with *capture / encoding* problems.
A verified SHA-256 proves the backend file equals the client file and nothing more,
so every frame anomaly carries its own evidence, confidence and attributed cause:

| Evidence | Detects | Confidence |
|---|---|---|
| Container timestamps (PTS gap > 1.5× nominal interval) | Missing / skipped frames, with time range and count | High |
| Container metadata vs decode | Truncated stream, undecodable frames | High |
| Decoder error log | Bitstream damage, invalid file | High |
| Frame content (consecutive frames identical) | Duplicated / frozen frames | Medium (low if the scene is static) |
| Frame content (motion jump, no timestamp gap) | Possible drops hidden by re-encoding | Low |
| Client's pre-upload probe (reference) | Confirms anomalies pre-date the transfer | — |

**30 FPS sampling.** The source timestamps are mapped onto a 30 FPS grid. Each slot
records its index, time and source frame, and is labelled `unique`, `gap_fill`
(no source frame exists: a real gap) or `conversion_duplicate` (the source simply
has a different frame rate). Repeats and omissions caused only by conversion are
reported as warnings, **not** anomalies.

## Test evidence

`python run_all.py` → [`evidence/SUMMARY.md`](evidence/SUMMARY.md), per-scenario
transfer logs in `evidence/transfer_logs/` (each with the signal-strength trace
the proxy applied), and sample integrity reports in `evidence/reports/`
(open `dropped_vfr.report.html`, or `12943168_1440_2560_30fps.report.html` for real footage).

The committed evidence is a 25/25 run: 11 transfer scenarios (T1–T10, T8b),
7 ground-truth video scenarios (V1–V7) and 7 real clips (R1–R7).

See [`WRITEUP.md`](WRITEUP.md) for a one-page summary and a checklist against the problem statement.

Detection is measured against generated videos with known injected faults:
4/4 dropped-frame gaps and 3/3 duplicate runs located, 0 false alarms, 0 on the clean video.

**Real footage (R1–R7).** Seven real clips were each uploaded through the fluctuating
proxy and analysed: three stock clips (R1–R3) and four phone recordings (R4–R7).
All seven transferred byte-perfect and decoded cleanly.

- Stock clips: **0 confirmed anomalies**; the 11.2 s clip is correctly flagged `duration_out_of_spec`.
- Phone recordings (variable frame rate): container timestamps reveal real capture drops: a
  **1.67 s hole (49 frames) at 1.67–3.30 s** in R5, and 10 single-frame holes across R4, R5, R7.
  These are attributed to capture/encoding, because the transfer was byte-verified. R6 is clean.

Real clips have no ground truth, so their scenarios check that the pipeline is sound (byte-perfect,
decodable, every confirmed anomaly backed by timestamp/metadata/decoder evidence), not that the
clip is clean. The clips are not committed (`samples/` is git-ignored); their reports are in
`evidence/reports/`. Put any `.mp4` in `samples/` to repeat this.

Real footage changed two thresholds that synthetic video could not reveal:

| Threshold | Before | After | Why |
|---|---|---|---|
| Duplicate frame (mean abs gray diff, 0–255) | 0.20 | 0.05 | Sensor + encoder noise keeps consecutive real frames ≥ ~0.08 apart even in a still scene; truly repeated frames measure ~0.002. The old value raised 90 false duplicates on a static real clip. |
| Motion jump | any 1.75–6× spike | must also be **isolated** (both neighbours < 1.4× local median) | A hidden drop is one sudden jump; several elevated differences in a row are genuine motion. Cut false hints on real clips from 12 to 2. |
| Decoder errors | every ffmpeg error line | output-muxer lines excluded (kept as `pipeline_notes`) | Irregular phone timestamps make ffmpeg's *output* muxer warn while writing our thumbnails; this marked a good phone clip `DEGRADED`. |

## Limitations

- A byte-perfect transfer cannot prove the original recording had no dropped frames.
- If frames were dropped and the video was then re-encoded with regenerated
  constant-rate timestamps, the timestamp evidence is gone. Only a low-confidence
  motion hint remains (scenario V4); confirming it needs a capture-side reference.
- Duplicate detection cannot separate a frozen capture from a truly still scene.
- The chaos proxy works on TCP streams: packet loss appears as stalls, resets and
  slowdowns, not as individually dropped IP packets (`tc netem` would add that).
- Detection accuracy (true / false positives) is measured on synthetic video with known faults.
  On real footage there is no ground truth, so the real clips show only that clean recordings
  produce no confirmed anomalies.
- The low-confidence motion-jump hint overlaps with real motion: a hidden one-frame drop in the
  synthetic test scores 1.9× local motion, ordinary handheld motion in real clips up to ~1.8×.
  That is why two real clips get `SUSPECTED_ANOMALIES` (low-confidence hints only) and why the
  hint is never reported as a confirmed anomaly.
- The simulated fault timeline starts at the first client connection, so results do not depend on
  how fast the machine hashes the file; exact retry counts still vary slightly between machines.
- No authentication or TLS: this is a challenge prototype, not a hardened service.
