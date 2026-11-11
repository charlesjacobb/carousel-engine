#!/usr/bin/env python3
"""calibrate.py: what one accent hex turns into, and whether it holds.

    python3 calibrate.py "#2F6BFF"
    python3 calibrate.py "#2F6BFF" --accent-print "#1D4FD8"
    python3 calibrate.py                  # what this checkout renders by default

Prints the tokens render.py derives from the hex, then the contrast of the
accent on the two surfaces it is used on, ink and paper, each against
the threshold lint.py will hold it to. A FAIL names the nearest shade of the
same hue that passes.

Without a hex it reads the same chain render.py reads (tokens/brand.local.json,
then tokens/brand.json) and, with nothing there, shows the black and white
default. Standard library only, writes nothing.

Exit 0 = every surface passes. Exit 1 = at least one FAIL. Exit 2 = bad input.
"""
from __future__ import annotations

import argparse
import sys

import render  # same folder: the derivation lives there, so the numbers match

# On ink and on paper the accent sets type down to 22px (alert pills, the
# highlighted value in a code block), so it has to clear WCAG's 4.5:1.
NEED_SMALL = 4.5


def main() -> int:
    ap = argparse.ArgumentParser(description="derive and check an accent colour")
    ap.add_argument("accent", nargs="?", metavar="#RRGGBB",
                    help="the accent; leave out to check what this checkout renders")
    ap.add_argument("--accent-print", "--accent_print", dest="accent_print", metavar="#RRGGBB",
                    help="a paper shade of your own instead of the derived one")
    args = ap.parse_args()

    try:
        accent, accent_print, where = render.accent_chain({}, args.accent, args.accent_print)
    except ValueError as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2

    if accent is None:
        print("No accent set: the engine renders black and white.\n")
        tokens = {"--accent": "#FFFFFF", "--accent-rgb": "255,255,255",
                  "--accent-light": "#FFFFFF", "--accent-print": render.INK}
        notes = {"--accent": "white, emphasis moves to form (see the `bw` rules)",
                 "--accent-print": "ink"}
    else:
        tokens = render.derive(accent, accent_print)
        print(f"Accent {tokens['--accent']} from {where}.\n")
        if accent_print:
            paper = "your paper shade"
        elif tokens["--accent-print"] == tokens["--accent"]:
            paper = f"clears {render.PRINT_CONTRAST}:1 on paper as it is"
        else:
            paper = f"darkened until it clears {render.PRINT_CONTRAST}:1 on paper"
        notes = {"--accent": "screen shade, on ink only",
                 "--accent-light": "one step lighter: gradients and edges, never type",
                 "--accent-print": paper}
    for k, v in tokens.items():
        print(f"  {k:<15} {v:<12} {notes.get(k, '')}")
    print(f"  {'--ed-accent':<15} {tokens['--accent-print']:<12} = --accent-print (editorial look)")

    checks = [("accent on ink", tokens["--accent"], render.INK, NEED_SMALL),
              ("print on paper", tokens["--accent-print"], render.PAPER, NEED_SMALL)]
    print(f"\n  {'surface':<16} {'colour':<9} {'ground':<9} {'ratio':>6}  {'needs':>5}")
    failed = 0
    for name, fg, bg, need in checks:
        ratio = render.contrast(fg, bg)
        ok = ratio >= need
        failed += not ok
        line = f"  {name:<16} {fg:<9} {bg:<9} {ratio:>6.2f}  {need:>5.1f}  {'PASS' if ok else 'FAIL'}"
        if not ok:
            near = render.toward(fg, bg, need)
            got = render.contrast(near, bg)
            line += (f"   nearest that holds: {near} ({got:.2f}:1)" if got >= need
                     else "   no shade of this hue holds here")
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
