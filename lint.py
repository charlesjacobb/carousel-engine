#!/usr/bin/env python3
"""Carousel linter — checks the rules you notice too late on the finished PNG.

Usage:  python3 lint.py --deck examples/014-mix.json [--deck ...]

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
            tokens: the slide is screenshot with the content layer hidden, and
            the contrast is computed per pixel under each text box. The score
            reported is the 10th percentile, so «passes» means nine tenths of
            the area behind that line clears the threshold, not the average.
            This is the rule that keeps `bg: {"kind":"photo"}` honest.

Exit 0 = clean. Exit 1 = at least one violation (details on stdout).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import render  # same folder: build_html, AUTOFIT_JS, W, H

# The wordmark is deliberately exempt from the overflow test: it is absolutely
# positioned and by definition sits on top of everything else.
PROBE_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('section.slide').forEach((sl, idx) => {
    const sb = sl.getBoundingClientRect();
    const rec = { i: idx + 1, cls: sl.className, mark: null, over: [], small: [], dash: [] };

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
        const fs = parseFloat(getComputedStyle(el, pe).fontSize);
        if (c.length > 3 && fs < MINSIZE)
          rec.small.push({ el: tag + pe, px: fs, txt: c.slice(0, 46) });
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


# Every element that carries its own text, with the box it occupies inside its
# slide and the colour it is painted in. This is the input for the contrast
# pass; the slide index lines up with the screenshots taken below.
TEXT_BOXES_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('section.slide').forEach((sl, idx) => {
    const sb = sl.getBoundingClientRect();
    const items = [];
    sl.querySelectorAll('*').forEach(el => {
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.opacity === '0') return;
      const own = Array.from(el.childNodes)
        .filter(n => n.nodeType === 3 && n.textContent.trim())
        .map(n => n.textContent.trim()).join(' ');
      if (!own) return;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      const tag = el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\s+/).join('.')
        : el.tagName.toLowerCase();
      items.push({
        el: tag, txt: own.slice(0, 46), px: parseFloat(st.fontSize),
        color: st.color,
        x: r.left - sb.left, y: r.top - sb.top, w: r.width, h: r.height,
      });
    });
    out.push(items);
  });
  return out;
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
      const x0 = Math.max(0, Math.round(it.x * sx)), y0 = Math.max(0, Math.round(it.y * sy));
      const w = Math.min(cv.width - x0, Math.round(it.w * sx));
      const h = Math.min(cv.height - y0, Math.round(it.h * sy));
      if (w < 2 || h < 2) continue;
      const d = cx.getImageData(x0, y0, w, h).data;
      const step = Math.max(1, Math.floor(Math.sqrt((w * h) / 4000))) * 4;
      const vals = [];
      for (let p = 0; p < d.length; p += step) vals.push(ratio(tl, lum(d[p], d[p + 1], d[p + 2])));
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


async def probe(html: Path, min_size: float):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": render.W, "height": render.H})
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

        # Hide the content, photograph the background, put the content back.
        # visibility, not display: display would let the layout reflow and the
        # boxes measured a moment ago would point at nothing.
        await page.evaluate("document.querySelectorAll('.layer')"
                            ".forEach(l => l.style.visibility = 'hidden')")
        shots = []
        for node in await page.query_selector_all("section.slide"):
            shots.append(base64.b64encode(await node.screenshot()).decode("ascii"))
        await page.evaluate("document.querySelectorAll('.layer')"
                            ".forEach(l => l.style.visibility = '')")
        contrast = await page.evaluate(
            CONTRAST_JS, {"shots": shots, "boxes": boxes, "w": render.W, "h": render.H})
        await b.close()
    return data, contrast


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
    args = ap.parse_args()

    baselines: dict[str, list] = {}
    bad = 0

    for d in args.deck:
        dp = Path(d).expanduser().resolve()
        deck = json.loads(dp.read_text(encoding="utf-8"))
        raw = json.dumps(deck, ensure_ascii=False).replace("{{DAY}}", render.day_number())
        deck = json.loads(raw)
        html = dp.parent / f".{dp.stem}.lint.html"
        html.write_text(render.build_html(deck, dp.parent), encoding="utf-8")
        try:
            data, contrast = asyncio.run(probe(html, args.min_size))
        finally:
            html.unlink(missing_ok=True)

        print(f"\n=== {dp.parent.name}")
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
                                f"(top {o['top']}, bottom {o['bottom']} of {render.H})")
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
