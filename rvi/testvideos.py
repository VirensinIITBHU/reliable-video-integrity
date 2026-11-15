"""Generate test videos with KNOWN ground truth, so detection can be measured.

All are derived from one clean 30 s, 30 FPS synthetic clip (ffmpeg `testsrc2`,
which has continuous motion and a burned-in frame counter).
"""
import json
import os
import random
import subprocess

FPS = 30
DURATION = 30
DROPS = [(120, 3), (333, 1), (600, 5), (801, 2)]   # (first source frame index, how many removed)
DUPS = [(200, 3), (450, 1), (700, 4)]              # (source frame index, extra repeats)


def _ff(args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-800:])


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-g", "30"]


def generate(outdir, seed=7):
    os.makedirs(outdir, exist_ok=True)
    p = lambda n: os.path.join(outdir, n)
    truth = {}

    # 1. clean reference
    _ff(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=%d:duration=%d" % (FPS, DURATION)]
        + ENC + ["-movflags", "+faststart", p("clean.mp4")])
    truth["clean.mp4"] = {"kind": "clean", "frames": FPS * DURATION}

    # 2. frames dropped at capture, original timestamps preserved (VFR) -> detectable
    sel = "+".join("between(n,%d,%d)" % (a, a + k - 1) for a, k in DROPS)
    _ff(["-i", p("clean.mp4"), "-vf", "select='not(%s)'" % sel, "-fps_mode", "passthrough"]
        + ENC + [p("dropped_vfr.mp4")])
    truth["dropped_vfr.mp4"] = {
        "kind": "dropped", "timestamps_preserved": True,
        "gaps": [{"after_source_frame": a - 1, "missing": k, "start_time": round(a / FPS, 4)} for a, k in DROPS]}

    # 3. same drops, then timestamps regenerated as constant 30 FPS -> gap evidence erased
    _ff(["-i", p("clean.mp4"), "-vf", "select='not(%s)',setpts=N/%d/TB" % (sel, FPS), "-r", str(FPS)]
        + ENC + [p("dropped_cfr.mp4")])
    truth["dropped_cfr.mp4"] = {"kind": "dropped", "timestamps_preserved": False,
                                "missing_total": sum(k for _, k in DROPS)}

    # 4. frozen / duplicated frames
    chain, shift, runs = [], 0, []
    for a, k in DUPS:
        chain.append("loop=loop=%d:size=1:start=%d" % (k, a + shift))
        runs.append({"first_index": a + shift, "repeats": k, "start_time": round((a + shift) / FPS, 4)})
        shift += k
    _ff(["-i", p("clean.mp4"), "-vf", ",".join(chain) + ",setpts=N/%d/TB" % FPS, "-r", str(FPS)]
        + ENC + [p("duplicated.mp4")])
    truth["duplicated.mp4"] = {"kind": "duplicated", "runs": runs}

    # 5. source file damaged BEFORE upload (transfers perfectly, decodes badly)
    data = bytearray(open(p("clean.mp4"), "rb").read())
    rng = random.Random(seed)
    lo, hi = int(len(data) * 0.45), int(len(data) * 0.55)
    for _ in range(400):
        data[rng.randrange(lo, hi)] ^= 0xFF
    open(p("corrupt_source.mp4"), "wb").write(bytes(data))
    truth["corrupt_source.mp4"] = {"kind": "corrupt_bitstream", "region_fraction": [0.45, 0.55]}

    # 6. truncated file and 7. not a video at all
    clean = open(p("clean.mp4"), "rb").read()
    open(p("truncated.mp4"), "wb").write(clean[: int(len(clean) * 0.6)])
    truth["truncated.mp4"] = {"kind": "truncated", "kept_fraction": 0.6}
    open(p("not_a_video.mp4"), "wb").write(random.Random(seed).randbytes(300_000))
    truth["not_a_video.mp4"] = {"kind": "invalid"}

    with open(p("ground_truth.json"), "w") as f:
        json.dump(truth, f, indent=2)
    return truth


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "test_videos"
    generate(out)
    for n in sorted(os.listdir(out)):
        print("%-22s %8d bytes" % (n, os.path.getsize(os.path.join(out, n))))
