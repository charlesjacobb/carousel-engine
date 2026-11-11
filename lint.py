#!/usr/bin/env python3
"""Carousel linter — checks the rules you notice too late on the finished PNG.

Usage:  python3 lint.py --deck examples/014-mix.json [--deck ...] [--format story]

Everything is measured in the real browser layout (not on the image), because
that is the only place that knows where an element actually sits:

  MARK      The wordmark sits at the same distance above the bottom edge on
            ALL slides of ALL decks. House rule.
  OVERFLOW  No element sticks out of the slide, no two blocks overlap
            vertically. That was the bug after the type size went up: the
            note ended up lying on top of the log rows.
  TYPE      No visible text below --min-size. 1080px of width land at about
            390pt on a phone (factor 0.36) — 22px are 8pt there, and that is
            the floor.
  DASH      No em dash (—) and no en dash (–) in visible text. House rule:
            the plain hyphen only.
  CONTRAST  Every line of type clears WCAG contrast against what is actually
            behind it. Measured on the rendered pixels, not guessed from the
            tokens: the slide is screenshot with every glyph made transparent,
            and the contrast is computed per pixel under each line of text.
            Only the glyphs go, so whatever the content paints itself (an
            inverted block, a chip, a card) stays in the picture and
            counts. The score reported is the 10th percentile, so «passes»
            means nine tenths of the area behind that line clears the
            threshold, not the average. This is the rule that keeps
            `bg: {"kind":"photo"}` honest.
  ZONE      No line of type reaches into a band the platform draws its own UI
            over. A story keeps the top 180px and the bottom 250px free; the
            feed has no such band. Lint with the same --format you render.

Exit 0 = clean. Exit 1 = at least one violation (details on stdout).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import render  # same folder: build_html, AUTOFIT_JS, FORMATS

# The wordmark is deliberately exempt from the overflow test: it is absolutely
# positioned and by definition sits on top of everything else.
PROBE_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('section.slide').forEach((sl, idx) => {
    const sb = sl.getBoundingClientRect();
    const rec = { i: idx + 1, cls: sl.className, mark: null, over: [], small: [], dash: [], pseudo: [] };

    const mk = sl.querySelector('.mark');
    if (mk) {
      const r = mk.getBoundingClientRect();
      // Distance from the bottom of the wordmark to the bottom of the slide.
      // That is the number that has to be identical across all slides.
      rec.mark = Math.round((sb.bottom - r.bottom) * 100) / 100;
    }

    // The content frame is the real boundary, not the slide edge. A panel that
    // grows out of .wrap at the bottom stays inside the slide and still looks
    // broken: it runs into the footer and under the wordmark. That exact case
    // slipped through for as long as the check only tested against sb.
    const wrap = sl.querySelector('.wrap');
    const box = wrap ? wrap.getBoundingClientRect() : sb;

    sl.querySelectorAll('*').forEach(el => {
      if (el.closest('.mark')) return;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.opacity === '0') return;

      const tag = el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\s+/).join('.')
        : el.tagName.toLowerCase();

      // 1px tolerance against subpixel rounding
      if (r.bottom > sb.bottom + 1 || r.top < sb.top - 1 ||
          r.right > sb.right + 1 || r.left < sb.left - 1) {
        rec.over.push({ el: tag, where: 'slide',
                        top: Math.round(r.top - sb.top),
                        bottom: Math.round(r.bottom - sb.top) });
      } else if (wrap && wrap.contains(el) && r.bottom > box.bottom + 1) {
        rec.over.push({ el: tag, where: 'wrap',
                        top: Math.round(r.top - sb.top),
                        bottom: Math.round(r.bottom - sb.top) });
      }

      // Only measure type size on elements that carry text themselves
      const own = Array.from(el.childNodes)
        .filter(n => n.nodeType === 3 && n.textContent.trim())
        .map(n => n.textContent.trim()).join(' ');
      if (own) {
        const fs = parseFloat(st.fontSize);
        if (fs < MINSIZE) rec.small.push({ el: tag, px: fs, txt: own.slice(0, 46) });
        if (/[—–]/.test(own)) rec.dash.push({ el: tag, txt: own.slice(0, 60) });
      }
      // In the editorial look pseudo elements carry real text (running heads).
      // The em dash hid there from the first version of this test, because
      // `content:` is not a text node.
      ['::before', '::after'].forEach(pe => {
        const c = getComputedStyle(el, pe).content;
        if (!c || c === 'none' || c === 'normal') return;
        if (/[—–]/.test(c)) rec.dash.push({ el: tag + pe, txt: c.slice(0, 60) });
        const ps = getComputedStyle(el, pe);
        const fs = parseFloat(ps.fontSize);
        if (c.length > 3 && fs < MINSIZE)
          rec.small.push({ el: tag + pe, px: fs, txt: c.slice(0, 46) });
        // For ZONE. A pseudo element has no box to ask for, but ::before
        // starts where its element's content starts and ::after ends where it
        // ends, which is enough to tell whether its line reaches into a band.
        if (c.length > 3) {
          const lh = parseFloat(ps.lineHeight) || fs * 1.2;
          const y = pe === '::before'
            ? r.top + parseFloat(st.borderTopWidth) + parseFloat(st.paddingTop) - sb.top
            : r.bottom - parseFloat(st.borderBottomWidth) - parseFloat(st.paddingBottom) - lh - sb.top;
          rec.pseudo.push({ el: tag + pe, txt: c.slice(0, 46), rects: [{ y, h: lh }] });
        }
      });
    });
    // SIBLING OVERLAP. The most common bug in this layout is not an overflow
    // but a collision: a flex child with `flex:1` and too much content grows
    // past its box, and the next block lies down on top of it. Both stay
    // inside .wrap, both tests above report nothing, and in the PNG a note
    // sits on a table row.
    // Only siblings in the flow are checked - absolutely positioned elements
    // are allowed to overlap by definition.
    sl.querySelectorAll('.wrap, .panel, .pbody, .region').forEach(box => {
      const kids = Array.from(box.children).filter(el => {
        const st = getComputedStyle(el);
        if (st.position === 'absolute' || st.position === 'fixed') return false;
        if (st.visibility === 'hidden' || st.opacity === '0') return false;
        const r = el.getBoundingClientRect();
        return r.height > 0 && r.width > 0;
      });
      for (let n = 0; n + 1 < kids.length; n++) {
        const a = kids[n].getBoundingClientRect();
        const b = kids[n + 1].getBoundingClientRect();
        // What counts is not the box but where the content really ends. A flex
        // child with `flex:1` gets a box that is too small and lets its content
        // spill over; the box itself then collides with nothing, the visible
        // image does.
        // scrollHeight only on real block containers. On text with a
        // line-height below 1 - and even more so with an inline <em> in it -
        // the browser reports a scrollHeight larger than the box although
        // nothing collides; that produced a 3px false positive on the headline
        // of the poster deck. An inline child cannot leave the flow by
        // definition, a block child can.
        const hasBlockChild = Array.from(kids[n].children)
          .some(c => !getComputedStyle(c).display.startsWith('inline'));
        const aEnd = hasBlockChild
          ? a.top + Math.max(a.height, kids[n].scrollHeight)
          : a.bottom;
        if (b.top < aEnd - 1) {
          const nm = el => el.className && typeof el.className === 'string'
            ? '.' + el.className.trim().split(/\s+/).join('.') : el.tagName.toLowerCase();
          rec.over.push({ el: nm(kids[n]) + ' × ' + nm(kids[n + 1]), where: 'overlap',
                          top: Math.round(b.top - sb.top),
                          bottom: Math.round(aEnd - sb.top) });
        }
      }
    });

    out.push(rec);
  });
  return out;
}
"""


# Every element that carries its own text, with the line boxes of that text
# inside its slide and the colour it is painted in. Line boxes, not the element
# box: a headline's box also covers the <em> inside it, and once that <em> sits
# on an inverted block, the headline would be measured against a patch of
# white it never touches. A text rect spans the font's full ascent and descent,
# which at a display leading of .8 is half again the line, so it is cut down to
# the line height, centred, the way the browser centres the line box on it.
# Otherwise each line of a tight stack is measured against its neighbours.
# The slide index lines up with the screenshots below.
TEXT_BOXES_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('section.slide').forEach((sl, idx) => {
    const sb = sl.getBoundingClientRect();
    const items = [];
    sl.querySelectorAll('*').forEach(el => {
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.opacity === '0') return;
      const nodes = Array.from(el.childNodes)
        .filter(n => n.nodeType === 3 && n.textContent.trim());
      if (!nodes.length) return;
      const rects = [];
      const lh = parseFloat(st.lineHeight);        // NaN for `normal`
      nodes.forEach(n => {
        const rg = document.createRange();
        rg.selectNodeContents(n);
        for (const r of rg.getClientRects()) {
          if (r.width < 2 || r.height < 2) continue;
          const h = lh > 0 && lh < r.height ? lh : r.height;
          rects.push({ x: r.left - sb.left, y: r.top - sb.top + (r.height - h) / 2,
                       w: r.width, h });
        }
      });
      if (!rects.length) return;
      const tag = el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\s+/).join('.')
        : el.tagName.toLowerCase();
      items.push({
        el: tag, txt: nodes.map(n => n.textContent.trim()).join(' ').slice(0, 46),
        px: parseFloat(st.fontSize), color: st.color, rects,
      });
    });
    out.push(items);
  });
  return out;
}
"""

# Glyphs off, everything else on: backgrounds, borders, rules and the shapes
# pseudo elements draw stay where they are. Text decoration and glow belong to
# the type, so they go with it.
GLYPHS_OFF_CSS = """
.layer, .layer *, .layer *::before, .layer *::after {
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  -webkit-text-stroke-color: transparent !important;
  text-shadow: none !important;
}
"""

# Draws each screenshot (content layer hidden) into a canvas and measures the
# contrast under every text box. Per pixel, not per average: a photo behind
# type has no single background colour, and an average hides exactly the patch
# where the line stops being readable.
CONTRAST_JS = r"""
async (arg) => {
  const lum = (r, g, b) => {
    const f = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  const out = [];
  for (let i = 0; i < arg.shots.length; i++) {
    const img = new Image();
    await new Promise(res => { img.onload = res; img.src = 'data:image/png;base64,' + arg.shots[i]; });
    const cv = document.createElement('canvas');
    cv.width = img.width; cv.height = img.height;
    const cx = cv.getContext('2d', { willReadFrequently: true });
    cx.drawImage(img, 0, 0);
    const sx = img.width / arg.w, sy = img.height / arg.h;   // screenshot may be 2x
    const rows = [];
    for (const it of arg.boxes[i]) {
      const m = it.color.match(/(\d+(?:\.\d+)?)/g) || [255, 255, 255];
      const tl = lum(+m[0], +m[1], +m[2]);
      const vals = [];
      for (const b of it.rects) {
        const x0 = Math.max(0, Math.round(b.x * sx)), y0 = Math.max(0, Math.round(b.y * sy));
        const w = Math.min(cv.width - x0, Math.round(b.w * sx));
        const h = Math.min(cv.height - y0, Math.round(b.h * sy));
        if (w < 2 || h < 2) continue;
        const d = cx.getImageData(x0, y0, w, h).data;
        const step = Math.max(1, Math.floor(Math.sqrt((w * h) / 4000))) * 4;
        for (let p = 0; p < d.length; p += step) vals.push(ratio(tl, lum(d[p], d[p + 1], d[p + 2])));
      }
      if (!vals.length) continue;
      vals.sort((a, b) => a - b);
      rows.push({ el: it.el, txt: it.txt, px: it.px,
                  p10: Math.round(vals[Math.floor(vals.length * 0.10)] * 100) / 100,
                  worst: Math.round(vals[0] * 100) / 100 });
    }
    out.push(rows);
  }
  return out;
}
"""


async def probe(html: Path, min_size: float, fmt: render.Format):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": fmt.w, "height": fmt.h})
        await page.goto(html.as_uri())
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass
        await page.wait_for_timeout(400)
        await page.evaluate(render.AUTOFIT_JS)
        await page.wait_for_timeout(120)
        data = await page.evaluate(PROBE_JS.replace("MINSIZE", str(min_size)))
        boxes = await page.evaluate(TEXT_BOXES_JS)

        # Make the glyphs transparent, photograph what is left, put them back.
        # Colour only, so nothing reflows and the line boxes measured a moment
        # ago still point at the right pixels. Hiding the whole content layer
        # (the first version of this rule) also took away every block the
        # content paints itself, and measured inverted type against the slide.
        style = await page.add_style_tag(content=GLYPHS_OFF_CSS)
        shots = []
        for node in await page.query_selector_all("section.slide"):
            shots.append(base64.b64encode(await node.screenshot()).decode("ascii"))
        await style.evaluate("el => el.remove()")
        contrast = await page.evaluate(
            CONTRAST_JS, {"shots": shots, "boxes": boxes, "w": fmt.w, "h": fmt.h})
        await b.close()
    return data, boxes, contrast


def zone_hits(items: list, fmt: render.Format) -> list[str]:
    """Lines of type that reach into a no-type band. Measured on the same line
    boxes CONTRAST uses, cut to the line height, so the air a display face
    carries above its caps does not count as type. Text a look writes with
    `content:` (editorial's running head) comes in with an estimated line,
    see PROBE_JS."""
    top, bottom = fmt.no_type_top, fmt.h - fmt.no_type_bottom
    out = []
    for it in items:
        y0 = min(r["y"] for r in it["rects"])
        y1 = max(r["y"] + r["h"] for r in it["rects"])
        if y0 < top - 1 or y1 > bottom + 1:   # 1px tolerance against subpixel rounding
            out.append(f"ZONE {it['el']}  «{it['txt']}»  (y {y0:.0f} to {y1:.0f}, "
                       f"type belongs between {top} and {bottom})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="carousel linter")
    ap.add_argument("--deck", action="append", required=True)
    ap.add_argument("--min-size", type=float, default=22.0)
    # WCAG AA: 4.5:1 for body text, 3:1 from 24px up («large text»). The floor
    # for type on a slide is 22px, so most lines land just under large and are
    # held to 4.5. That is deliberate, a carousel is read at arm's length.
    ap.add_argument("--min-contrast", type=float, default=4.5,
                    help="required contrast below 24px (default 4.5)")
    ap.add_argument("--min-contrast-large", type=float, default=3.0,
                    help="required contrast from 24px up (default 3.0)")
    render.accent_args(ap)
    render.format_args(ap)
    args = ap.parse_args()
    fmt = render.FORMATS[args.format]
    banded = bool(fmt.no_type_top or fmt.no_type_bottom)

    baselines: dict[str, list] = {}
    bad = 0

    for d in args.deck:
        dp = Path(d).expanduser().resolve()
        deck = json.loads(dp.read_text(encoding="utf-8"))
        try:
            raw = json.dumps(deck, ensure_ascii=False).replace("{{DAY}}", render.day_number())
            deck = json.loads(raw)
            page = render.build_html(deck, dp.parent, args.accent, args.accent_print, args.format)
        except ValueError as e:
            print(f"STOP: {e}", file=sys.stderr)
            return 2
        html = dp.parent / f".{dp.stem}.lint.html"
        html.write_text(page, encoding="utf-8")
        try:
            data, boxes, contrast = asyncio.run(probe(html, args.min_size, fmt))
        finally:
            html.unlink(missing_ok=True)

        print(f"\n=== {dp.parent.name}" + (f"  ({args.format} {fmt.w}×{fmt.h})" if banded else ""))
        for rec in data:
            name = deck["slides"][rec["i"] - 1].get("name", rec["i"])
            msgs = []
            for c in contrast[rec["i"] - 1]:
                need = args.min_contrast_large if c["px"] >= 24 else args.min_contrast
                if c["p10"] < need:
                    msgs.append(f"CONTRAST {c['p10']}:1 (needs {need}:1 at {c['px']:.0f}px)  "
                                f"{c['el']}  «{c['txt']}»")
            if rec["mark"] is None:
                msgs.append("NO wordmark")
            else:
                baselines.setdefault(str(rec["mark"]), []).append(f"{dp.parent.name}/{name}")
            for o in rec["over"]:
                if o["where"] == "overlap":
                    msgs.append(f"OVERLAP {o['el']} "
                                f"({o['bottom'] - o['top']}px, from y={o['top']})")
                else:
                    boundary = "slide edge" if o["where"] == "slide" else "content frame .wrap"
                    msgs.append(f"OVERFLOW past the {boundary}: {o['el']} "
                                f"(top {o['top']}, bottom {o['bottom']} of {fmt.h})")
            if banded:
                msgs += zone_hits(boxes[rec["i"] - 1] + rec["pseudo"], fmt)
            for s in rec["small"]:
                msgs.append(f"TOO SMALL {s['px']}px  {s['el']}  «{s['txt']}»")
            for x in rec["dash"]:
                msgs.append(f"EM/EN DASH  {x['el']}  «{x['txt']}»")
            flag = "✗" if msgs else "✓"
            bad += len(msgs)
            print(f"  {flag} {rec['i']:02d} {name}   mark={rec['mark']}px")
            for m in msgs:
                print(f"      · {m}")

    print("\n=== mark height (distance from the slide bottom)")
    for px, where in sorted(baselines.items(), key=lambda kv: -len(kv[1])):
        print(f"  {px}px  ×{len(where)}   {', '.join(where[:6])}{' …' if len(where) > 6 else ''}")
    if len(baselines) > 1:
        print("  ✗ more than ONE height, house rule broken")
        bad += 1
    else:
        print("  ✓ one single height across all checked slides")

    print(f"\n=== {bad} violation(s)" if bad else "\n=== clean")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
