#!/usr/bin/env python3
"""Build the page's videos and experiment data from the real-robot runs and the paper's table.

    python3 scripts/build_media.py /path/to/far_runs [--out REPO_ROOT]
    python3 scripts/build_media.py /path/to/far_runs --stats    # data only, no videos
    python3 scripts/build_media.py --terminals ~/far_offline/videos   # terminal clips, on this workstation

The full set of videos lives on WorkStation_4090 (~/CoRL_late-breaking/agentic_robotics_real/far_runs);
build there (with static/videos/sim_videos copied into --out) and copy static/ back.

The numbers shown come from the paper's table (far_trials/paper_table.tex). far_runs/results.csv
gives the per-seed values (hover) and the donors' own cost; far_runs/success_videos.csv (one row per
successful attempt) gives the videos. Every dashboard.mp4 has the side and wrist cameras side by side
on top (each 16:9, half the frame width) and the agent log below.

Writes static/videos/*.mp4|jpg and static/data/experiments.json.
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMS = {"side": "crop=iw/2:iw*9/32:0:0", "wrist": "crop=iw/2:iw*9/32:iw/2:0"}
SKIP_S = 1  # some dashboards open on black frames while the cameras start

# Mosaic: COLS x ROWS tiles separated by white gutters, row by row. A real tile is one full
# successful attempt time-lapsed into TILE_S seconds: (task, condition, seed, attempt, camera).
# A sim tile is ("sim", file in static/videos/sim_videos), played at 1x and held on its last frame.
COLS, ROWS, TILE_W, TILE_H, GUTTER, TILE_S, FPS = 6, 4, 316, 178, 4, 16, 24
MOSAIC_TILES = [
    ("3cups_up_stacking_left_right", "skill_transfer-cup_up_stacking", "4", "6", "side"),
    ("3cups_down_stacking", "skill_transfer-cup_down_stacking", "1", "6", "side"),
    ("cap_holder", "baseline", "1", "1", "wrist"),
    ("sim", "00_Donor_coffee_setup_mug.mp4"),
    ("cup_up_stacking", "baseline", "4", "1", "side"),
    ("cap_holder", "baseline", "2", "5", "side"),
    ("3cups_down_stacking", "skill_transfer-cup_down_stacking", "2", "11", "side"),
    ("6cups_down_stacking_random", "baseline", "1", "1", "side"),
    ("cap_holder", "skill_transfer-hand_cup_holder", "1", "2", "side"),
    ("cup_down_stacking", "skill_transfer-cup_up_stacking", "1", "2", "side"),
    ("sim", "02_Recipient_pnp_counter_to_sink.mp4"),
    ("cup_down_stacking", "skill_transfer-cup_up_stacking", "1", "1", "wrist"),
    ("hand_cup_holder", "baseline", "3", "5", "wrist"),
    ("6cups_down_stacking_random", "skill_transfer-3cups_down_stacking", "2", None, "wrist"),
    ("battery_placement", "baseline", "1", "8", "wrist"),
    ("marker_placement", "baseline", "1", "15", "wrist"),
    ("sim", "04_Recipient_open_drawer.mp4"),
    ("5cups_down_stacking", "baseline", "1", "11", "side"),
    ("hand_cup_holder", "baseline", "2", "10", "wrist"),
    ("5cups_down_stacking", "skill_transfer-3cups_down_stacking", "2", "3", "side"),
    ("5cups_down_stacking_random", "skill_transfer-3cups_down_stacking", "1", "2", "side"),
    ("sim", "05_Recipient_turn_off_sink_faucet.mp4"),
    ("6cups_down_stacking", "skill_transfer-3cups_down_stacking", "1", "4", "side"),
    ("cup_up_stacking", "skill_transfer-cup_down_stacking", "1", "2", "side"),
]
EXP_SPEED = 4  # experiment clips all play at the same speed so durations stay comparable

# The ten transfers in the paper's table, in its order; source/target match its Donor task / New task columns.
# donor: the library the shown with-library seed copied (far_skills/*/PROVENANCE.md, attempt = the
# library's source commit). baseline/reuse: (task, condition, seeds the paper averages or None if
# results.csv does not reproduce the paper's value, seed whose video is shown).
EXPERIMENTS = [
    dict(id="2down_to_2up", source="Stack 2 inverted cups", target="Nest 2 upright cups",
         donor=("cup_down_stacking", "baseline", "4", "2"),
         baseline=("cup_up_stacking", "baseline", ["5", "7"], "5"),
         reuse=("cup_up_stacking", "skill_transfer-cup_down_stacking", ["1", "2"], "2")),
    dict(id="2up_to_2down", source="Nest 2 upright cups", target="Stack 2 inverted cups",
         donor=("cup_up_stacking", "baseline", "4", "1"),
         baseline=("cup_down_stacking", "baseline", ["1", "2"], "1"),
         reuse=("cup_down_stacking", "skill_transfer-cup_up_stacking", ["1", "2"], "1")),
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
    dict(id="3to6cups_random", source="Build 3-cup pyramid", target="Build 6-cup pyramid (random)",
         donor=("3cups_down_stacking", "baseline", "2", "4"),
         baseline=("6cups_down_stacking_random", "baseline", ["1"], "1"),
         # paper: 6.70M, 17.4 min; no single seed in results.csv matches
         reuse=("6cups_down_stacking_random", "skill_transfer-3cups_down_stacking", None, "2")),
    dict(id="cap_to_cup_holder", source="Hang cap on branch", target="Cup on wooden finger",
         donor=("cap_holder", "baseline", "1", "4"),
         baseline=("hand_cup_holder", "baseline", ["2", "3"], "2"),
         reuse=("hand_cup_holder", "skill_transfer-cap_holder", ["1"], "1")),
    dict(id="cup_to_cap_holder", source="Cup on wooden finger", target="Hang cap on branch",
         donor=("hand_cup_holder", "baseline", "2", "8"),
         baseline=("cap_holder", "baseline", ["1", "2"], "1"),
         reuse=("cap_holder", "skill_transfer-hand_cup_holder", ["1"], "1")),
    dict(id="marker_to_battery", source="Place marker in bin", target="Place battery in bowl",
         donor=("marker_placement", "baseline", "1", "2"),
         baseline=("battery_placement", "baseline", ["1"], "1"),
         reuse=("battery_placement", "skill_transfer-marker_placement", ["1"], "1")),
    dict(id="battery_to_marker", source="Place battery in bowl", target="Place marker in bin",
         donor=("battery_placement", "baseline", "1", "4"),
         baseline=("marker_placement", "baseline", ["1"], "1"),
         # paper: 5.00M, 14.9 min; results.csv seed 1 has 5.68M, 15.1 min
         reuse=("marker_placement", "skill_transfer-battery_placement", None, "1")),
]

# Final Table 2 values where far_trials/paper_table.tex on WorkStation_4090 is out of date
# (from commit fd29491): (baseline, with library, change) per metric, and the mean row.
TABLE_OVERRIDES = {
    "marker_to_battery": {"tokens": (15.95, 5.86, -0.63), "minutes": (32.9, 12.1, -0.63)},
}
MEAN_OVERRIDE = {"tokens": -0.31, "minutes": -0.45}

# Donor cost shown on each card: (tokens in M, agent minutes) for the donor task, on the same basis
# as the paper's baseline rows. Cards not listed fall back to the donor trial in results.csv.
DONOR_COST = {
    "2down_to_2up": (8.88, 23.7),
    "2up_to_2down": (4.45, 26.8),
    "2_to_3cups": (8.88, 23.7),
    "3to5cups": (6.17, 18.9),
    "3to6cups": (6.17, 18.9),
    "3to6cups_random": (6.17, 18.9),
    "cap_to_cup_holder": (15.05, 34.1),
    "cup_to_cap_holder": (18.21, 40.7),
    "marker_to_battery": (12.89, 30.6),
    "battery_to_marker": (15.95, 32.9),
}

# Simulation (Embodied First Exam): donor and recipient rollouts in static/videos/sim_videos.
# Per-task numbers are not in the paper source here yet; the summary is the paper's Results text.
SIM = {
    "donor": ("Set up mug", "00_Donor_coffee_setup_mug.mp4"),
    "tasks": [("Serve mug", "01_Recipient_coffee_serve_mug.mp4"),
              ("Apple to sink", "02_Recipient_pnp_counter_to_sink.mp4"),  # name as in the paper figure
              ("Counter to cabinet", "03_Recipient_pnp_counter_to_cab.mp4"),
              ("Open drawer", "04_Recipient_open_drawer.mp4"),
              ("Turn off sink faucet", "05_Recipient_turn_off_sink_faucet.mp4")],
    "summary": {"minutes": [-0.23, -0.14], "tokens": [-0.32, -0.25]},
    "speed": 2,  # the rollout videos are already sped up 2x
}

# Terminal recordings of the transfer agent, re-run offline with the same demos and skill library
# but no robot (~/far_offline/videos on this workstation, already sped up 8x). One per table row.
# Each clip is a TERM_CLIP_S excerpt from the start of the session.
TERMINALS = {
    "2down_to_2up": "row1_cup_up_stacking_offline_transfer-cup_down_stacking_seed3_8x.mp4",
    "2up_to_2down": "row2_cup_down_stacking_offline_transfer-cup_up_stacking_8x.mp4",
    "2_to_3cups": "row3_3cups_down_stacking_offline_transfer-cup_down_stacking_8x.mp4",
    "3to5cups": "row4_5cups_down_stacking_offline_transfer-3cups_down_stacking_8x.mp4",
    "3to6cups": "row5_6cups_down_stacking_offline_transfer-3cups_down_stacking_8x.mp4",
    "3to6cups_random": "row6_6cups_down_stacking_random_offline_transfer-3cups_down_stacking_8x.mp4",
    "cap_to_cup_holder": "row7_hand_cup_holder_offline_transfer-cap_holder_seed2_8x.mp4",
    "cup_to_cap_holder": "row8_cap_holder_offline_transfer-hand_cup_holder_8x.mp4",
    "marker_to_battery": "row9_battery_placement_offline_transfer-marker_placement_8x.mp4",
    "battery_to_marker": "row10_marker_placement_offline_transfer-battery_placement_8x.mp4",
}
TERM_SPEED = 8
TERM_CLIP_S = 13
TERM_SKIP_S = 1.5  # each recording opens on an empty terminal while the session starts
TERM_NOTE = "excerpt of an offline re-run"

# Highlights (listed in ~/path.txt on WorkStation_4090): both cameras side by side.
# name -> (run under far_runs, speed, poster time in source seconds)
HIGHLIGHTS = {
    "highlight1": ("6cups_down_stacking_random/skill_transfer-3cups_down_stacking/seed_1/kinova_a/runs/attempt_6",
                   8, 45),  # moves a cup away first
    "highlight2": ("hand_cup_holder/baseline/seed_3/kinova_b/runs/attempt_5", 4, 80),  # cup upright on the fingertip
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
    n = COLS * ROWS
    assert len(MOSAIC_TILES) == n
    tmp = tempfile.mkdtemp()
    try:
        tiles = []
        for i, t in enumerate(MOSAIC_TILES):
            if t[0] == "sim":
                src, skip, crop = os.path.join(out, "sim_videos", t[1]), 0, ""
                speed = max(duration(src) / TILE_S, 1)
            else:
                src, skip, crop = find_video(rows, *t[:4])["path"], SKIP_S, CAMS[t[4]] + ","
                speed = max((duration(src) - SKIP_S) / TILE_S, 2)
            tile = os.path.join(tmp, f"{i:02d}.mkv")
            ffmpeg("-ss", str(skip), "-i", src, "-an", "-vf",
                   f"{crop}scale={TILE_W}:{TILE_H},setpts=PTS/{speed:.3f},fps={FPS},"
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
    print(f"mosaic: {n} tiles, {sum(t[0] == 'sim' for t in MOSAIC_TILES)} sim")


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


def build_terminals(src_dir, out):
    for eid, f in TERMINALS.items():
        path = os.path.join(out, f"{eid}_terminal.mp4")
        ffmpeg("-ss", str(TERM_SKIP_S), "-t", str(TERM_CLIP_S), "-i", os.path.join(src_dir, f), "-an",
               "-vf", "scale=960:600:flags=lanczos,fps=8",
               "-c:v", "libx264", "-crf", "30", "-preset", "slow", "-tune", "stillimage", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", path)
        ffmpeg("-ss", str(duration(path) / 2), "-i", path, "-frames:v", "1", "-q:v", "4", path[:-4] + ".jpg")
        print(f"{eid}_terminal: {f}")


def build_highlights(runs, out):
    for name, (run, speed, poster_s) in HIGHLIGHTS.items():
        src = os.path.join(runs, run, "dashboard.mp4")
        path = os.path.join(out, f"{name}.mp4")
        if not os.path.exists(src):  # e.g. the trial was discarded later; keep the clip already built
            print(f"{name}: {src} missing, kept existing clip")
            continue
        ffmpeg("-ss", str(SKIP_S), "-i", src, "-an", "-vf",
               f"{BOTH_CAMS},scale=1280:360,setpts=PTS/{speed},fps=30",
               "-c:v", "libx264", "-crf", "26", "-preset", "slow", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", path)
        ffmpeg("-ss", str((poster_s - SKIP_S) / speed), "-i", path, "-frames:v", "1", "-q:v", "4", path[:-4] + ".jpg")
        print(f"{name}: {src} at {speed}x")


def paper_table(path):
    """Rows of the paper's physical table: (donor task, new task) -> numbers, plus the mean row."""
    rows, mean = {}, None
    for line in open(path):
        line = re.sub(r"\$\^\{[^}]*\}\$", "", line)  # footnote marks
        line = re.sub(r"\\textbf\{([^}]*)\}", r"\1", line).replace("$-$", "-").replace("\\%", "%")
        cells = [c.strip() for c in line.split("&")]
        if len(cells) == 4 and "\\to" in line:
            (tb, tr, tc), (mb, mr, mc) = (re.findall(r"[-+]?\d+(?:\.\d+)?", c) for c in cells[2:])
            rows[(cells[0], cells[1])] = {"tokens": (float(tb), float(tr), int(tc) / 100),
                                          "minutes": (float(mb), float(mr), int(mc) / 100)}
        elif line.lstrip().startswith("\\multicolumn") and "Mean change" in line:
            tc, mc = (int(x) for x in re.findall(r"-?\d+(?=%)", line))
            mean = {"tokens": tc / 100, "minutes": mc / 100}
    return rows, mean


def experiment_data(runs, rows, out, videos, table):
    with open(os.path.join(runs, "results.csv")) as f:
        res = [r for r in csv.DictReader(f) if r["success"] == "True"]
    paper, paper_mean = paper_table(table)

    def trials(task, cond, seeds):
        found = [r for r in res if r["task"] == task and r["condition"] == cond and r["seed"] in seeds]
        if len(found) != len(seeds):
            raise SystemExit(f"results.csv: {task} / {cond} has {len(found)} of seeds {seeds}")
        return [{"seed": r["seed"], "robot": r["robot"],
                 "minutes": round(float(r["agent_minutes_to_success"]), 1),
                 "tokens_m": round(float(r["k_tokens_to_success"]) / 1000, 2)} for r in found]

    def side(spec, minutes, tokens, label):
        # The paper's mean; per-seed values only when results.csv reproduces it.
        task, cond, seeds, _ = spec
        ts = trials(task, cond, seeds) if seeds else []
        if ts:
            m = sum(t["minutes"] for t in ts) / len(ts)
            k = sum(t["tokens_m"] for t in ts) / len(ts)
            if abs(m - minutes) > 0.06 or abs(k - tokens) > 0.006:
                print(f"  ! {label}: results.csv seeds {seeds} give {k:.2f}M, {m:.1f} min; paper {tokens}M, {minutes} min")
                ts = []
        elif seeds is None:
            print(f"  ! {label}: paper value {tokens}M, {minutes} min not reproduced by results.csv")
        return {"minutes": minutes, "tokens_m": tokens, "seeds": ts}

    def terminal(eid):
        if eid not in TERMINALS:
            return None
        v = "?v=4"  # bump when the clips are rebuilt so browsers skip their cached copies
        return {"video": f"static/videos/{eid}_terminal.mp4{v}", "poster": f"static/videos/{eid}_terminal.jpg{v}",
                "speed": TERM_SPEED, "note": TERM_NOTE}

    data = []
    for e in EXPERIMENTS:
        p = paper.get((e["source"], e["target"]))
        if not p:
            raise SystemExit(f"paper table has no row {e['source']} -> {e['target']}")
        p = TABLE_OVERRIDES.get(e["id"], p)
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
        label = f"{e['source']} => {e['target']}"
        donor = trials(e["donor"][0], e["donor"][1], [e["donor"][2]])[0]
        if e["id"] in DONOR_COST:
            donor["tokens_m"], donor["minutes"] = DONOR_COST[e["id"]]
        data.append({"id": e["id"], "source": e["source"], "target": e["target"],
                     "robot": clips["reuse"]["robot"], "clips": clips,
                     "donor": {**donor, "seeds": []},
                     "baseline": side(e["baseline"], p["minutes"][0], p["tokens"][0], label + " baseline"),
                     "reuse": side(e["reuse"], p["minutes"][1], p["tokens"][1], label + " with library"),
                     "change": {"minutes": p["minutes"][2], "tokens": p["tokens"][2]},
                     "terminal": terminal(e["id"])})
        print(f"{label}: tokens {p['tokens'][0]} -> {p['tokens'][1]} ({p['tokens'][2]:+.0%}), "
              f"agent min {p['minutes'][0]} -> {p['minutes'][1]} ({p['minutes'][2]:+.0%})")
    paper_mean = MEAN_OVERRIDE or paper_mean
    print(f"paper mean change: tokens {paper_mean['tokens']:+.0%}, agent time {paper_mean['minutes']:+.0%}")
    sim = {"donor": {"name": SIM["donor"][0], "video": f"static/videos/sim_videos/{SIM['donor'][1]}"},
           "tasks": [{"name": n, "video": f"static/videos/sim_videos/{f}"} for n, f in SIM["tasks"]],
           "summary": SIM["summary"], "speed": SIM["speed"]}
    return {"speed": EXP_SPEED, "mean_change": paper_mean, "experiments": data, "sim": sim}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="?", help="far_runs directory")
    ap.add_argument("--out", default=ROOT, help="site root to write static/ into")
    ap.add_argument("--stats", action="store_true", help="write experiments.json only, no videos")
    ap.add_argument("--terminals", metavar="DIR", help="only build the terminal clips from DIR (~/far_offline/videos)")
    ap.add_argument("--table", help="paper table (default: far_trials/paper_table.tex next to far_runs)")
    a = ap.parse_args()
    vid_dir, data_dir = os.path.join(a.out, "static", "videos"), os.path.join(a.out, "static", "data")
    os.makedirs(vid_dir, exist_ok=True)
    if a.terminals:  # these recordings live on this workstation, not on WorkStation_4090
        build_terminals(a.terminals, vid_dir)
        return
    if not a.runs:
        ap.error("far_runs directory is required")
    os.makedirs(data_dir, exist_ok=True)
    rows = load_videos(a.runs)
    table = a.table or os.path.join(os.path.dirname(os.path.abspath(a.runs)), "far_trials", "paper_table.tex")
    data = experiment_data(a.runs, rows, vid_dir, videos=not a.stats, table=table)
    with open(os.path.join(data_dir, "experiments.json"), "w") as f:  # generated; do not edit
        json.dump(data, f, ensure_ascii=False, indent=1)
    if not a.stats:
        build_mosaic(rows, vid_dir)
        build_highlights(a.runs, vid_dir)


if __name__ == "__main__":
    main()
