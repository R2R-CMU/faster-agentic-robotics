#!/usr/bin/env python3
"""Build the page's videos and experiment data from the real-robot runs.

    python3 scripts/build_media.py /path/to/far_runs [--out REPO_ROOT]
    python3 scripts/build_media.py /path/to/far_runs --stats    # data only, no videos

The full set of videos lives on WorkStation_4090 (~/CoRL_late-breaking/agentic_robotics_real/far_runs);
build there and copy static/ back.

Reads far_runs/success_videos.csv (one row per successful attempt) and far_runs/results.csv
(agent time and tokens per trial). Every dashboard.mp4 has the side and wrist cameras side by
side on top (each 16:9, half the frame width) and the agent log below.

Writes static/videos/*.mp4|jpg and static/data/experiments.json (the numbers on the page).
"""
import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMS = {"side": "crop=iw/2:iw*9/32:0:0", "wrist": "crop=iw/2:iw*9/32:iw/2:0"}
SKIP_S = 1  # some dashboards open on black frames while the cameras start

# Mosaic: COLS x ROWS tiles separated by white gutters; each tile is one full successful attempt
# time-lapsed into TILE_S seconds, from a randomly chosen camera.
COLS, ROWS, TILE_W, TILE_H, GUTTER, TILE_S, FPS = 6, 4, 316, 178, 4, 16, 24
# Hand-picked replacements for random tiles: index (row * COLS + col) -> (task, condition, seed,
# attempt or None for the first success, camera).
MOSAIC_OVERRIDES = {
    7: ("6cups_down_stacking_random", "baseline", "1", None, "side"),  # was the shirt-folding wrist view
}
EXP_SPEED = 4  # experiment clips all play at the same speed so durations stay comparable

# The seven comparisons in the paper's table (far_trials/paper_table.tex), same seeds.
# donor: the library the shown skill-reuse seed copied (far_skills/*/PROVENANCE.md, attempt = the
# library's source commit). baseline/reuse: seeds averaged; video_seed is the one shown.
EXPERIMENTS = [
    dict(id="2up_to_2down", source="Nest 2 upright cups", target="Stack 2 inverted cups",
         donor=("cup_up_stacking", "baseline", "4", "1"),
         baseline=("cup_down_stacking", "baseline", ["1", "2", "3", "4"], "1"),
         reuse=("cup_down_stacking", "skill_transfer-cup_up_stacking", ["1", "2"], "1")),
    dict(id="2down_to_2up", source="Stack 2 inverted cups", target="Nest 2 upright cups",
         donor=("cup_down_stacking", "baseline", "4", "2"),
         baseline=("cup_up_stacking", "baseline", ["2", "4", "5", "7"], "5"),
         reuse=("cup_up_stacking", "skill_transfer-cup_down_stacking", ["1", "2"], "2")),
    dict(id="2_to_3cups", source="Stack 2 inverted cups", target="Build 3-cup pyramid",
         donor=("cup_down_stacking", "baseline", "1", "1"),
         baseline=("3cups_down_stacking", "baseline", ["1", "2"], "1"),
         reuse=("3cups_down_stacking", "skill_transfer-cup_down_stacking", ["1", "2"], "1")),
    dict(id="3to5cups", source="Build 3-cup pyramid", target="Build 5-cup pyramid",
         donor=("3cups_down_stacking", "baseline", "1", "4"),
         baseline=("5cups_down_stacking", "baseline", ["1", "2"], "1"),
         reuse=("5cups_down_stacking", "skill_transfer-3cups_down_stacking", ["1", "2"], "1")),
    dict(id="3to6cups", source="Build 3-cup pyramid", target="Build 6-cup pyramid",
         donor=("3cups_down_stacking", "baseline", "1", "4"),
         baseline=("6cups_down_stacking", "baseline", ["1"], "1"),
         reuse=("6cups_down_stacking", "skill_transfer-3cups_down_stacking", ["1"], "1")),
    dict(id="cap_to_cup_holder", source="Hang cap on branch", target="Cup on wooden finger",
         donor=("cap_holder", "baseline", "1", "4"),
         baseline=("hand_cup_holder", "baseline", ["2", "3"], "2"),
         reuse=("hand_cup_holder", "skill_transfer-cap_holder", ["1"], "1")),
    dict(id="cup_to_cap_holder", source="Cup on wooden finger", target="Hang cap on branch",
         donor=("hand_cup_holder", "baseline", "2", "8"),
         baseline=("cap_holder", "baseline", ["1", "2"], "1"),
         reuse=("cap_holder", "skill_transfer-hand_cup_holder", ["1"], "1")),
]

# Claude Code screen recordings (OBS, ~/Videos/OBS on WorkStation_4090) of the shown skill-reuse
# trial: file, start and end in seconds. Only these two transfers were recorded.
TERMINALS = {
    # seed 1, whole session up to the operator's success at 21:29:27 (+30 s)
    "2up_to_2down": ("cup up to down transfer kinova_a_seed1.mkv", 0, 1017, "full session"),
    # seed 2, recording covers the last minutes, up to the success at 00:02:57 (+30 s)
    "2down_to_2up": ("cup down to up transfer kinova_b_seed2.mkv", 0, 427, "last 7 min of the session"),
}
TERM_SPEED = 16
TERM_CROP = "crop=1232:693:48:22"  # terminal window inside the 1280x720 desktop capture

# Highlights (listed in ~/path.txt on WorkStation_4090): both cameras side by side.
# name -> (task, condition, seed, attempt, speed, poster time in source seconds)
HIGHLIGHTS = {
    "highlight1": ("6cups_down_stacking_random", "skill_transfer-3cups_down_stacking", "1", "6", 8, 45),  # moves a cup away first
    "highlight2": ("hand_cup_holder", "baseline", "3", "5", 4, 80),  # places the cup upright on the fingertip
}
BOTH_CAMS = "crop=iw:iw*9/32:0:0"

# Task directories renamed after success_videos.csv was written.
RENAMED = {"3cups_up_stacking_left_right": "3cups_up_stacking"}


def resolve(path):
    if os.path.exists(path):
        return path
    for old, new in RENAMED.items():
        alt = path.replace(f"/{old}/", f"/{new}/")
        if os.path.exists(alt):
            return alt
    return None


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return float(out)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def load_videos(runs):
    rows = []
    with open(os.path.join(runs, "success_videos.csv")) as f:
        for r in csv.DictReader(f):
            r["path"] = resolve(r["video"])
            if r["path"]:
                rows.append(r)
            else:
                print("missing:", r["video"])
    return rows


def build_mosaic(rows, out):
    # One random attempt per task first so every task appears, then fill at random.
    rng = random.Random(0)
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["task"]].append(r)
    n = COLS * ROWS
    picks = [rng.choice(v) for _, v in sorted(by_task.items())][:n]
    rest = [r for r in rows if r not in picks]
    picks += rng.sample(rest, n - len(picks))
    rng.shuffle(picks)
    cams = (["side", "wrist"] * n)[:n]
    rng.shuffle(cams)
    for i, (task, cond, seed, attempt, cam) in MOSAIC_OVERRIDES.items():
        picks[i], cams[i] = find_video(rows, task, cond, seed, attempt), cam

    tmp = tempfile.mkdtemp()
    try:
        tiles = []
        for i, (r, cam) in enumerate(zip(picks, cams)):
            speed = max((duration(r["path"]) - SKIP_S) / TILE_S, 2)
            tile = os.path.join(tmp, f"{i:02d}.mkv")
            ffmpeg("-ss", str(SKIP_S), "-i", r["path"], "-an", "-vf",
                   f"{CAMS[cam]},scale={TILE_W}:{TILE_H},setpts=PTS/{speed:.3f},fps={FPS},"
                   f"tpad=stop_mode=clone:stop_duration={TILE_S},format=yuv444p,"
                   f"pad={TILE_W + GUTTER}:{TILE_H + GUTTER}:{GUTTER // 2}:{GUTTER // 2}:white",
                   # Lossless 4:4:4 tiles: stacking odd-height 4:2:0 frames crashes ffmpeg 7.1.1.
                   "-t", str(TILE_S), "-c:v", "ffv1", tile)
            tiles.append(tile)
        tw, th = TILE_W + GUTTER, TILE_H + GUTTER
        layout = "|".join(f"{(i % COLS) * tw}_{(i // COLS) * th}" for i in range(n))
        inputs = [a for t in tiles for a in ("-i", t)]
        ffmpeg(*inputs, "-filter_complex", f"xstack=inputs={n}:layout={layout}", "-an",
               "-c:v", "libx264", "-crf", "26", "-preset", "slow", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", os.path.join(out, "mosaic.mp4"))
        ffmpeg("-sseof", "-0.1", "-i", os.path.join(out, "mosaic.mp4"), "-frames:v", "1",
               "-q:v", "4", os.path.join(out, "mosaic.jpg"))
    finally:
        shutil.rmtree(tmp)
    print(f"mosaic: {n} of {len(rows)} videos, tasks: {len({r['task'] for r in picks})}, "
          f"cams: {cams.count('side')} side / {cams.count('wrist')} wrist")


def find_video(rows, task, cond, seed, attempt=None):
    match = [r for r in rows if (r["task"], r["condition"], r["seed"]) == (task, cond, seed)
             and (attempt is None or r["attempt"] == attempt)]
    if not match:
        raise SystemExit(f"no success video for {task} / {cond} / seed {seed} / attempt {attempt}")
    return min(match, key=lambda r: int(r["attempt"]))


def build_clip(r, out, name):
    path = os.path.join(out, f"{name}.mp4")
    ffmpeg("-ss", str(SKIP_S), "-i", r["path"], "-an", "-vf",
           f"{CAMS['side']},scale=640:360,setpts=PTS/{EXP_SPEED},fps=30",
           "-c:v", "libx264", "-crf", "26", "-preset", "slow", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", path)
    ffmpeg("-sseof", "-0.1", "-i", path, "-frames:v", "1", "-q:v", "4", path[:-4] + ".jpg")
    print(f"  {name}: {r['path']}")


def build_terminal(obs, out, name, rec):
    path = os.path.join(out, f"{name}.mp4")
    f, start, end, _ = rec
    ffmpeg("-ss", str(start), "-to", str(end), "-i", os.path.join(obs, f), "-an", "-vf",
           f"{TERM_CROP},scale=1280:720,setpts=PTS/{TERM_SPEED},fps=15",
           "-c:v", "libx264", "-crf", "30", "-preset", "slow", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", path)
    ffmpeg("-ss", str((end - start) / TERM_SPEED / 2), "-i", path, "-frames:v", "1", "-q:v", "4", path[:-4] + ".jpg")
    print(f"  {name}: {f} [{start}-{end} s]")


def build_highlights(rows, out):
    for name, (task, cond, seed, attempt, speed, poster_s) in HIGHLIGHTS.items():
        r = find_video(rows, task, cond, seed, attempt)
        path = os.path.join(out, f"{name}.mp4")
        ffmpeg("-ss", str(SKIP_S), "-i", r["path"], "-an", "-vf",
               f"{BOTH_CAMS},scale=1280:360,setpts=PTS/{speed},fps=30",
               "-c:v", "libx264", "-crf", "26", "-preset", "slow", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", path)
        ffmpeg("-ss", str((poster_s - SKIP_S) / speed), "-i", path, "-frames:v", "1", "-q:v", "4", path[:-4] + ".jpg")
        print(f"{name}: {r['path']} at {speed}x")


def experiment_data(runs, rows, out, videos, obs):
    with open(os.path.join(runs, "results.csv")) as f:
        res = [r for r in csv.DictReader(f) if r["success"] == "True"]

    def trials(task, cond, seeds):
        found = [r for r in res if r["task"] == task and r["condition"] == cond and r["seed"] in seeds]
        if len(found) != len(seeds):
            raise SystemExit(f"results.csv: {task} / {cond} has {len(found)} of seeds {seeds}")
        return [{"seed": r["seed"], "robot": r["robot"],
                 "minutes": float(r["agent_minutes_to_success"]),
                 "tokens_m": float(r["k_tokens_to_success"]) / 1000,
                 "attempts": int(r["attempts_to_success"]),
                 "successes": int(r["successes_required"])} for r in found]

    def summary(ts):
        # Means and changes from unrounded values, as in the paper's table; round only for output.
        return {"minutes": sum(t["minutes"] for t in ts) / len(ts),
                "tokens_m": sum(t["tokens_m"] for t in ts) / len(ts), "seeds": ts}

    def rounded(s):
        r = lambda t: {**t, "minutes": round(t["minutes"], 1), "tokens_m": round(t["tokens_m"], 2)}
        return {**r(s), "seeds": [r(t) for t in s["seeds"]]}

    def terminal(eid):
        if eid not in TERMINALS:
            return None
        name = f"{eid}_terminal"
        if videos:
            build_terminal(obs, out, name, TERMINALS[eid])
        return {"video": f"static/videos/{name}.mp4", "poster": f"static/videos/{name}.jpg",
                "speed": TERM_SPEED, "note": TERMINALS[eid][3]}

    data, d_min, d_tok = [], [], []
    for e in EXPERIMENTS:
        if videos:
            print(e["id"])
        clips = {}
        for role in ("donor", "baseline", "reuse"):
            if role == "donor":
                task, cond, shown, attempt = e[role]
            else:
                (task, cond, _, shown), attempt = e[role], None
            r = find_video(rows, task, cond, shown, attempt)
            name = f"{e['id']}_{role}"
            if videos:
                build_clip(r, out, name)
            clips[role] = {"video": f"static/videos/{name}.mp4", "poster": f"static/videos/{name}.jpg",
                           "seed": shown, "attempt": r["attempt"], "robot": r["robot"],
                           "robot_s": round(float(r["video_s"]))}
        base = summary(trials(e["baseline"][0], e["baseline"][1], e["baseline"][2]))
        reuse = summary(trials(e["reuse"][0], e["reuse"][1], e["reuse"][2]))
        donor = summary(trials(e["donor"][0], e["donor"][1], [e["donor"][2]]))
        d_min.append(reuse["minutes"] / base["minutes"] - 1)
        d_tok.append(reuse["tokens_m"] / base["tokens_m"] - 1)
        data.append({"id": e["id"], "source": e["source"], "target": e["target"],
                     "robot": clips["reuse"]["robot"], "clips": clips,
                     "donor": rounded(donor), "baseline": rounded(base), "reuse": rounded(reuse),
                     "change": {"minutes": round(d_min[-1], 4), "tokens": round(d_tok[-1], 4)},
                     "terminal": terminal(e["id"])})
        print(f"{e['source']} => {e['target']}: tokens {base['tokens_m']:.2f} -> {reuse['tokens_m']:.2f} "
              f"({d_tok[-1]:+.0%}), agent min {base['minutes']:.1f} -> {reuse['minutes']:.1f} ({d_min[-1]:+.0%})")
    mean = {"minutes": round(sum(d_min) / len(d_min), 4), "tokens": round(sum(d_tok) / len(d_tok), 4)}
    print(f"mean row-wise change: tokens {mean['tokens']:+.0%}, agent time {mean['minutes']:+.0%}")
    return {"speed": EXP_SPEED, "mean_change": mean, "experiments": data}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", help="far_runs directory")
    ap.add_argument("--out", default=ROOT, help="site root to write static/ into")
    ap.add_argument("--stats", action="store_true", help="write experiments.json only, no videos")
    ap.add_argument("--obs", default=os.path.expanduser("~/Videos/OBS"), help="OBS screen recordings")
    a = ap.parse_args()
    vid_dir, data_dir = os.path.join(a.out, "static", "videos"), os.path.join(a.out, "static", "data")
    os.makedirs(vid_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    rows = load_videos(a.runs)
    data = experiment_data(a.runs, rows, vid_dir, videos=not a.stats, obs=a.obs)
    with open(os.path.join(data_dir, "experiments.json"), "w") as f:  # generated; do not edit
        json.dump(data, f, ensure_ascii=False, indent=1)
    if not a.stats:
        build_mosaic(rows, vid_dir)
        build_highlights(rows, vid_dir)


if __name__ == "__main__":
    main()
