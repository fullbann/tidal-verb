#!/usr/bin/env python3
"""make_ui_kit.py - render the SDK's film-strip UI kit (knobs, faders, switches) as PNGs.

The kit is a themed set of control graphics in the exact pixel sizes the generated skins use.
The MPC draws film-strip frames and button images at their native size (nothing is scaled),
so every asset here is rendered at the size it is shown.

Usage:
    make_ui_kit.py --out <dir> [--theme mpc-red|amber|cyan|lime|violet|mono] [--accent AARRGGBB]
                   [--only knob_uni,fader_h_bi,...] [--preview preview.png] [--quiet]
    make_ui_kit.py --list

Assets written to <dir> (see ui-kit/README.md for pictures and how to use each one):

    knob_uni.png / knob_bi.png              90 x 90, 127 frames   rotary knob, arc from min / from centre
    knob_small_uni.png / knob_small_bi.png  56 x 56, 127 frames   the same, for dense hand-written layouts
    fader_h_uni.png / fader_h_bi.png        248 x 36, 127 frames  horizontal fader with cap, fill from left / centre
    fader_v_uni.png / fader_v_bi.png        36 x 160, 127 frames  vertical fader (hand-written skins)
    toggle_on.png / toggle_off.png          64 x 32               pill switch for bool parameters (Button widget)
    button_on.png / button_off.png          120 x 44              push button with LED (Button widget + Label)
    seg2_on/off, seg3_on/off, seg4_on/off   122/80/58 x 40        radio segments for 2-, 3- and 4-way choices
    kit.json                                manifest: sizes, frame counts, theme colours

Film strips are vertical: frame 0 (minimum) at the top. 127 frames gives bipolar strips an
exact centre frame (63). Standard library only; Python 3.9+. Rendering a whole kit takes a
few seconds; tools/gen_ui.py caches the result per theme under build/.
"""

import argparse
import json
import math
import os
import struct
import sys
import zlib

KIT_VERSION = 1

# --------------------------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------------------------
GREYS = {
    "bg": "ff1e1d1e",          # page background (reference only; the kit draws no backgrounds)
    "body": "ff2a2a2d",        # knob / button body
    "bodyLight": "ff3c3c40",   # lit edge of bodies
    "bodyDark": "ff171718",    # shaded edge of bodies, grooves
    "track": "ff434347",       # unfilled arc / track
    "outline": "ff0e0e0f",     # 1 px outlines
    "pointer": "fff2f2f2",     # knob pointer, thumb grip line
    "thumb": "ffb9b9bd",       # fader cap
    "thumbLight": "ffe6e6e8",
    "thumbDark": "ff8a8a8e",
    "ledOff": "ff3a2b2e",
    "textOn": "ffffffff",
}

THEMES = {
    "mpc-red": "ffd31145",
    "amber": "ffffa21a",
    "cyan": "ff19c6e0",
    "lime": "ff8fd21f",
    "violet": "ff9b63ff",
    "mono": "ffe4e4e6",
}


def parse_argb(text):
    t = str(text).strip().lstrip("#")
    if len(t) == 6:
        t = "ff" + t
    if len(t) != 8 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise ValueError('colour must be 8-digit ARGB hex like "ffd31145", got %r' % text)
    v = int(t, 16)
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)


def argb_text(c):
    return "%02x%02x%02x%02x" % (c[3], c[0], c[1], c[2])


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(4))


def with_alpha(c, alpha):
    return (c[0], c[1], c[2], int(round(alpha)))


def make_theme(name, accent=None):
    if name not in THEMES:
        raise ValueError("unknown theme %r (choose from %s)" % (name, ", ".join(sorted(THEMES))))
    acc = parse_argb(accent) if accent else parse_argb(THEMES[name])
    th = {k: parse_argb(v) for k, v in GREYS.items()}
    th["accent"] = acc
    th["accentDark"] = mix(acc, th["outline"], 0.35)
    th["accentGlow"] = with_alpha(acc, 70)
    th["ledOff"] = mix(acc, th["body"], 0.78)
    return th


# --------------------------------------------------------------------------------------------
# Tiny raster library (float RGBA, source-over, analytic anti-aliasing)
# --------------------------------------------------------------------------------------------
class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = [(0.0, 0.0, 0.0, 0.0)] * (w * h)

    def copy(self):
        c = Canvas.__new__(Canvas)
        c.w, c.h, c.px = self.w, self.h, list(self.px)
        return c

    def blend(self, i, colour, cov):
        if cov <= 0.0:
            return
        sa = colour[3] / 255.0 * (1.0 if cov >= 1.0 else cov)
        if sa <= 0.0:
            return
        dr, dg, db, da = self.px[i]
        oa = sa + da * (1.0 - sa)
        k = da * (1.0 - sa)
        self.px[i] = ((colour[0] * sa + dr * k) / oa, (colour[1] * sa + dg * k) / oa,
                      (colour[2] * sa + db * k) / oa, oa)

    def blit(self, other, ox, oy):
        for y in range(other.h):
            ty = y + oy
            if ty < 0 or ty >= self.h:
                continue
            for x in range(other.w):
                tx = x + ox
                if tx < 0 or tx >= self.w:
                    continue
                r, g, b, a = other.px[y * other.w + x]
                if a > 0.0:
                    self.blend(ty * self.w + tx, (r, g, b, 255), a)

    def rows(self):
        out = []
        for y in range(self.h):
            row = bytearray()
            for x in range(self.w):
                r, g, b, a = self.px[y * self.w + x]
                row += bytes((int(r + 0.5), int(g + 0.5), int(b + 0.5), int(a * 255.0 + 0.5)))
            out.append(bytes(row))
        return out


def cov(sdf):
    """Coverage from a signed distance (negative inside); 1 px anti-aliasing band."""
    return 0.0 if sdf >= 0.5 else (1.0 if sdf <= -0.5 else 0.5 - sdf)


def sd_rrect(px, py, x, y, w, h, r):
    cx, cy = x + w / 2.0, y + h / 2.0
    qx, qy = abs(px - cx) - (w / 2.0 - r), abs(py - cy) - (h / 2.0 - r)
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def fill(canvas, bbox, sdf, colour):
    """colour: RGBA tuple or callable(px, py) -> RGBA. bbox: (x0, y0, x1, y1) exclusive."""
    x0, y0, x1, y1 = (max(0, int(math.floor(bbox[0]))), max(0, int(math.floor(bbox[1]))),
                      min(canvas.w, int(math.ceil(bbox[2])) + 1), min(canvas.h, int(math.ceil(bbox[3])) + 1))
    call = callable(colour)
    for y in range(y0, y1):
        py = y + 0.5
        base = y * canvas.w
        for x in range(x0, x1):
            px = x + 0.5
            c = cov(sdf(px, py))
            if c > 0.0:
                canvas.blend(base + x, colour(px, py) if call else colour, c)


def rrect(canvas, x, y, w, h, r, colour):
    fill(canvas, (x - 1, y - 1, x + w + 1, y + h + 1), lambda px, py: sd_rrect(px, py, x, y, w, h, r), colour)


def rrect_outline(canvas, x, y, w, h, r, thickness, colour):
    fill(canvas, (x - 1, y - 1, x + w + 1, y + h + 1),
         lambda px, py: abs(sd_rrect(px, py, x, y, w, h, r) + thickness / 2.0) - thickness / 2.0, colour)


def circle(canvas, cx, cy, r, colour):
    fill(canvas, (cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1), lambda px, py: sd_circle(px, py, cx, cy, r), colour)


def ring(canvas, cx, cy, r_in, r_out, colour):
    fill(canvas, (cx - r_out - 1, cy - r_out - 1, cx + r_out + 1, cy + r_out + 1),
         lambda px, py: max(sd_circle(px, py, cx, cy, r_out), -sd_circle(px, py, cx, cy, r_in)), colour)


def vgrad(y0, y1, c0, c1):
    """Vertical gradient colour function from y0 (c0) to y1 (c1)."""
    span = float(y1 - y0) or 1.0
    return lambda px, py: mix(c0, c1, max(0.0, min(1.0, (py - y0) / span)))


def light_rim(cx, cy, c_light, c_dark, light_angle=-0.75 * math.pi):
    """Colour function: lit towards light_angle (default top-left), shaded opposite."""
    def f(px, py):
        a = math.atan2(py - cy, px - cx)
        t = 0.5 - 0.5 * math.cos(a - light_angle)
        return mix(c_light, c_dark, t)
    return f


def ang_diff(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def arc(canvas, cx, cy, r_in, r_out, a_from, a_to, colour, cap=True):
    """Annular arc; angles in radians, 0 at 12 o'clock, clockwise positive. Rounded caps."""
    if a_to < a_from:
        a_from, a_to = a_to, a_from
    if a_to - a_from < 1e-4:
        return
    r_mid = (r_in + r_out) / 2.0
    cap_r = (r_out - r_in) / 2.0

    def sdf(px, py):
        dx, dy = px - cx, py - cy
        d = math.hypot(dx, dy)
        ang = math.atan2(dx, -dy)
        s_ring = max(d - r_out, r_in - d)
        if a_from <= ang <= a_to or (a_to > math.pi and ang < a_to - 2.0 * math.pi) or (a_from < -math.pi and ang > a_from + 2.0 * math.pi):
            return s_ring
        if not cap:
            return 1.0
        best = 1e9
        for a in (a_from, a_to):
            ex, ey = cx + r_mid * math.sin(a), cy - r_mid * math.cos(a)
            best = min(best, math.hypot(px - ex, py - ey) - cap_r)
        return best

    fill(canvas, (cx - r_out - 1, cy - r_out - 1, cx + r_out + 1, cy + r_out + 1), sdf, colour)


def capsule(canvas, x0, y0, x1, y1, width, colour):
    """A line segment with round ends."""
    def sdf(px, py):
        vx, vy = x1 - x0, y1 - y0
        ll = vx * vx + vy * vy or 1.0
        t = max(0.0, min(1.0, ((px - x0) * vx + (py - y0) * vy) / ll))
        return math.hypot(px - (x0 + vx * t), py - (y0 + vy * t)) - width / 2.0
    pad = width + 1
    fill(canvas, (min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad), sdf, colour)


# --------------------------------------------------------------------------------------------
# PNG output
# --------------------------------------------------------------------------------------------
def write_png(path, width, rows):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + r for r in rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)


# --------------------------------------------------------------------------------------------
# Knob
# --------------------------------------------------------------------------------------------
SWEEP = math.radians(270.0)


def knob_static(size, th):
    c = Canvas(size, size)
    cx = cy = size / 2.0
    r = size / 2.0
    ring_out, ring_in = r - 1.0, r - max(4.0, size * 0.1)
    body_r = ring_in - max(3.0, size * 0.07)
    # unfilled track over the sweep
    arc(c, cx, cy, ring_in, ring_out, -SWEEP / 2.0, SWEEP / 2.0, th["track"])
    # body: shadow, rim lit from top-left, face with a soft vertical gradient
    circle(c, cx, cy + 1.5, body_r + 1.0, with_alpha(th["outline"], 150))
    circle(c, cx, cy, body_r, light_rim(cx, cy, th["bodyLight"], th["bodyDark"]))
    face_r = body_r - max(1.5, size * 0.035)
    circle(c, cx, cy, face_r, vgrad(cy - face_r, cy + face_r, mix(th["body"], th["bodyLight"], 0.35), th["body"]))
    return c, (cx, cy, ring_in, ring_out, body_r, face_r)


def knob_frame(static, geo, t, th, bipolar):
    c = static.copy()
    cx, cy, ring_in, ring_out, body_r, face_r = geo
    a = -SWEEP / 2.0 + SWEEP * t
    if bipolar:
        # centre notch on the track, arc from 12 o'clock to the value
        capsule(c, cx, cy - ring_out - 0.5, cx, cy - ring_in + 0.5, 2.0, th["thumbDark"])
        if abs(t - 0.5) > 0.002:
            arc(c, cx, cy, ring_in, ring_out, min(0.0, a), max(0.0, a), th["accent"])
    else:
        if t > 0.002:
            arc(c, cx, cy, ring_in, ring_out, -SWEEP / 2.0, a, th["accent"])
    # pointer
    px0 = cx + math.sin(a) * face_r * 0.38
    py0 = cy - math.cos(a) * face_r * 0.38
    px1 = cx + math.sin(a) * (face_r - 2.0)
    py1 = cy - math.cos(a) * (face_r - 2.0)
    w = max(2.5, face_r * 0.11)
    capsule(c, px0, py0, px1, py1, w + 1.5, with_alpha(th["outline"], 120))
    capsule(c, px0, py0, px1, py1, w, th["pointer"])
    return c


def render_knob(size, frames, th, bipolar):
    static, geo = knob_static(size, th)
    rows = []
    for i in range(frames):
        rows.extend(knob_frame(static, geo, i / (frames - 1.0), th, bipolar).rows())
    return rows


# --------------------------------------------------------------------------------------------
# Fader (horizontal; the vertical one is the same geometry transposed)
# --------------------------------------------------------------------------------------------
def fader_geometry(length, thickness):
    cap_len, cap_th = 22, thickness - 6
    travel_from = cap_len / 2.0
    travel_to = length - cap_len / 2.0
    return {"len": length, "th": thickness, "cap_len": cap_len, "cap_th": cap_th,
            "from": travel_from, "to": travel_to, "track_h": 8, "mid": thickness / 2.0}


def fader_static(g, th):
    c = Canvas(g["len"], g["th"])
    x0, x1 = g["from"], g["to"]
    y = g["mid"] - g["track_h"] / 2.0
    # track in the same grey as the knob ring, with a dark inner shadow along the top
    rrect(c, x0 - 4, y + 1, x1 - x0 + 8, g["track_h"], 4.0, with_alpha(th["outline"], 160))
    rrect(c, x0 - 4, y, x1 - x0 + 8, g["track_h"], 4.0, vgrad(y, y + g["track_h"], th["bodyDark"], th["track"]))
    return c


def fader_cap(g, th):
    w, h = g["cap_len"], g["cap_th"]
    c = Canvas(w + 2, h + 4)  # room for the drop shadow
    rrect(c, 1, 3, w, h, 5.0, with_alpha(th["outline"], 170))
    rrect(c, 1, 1, w, h, 5.0, vgrad(1, 1 + h, th["thumbLight"], th["thumbDark"]))
    rrect_outline(c, 1, 1, w, h, 5.0, 1.0, with_alpha(th["outline"], 180))
    cx = 1 + w / 2.0
    capsule(c, cx, 1 + h * 0.28, cx, 1 + h * 0.72, 2.0, th["accentDark"])
    capsule(c, cx, 1 + h * 0.28, cx, 1 + h * 0.72, 1.4, th["accent"])
    return c


def fader_frame(static, cap, g, t, th, bipolar):
    c = static.copy()
    x = g["from"] + (g["to"] - g["from"]) * t
    y = g["mid"] - g["track_h"] / 2.0 + 2
    fh = g["track_h"] - 4
    if bipolar:
        mid = (g["from"] + g["to"]) / 2.0
        capsule(c, mid, y - 4, mid, y + fh + 4, 2.0, th["thumbDark"])
        lo, hi = min(mid, x), max(mid, x)
        if hi - lo > 0.5:
            rrect(c, lo, y, hi - lo, fh, 2.0, th["accent"])
    else:
        if x - g["from"] > 0.5:
            rrect(c, g["from"] - 2, y, x - g["from"] + 2, fh, 2.0, th["accent"])
    c.blit(cap, int(round(x - cap.w / 2.0)), int(round(g["mid"] - cap.h / 2.0)))
    return c


def transpose(canvas):
    """Rotate 90 degrees counter-clockwise so a horizontal fader becomes vertical (max at the top)."""
    out = Canvas(canvas.h, canvas.w)
    for y in range(canvas.h):
        for x in range(canvas.w):
            out.px[(canvas.w - 1 - x) * out.w + y] = canvas.px[y * canvas.w + x]
    return out


def render_fader(length, thickness, frames, th, bipolar, vertical):
    g = fader_geometry(length, thickness)
    static, cap = fader_static(g, th), fader_cap(g, th)
    rows = []
    for i in range(frames):
        frame = fader_frame(static, cap, g, i / (frames - 1.0), th, bipolar)
        rows.extend((transpose(frame) if vertical else frame).rows())
    return rows


# --------------------------------------------------------------------------------------------
# Switches and buttons (two-state PNGs for the Button widget)
# --------------------------------------------------------------------------------------------
def render_toggle(th, on):
    w, h = 64, 32
    c = Canvas(w, h)
    rrect(c, 1, 2, w - 2, h - 3, (h - 3) / 2.0, with_alpha(th["outline"], 150))
    track = th["accent"] if on else th["track"]
    rrect(c, 1, 1, w - 2, h - 3, (h - 3) / 2.0, vgrad(1, h - 2, mix(track, th["outline"], 0.25), track))
    rrect_outline(c, 1, 1, w - 2, h - 3, (h - 3) / 2.0, 1.0, with_alpha(th["outline"], 200))
    r = (h - 3) / 2.0 - 3.0
    cx = (w - 1 - r - 4.0) if on else (1 + r + 4.0)
    cy = 1 + (h - 3) / 2.0
    circle(c, cx, cy + 1.0, r, with_alpha(th["outline"], 150))
    circle(c, cx, cy, r, vgrad(cy - r, cy + r, th["thumbLight"], th["thumb"] if on else th["thumbDark"]))
    return c.rows(), w


def render_button(th, on, w=120, h=44, led=True):
    c = Canvas(w, h)
    rad = 8.0
    rrect(c, 1, 3, w - 2, h - 4, rad, with_alpha(th["outline"], 150))
    if on and not led:
        rrect(c, 1, 1, w - 2, h - 4, rad, vgrad(1, h - 3, th["accent"], th["accentDark"]))
    else:
        rrect(c, 1, 1, w - 2, h - 4, rad, vgrad(1, h - 3, th["bodyLight"], th["body"]))
    rrect_outline(c, 1, 1, w - 2, h - 4, rad, 1.0, th["accent"] if on else with_alpha(th["outline"], 220))
    if led:
        lx, ly, lw, lh = 11, (h - 4) / 2.0 + 1 - 9, 6, 18
        if on:
            rrect(c, lx - 3, ly - 3, lw + 6, lh + 6, 6.0, th["accentGlow"])
        rrect(c, lx, ly, lw, lh, 3.0, th["accent"] if on else th["ledOff"])
    return c.rows(), w


# --------------------------------------------------------------------------------------------
# Kit assembly
# --------------------------------------------------------------------------------------------
FRAMES = 127


def asset_table():
    """name -> (renderer, frame size, frames)"""
    t = {}
    t["knob_uni"] = (lambda th: render_knob(90, FRAMES, th, False), (90, 90), FRAMES)
    t["knob_bi"] = (lambda th: render_knob(90, FRAMES, th, True), (90, 90), FRAMES)
    t["knob_small_uni"] = (lambda th: render_knob(56, FRAMES, th, False), (56, 56), FRAMES)
    t["knob_small_bi"] = (lambda th: render_knob(56, FRAMES, th, True), (56, 56), FRAMES)
    t["fader_h_uni"] = (lambda th: render_fader(248, 36, FRAMES, th, False, False), (248, 36), FRAMES)
    t["fader_h_bi"] = (lambda th: render_fader(248, 36, FRAMES, th, True, False), (248, 36), FRAMES)
    t["fader_v_uni"] = (lambda th: render_fader(160, 36, FRAMES, th, False, True), (36, 160), FRAMES)
    t["fader_v_bi"] = (lambda th: render_fader(160, 36, FRAMES, th, True, True), (36, 160), FRAMES)
    t["toggle_on"] = (lambda th: render_toggle(th, True)[0], (64, 32), 1)
    t["toggle_off"] = (lambda th: render_toggle(th, False)[0], (64, 32), 1)
    t["button_on"] = (lambda th: render_button(th, True)[0], (120, 44), 1)
    t["button_off"] = (lambda th: render_button(th, False)[0], (120, 44), 1)
    for n, w in ((2, 122), (3, 80), (4, 58)):
        t["seg%d_on" % n] = (lambda th, w=w: render_button(th, True, w, 40, led=False)[0], (w, 40), 1)
        t["seg%d_off" % n] = (lambda th, w=w: render_button(th, False, w, 40, led=False)[0], (w, 40), 1)
    return t


def manifest(theme_name, th, assets):
    return {
        "kitVersion": KIT_VERSION,
        "theme": theme_name,
        "colours": {k: argb_text(v) for k, v in th.items()},
        "assets": {name: {"file": name + ".png", "width": size[0], "height": size[1], "frames": frames}
                   for name, (_, size, frames) in assets.items()},
        "notes": "Film strips are vertical, frame 0 = minimum at the top. Draw each asset at its native size: the MPC does not scale film-strip frames or button images.",
    }


def render_kit(out_dir, theme_name="mpc-red", accent=None, only=None, quiet=False):
    th = make_theme(theme_name, accent)
    assets = asset_table()
    names = list(assets)
    if only:
        bad = [n for n in only if n not in assets]
        if bad:
            raise ValueError("unknown asset(s): %s (choose from %s)" % (", ".join(bad), ", ".join(names)))
        names = list(only)
    os.makedirs(out_dir, exist_ok=True)
    for name in names:
        render, size, frames = assets[name]
        rows = render(th)
        path = os.path.join(out_dir, name + ".png")
        write_png(path, size[0], rows)
        if not quiet:
            print("make_ui_kit: wrote %s (%dx%d%s)" % (path, size[0], size[1], ", %d frames" % frames if frames > 1 else ""))
    with open(os.path.join(out_dir, "kit.json"), "w", encoding="utf-8") as f:
        json.dump(manifest(theme_name, th, assets), f, indent=2)
        f.write("\n")
    return th


# --------------------------------------------------------------------------------------------
# Preview sheet: every asset at a few values, on the page background
# --------------------------------------------------------------------------------------------
def frame_canvas(rows, w, fh, index):
    c = Canvas(w, fh)
    for y in range(fh):
        row = rows[index * fh + y]
        for x in range(w):
            r, g, b, a = row[x * 4:x * 4 + 4]
            c.px[y * w + x] = (float(r), float(g), float(b), a / 255.0)
    return c


def render_preview(path, th, quiet=False):
    values = [0.0, 0.25, 0.5, 0.75, 1.0]
    sheet = Canvas(940, 600)
    sheet.px = [(float(th["bg"][0]), float(th["bg"][1]), float(th["bg"][2]), 1.0)] * (sheet.w * sheet.h)
    y = 20

    def strip_row(rows, w, fh, x0, y0, gap):
        n = len(rows) // fh
        for i, t in enumerate(values):
            sheet.blit(frame_canvas(rows, w, fh, int(round(t * (n - 1)))), x0 + i * (w + gap), y0)

    for name, bipolar in (("knob", False), ("knob", True)):
        strip_row(render_knob(90, 9, th, bipolar), 90, 90, 20, y, 30)
        strip_row(render_knob(56, 9, th, bipolar), 56, 56, 620, y + 17, 6)
        y += 110
    for bipolar in (False, True):
        rows = render_fader(248, 36, 9, th, bipolar, False)
        for i, t in enumerate(values[::2]):
            sheet.blit(frame_canvas(rows, 248, 36, int(round(t * 8))), 20 + i * 268, y)
        y += 50
    vy = y
    for i, bipolar in enumerate((False, True)):
        rows = render_fader(160, 36, 9, th, bipolar, True)
        for j, t in enumerate(values[::2]):
            sheet.blit(frame_canvas(rows, 36, 160, int(round(t * 8))), 20 + i * 180 + j * 50, vy)
    x = 420
    for on in (False, True):
        r, w = render_toggle(th, on)
        sheet.blit(frame_canvas(r, w, 32, 0), x, vy)
        x += w + 16
    x = 420
    for on in (False, True):
        r, w = render_button(th, on)
        sheet.blit(frame_canvas(r, w, 44, 0), x, vy + 50)
        x += w + 16
    yy = vy + 112
    for n, w in ((2, 122), (3, 80), (4, 58)):
        x = 420
        for i in range(n):
            r, _ = render_button(th, i == 1, w, 40, led=False)
            sheet.blit(frame_canvas(r, w, 40, 0), x, yy)
            x += w + 4
        yy += 48
    write_png(path, sheet.w, sheet.rows())
    if not quiet:
        print("make_ui_kit: wrote preview %s" % path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the SDK film-strip UI kit (knobs, faders, switches) as PNGs.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--out", help="output directory (e.g. plugins/<Name>/skins or ui-kit/mpc-red)")
    ap.add_argument("--theme", default="mpc-red", choices=sorted(THEMES), help="accent colour preset")
    ap.add_argument("--accent", help="override the accent colour (ARGB hex, e.g. ff19c6e0)")
    ap.add_argument("--only", help="comma-separated asset names to render (default: all)")
    ap.add_argument("--preview", help="also write a preview sheet PNG to this path")
    ap.add_argument("--list", action="store_true", help="list the assets and exit")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        for name, (_, size, frames) in asset_table().items():
            print("%-16s %4d x %-4d %s" % (name, size[0], size[1], "%d frames" % frames if frames > 1 else "1 image"))
        return 0
    if not args.out and not args.preview:
        ap.error("--out and/or --preview is required")
    try:
        th = make_theme(args.theme, args.accent)
        if args.out:
            th = render_kit(args.out, args.theme, args.accent, args.only.split(",") if args.only else None, args.quiet)
        if args.preview:
            render_preview(args.preview, th, args.quiet)
    except ValueError as e:
        print("make_ui_kit: ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
