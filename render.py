#!/usr/bin/env python3
"""render.py — carousel slides from a deck.json to PNG, 1080×1350 for the feed
or 1080×1920 for a story.

Why HTML instead of an image model: this design system is specified down to
the pixel (exact fonts, glow radii, texture opacities, hex values). A diffusion
model does not hit that — and across 8 information slides it will misspell
something, guaranteed. Rendered HTML is exact, reproducible, versionable and
costs nothing.

    python3 render.py --deck examples/014-mix.json --out out/014-mix/
    python3 render.py --deck examples/018-drop.json --format story

deck.json:
{
  "slug": "agent-security-audit",
  "title": "…",                      # documentation only
  "slides": [
    {"type": "cover",  "surface": "cord",
     "stack": ["FOUR HOLES.", "TEN DAYS.", "ONE STACK."],
     "sub": "…", "footer": ["HOLES.", "DAYS.", "STACK."]},
    {"type": "stake",  "surface": "felt",
     "headline": "I audited my own <em>agent stack</em>", "micro": "…"},
    {"type": "insight","surface": "grain",
     "kicker": "01 · SANDBOX", "headline": "…", "micro": "…", "term": "…"},
    {"type": "proof",  "kicker": "RECEIPT", "headline": "…", "term": "…", "micro": "…"},
    {"type": "photo",  "image": "photos/shot.png", "headline": "…", "micro": "…"},
    {"type": "cta",    "surface": "felt", "headline": "…",
     "actions": ["Save this…", "Send this…"],
     "slots": {"tl": "…", "tr": "…", "tc": "…", "bl": "…", "br": "…"}}
  ]
}

`headline`/`micro`/`term` may contain <em>…</em> and <b>…</b> as accent markup.
Everything else is escaped — text comes from the deck, not from HTML.

bg — valid on EVERY slide type, replaces the surface:

  {"kind": "photo",    "src": "photos/x.jpg", "focus": "50% 30%",
                       "dim": 0.55, "grade": "cool|warm|none", "scrim": "ink|paper"}
  {"kind": "gradient", "from": "ink", "to": "accent", "angle": 135}
  {"kind": "texture",  "name": "felt|grain|scan|paper|concrete|halftone",
                       "opacity": 0.8}

`from`/`to` name palette tokens from a fixed table, never raw CSS. `dim` is the
opacity of a flat scrim in the slide's own base colour: 0 is the raw photo, 1 a
solid plane. Measured on the shipped plates with lint.py's CONTRAST rule, type
stops clearing WCAG below `dim` 0.50 on ink and below 0.70 on paper — and the
first line to go is the muted counter, not the headline. Your images will sit
somewhere else; run the linter instead of copying the number.

Every texture gets an offset seeded from the deck slug and the slide index, so
no two slides show the same corner of the same tile and the same deck still
renders identically tomorrow.

Colour is a calibration, not part of the code. Without one the engine renders
black and white. One hex sets it, first match wins:

  --accent "#RRGGBB" on the command line
  "accent": "#RRGGBB" in the deck.json
  "accent": "#RRGGBB" in tokens/brand.local.json (gitignored)
  "accent": "#RRGGBB" in tokens/brand.json

`accent_print` may sit next to `accent` in the same place, for a brand with a
defined paper shade; otherwise it is derived. calibrate.py prints what a hex
turns into and whether it holds.

`--format feed|story` picks the canvas, feed (4:5) by default. A story is the
same deck on 9:16 with a band at the top and one at the bottom that type stays
out of, because Instagram draws its own UI there. It renders to stills-story/
unless --out says otherwise, so it never overwrites the feed stills.
"""
from __future__ import annotations

import argparse
import asyncio
import colorsys
import hashlib
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

ENGINE = Path(__file__).resolve().parent
TOKENS = ENGINE / "tokens"
DAY_ZERO = date(2024, 1, 1)   # fallback day counter origin; override in tokens/brand.json


class Format(NamedTuple):
    """A canvas, and the bands at its top and bottom that type has to stay out
    of because the platform draws its own UI over them."""
    w: int
    h: int
    no_type_top: int
    no_type_bottom: int
    suffix: str        # on the default output folder, so formats never overwrite each other


# render.py, lint.py and animate.py all read their size from here. Feed is the
# canvas carousel.css is written for (--slide-w, --slide-h); any other format
# hands its numbers to the stylesheet through build_html().
FORMATS = {
    "feed":  Format(1080, 1350, 0, 0, ""),
    "story": Format(1080, 1920, 180, 250, "-story"),
}


def format_args(ap: argparse.ArgumentParser) -> None:
    """The same switch on every command that builds a page, so the linter
    measures the canvas the renderer shoots."""
    ap.add_argument("--format", choices=tuple(FORMATS), default="feed",
                    help="feed = 1080×1350 (default), story = 1080×1920")


# Each surface is a base fill plus the texture layers that belong to it. The
# layers are named, not spelled out as HTML, because every one of them gets a
# per-slide offset stamped on at render time (see _seed_offset).
SURFACES = {
    "cord":  (("tex tex-lines", "tex tex-grain"), "surface-cord"),
    "grain": (("tex tex-grain",), "surface-grain"),
    "felt":  (("tex tex-felt", "vignette"), "surface-felt"),
    "scan":  (("tex tex-scan", "tex tex-grain"), "surface-scan"),
}
# Surface names used by v0.1 decks. Kept as aliases so existing decks keep
# rendering after the rename.
SURFACE_ALIASES = {"kord": "cord", "korn": "grain", "filz": "felt"}

# --- bg ---------------------------------------------------------------------
# `bg` is valid on EVERY slide type and replaces the surface treatment. Three
# kinds: photo (a real image behind the content), gradient (two token colours),
# texture (one of the six shipped tiles).

TEXTURES = {"felt", "grain", "scan", "paper", "concrete", "halftone"}
GRADES = {"none", "cool", "warm"}
SCRIMS = {"ink", "paper"}
# Colours a deck may name in a gradient. A fixed table on purpose: a deck must
# never be able to write arbitrary CSS into a style attribute, and a short name
# survives a token rename better than a raw custom property does.
BG_COLORS = {
    "ink": "--ink", "ink-cord": "--ink-cord", "ink-scan": "--ink-scan",
    "ink-frame": "--ink-frame", "accent": "--accent", "accent-print": "--accent-print",
    "accent-light": "--accent-light",
    "paper": "--paper", "fg-1": "--fg-1", "fg-2": "--fg-2", "fg-3": "--fg-3",
    "white": "--on-ink",
}

# --- colour -----------------------------------------------------------------
# A deck, a brand file or the command line hands in ONE hex. Everything the
# stylesheets read is derived from it here, so a colour never has to be typed
# twice and can never be half changed.

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
INK, PAPER = "#0A0A0A", "#FFFFFF"   # --ink and --paper in tokens.css
PRINT_CONTRAST = 4.5                # the print shade has to carry 22px type on paper
LIGHT_STEP = 0.08                   # «one step lighter», in HLS lightness


def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(c * 255):02X}" for c in rgb)


def luminance(hex_: str) -> float:
    """WCAG relative luminance, the same formula lint.py measures pixels with."""
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(hex_)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def shade(hex_: str, step: float) -> str:
    """Same hue, same saturation, lightness moved by `step`."""
    h, l, s = colorsys.rgb_to_hls(*_rgb(hex_))
    return _hex(colorsys.hls_to_rgb(h, max(0.0, min(1.0, l + step)), s))


def toward(hex_: str, bg: str, need: float) -> str:
    """The nearest shade of `hex_` that clears `need` against `bg`: darker on a
    light ground, lighter on a dark one. The colour itself if it already holds."""
    step = -0.005 if luminance(bg) > 0.18 else 0.005
    c, n = hex_, 0
    while contrast(c, bg) < need and n < 200:
        n += 1
        c = shade(hex_, step * n)
    return c


def derive(accent: str, accent_print: str | None = None) -> dict[str, str]:
    """One hex in, the four accent tokens out. The print shade is darkened until
    it reaches 4.5:1 on --paper, unless the brand names its own."""
    r, g, b = (int(accent[i:i + 2], 16) for i in (1, 3, 5))
    return {
        "--accent": accent.upper(),
        "--accent-rgb": f"{r},{g},{b}",
        "--accent-light": shade(accent, LIGHT_STEP),
        "--accent-print": (accent_print or toward(accent, PAPER, PRINT_CONTRAST)).upper(),
    }


def _read_json(p: Path) -> dict:
    """A missing file is no setting. A broken one is an error, not a silent
    fallback to somebody else's mark or colour."""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{p.name} is not valid JSON: {e}") from None


def accent_chain(deck: dict, accent: str | None = None,
                 accent_print: str | None = None) -> tuple[str | None, str | None, str]:
    """(accent, accent_print, where it came from). One chain, one winner: the
    first place that names an accent decides, and its accent_print comes with
    it. Mixing a deck's accent with a brand file's print shade would pair two
    colours nobody chose together. (None, None, …) means black and white."""
    places = [("--accent", {"accent": accent, "accent_print": accent_print}),
              ("deck.json", deck),
              ("tokens/brand.local.json", _read_json(TOKENS / "brand.local.json")),
              ("tokens/brand.json", _read_json(TOKENS / "brand.json"))]
    for where, src in places:
        a, p = src.get("accent"), src.get("accent_print")
        if a is None and p is None:
            continue
        if a is None:
            raise ValueError(f'{where}: accent_print needs an accent next to it')
        for key, v in (("accent", a), ("accent_print", p)):
            if v is not None and not HEX.match(str(v)):
                raise ValueError(f'{where}: {key} "{v}" is not a colour, write it as "#1A2B3C"')
        return a, p, where
    return None, None, "black and white"


def accent_args(ap: argparse.ArgumentParser) -> None:
    """The same two flags on every command that builds a page, so the linter
    checks exactly the colour the renderer paints."""
    ap.add_argument("--accent", metavar="#RRGGBB",
                    help="accent colour, beats deck.json and tokens/brand*.json")
    ap.add_argument("--accent-print", "--accent_print", dest="accent_print", metavar="#RRGGBB",
                    help="its shade for paper, derived from --accent when left out")


def _seed_offset(slug: str, idx: int) -> tuple[int, int]:
    """Texture offset for one slide, derived from the deck slug and the slide
    index. Deterministic, so the same deck renders the same way tomorrow.
    Without it every slide shows the identical corner of the same 256px tile
    and a carousel of eight reads as eight screenshots of one surface."""
    h = hashlib.md5(f"{slug}:{idx}".encode("utf-8")).digest()
    return h[0] * 2 % 512, h[1] * 2 % 512


def _tex_layers(classes, ox: int, oy: int) -> str:
    """Texture divs with the seeded offset stamped on. The vignette is the one
    layer that must NOT move: it is anchored to the slide, not to a tile."""
    out = []
    for c in classes:
        if c == "vignette":
            out.append('<div class="vignette"></div>')
        else:
            out.append(f'<div class="{c}" style="background-position:{ox}px {oy}px"></div>')
    return "".join(out)


def build_bg(bg: dict, ox: int, oy: int, on_paper: bool) -> tuple[str, str]:
    """One `bg` spec to (layer HTML, extra class on the section).

      {"kind":"texture",  "name":"felt|grain|scan|paper|concrete|halftone", "opacity":0.8}
      {"kind":"gradient", "from":"ink", "to":"accent", "angle":135}
      {"kind":"photo",    "src":"photos/x.jpg", "focus":"50% 30%",
                          "dim":0.55, "grade":"cool|warm|none", "scrim":"ink|paper"}

    Every unknown value raises instead of rendering something almost right —
    a silently ignored `grade` is a bug you only find on the finished PNG.
    """
    kind = bg.get("kind", "")

    if kind == "texture":
        name = bg.get("name", "grain")
        if name not in TEXTURES:
            raise ValueError(f'bg texture "{name}" is unknown, pick one of {sorted(TEXTURES)}')
        style = f"background-position:{ox}px {oy}px"
        if bg.get("opacity") is not None:
            style += f";opacity:{float(bg['opacity'])}"
        return f'<div class="tex tex-{name}" style="{style}"></div>', "bg-texture"

    if kind == "gradient":
        a, b = bg.get("from", "ink"), bg.get("to", "accent")
        for c in (a, b):
            if c not in BG_COLORS:
                raise ValueError(f'bg colour "{c}" is unknown, pick one of {sorted(BG_COLORS)}')
        angle = float(bg.get("angle", 135))
        return (f'<div class="bg-fill" style="background:linear-gradient({angle}deg,'
                f'var({BG_COLORS[a]}),var({BG_COLORS[b]}))"></div>', "bg-gradient")

    if kind == "photo":
        src = bg.get("src", "")
        if not src:
            raise ValueError('bg kind "photo" needs a "src"')
        grade = bg.get("grade", "none")
        if grade not in GRADES:
            raise ValueError(f'bg grade "{grade}" is unknown, pick one of {sorted(GRADES)}')
        scrim = bg.get("scrim") or ("paper" if on_paper else "ink")
        if scrim not in SCRIMS:
            raise ValueError(f'bg scrim "{scrim}" is unknown, pick one of {sorted(SCRIMS)}')
        # dim is the opacity of a flat scrim in the slide's own base colour.
        # 0 is the raw photo, 1 is a solid plane. Below ~0.45 white display type
        # stops clearing 3:1 on a busy image — lint.py measures it, see CONTRAST.
        dim = max(0.0, min(1.0, float(bg.get("dim", 0.55))))
        focus = html.escape(str(bg.get("focus", "center")))
        return (f'<div class="bg-photo grade-{grade}" '
                f'style="background-image:url(\'{html.escape(src)}\');'
                f'background-position:{focus}"></div>'
                f'<div class="bg-dim scrim-{scrim}" style="opacity:{dim}"></div>',
                "has-bg-photo")

    raise ValueError(f'bg kind "{kind}" is unknown, pick photo, gradient or texture')
# <i> has been allowed since the dashboard type: inside a terminal block it
# marks the dimmed prompt/comment line, inside a breadcrumb the red slash.
ALLOWED_TAGS = re.compile(r"&lt;(/?(?:em|b|i))&gt;")


def rich(text: str) -> str:
    """Escape, but let <em> and <b> through as markup."""
    return ALLOWED_TAGS.sub(r"<\1>", html.escape(str(text or "")))


def day_number() -> str:
    """Day N since dayZero, Swiss apostrophe as the thousands separator. It has
    to be correct at render time — the whole point is that the number is still
    true tomorrow."""
    dz = _brand().get("day_zero")
    base = date.fromisoformat(dz) if dz else DAY_ZERO
    n = (date.today() - base).days
    return f"{n:,}".replace(",", "’")


def _brand() -> dict:
    """tokens/brand.json: {"mark": "yb.", "handle": "@yourhandle", "day_zero": "2024-01-01"}

    tokens/brand.local.json, if present, overrides any of these keys. Like
    brand.local.css it is gitignored, so your real mark and handle never end up
    in a public fork of this repo. Both files may also carry "accent" and
    "accent_print", see accent_chain()."""
    out: dict = {}
    for name in ("brand.json", "brand.local.json"):
        out.update(_read_json(TOKENS / name))
    return out


def mark() -> str:
    t = _brand().get("mark", "yb.")
    return f'<div class="mark">{html.escape(t[:-1])}<i>{html.escape(t[-1:])}</i></div>'


def cross(style: str) -> str:
    return f'<div class="cross" style="{style}"></div>'


# Cascade lines are set to `white-space: nowrap` (a system rule — the stack is a
# shape, not body copy). So that an arbitrarily long claim still never runs out
# of the frame, every line is scaled down to the available width after the fonts
# have loaded. Down yes, up never: nothing grows past its design size.
AUTOFIT_JS = """
() => {
  const PAD = 72, SAFE_RIGHT = 56;
  document.querySelectorAll('.slide.cover .stack .line, .slide.photo .stack .line').forEach(el => {
    const ml = parseFloat(getComputedStyle(el).marginLeft) || 0;
    const avail = 1080 - PAD - SAFE_RIGHT - ml;
    const base = parseFloat(getComputedStyle(el).fontSize);
    // scrollWidth, NOT getBoundingClientRect: .line is block-level, so the rect
    // reports the container width. Under nowrap only scrollWidth carries the
    // real text width — otherwise the scaling never kicks in and the claim
    // runs off the frame.
    const w = el.scrollWidth;
    if (w > avail) el.style.fontSize = Math.floor(base * (avail / w) * 100) / 100 + 'px';
  });
}
"""


# ---------------------------------------------------------------------------
# Dashboard building blocks. Each takes raw deck data and returns HTML; empty
# input returns "" — that way unused fields can simply be left out of the
# deck.json instead of rendering empty boxes (same rule as the CTA slots).
# ---------------------------------------------------------------------------

def _pill(spec) -> str:
    """Status pill. A string is neutral, a dict with alert=true is red."""
    if not spec:
        return ""
    text, alert = (spec.get("text", ""), spec.get("alert")) if isinstance(spec, dict) else (spec, False)
    return f'<span class="pill{" alert" if alert else ""}">{rich(text)}</span>'


def _stats(items: list, cols: int = 2) -> str:
    """Tile grid: big number plus label. `v` allows <em> for red part-numbers."""
    if not items:
        return ""
    tiles = "".join(
        '<div class="tile">'
        + (f'<div class="k">{rich(i["k"])}</div>' if i.get("k") else "")
        + f'<div class="v">{rich(i.get("v", ""))}</div>'
        + (f'<div class="s">{rich(i["s"])}</div>' if i.get("s") else "")
        + "</div>"
        for i in items
    )
    return f'<div class="stats" style="--cols:{int(cols)}">{tiles}</div>'


def _rows(items: list) -> str:
    """Table rows with a status dot. dot: ok | alert | (empty = dimmed)."""
    if not items:
        return ""
    out = []
    for i in items:
        dot = str(i.get("dot", "")).strip()
        dot_cls = f" {dot}" if dot in ("ok", "alert") else ""
        out.append(
            f'<div class="row"><span class="dot{dot_cls}"></span>'
            + (f'<span class="lb">{rich(i["lb"])}</span>' if i.get("lb") else "")
            + f'<span class="meta">{rich(i.get("meta", ""))}</span>'
            + (f'<span class="val">{rich(i["val"])}</span>' if i.get("val") else "")
            + "</div>"
        )
    return f'<div class="rows">{"".join(out)}</div>'


def _bars(items: list) -> str:
    """Bars from percentages (0–100). Without an explicit flag the last bar is
    highlighted — «now» is the statement a bar chart carries."""
    if not items:
        return ""
    last = len(items) - 1
    out = []
    for n, i in enumerate(items):
        pct = max(0, min(100, float(i.get("value", 0))))
        if i.get("hot"):
            cls = " hot"
        elif i.get("now") or (n == last and not any(x.get("now") for x in items)):
            cls = " now"
        else:
            cls = ""
        out.append(f'<div class="bar{cls}" style="height:{pct:.0f}%"></div>')
    return f'<div class="bars">{"".join(out)}</div>'


def _cluster(spec: dict) -> str:
    """Dot cloud for ratios: `total` dots, of which `on` are bright and `hot` red.
    Proportions are shown, not asserted — the number sits next to it in the text."""
    if not spec:
        return ""
    total = int(spec.get("total", 0))
    if total <= 0:
        return ""
    on, hot = int(spec.get("on", 0)), int(spec.get("hot", 0))
    pts = []
    for n in range(total):
        if n < hot:
            pts.append('<span class="pt hot"></span>')
        elif n < hot + on:
            pts.append('<span class="pt on"></span>')
        else:
            pts.append('<span class="pt"></span>')
    return f'<div class="cluster">{"".join(pts)}</div>'


def _chips(items: list, n: int = 0) -> str:
    """Channel/connection cells. `hot` turns the dot red."""
    if not items:
        return ""
    cells = "".join(
        f'<div class="chip{" hot" if i.get("hot") else ""}">'
        f'<div class="t">{rich(i.get("t", ""))}</div>'
        + (f'<div class="d">{rich(i["d"])}</div>' if i.get("d") else "")
        + "</div>"
        for i in items
    )
    return f'<div class="chips" style="--n:{int(n) or len(items)}">{cells}</div>'


def _logs(items: list) -> str:
    """Event lines: dot · channel tag · sender · preview · time · badge.
    The densest zone — this is where the «real program» impression comes from."""
    if not items:
        return ""
    out = []
    for i in items:
        sq = str(i.get("sq", "")).strip()
        out.append(
            f'<div class="lg"><span class="sq{" " + sq if sq in ("on", "hot") else ""}"></span>'
            + (f'<span class="tag">{rich(i["tag"])}</span>' if i.get("tag") else "")
            + (f'<span class="who">{rich(i["who"])}</span>' if i.get("who") else "")
            + f'<span class="txt">{rich(i.get("txt", ""))}</span>'
            + (f'<span class="ago">{rich(i["ago"])}</span>' if i.get("ago") else "")
            + (f'<span class="badge">{rich(i["badge"])}</span>' if i.get("badge") else "")
            + "</div>"
        )
    return f'<div class="logs">{"".join(out)}</div>'


def _acts(items: list) -> str:
    """Rows with an action button — the button is a prop, the image stands still."""
    if not items:
        return ""
    out = "".join(
        '<div class="act"><span class="sq"></span>'
        f'<span class="nm">{rich(i.get("nm", ""))}</span>'
        + (f'<span class="st">{rich(i["st"])}</span>' if i.get("st") else "")
        + (f'<span class="btn">{rich(i["btn"])}</span>' if i.get("btn") else "")
        + "</div>"
        for i in items
    )
    return f'<div class="acts">{out}</div>'


def _funnel(items: list) -> str:
    """Stages side by side, each with a proportional dot cloud underneath."""
    if not items:
        return ""
    out = []
    for i in items:
        cl = i.get("cluster") or {}
        out.append(
            '<div class="stg">'
            f'<div class="sl">{rich(i.get("sl", ""))}</div>'
            f'<div class="sv">{rich(i.get("sv", ""))}</div>'
            + (f'<div class="sp">{rich(i["sp"])}</div>' if i.get("sp") else "")
            + _cluster(cl)
            + "</div>"
        )
    return f'<div class="funnel" style="--n:{len(items)}">{"".join(out)}</div>'


def _legend(items: list) -> str:
    if not items:
        return ""
    out = "".join(
        f'<span class="{html.escape(str(i.get("s", "")))}">{rich(i.get("t", ""))}</span>'
        for i in items
    )
    return f'<div class="legend">{out}</div>'


# Block type → renderer. The deck decides the order, not the code.
BLOCKS = {
    "chips":   lambda b: _chips(b.get("items", []), b.get("cols", 0)),
    "stats":   lambda b: _stats(b.get("items", []), b.get("cols", 2)),
    "rows":    lambda b: _rows(b.get("items", [])),
    "logs":    lambda b: _logs(b.get("items", [])),
    "acts":    lambda b: _acts(b.get("items", [])),
    "funnel":  lambda b: _funnel(b.get("items", [])),
    "bars":    lambda b: _bars(b.get("items", [])),
    "cluster": lambda b: _cluster(b),
    "legend":  lambda b: _legend(b.get("items", [])),
    "term":    lambda b: f'<div class="term">{rich(b.get("text", ""))}</div>' if b.get("text") else "",
    "note":    lambda b: f'<div class="pnote">{rich(b.get("text", ""))}</div>' if b.get("text") else "",
}


def _blocks(blocks: list) -> str:
    """Panel content from an ordered block list. A block with `label` becomes a
    labelled region — that is the difference between «box with content» and
    «program window with areas»."""
    out = []
    for b in blocks or []:
        inner = BLOCKS.get(b.get("kind", ""), lambda _: "")(b)
        if not inner:
            continue
        cls = "region" if b.get("label") else ""
        if b.get("grow"):
            cls = (cls + " grow").strip()
        if cls:
            lab = f'<div class="rlabel">{rich(b["label"])}</div>' if b.get("label") else ""
            inner = f'<div class="{cls}">{lab}{inner}</div>'
        out.append(inner)
    return "".join(out)


def render_slide(s: dict, idx: int, total: int, slug: str = "") -> str:
    t = s.get("type", "insight")
    surface = s.get("surface", "grain")
    surface = SURFACE_ALIASES.get(surface, surface)
    tex_classes, scls = SURFACES.get(surface, SURFACES["grain"])
    ox, oy = _seed_offset(slug, idx)
    counter = f'<div class="counter">{idx:02d}/{total:02d}</div>'
    body = ""

    if t == "cover":
        lines = "".join(
            f'<div class="line{" white" if i == 2 else ""}">{rich(l)}</div>'
            for i, l in enumerate(s.get("stack", []))
        )
        footer = "".join(f"<span>{rich(f)}</span>" for f in s.get("footer", []))
        body = (
            f'{mark()}{counter}'
            f'<div class="stack">{lines}</div>'
            + (f'<div class="sub">{rich(s["sub"])}</div>' if s.get("sub") else "")
            + (f'<div class="footer">{footer}</div>' if footer else "")
        )

    elif t == "stake":
        body = (
            f'{counter}<div class="body">'
            f'<div class="headline">{rich(s.get("headline",""))}</div>'
            + (f'<div class="micro">{rich(s["micro"])}</div>' if s.get("micro") else "")
            + f'</div>{mark()}'
        )

    elif t in ("insight", "proof"):
        term = (f'<div class="term">{rich(s["term"])}</div>' if s.get("term") else "")
        micro = (f'<div class="micro">{rich(s["micro"])}</div>' if s.get("micro") else "")
        body = (
            f'{counter}'
            f'<div class="head">'
            + (f'<div class="kicker">{rich(s["kicker"])}</div>' if s.get("kicker") else "")
            + f'<div class="headline">{rich(s.get("headline",""))}</div></div>'
            f'<div class="evidence"><div class="rule"></div>{micro}{term}</div>'
            f'{mark()}'
        )

    elif t == "photo":
        img = s.get("image", "")
        # With `stack` the photo slide turns into an image cover: the cascade
        # sits on the image instead of on a surface. This is the scroll-stopper.
        if s.get("stack"):
            lines = "".join(
                f'<div class="line{" white" if i == 2 else ""}">{rich(l)}</div>'
                for i, l in enumerate(s["stack"])
            )
            text = f'<div class="stack">{lines}</div>'
        else:
            text = f'<div class="headline">{rich(s.get("headline",""))}</div>'
        # `focus` steers the crop (CSS background-position). Without it, cover
        # centres blindly and crops the subject out of portrait shots.
        focus = html.escape(str(s.get("focus", "center")))
        body = (
            f'<div class="shot" style="background-image:url(\'{html.escape(img)}\');'
            f'background-position:{focus}"></div>'
            f'<div class="scrim"></div>{counter}{text}'
            + (f'<div class="micro">{rich(s["micro"])}</div>' if s.get("micro") else "")
            + mark()
        )

    elif t == "dashboard":
        # Prefers the ordered `blocks` list. The flat fields stay as a shorthand
        # — simple slides do not need a block scaffold.
        parts = _blocks(s.get("blocks", [])) or "".join([
            _stats(s.get("stats", []), s.get("cols", 2)),
            _rows(s.get("rows", [])),
            _bars(s.get("bars", [])),
            _cluster(s.get("cluster", {})),
            (f'<div class="term">{rich(s["term"])}</div>' if s.get("term") else ""),
        ])
        phead = ""
        if s.get("ptitle") or s.get("pill"):
            phead = (
                '<div class="phead">'
                f'<span>{rich(s.get("ptitle", ""))}</span>{_pill(s.get("pill"))}</div>'
            )
        panel = f'<div class="panel">{phead}<div class="pbody">{parts}</div></div>' if (parts or phead) else ""
        # Breadcrumb: the slash is marked up red so the line carries a signal
        # instead of being nothing but grey fine print.
        crumb = rich(s.get("crumb", "")).replace("/", "<i>/</i>")
        # Number strip (poster foot): three values, no box.
        strip = ""
        if s.get("strip"):
            its = "".join(
                f'<div class="it"><div class="v">{rich(i.get("v",""))}</div>'
                f'<div class="k">{rich(i.get("k",""))}</div></div>'
                for i in s["strip"]
            )
            strip = f'<div class="strip" style="--n:{len(s["strip"])}">{its}</div>'
        body = (
            f'{counter}<div class="wrap">'
            + (f'<div class="crumb">{crumb}</div>' if crumb else "")
            + f'<div class="headline">{rich(s.get("headline",""))}</div>'
            + (f'<div class="paren">{rich(s["paren"])}</div>' if s.get("paren") else "")
            + (f'<div class="sub">{rich(s["sub"])}</div>' if s.get("sub") else "")
            # Marginal note / editor's aside: one short, marked-up sentence
            # between the intro and the content. In the editorial look it
            # replaces the rotated running head — a line you have to turn your
            # head to read carries no information. It sits in the flow so that
            # every look can place it itself.
            + (f'<div class="marg">{rich(s["margin"])}</div>' if s.get("margin") else "")
            + panel + strip
            + f'<div class="foot"><span>{rich(s.get("foot","swipe"))}</span></div>'
            + "</div>"
            # The wordmark deliberately sits NEXT TO .wrap, not inside the
            # footer: as an absolutely positioned element it would otherwise
            # need .foot as its containing block and would land somewhere else
            # in every layout variant. Directly under .layer the containing
            # block is always the slide itself — one height, no matter what a
            # look does with the footer.
            + mark()
        )

    elif t == "cta":
        slots = s.get("slots", {})
        slot_html = "".join(
            f'<div class="slot {k}">{rich(v)}</div>' for k, v in slots.items() if v
        )
        actions = "".join(f'<div class="micro">{rich(a)}</div>' for a in s.get("actions", []))
        body = (
            f'{slot_html}'
            + cross("top:60px;left:220px") + cross("top:60px;right:220px")
            + cross("bottom:60px;left:260px") + cross("bottom:60px;right:260px")
            + f'<div class="center"><div class="headline">{rich(s.get("headline",""))}</div>'
            f'<div class="actions">{actions}</div></div>{mark()}'
        )

    # Classes plus texture. `variant` applies to dashboard only: "mono"
    # (headline in mono instead of display), "cond" (condensed wdth axis),
    # "paper" (light slide — then no texture, same as proof).
    variants = s.get("variant", "")
    variants = " ".join(variants) if isinstance(variants, list) else str(variants)
    on_paper = t == "proof" or (t == "dashboard" and "paper" in variants.split())

    # An explicit `bg` replaces the surface entirely: no surface class, and the
    # defaults below that suppress textures do not apply. Explicit beats
    # implicit — a slide that asks for a photo gets the photo, even on paper.
    bg = s.get("bg")
    bg_html, bg_cls = build_bg(bg, ox, oy, on_paper) if bg else ("", "")
    surface_cls = "" if bg else scls

    # `variant` originally applied to dashboard only. Since the mixed deck (014)
    # a cover or proof slide has to be able to carry its look as well —
    # otherwise you cannot switch between poster, console, editorial and diagram
    # inside ONE carousel without the stylesheets overwriting each other.
    if t == "proof":
        cls = f"slide proof paper {variants}".strip()
    elif t == "dashboard":
        cls = f"slide dashboard {variants}".strip()
        if not on_paper:
            cls += f" {surface_cls}"
    else:
        cls = f"slide {t} {variants} {surface_cls}".strip()
    if bg_cls:
        cls = f"{cls} {bg_cls}".strip()

    # Dashboard slides are TEXTURE-FREE by default. The grain texture is a
    # 256px placeholder tile at full opacity — laid over a UI surface it reads
    # as compression noise, not as material. Both references are flat. Anyone
    # who does want it sets "texture": true on the slide.
    flat_dash = t == "dashboard" and not s.get("texture")
    if bg:
        tex_html = bg_html
    elif on_paper or t == "photo" or flat_dash:
        tex_html = ""
    else:
        tex_html = _tex_layers(tex_classes, ox, oy)
    return f'<section class="{cls}">{tex_html}<div class="layer">{body}</div></section>'


def build_html(deck: dict, deck_dir: Path = None,
               accent: str | None = None, accent_print: str | None = None,
               fmt: str = "feed") -> str:
    if fmt not in FORMATS:
        raise ValueError(f'format "{fmt}" is unknown, pick one of {sorted(FORMATS)}')
    slides = deck.get("slides", [])
    total = len(slides)
    slug = str(deck.get("slug", ""))
    rendered = "\n".join(render_slide(s, i + 1, total, slug) for i, s in enumerate(slides))
    # Optional deck-local stylesheets ("css": ["look.css"], relative to the
    # deck). That way a look can bring its own rules along without several
    # parallel drafts overwriting each other inside carousel.css.
    extra = ""
    for rel in deck.get("css", []) or []:
        path = (deck_dir / rel) if deck_dir else Path(rel)
        extra += f'\n<link rel="stylesheet" href="{path.resolve()}">'
    # The calibrated accent. It lands AFTER tokens.css and the looks, so a look
    # that names the accent reads the calibrated one, and BEFORE brand.local.css,
    # so a hand-written override still beats everything. Without an accent there
    # is no block at all: tokens.css carries the black and white values, and the
    # `bw` class on <html> tells every look to show emphasis by form, because
    # colour is no longer there to do it.
    a, p, _ = accent_chain(deck, accent, accent_print)
    root = ["bw"] if a is None else []
    if a is not None:
        decl = "".join(f"\n  {k}: {v};" for k, v in derive(a, p).items())
        extra += f"\n<style>\n:root {{{decl}\n}}\n</style>"
    # The canvas. Feed is the one carousel.css is written for and adds nothing,
    # so a feed page is byte for byte the page it was before formats existed.
    # Any other format puts its name on <html> and hands in its size and its
    # no-type bands; carousel.css, section STORY, centres the layer between them.
    if fmt != "feed":
        f = FORMATS[fmt]
        root.append(fmt)
        extra += (f"\n<style>\n:root {{\n  --slide-w: {f.w}px;\n  --slide-h: {f.h}px;"
                  f"\n  --no-type-top: {f.no_type_top}px;\n  --no-type-bottom: {f.no_type_bottom}px;"
                  f"\n}}\n</style>")
    root = f' class="{" ".join(root)}"' if root else ""
    # Optional private override. If tokens/brand.local.css exists it is loaded
    # LAST and therefore wins over tokens.css and over every look — that is the
    # point: a look may define its own accent, and your brand has to beat it.
    # The file is gitignored, so your palette and faces never travel with a
    # fork of the engine, and a `git pull` never overwrites them.
    local = TOKENS / "brand.local.css"
    if local.exists():
        extra += f'\n<link rel="stylesheet" href="{local}">'
    return f"""<!doctype html>
<html{root}><head><meta charset="utf-8">
<link rel="stylesheet" href="{TOKENS}/tokens.css">
<link rel="stylesheet" href="{ENGINE}/carousel.css">{extra}
</head><body>
{rendered}
</body></html>"""


async def shoot(html_path: Path, out_dir: Path, deck: dict, scale: int = 2,
                fmt: str = "feed") -> list[Path]:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    f = FORMATS[fmt]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        # scale=2 → 2160×2700. Instagram downscales anyway, but edges and mono
        # text come out visibly sharper from a source twice the size. At
        # scale=1 hairlines and small labels looked frayed.
        page = await browser.new_page(viewport={"width": f.w, "height": f.h},
                                      device_scale_factor=scale)
        await page.goto(html_path.as_uri())
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass
        await page.wait_for_timeout(400)          # fonts and textures safely loaded
        await page.evaluate(AUTOFIT_JS)           # pull nowrap lines onto the safe width
        await page.wait_for_timeout(120)
        nodes = await page.query_selector_all("section.slide")
        for i, node in enumerate(nodes, start=1):
            name = deck["slides"][i - 1].get("name") or deck["slides"][i - 1].get("type", "slide")
            slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
            # Zero-padded: the carousel order depends on this sort
            target = out_dir / f"{i:02d}-{slug}.png"
            await node.screenshot(path=str(target))
            written.append(target)
            print(f"  ✓ {target.name}")
        await browser.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="carousel renderer (feed 1080×1350, story 1080×1920)")
    ap.add_argument("--deck", required=True, help="path to the deck.json")
    ap.add_argument("--out", default="",
                    help="output folder (default: <deck-dir>/stills, stills-story for a story)")
    ap.add_argument("--keep-html", action="store_true", help="keep the rendered HTML")
    ap.add_argument("--scale", type=int, default=2,
                    help="device_scale_factor (default 2 = 2160×2700 for crisp edges)")
    accent_args(ap)
    format_args(ap)
    args = ap.parse_args()
    fmt = FORMATS[args.format]

    deck_path = Path(args.deck).expanduser().resolve()
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slides = deck.get("slides", [])
    if not slides:
        print("The deck has no slides.", file=sys.stderr)
        return 2
    # Instagram allows 2 to 10 carousel items; 7 to 10 is the sweet spot.
    if not 2 <= len(slides) <= 10:
        print(f"WARNING: {len(slides)} slides, Instagram allows 2 to 10 per carousel.",
              file=sys.stderr)

    # Colour first: a bad hex or a broken brand file stops here, before anything
    # is written, and loudly. A typo must not render as black and white.
    try:
        accent, _, where = accent_chain(deck, args.accent, args.accent_print)
    except ValueError as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2

    # Substitute the day counter at render time
    raw = json.dumps(deck, ensure_ascii=False).replace("{{DAY}}", day_number())
    deck = json.loads(raw)

    out_dir = Path(args.out).expanduser() if args.out else deck_path.parent / f"stills{fmt.suffix}"

    # Guard: the renderer writes NN-<name>.png — if a source image has the same
    # name, the render overwrites its own input and the next run renders the
    # slide onto itself. That is exactly how a cover photo was lost once.
    planned = {f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', str(s.get('name') or s.get('type','slide')).lower()).strip('-')}.png"
               for i, s in enumerate(slides, start=1)}
    for s in slides:
        img = s.get("image")
        if not img:
            continue
        src = (deck_path.parent / img).resolve()
        if src.parent == out_dir.resolve() and src.name in planned:
            print(f"STOP: source image {img} sits in the output folder and would be overwritten by the render "
                  f"on the next run. Source images belong in <deck-dir>/photos/.", file=sys.stderr)
            return 2
    try:
        page = build_html(deck, deck_path.parent, args.accent, args.accent_print, args.format)
    except ValueError as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2
    html_path = deck_path.parent / f".{deck_path.stem}.render.html"
    html_path.write_text(page, encoding="utf-8")

    colour = f"accent {accent.upper()} from {where}" if accent else where
    print(f"{len(slides)} slides → {out_dir}  (day {day_number()}, {args.format} "
          f"{fmt.w*args.scale}×{fmt.h*args.scale}, {colour})")
    written = asyncio.run(shoot(html_path, out_dir, deck, args.scale, args.format))
    if not args.keep_html:
        html_path.unlink(missing_ok=True)
    print(f"\n=== {len(written)}/{len(slides)} rendered ===")
    return 0 if len(written) == len(slides) else 1


if __name__ == "__main__":
    raise SystemExit(main())
