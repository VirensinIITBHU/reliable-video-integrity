# Reliable Video Upload & Frame Integrity Detection

**Confluentia 2026 · nSEPIA Technical Challenge 01 · IIT (BHU)**
**Virendra Singh** · [virendrasingh-ai-ml.vercel.app](https://virendrasingh-ai-ml.vercel.app/)

> A video is accepted only when it is **proven** byte-perfect. Every frame anomaly
> is reported with **the evidence behind it**, so a transfer fault is never confused
> with a capture or encoding fault. One command (`python run_all.py`) reproduces
> every claim below.

---

## Approach in 30 seconds

```
client ──► chaos proxy (20–100% signal) ──► backend
 chunk + SHA-256 each      bandwidth, latency,      per-chunk hash check ─► assemble ─► whole-file SHA-256
 timeout, backoff,         stalls, resets,                                  │ match          │ mismatch
 resume, failover          blackouts, corruption                         COMPLETE         REJECTED
                                                                            ▼
                                       validate ─► full decode ─► 30 FPS slot map ─► evidence-graded report
```

1. **Transfer:** a manifest (file hash + hash of every 256 KB chunk) is sent first. Chunks are retried with timeouts and jittered backoff; after any failure the client asks the server what it already holds and sends only the rest. Multiple endpoints give Wi-Fi → cellular style failover.
2. **Gate:** the server refuses to finalise while chunks are missing and rejects a file whose assembled hash differs. Analysis can start **only** from `COMPLETE`.
3. **Analysis:** ffprobe validation, a full decode, timestamp-gap detection, content comparison and a 30 FPS slot map. Each finding gets an evidence tier, a confidence and an attributed cause.

---

## What sets this apart

| | Typical submission | This project |
|---|---|---|
| **Accepting a file** | "Upload finished" = success | Success only after per-chunk **and** whole-file SHA-256 match; partial / altered files are refused (T7, T8) |
| **The PS's "important technical distinction"** | One verdict per video | Every anomaly states its **evidence** (timestamps, metadata, decoder, frame content), **confidence** and **cause**: transfer vs capture/encoding vs bitstream damage |
| **30 FPS conversion** | Resampling hides or invents frames | Each 30 FPS slot is labelled `unique`, `gap_fill` (real hole) or `conversion_duplicate`; conversion artefacts are warnings, **not** anomalies |
| **Accuracy** | Claimed | **Measured** on videos with injected faults: 4/4 gaps, 3/3 freeze runs, **0 false alarms** |
| **Real footage** | Untested | Run on 7 real clips incl. 4 of my own phone recordings: found a real **1.67 s / 49-frame capture drop** and 10 single-frame drops, all located from container timestamps (R4, R5, R7) |
| **Honesty** | Overclaims | Test V4 *asserts* we do **not** claim certainty when re-encoding has erased the evidence |
| **Bad network** | Manual Wi-Fi toggling | Seeded, reproducible chaos proxy; the signal-strength trace is saved with every transfer log |
| **Setup** | pip installs, Docker | **Zero dependencies**: Python standard library + ffmpeg |

**Tuned on reality, not just synthetic data.** Running real clips exposed two flaws that synthetic tests hide, and both were fixed:
- The duplicate-frame threshold produced 90 false alarms on a still real scene. Sensor noise keeps real frames ≥ 0.08 apart and true repeats measure ~0.002, so the threshold moved from 0.20 to 0.05. Result: 0 false alarms, and every injected freeze is still found.
- Irregular phone timestamps made ffmpeg's *output* muxer print warnings that were being counted as damage in the source. These are now kept as pipeline notes and never mark a good file as damaged.

---

## Checklist against the problem statement

Stages: **①** transfer client + network · **②** server verification gate · **③** validate & decode · **④** 30 FPS analysis · **⑤** report · **🧪** automated test

| PS requirement | Stage | How | Evidence |
|---|---|---|---|
| **Operating conditions** | | | |
| Low bandwidth / variable speed | ① | Small independent chunks; bandwidth = signal² in proxy | T2, T4 |
| High latency | ① | Proxy adds latency as signal drops (~330 ms per connection at 20%) | T2 |
| Packet loss / upload interruptions | ① | Resets mid-chunk → per-chunk retry, backoff + jitter | T2 (7 resets), T6 |
| Temporary disconnections | ① | 3 s blackout → resync with server, resend only missing | T3 |
| Request timeouts | ① ② | Every request timed out; server discards half bodies | T4 (5 timeouts) |
| Connection switching | ① | Endpoint failover after repeated failures | T5 |
| Fluctuation 20–100% in one transfer | ① | Seeded profile changes every 0.5 s; trace logged | T2 (29–100%) |
| **Reliable completion** | | | |
| Maintain delivery under variable connectivity | ① | Retry, resume, failover, deadline | T2–T6, all V/R uploads |
| Recognise stalled / failed / partial | ② | `STALLED`, `EXPIRED`, `FAILED` states; finalize refused if incomplete | T7, T9 |
| Never accept incomplete / damaged | ② | Per-chunk + whole-file SHA-256; atomic writes | T6, T7, T8, T8b |
| Success only when intact; report status | ② ⑤ | `COMPLETE` only after hash match; status in report | every report §1 |
| **Backend processing** | | | |
| Proceed only after integrity checks | ② | Analysis starts only from `COMPLETE` | server state machine |
| Validate & decode; record errors | ③ | ffprobe + full decode; `VALID` / `DEGRADED` / `INVALID` | V5, V7 |
| Extract at 30 FPS with timestamps & indices | ④ | 30 FPS slot map: slot index, time, source frame | every report §3 |
| Localise missing / duplicated / abnormal frames | ④ | PTS gaps, metadata vs decode, content diff, motion jumps | V2, V3, V6, R4, R5, R7 |
| Integrity report with limitations | ⑤ | JSON + HTML timeline; limitations in every report | `evidence/reports/` |
| **Technical distinction** | | | |
| Byte-perfect ≠ no dropped frames | ④ ⑤ | Stated in every report; transfer & frame verdicts separate | every report |
| 30 FPS conversion may duplicate / omit | ④ | Conversion artefacts labelled and excluded from anomalies | report §3 |
| Evidence behind each conclusion | ④ ⑤ | Evidence + confidence + cause per anomaly | all reports |
| Transfer vs capture / encoding issues | ④ | Cause attribution; client's pre-upload probe as reference | V2, V5, R5 |
| **Test scenarios** | | | |
| Stable + variable signal | 🧪 | | T1, T2 |
| Interruption, timeout, reconnection | 🧪 | | T3, T4, T9, T10 |
| Connection switching / fluctuating speeds | 🧪 | | T5, T2 |
| Altered, truncated, invalid video | 🧪 | | T8, V5, V6, V7 |
| **Deliverables** | | | |
| Working client → backend | ①–⑤ | `rvi/` | `python run_all.py` |
| Architecture / README / run instructions | | | `README.md` |
| Transfer tests & evidence | 🧪 | | `evidence/SUMMARY.md`, `evidence/transfer_logs/` |
| Sample integrity report & limitations | ⑤ | | `evidence/reports/*.html` |

### Beyond the brief

- **Crash-safe resume:** the server is the source of truth, so a brand-new client process picks up a stalled upload and sends only the missing chunks (T10: 1225 of 1737 KB).
- **Corruption in transit** is caught per chunk and resent, not discovered at the end (T6).
- **Tampering at rest** after chunks were accepted is caught by the whole-file hash (T8).
- **Pre-upload reference:** the client probes the video before sending, so the report can prove an anomaly existed *before* the transfer.
- **Ground-truth test videos** with known injected faults, scored for hits, misses and false alarms.
- **Real-footage section:** any clip dropped into `samples/` is uploaded over the bad link and analysed automatically.
- **Honest limits, tested:** drops hidden by a constant-rate re-encode yield only low-confidence hints (V4), never a false certainty.

---

## Try it

```bash
python run_all.py        # 25/25 in the committed evidence (18 + 7 real clips), ~9 min
open evidence/SUMMARY.md
open evidence/reports/dropped_vfr.report.html
```

**Known limits:** frame accuracy is proven on synthetic ground truth (real clips have none); the proxy emulates loss at TCP level, not individual packets; no auth/TLS (prototype). Full list in `README.md`.
