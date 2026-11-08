#!/usr/bin/env python3
"""make_knob.py - generate a PNG film strip (vertical, N frames) of a flat rotary knob.

Usage:
    make_knob.py knob.png [--frames 127] [--size 77] [--body ff2b2b2e] [--track ff46464a]
                 [--arc ffd31145] [--pointer ffffffff] [--sweep 270]

Each frame is size x size pixels; frames are stacked top to bottom from minimum to maximum.
Colours are ARGB hex, like ui.json "style" colours. Reference the file from ui.json with
    "knobImage": "knob.png", "knobFrames": 127
and keep it in the project's assets/ folder (build.py copies assets/ into "Plugin Skins/").

Standard library only (zlib for PNG). Python 3.9+.
"""

import argparse
import math
import struct
import sys
import zlib


def parse_argb(text):
    t = text.strip().lstrip("#")
    if len(t) == 6:
        t = "ff" + t
    if len(t) != 8 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise argparse.ArgumentTypeError('colour must be 8-digit ARGB hex like "ffd31145", got %r' % text)
    v = int(t, 16)
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)  # r, g, b, a


def write_png(path, width, height, rows):
    """rows: list of bytes objects, each width*4 RGBA bytes."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + r for r in rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def blend(dst, src, coverage):
    """Source-over composite of src (r,g,b,a) onto dst with extra coverage 0..1."""
    sa = src[3] / 255.0 * coverage
    if sa <= 0:
        return dst
    da = dst[3] / 255.0
    oa = sa + da * (1 - sa)
    if oa <= 0:
        return (0, 0, 0, 0)
    out = tuple(int(round((src[i] * sa + dst[i] * da * (1 - sa)) / oa)) for i in range(3))
    return out + (int(round(oa * 255)),)


def coverage_band(d, inner, outer):
    """Anti-aliased coverage of distance d within [inner, outer]."""
    return max(0.0, min(1.0, d - inner + 0.5)) * max(0.0, min(1.0, outer - d + 0.5))


def ang_diff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def render_frame(size, t, geo, colours):
    """One frame; t in 0..1. geo: dict of radii; colours: dict of RGBA tuples."""
    cx = cy = (size - 1) / 2.0
    sweep = geo["sweep"]
    start = -sweep / 2.0  # angles measured clockwise from the top (12 o'clock)
    value_angle = start + sweep * t
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx, dy = x - cx, y - cy
            d = math.hypot(dx, dy)
            ang = math.atan2(dx, -dy)  # 0 at top, clockwise positive
            px = (0, 0, 0, 0)
            # Track ring (full sweep) then value arc (start -> value_angle)
            ring = coverage_band(d, geo["ring_in"], geo["ring_out"])
            if ring > 0:
                in_sweep = abs(ang_diff(ang, 0.0)) <= sweep / 2.0 + 0.02
                if in_sweep:
                    px = blend(px, colours["track"], ring)
                    if start - 0.02 <= ang <= value_angle:
                        px = blend(px, colours["arc"], ring)
            # Body disc
            body = coverage_band(d, -1.0, geo["body_r"])
            if body > 0:
                px = blend(px, colours["body"], body)
            # Pointer: a rounded bar from body centre outwards along value_angle
            vx, vy = math.sin(value_angle), -math.cos(value_angle)
            along = dx * vx + dy * vy
            across = abs(dx * vy - dy * vx)
            if geo["ptr_from"] <= along <= geo["ptr_to"]:
                pc = max(0.0, min(1.0, geo["ptr_w"] / 2.0 - across + 0.5))
                if pc > 0:
                    px = blend(px, colours["pointer"], pc)
            row += bytes(px)
        rows.append(bytes(row))
    return rows


def make_strip(path, frames, size, colours, sweep_deg):
    r = size / 2.0
    geo = {
        "sweep": math.radians(sweep_deg),
        "ring_out": r - 1.0,
        "ring_in": r - max(3.0, size * 0.09),
        "body_r": r - max(6.0, size * 0.17),
        "ptr_w": max(2.0, size * 0.05),
    }
    geo["ptr_from"] = geo["body_r"] * 0.45
    geo["ptr_to"] = geo["body_r"] - max(2.0, size * 0.05)
    rows = []
    for i in range(frames):
        t = i / (frames - 1) if frames > 1 else 0.0
        rows.extend(render_frame(size, t, geo, colours))
    write_png(path, size, size * frames, rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a vertical PNG film strip of a flat rotary knob for MPC skins.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("output", help="output PNG path, e.g. assets/knob.png")
    ap.add_argument("--frames", type=int, default=127, help="number of frames (ui.json knobFrames)")
    ap.add_argument("--size", type=int, default=77, help="frame width and height in pixels")
    ap.add_argument("--body", type=parse_argb, default="ff2b2b2e", help="knob body colour (ARGB hex)")
    ap.add_argument("--track", type=parse_argb, default="ff46464a", help="unfilled arc colour")
    ap.add_argument("--arc", type=parse_argb, default="ffd31145", help="filled value arc colour")
    ap.add_argument("--pointer", type=parse_argb, default="ffffffff", help="pointer colour")
    ap.add_argument("--sweep", type=float, default=270.0, help="total rotation in degrees")
    args = ap.parse_args(argv)
    if args.frames < 2 or args.size < 8:
        ap.error("--frames must be >= 2 and --size >= 8")
    colours = {"body": args.body, "track": args.track, "arc": args.arc, "pointer": args.pointer}
    make_strip(args.output, args.frames, args.size, colours, args.sweep)
    print("make_knob: wrote %s (%d frames of %dx%d; strip is %dx%d). In ui.json: \"knobImage\": \"%s\", \"knobFrames\": %d"
          % (args.output, args.frames, args.size, args.size, args.size, args.size * args.frames,
             args.output.replace("\\", "/").split("/")[-1], args.frames))
    return 0


if __name__ == "__main__":
    sys.exit(main())
