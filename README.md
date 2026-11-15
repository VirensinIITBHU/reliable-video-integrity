# Reliable Video Upload & Frame Integrity Detection

Confluentia 2026 · nSEPIA Technical Challenge (IIT BHU)

A fault-tolerant client → backend video transfer that survives a network swinging
between 20% and 100% signal, accepts a file **only** when it is proven byte-perfect,
and then analyses the video at 30 FPS for missing, duplicated and abnormal frames,
stating the evidence behind every conclusion.

**No dependencies:** Python 3.9+ standard library and `ffmpeg`/`ffprobe` on PATH.

## Quick start

```bash
python run_all.py          # runs all 18 scenarios (~80 s), writes ./evidence/
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
transfer logs in `evidence/transfer_logs/`, and sample integrity reports in
`evidence/reports/` (open `dropped_vfr.report.html`).

Detection is measured against generated videos with known injected faults:
4/4 dropped-frame gaps and 3/3 duplicate runs located, 0 false alarms, 0 on the clean video.

## Limitations

- A byte-perfect transfer cannot prove the original recording had no dropped frames.
- If frames were dropped and the video was then re-encoded with regenerated
  constant-rate timestamps, the timestamp evidence is gone. Only a low-confidence
  motion hint remains (scenario V4); confirming it needs a capture-side reference.
- Duplicate detection cannot separate a frozen capture from a truly still scene.
- The chaos proxy works on TCP streams: packet loss appears as stalls, resets and
  slowdowns, not as individually dropped IP packets (`tc netem` would add that).
- Detection accuracy is measured on synthetic video; thresholds are not yet tuned on real camera footage.
- No authentication or TLS: this is a challenge prototype, not a hardened service.
