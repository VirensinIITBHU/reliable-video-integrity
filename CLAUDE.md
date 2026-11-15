# Project context for Claude Code

## What this is
Entry for the Confluentia 2026 nSEPIA Technical Challenge (IIT BHU):
"Reliable Video Upload & Frame Integrity Detection under low / unstable network".
Deliver a 30 s video from client to backend while signal swings 20%-100%, accept it
only when proven complete and uncorrupted, then analyse it at 30 FPS for missing,
skipped, duplicated or abnormal frames and produce an integrity report.

## Deadline and submission
- Deadline: 15 November 2026, 11:59 PM IST. Target: send by 10:45 PM IST.
- Send to iitbhu_2026@nihilent.com, subject
  "Confluentia 2026 | Technical Challenge | [Name / Team]".
- Include: repo link (repo is private: share access or attach a zip), brief
  documentation, test evidence, sample integrity report.
- Required deliverables: working client-to-backend implementation, architecture /
  README with run instructions, transfer reliability tests and evidence, sample
  integrity report and limitations.
- Judges' key point ("important technical distinction"): a byte-perfect transfer
  cannot prove the recording had no dropped frames; 30 FPS conversion may duplicate
  or omit frames; every anomaly must state its supporting evidence and separate
  transfer issues from capture / encoding issues.

## Hard constraints
- Python standard library + ffmpeg/ffprobe only. Do NOT add pip dependencies.
- `python run_all.py` must stay a one-command, all-green evidence run.
- Never decode or analyse a file that is not in state COMPLETE (hash-verified).
- Keep honest limitations in the README; do not overclaim detection ability.

## Commands
- `python run_all.py` : 18 scenarios + one per clip in `samples/` (~4 min with the 3 clips), rewrites `evidence/`
- `python run_all.py --quick` : skips the slowest network scenario
- `python -m rvi.server --port 8000` : backend
- `python -m rvi.chaos --listen 9000 --upstream 127.0.0.1:8000` : bad network
- `python -m rvi.client <video> --server http://127.0.0.1:9000` : upload + report
- `python -m rvi.analyze <video>` : analysis only, prints JSON

## Layout
- `rvi/server.py` : upload API, state machine (UPLOADING, STALLED, VERIFYING,
  COMPLETE, REJECTED, EXPIRED, then ANALYZING, ANALYZED), stall monitor, atomic writes
- `rvi/client.py` : chunked uploader, timeouts, backoff + jitter, resume, failover
- `rvi/chaos.py` : seeded TCP proxy mapping signal strength to bandwidth, latency,
  stalls, resets, blackouts, optional byte corruption
- `rvi/analyze.py` : ffprobe validation, full decode, PTS gap detection, duplicate
  detection on 32x18 gray thumbnails, motion-jump hint, 30 FPS slot map
- `rvi/report.py` : JSON report + HTML timeline
- `rvi/testvideos.py` : ground-truth videos (clean, dropped_vfr, dropped_cfr,
  duplicated, corrupt_source, truncated, not_a_video)
- `run_all.py` : scenarios T1-T10 (transfer) and V1-V7 (video); `evidence/` is committed

## Status
Baseline complete and pushed. 25/25 pass with the 7 real clips in `samples/` (git-ignored);
R4-R7 are the user's phone recordings and contain real timestamp gaps. WRITEUP.md = judge-facing summary.
Done: real-footage tuning (DUP_MAD 0.05, isolated motion jumps), harsher T2, proxy fault
clock armed on first connection (fixed T9/T10 timing on fast/slow machines).

## Known gaps / next improvements, in priority order
1. Optional: a clip recorded on an actual phone (current real clips are stock footage).
3. View `evidence/reports/*.html` in a browser and polish; never visually checked.
4. Localise decoder errors to timestamps in the corrupt-bitstream case.
5. Optional: adaptive chunk size by measured throughput; server-restart-mid-upload
   scenario; architecture diagram image; 60 s demo recording.
6. Final pass on README, then prepare the submission email.
