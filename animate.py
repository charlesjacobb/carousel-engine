#!/usr/bin/env python3
"""animate.py — one short clip per slide, as MP4.

    python3 animate.py --deck examples/018-drop.json --out out/018-drop/
    python3 animate.py --deck examples/018-drop.json --motion swipe --seconds 4

Why bother: Instagram takes video as a carousel item, so the same deck that
renders to stills can render to a carousel that moves. The clips are also the
raw material for a Reel.

Two motions, and two is enough:

  drop    every unit falls in from above, top to bottom, one after the other.
  swipe   every unit comes in from the right, in the same order.

The animation is NOT a CSS animation that gets photographed as it happens.
Every frame is computed from its own frame number and applied before the
screenshot, so the clip has exactly the frames it says, renders the same on a
slow machine, and does not depend on the browser's timer. Same reason the
stills are HTML and not a diffusion model: reproducible beats convenient.

Needs ffmpeg on the PATH. No Python package beyond Playwright, which
render.py already requires.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import render

# The units that move. Deliberately a list of ROWS, not of every text node: a
# headline and its kicker arriving separately reads as a build, the same
# headline arriving letter by letter reads as a title template from 2013.
UNIT_SELECTOR = (
    ".stack .line, .headline, .kicker, .crumb, .sub, .marg, .paren, .micro, "
    ".term, .pnote, .stats .tile, .rows .row, .chips .chip, .logs .lg, "
    ".acts .act, .funnel .stg, .strip .it, .footer span, .foot, .counter, "
    ".mark, .slot, .bars, .cluster, .legend, .phead"
)

SETUP_JS = r"""
(sel) => {
  window.__units = [];
  document.querySelectorAll('section.slide').forEach(sl => {
    let els = Array.from(sl.querySelectorAll(sel));
    // Drop any unit that contains another unit. Otherwise a panel and its own
    // rows both animate and the rows travel twice as far as they should.
    els = els.filter(e => !els.some(o => o !== e && e.contains(o)));
    els = els.filter(e => {
      const r = e.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    });
    const sb = sl.getBoundingClientRect();
    // Top to bottom, then left to right: the order the eye reads in.
    els.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return Math.round(ra.top - rb.top) || (ra.left - rb.left);
    });
    // Travel distance per unit, not one distance for all: a 40px footer word
    // dropping the same 96px as a 158px headline swings across its neighbour
    // on the way in and the build reads as a collision instead of a build.
    // Proportional to the unit's own height, clamped so nothing crawls and
    // nothing flies.
    const dist = els.map(e => {
      const h = e.getBoundingClientRect().height;
      return Math.max(22, Math.min(104, h * 0.62));
    });
    window.__units.push({ sl, els, dist, top: sb.top });
  });
  return window.__units.map(u => u.els.length);
}
"""

APPLY_JS = r"""
(arg) => {
  const ease = p => 1 - Math.pow(1 - p, 3);          // easeOutCubic, no overshoot
  const SPREAD = 0.50;   // all entrances start inside the first half of the clip
  const ENTER  = 0.28;   // how long one unit takes to arrive
  window.__units.forEach(u => {
    const n = u.els.length;
    u.els.forEach((el, i) => {
      const start = n > 1 ? (i / (n - 1)) * SPREAD : 0;
      const p = ease(Math.max(0, Math.min(1, (arg.t - start) / ENTER)));
      if (p >= 1) {
        // Arrived: take the inline styles off completely instead of writing
        // identity values. `will-change` and an opacity below 1 both promote
        // the element to its own compositing layer, and a composited layer
        // antialiases differently. With identity values the LAST frame of a
        // clip was not byte-identical to the still of the same slide, which
        // would show as a flicker in a carousel that mixes video and stills.
        el.style.opacity = ''; el.style.transform = ''; el.style.willChange = '';
        return;
      }
      const d = 1 - p;
      const travel = u.dist[i];
      el.style.willChange = 'transform, opacity';
      el.style.opacity = p.toFixed(4);
      el.style.transform = arg.motion === 'swipe'
        ? `translateX(${(d * travel * 2.4).toFixed(2)}px)`
        : `translateY(${(-d * travel).toFixed(2)}px)`;
    });
  });
}
"""

CLEAR_JS = r"""
() => window.__units.forEach(u => u.els.forEach(el => {
  el.style.opacity = ''; el.style.transform = ''; el.style.willChange = '';
}))
"""


async def shoot_frames(html_path: Path, deck: dict, motion: str, seconds: float,
                       fps: int, want: list[int], frame_root: Path) -> list[tuple[int, Path]]:
    from playwright.async_api import async_playwright

    total = max(2, int(round(seconds * fps)))
    out: list[tuple[int, Path]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": render.W, "height": render.H})
        await page.goto(html_path.as_uri())
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass
        await page.wait_for_timeout(400)
        await page.evaluate(render.AUTOFIT_JS)      # same width fitting as the stills
        await page.wait_for_timeout(120)
        counts = await page.evaluate(SETUP_JS, UNIT_SELECTOR)
        nodes = await page.query_selector_all("section.slide")

        for i in want:
            slide = deck["slides"][i]
            name = slide.get("name") or slide.get("type", "slide")
            slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
            fdir = frame_root / f"{i + 1:02d}-{slug}"
            fdir.mkdir(parents=True, exist_ok=True)
            print(f"  {i + 1:02d} {slug}  {counts[i]} units, {total} frames")
            for f in range(total):
                t = f / (total - 1)
                await page.evaluate(APPLY_JS, {"t": t, "motion": motion})
                await nodes[i].screenshot(path=str(fdir / f"{f:05d}.png"))
            out.append((i, fdir))
        await page.evaluate(CLEAR_JS)
        await browser.close()
    return out


def encode(frame_dir: Path, target: Path, fps: int, silent_track: bool) -> None:
    """Frames to H.264. yuv420p and even dimensions because everything else is
    what makes a file play everywhere except on the one phone that matters."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", str(frame_dir / "%05d.png")]
    if silent_track:
        # Instagram has rejected silent files often enough that a real, empty
        # audio track is cheaper than finding out again on posting day.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest", "-c:a", "aac", "-b:a", "64k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="carousel animator (1080×1350 MP4)")
    ap.add_argument("--deck", required=True, help="path to the deck.json")
    ap.add_argument("--out", default="", help="output folder (default: <deck-dir>/video)")
    ap.add_argument("--motion", choices=("drop", "swipe"), default="drop")
    ap.add_argument("--seconds", type=float, default=5.0, help="clip length (4 to 6 is the useful range)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--slide", type=int, default=0,
                    help="render only slide N (1-based); 0 renders every slide")
    ap.add_argument("--keep-frames", action="store_true", help="keep the PNG frames")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg is not on the PATH. Install it and run again.", file=sys.stderr)
        return 2

    deck_path = Path(args.deck).expanduser().resolve()
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slides = deck.get("slides", [])
    if not slides:
        print("The deck has no slides.", file=sys.stderr)
        return 2
    if not 4.0 <= args.seconds <= 6.0:
        print(f"NOTE: {args.seconds}s is outside the 4 to 6 second range these clips "
              f"are tuned for; the entrances will feel rushed or stretched.", file=sys.stderr)

    raw = json.dumps(deck, ensure_ascii=False).replace("{{DAY}}", render.day_number())
    deck = json.loads(raw)

    want = [args.slide - 1] if args.slide else list(range(len(slides)))
    for i in want:
        if not 0 <= i < len(slides):
            print(f"Slide {i + 1} does not exist, the deck has {len(slides)}.", file=sys.stderr)
            return 2

    out_dir = Path(args.out).expanduser() if args.out else deck_path.parent / "video"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = deck_path.parent / f".{deck_path.stem}.animate.html"
    html_path.write_text(render.build_html(deck, deck_path.parent), encoding="utf-8")

    print(f"{len(want)} clip(s) → {out_dir}  ({args.motion}, {args.seconds}s, {args.fps} fps)")
    tmp = (out_dir / "frames") if args.keep_frames else Path(tempfile.mkdtemp(prefix="carousel-"))
    try:
        shot = asyncio.run(shoot_frames(html_path, deck, args.motion, args.seconds,
                                        args.fps, want, tmp))
        written = []
        for i, fdir in shot:
            target = out_dir / f"{fdir.name}.mp4"
            encode(fdir, target, args.fps, silent_track=True)
            size = target.stat().st_size / 1024
            print(f"  ✓ {target.name}  {size:.0f} KB")
            written.append(target)
    finally:
        html_path.unlink(missing_ok=True)
        if not args.keep_frames:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== {len(written)}/{len(want)} encoded ===")
    return 0 if len(written) == len(want) else 1


if __name__ == "__main__":
    raise SystemExit(main())
