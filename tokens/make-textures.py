#!/usr/bin/env python3
"""make-textures.py — the six surface textures the engine ships with.

Generates seamless, tileable PNGs:
  tex-grain.png     256²  film grain       → GRAIN (opacity 1), CORD (.60), SCAN (.45)
  tex-felt.png      512²  mottled felt     → FELT (.85, background-size 440px)
  tex-scan.png      320²  scan streaks     → SCAN (.80)
  tex-paper.png     512²  laid paper fibre → bg texture "paper"
  tex-concrete.png  512²  cast concrete    → bg texture "concrete"
  tex-halftone.png  160²  45° dot screen   → bg texture "halftone"

The first three pair with a surface; the last three exist only for
`"bg": {"kind": "texture", "name": "..."}` and stand on their own.

Note: these are generated, not photographed. Drop your own PNGs into assets/
under the same file names and everything picks them up automatically — the
engine only ever references the paths, never the pixels.

All three are RGBA with an alpha channel: they sit as an overlay on top of
the base colour, so only the alpha channel carries information, never a
background of its own.
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

OUT = Path(__file__).resolve().parent / "assets"
RNG = np.random.default_rng(20260801)  # fixed seed → reproducible


def _tile_blur(a: np.ndarray, radius: float) -> np.ndarray:
    """Gaussian blur that wraps across the edges → stays tileable."""
    h, w = a.shape
    big = np.tile(a, (3, 3))
    img = Image.fromarray(big.astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(img, dtype=float)[h:2 * h, w:2 * w]


def _to_alpha_png(alpha: np.ndarray, path: Path, white: bool = True) -> None:
    """Save greyscale values as alpha on a white (or black) plane."""
    h, w = alpha.shape
    rgb = np.full((h, w, 3), 255 if white else 0, dtype=np.uint8)
    a = np.clip(alpha, 0, 255).astype(np.uint8)
    Image.fromarray(np.dstack([rgb, a]), mode="RGBA").save(path, optimize=True)
    print(f"  ✓ {path.name}  {w}×{h}")


def grain(size: int = 256) -> None:
    """Fine film grain — high-frequency noise, minimally smoothed."""
    n = RNG.normal(0, 1, (size, size))
    n = _tile_blur((n - n.min()) / (n.max() - n.min()) * 255, 0.5)
    n = (n - n.mean()) / (n.std() + 1e-9)
    _to_alpha_png(np.abs(n) * 14, OUT / "tex-grain.png")


def felt(size: int = 512) -> None:
    """Mottled felt — several octaves of soft noise (cloud-like character)."""
    acc = np.zeros((size, size))
    for octave, weight in ((4, 1.0), (8, 0.55), (16, 0.3), (32, 0.18)):
        small = RNG.random((octave, octave))
        # Tile 3×3 BEFORE upscaling, then take the middle field:
        # that way BICUBIC interpolates across the tile boundary and the edge
        # lines up seamlessly with the opposite side (otherwise you get a visible seam).
        tiled = np.tile(small, (3, 3))
        up = np.asarray(
            Image.fromarray((tiled * 255).astype(np.uint8))
            .resize((size * 3, size * 3), Image.BICUBIC),
            dtype=float,
        )[size:2 * size, size:2 * size]
        acc += _tile_blur(up, 1.5) * weight
    acc = (acc - acc.min()) / (acc.max() - acc.min())
    # Amplitude deliberately kept low: the handoff says «it should be felt, not seen».
    # At 46 the surface turned visibly cloudy instead of merely alive.
    _to_alpha_png(acc * 24, OUT / "tex-felt.png")


def scan(size: int = 320) -> None:
    """Scan streaks + dust — horizontal lines, irregular, plus speckles."""
    rows = RNG.random(size)
    streaks = np.tile((rows > 0.72).astype(float)[:, None], (1, size))
    streaks *= RNG.uniform(0.35, 1.0, (size, 1))          # streaks vary in strength
    streaks *= RNG.uniform(0.75, 1.0, (size, size))       # fray along the line
    streaks = _tile_blur(streaks * 255, 0.7)
    dust = (RNG.random((size, size)) > 0.9975).astype(float) * 255
    dust = _tile_blur(dust, 0.6)
    _to_alpha_png(np.clip(streaks * 0.13 + dust * 0.5, 0, 255), OUT / "tex-scan.png")


def _octaves(size: int, spec, blur: float = 1.5) -> np.ndarray:
    """Sum of upscaled random fields, normalised to 0..1. Same 3×3 trick as in
    felt(): tile before the upscale so BICUBIC interpolates across the seam."""
    acc = np.zeros((size, size))
    for octave, weight in spec:
        tiled = np.tile(RNG.random((octave, octave)), (3, 3))
        up = np.asarray(
            Image.fromarray((tiled * 255).astype(np.uint8))
            .resize((size * 3, size * 3), Image.BICUBIC),
            dtype=float,
        )[size:2 * size, size:2 * size]
        acc += _tile_blur(up, blur) * weight
    return (acc - acc.min()) / (acc.max() - acc.min() + 1e-9)


def paper(size: int = 512) -> None:
    """Laid paper — fine fibre running one way, over a faint cloud. The two
    have to stay far apart in frequency: at similar scales the fibre reads as
    noise on top of noise instead of as a material with a direction."""
    cloud = _octaves(size, ((8, 1.0), (24, 0.45)))
    fibre = RNG.normal(0, 1, (size, size))
    # Smear along x only, wrapping, so the fibre gets a direction without a seam
    fibre = sum(np.roll(fibre, k, axis=1) for k in range(-3, 4)) / 7.0
    fibre = (fibre - fibre.min()) / (fibre.max() - fibre.min() + 1e-9)
    _to_alpha_png(cloud * 16 + fibre * 20, OUT / "tex-paper.png")


def concrete(size: int = 512) -> None:
    """Cast concrete — coarse mottling plus sparse pits. The pits do the work:
    without them it is just cloud, and cloud at this amplitude reads as a badly
    compressed gradient rather than as a cast surface."""
    body = _octaves(size, ((3, 1.0), (6, 0.6), (12, 0.35), (48, 0.14)), blur=2.0)
    pits = (RNG.random((size, size)) > 0.9965).astype(float)
    pits = _tile_blur(pits * 255, 1.2)
    _to_alpha_png(np.clip(body * 30 + pits * 0.55, 0, 255), OUT / "tex-concrete.png")


def halftone(size: int = 160, pitch: int = 20) -> None:
    """45° dot screen. `size` must be a whole multiple of `pitch` and the row
    count must be even, otherwise the half-row offset does not line up across
    the tile edge and you get a visible break every 160px."""
    assert size % pitch == 0 and (size // pitch) % 2 == 0, "size must be an even multiple of pitch"
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    row = (yy // pitch).astype(int)
    cx = (xx % pitch) - pitch / 2 + np.where(row % 2 == 1, pitch / 2, 0)
    cx = (cx + pitch / 2) % pitch - pitch / 2      # wrap the offset back into the cell
    cy = (yy % pitch) - pitch / 2
    d = np.sqrt(cx ** 2 + cy ** 2)
    dots = np.clip((pitch * 0.26 - d) * 255, 0, 255)
    _to_alpha_png(_tile_blur(dots, 0.8), OUT / "tex-halftone.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    print("surface textures (seamless):")
    grain()
    felt()
    scan()
    paper()
    concrete()
    halftone()
    print(f"→ {OUT}")
