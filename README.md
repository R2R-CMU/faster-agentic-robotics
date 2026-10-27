# FAR: Faster Agentic Robotics with Fewer Tokens — project page

Static project page served with GitHub Pages from the repository root (`main` branch).

- `index.html` — the page; the transfer cards render from `static/data/experiments.json`
- `static/images/` — figures (replace the figure placeholder with `mechanism.png` when ready)
- `static/videos/` — videos, built by `scripts/build_media.py`. Optional slots fill in by themselves once a file exists:
  `<id>_terminal.mp4` for each experiment's terminal column, `highlight{1,2}_{a,b,c,d}.mp4` for the highlights.
- `scripts/build_media.py` — builds the mosaic, the per-experiment clips and `static/data/experiments.json`
  (agent time and tokens, same seeds as the paper's table) from `far_runs`. The full set of videos is on
  WorkStation_4090, so build there and copy `static/` back:

  ```bash
  ssh WorkStation_4090 'mkdir -p /tmp/far_site/scripts'
  scp scripts/build_media.py WorkStation_4090:/tmp/far_site/scripts/
  ssh WorkStation_4090 'cd /tmp/far_site && python3 scripts/build_media.py ~/CoRL_late-breaking/agentic_robotics_real/far_runs --out /tmp/far_site'
  scp -r WorkStation_4090:/tmp/far_site/static/videos WorkStation_4090:/tmp/far_site/static/data static/
  ```

  `--stats` regenerates `experiments.json` only.

Layout adapted from the [Nerfies](https://github.com/nerfies/nerfies.github.io) template (CC BY-SA 4.0).
