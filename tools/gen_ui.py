#!/usr/bin/env python3
"""gen_ui.py - generate MPC "Plugin Skins" (TUI.json + Q-Links.json) for a plugin.

Usage:
    gen_ui.py --params build/params.json [--ui ui.json] --out "<output folder>/Plugin Skins"
              [--assets-dir skins-assets]

Inputs
    params.json   dumped by host_sim from the native build (parameter names, kinds, choices).
    ui.json       OPTIONAL layout spec. Without it, pages of 16 parameters are generated in
                  index order, named "1", "2", ...
    --kit-cache   OPTIONAL directory where the rendered film-strip kit (tools/make_ui_kit.py) is
                  cached between builds; without it the kit is rendered into the output folder.

Output
    TUI.json      the scripted UI: one tab per page, a 4 x 4 grid of controls per tab.
    Q-Links.json  maps the 16 hardware Q-Link knobs of every tab to parameter indices.
    *.png         the film-strip kit assets used by "knob", "fader" and "switch" widgets.

The JSON written here mirrors the shapes the MPC firmware expects, including the DUPLICATE
"version" keys inside the KnobOverlay / MenuOverlay widget data. Python's json module cannot
write duplicate keys, so this file carries its own tiny ordered-pairs JSON writer.

Standard library only; Python 3.9+.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys

# --------------------------------------------------------------------------------------------
# Layout constants (1280 x 628 tab area, 4 x 4 grid, reading order)
# --------------------------------------------------------------------------------------------
SLOT_X = [23, 343, 663, 983]
SLOT_Y = [45, 196, 346, 497]
SLOT_W, SLOT_H = 278, 107
QLINK_BOUNDS = ["38 0 248 624", "358 0 248 624", "678 0 248 624", "998 0 248 624"]
MAX_CONTROLS_PER_PAGE = 16
MAX_PAGES = 32
FONT_NAME = "Titillium Web"

DEFAULT_STYLE = {
    "background": "ff1e1d1e",
    "accent": "ffd31145",
    "text": "ffffffff",
    "muted": "ff6a6a6c",
}

WIDGETS = ("slider", "knob", "fader", "switch", "empty", "text")
OVERLAYS = ("knob", "menu")
POLARITIES = ("auto", "unipolar", "bipolar")
CONTROL_KEYS = ("param", "widget", "visibleWhen", "text", "overlay", "polarity")
PAGE_KEYS = ("name", "controls")
UI_KEYS = ("pages", "style", "kit", "knobImage", "knobFrames")
KIT_KEYS = ("theme", "accent")
KIT_WIDGETS = ("knob", "fader", "switch")
MAX_SWITCH_CHOICES = 4
BIPOLAR_ZERO_RE = re.compile(r"^[+-]?0*(\.0+)?\s*[^0-9.]*$")

# Segment buttons for choice switches: (asset width, x positions in the slot) per choice count.
SEGMENTS = {2: (122, [15, 141]), 3: (80, [15, 99, 183]), 4: (58, [15, 77, 139, 201])}


def slot_qlink(row, col):
    """Hardware Q-Link number (1..16) for grid slot (row, col), both 0-based."""
    return 13 + col - 4 * row


# --------------------------------------------------------------------------------------------
# Ordered JSON with duplicate keys
# --------------------------------------------------------------------------------------------
class Obj(list):
    """A JSON object as an ordered list of (key, value) pairs. Keys may repeat."""


def obj(*pairs):
    return Obj(pairs)


def dump_json(value, indent=0):
    """Pretty-print like json.dumps(indent=4) but accepting Obj (ordered, duplicate keys)."""
    pad = " " * indent
    inner_pad = " " * (indent + 4)
    if isinstance(value, Obj):
        if not value:
            return "{}"
        parts = [inner_pad + json.dumps(str(k)) + ": " + dump_json(v, indent + 4) for k, v in value]
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(value, dict):
        return dump_json(Obj(value.items()), indent)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        parts = [inner_pad + dump_json(v, indent + 4) for v in value]
        return "[\n" + ",\n".join(parts) + "\n" + pad + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------
class UiError(Exception):
    """A problem in ui.json / params.json, with a message aimed at whoever edits ui.json."""


def fail(where, msg, *hints):
    text = "ERROR in %s: %s" % (where, msg) if where else "ERROR: " + msg
    for h in hints:
        if h:
            text += "\n  " + h
    raise UiError(text)


# --------------------------------------------------------------------------------------------
# params.json
# --------------------------------------------------------------------------------------------
class Param:
    def __init__(self, index, name, kind, choices, default=None, display=""):
        self.index = index
        self.name = name
        self.kind = kind
        self.choices = choices  # list of labels for choice/bool kinds, else None
        self.default = default  # normalised default (0..1) when known
        self.display = display  # display text at the default value

    @property
    def looks_bipolar(self):
        """A continuous parameter whose default sits at the centre and reads as zero ("0", "0.0 dB", "+0 st")."""
        if self.is_discrete or not isinstance(self.default, (int, float)):
            return False
        return abs(self.default - 0.5) <= 0.01 and bool(BIPOLAR_ZERO_RE.match(self.display.strip()))

    @property
    def handle(self):
        return "Parameter %d" % self.index

    @property
    def is_discrete(self):
        return self.kind in ("choice", "bool")


def load_params(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        fail(None, "cannot read params.json: %s" % e,
             "params.json is produced by the build (host_sim <native lib> dump). Run tools/build.py first.")
    except ValueError as e:
        fail(path, "not valid JSON (%s)" % e)
    if not isinstance(data, dict) or not isinstance(data.get("params"), list):
        fail(path, 'expected an object with a "params" array (as written by host_sim dump).')
    params = []
    for i, p in enumerate(data["params"]):
        if not isinstance(p, dict) or "name" not in p:
            fail(path, "params[%d] is not an object with a \"name\"" % i)
        index = p.get("index", i)
        if not isinstance(index, int):
            fail(path, "params[%d].index must be an integer" % i)
        kind = p.get("kind") or "float"
        choices = p.get("choices")
        if kind == "bool":
            if not (isinstance(choices, list) and len(choices) == 2):
                choices = ["Off", "On"]
        elif kind == "choice":
            if not (isinstance(choices, list) and len(choices) >= 1):
                fail(path, 'params[%d] ("%s") has kind "choice" but no "choices" list' % (i, p["name"]))
        else:
            choices = None
        default = p.get("default")
        params.append(Param(index, str(p["name"]), kind, [str(c) for c in choices] if choices else None,
                            default if isinstance(default, (int, float)) and not isinstance(default, bool) else None,
                            str(p.get("display", ""))))
    if not params:
        fail(path, "the plugin reports no parameters; there is nothing to lay out.",
             "Add parameters with addParam() in the plugin source, or use --no-ui in build.py.")
    return params


class ParamTable:
    def __init__(self, params):
        self.params = params
        self.by_name = {}
        self.by_lower = {}
        for p in params:
            self.by_name.setdefault(p.name, p)
            self.by_lower.setdefault(p.name.lower(), p)

    def lookup(self, name, where):
        if not isinstance(name, str):
            fail(where, '"param" must be a parameter name (a string), got %s' % json.dumps(name))
        p = self.by_name.get(name)
        if p is None:
            p = self.by_lower.get(name.lower())
        if p is None:
            names = [q.name for q in self.params]
            close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
            fail(where, 'unknown parameter "%s".' % name,
                 "Available parameter names (from params.json): " + ", ".join('"%s"' % n for n in names),
                 ("Did you mean " + " or ".join('"%s"' % c for c in close) + "?") if close else "")
        return p


# --------------------------------------------------------------------------------------------
# ui.json
# --------------------------------------------------------------------------------------------
class Control:
    def __init__(self, widget, param=None, text=None, visible=None, overlay="knob", polarity="auto"):
        self.widget = widget      # slider | knob | fader | switch | empty | text
        self.param = param        # Param or None
        self.text = text          # for widget "text"
        self.visible = visible    # None or (Param, choiceIndex)
        self.overlay = overlay    # knob | menu
        self.polarity = polarity  # auto | unipolar | bipolar (knob and fader only)

    @property
    def bipolar(self):
        if self.polarity == "bipolar":
            return True
        if self.polarity == "unipolar":
            return False
        return self.param is not None and self.param.looks_bipolar

    @property
    def definition(self):
        """Name of the reusable component definition this control instantiates."""
        if self.widget == "switch":
            return "toggle" if self.param.kind == "bool" or len(self.param.choices) == 1 else "radio%d" % len(self.param.choices)
        pol = "Bi" if self.bipolar else "Uni"
        base = {"slider": "sliderWithFocus", "knob": "knob" + pol, "fader": "fader" + pol}[self.widget]
        return base + ("Menu" if self.overlay == "menu" else "")


class Page:
    def __init__(self, name, controls):
        self.name = name
        self.controls = controls  # list of Control, at most 16, in reading order


def check_keys(where, d, allowed):
    for k in d:
        if k not in allowed:
            close = difflib.get_close_matches(k, allowed, n=1)
            fail(where, 'unknown key "%s".' % k,
                 "Allowed keys: " + ", ".join('"%s"' % a for a in allowed),
                 ('Did you mean "%s"?' % close[0]) if close else "")


def parse_visible_when(spec, table, where):
    if not isinstance(spec, dict):
        fail(where, '"visibleWhen" must be an object like {"param": "Mode", "choice": "Dual"} or {"param": "Mode", "index": 1}')
    check_keys(where, spec, ("param", "choice", "index"))
    if "param" not in spec:
        fail(where, '"visibleWhen" needs a "param" (the name of the choice/bool parameter it depends on)')
    p = table.lookup(spec["param"], where)
    if not p.is_discrete:
        fail(where, '"visibleWhen" parameter "%s" is kind "%s"; it must be a choice or bool parameter.' % (p.name, p.kind),
             "Only discrete parameters can drive IndexedEnabling. Choice/bool parameters: "
             + (", ".join('"%s"' % q.name for q in table.params if q.is_discrete) or "(none)"))
    if ("choice" in spec) == ("index" in spec):
        fail(where, '"visibleWhen" needs exactly one of "choice" (a label) or "index" (0-based number)')
    if "index" in spec:
        idx = spec["index"]
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(p.choices)):
            fail(where, '"visibleWhen".index must be an integer in 0..%d for "%s" (choices: %s)'
                 % (len(p.choices) - 1, p.name, ", ".join('"%s"' % c for c in p.choices)))
        return (p, idx)
    label = spec["choice"]
    if not isinstance(label, str):
        fail(where, '"visibleWhen".choice must be a string label')
    if label in p.choices:
        return (p, p.choices.index(label))
    lowered = [c.lower() for c in p.choices]
    if label.lower() in lowered:
        return (p, lowered.index(label.lower()))
    fail(where, 'parameter "%s" has no choice "%s".' % (p.name, label),
         "Its choices are: " + ", ".join('"%s"' % c for c in p.choices))


def parse_control(spec, table, where):
    if isinstance(spec, str):
        if spec.strip().lower() == "empty" and spec not in table.by_name:
            return Control("empty")
        return Control("slider", param=table.lookup(spec, where))
    if not isinstance(spec, dict):
        fail(where, "each control must be a parameter name (string) or an object, got %s" % json.dumps(spec))
    check_keys(where, spec, CONTROL_KEYS)
    widget = spec.get("widget", "slider")
    if widget not in WIDGETS:
        fail(where, '"widget" must be one of %s, got %s' % (", ".join('"%s"' % w for w in WIDGETS), json.dumps(widget)))
    overlay = spec.get("overlay", "menu" if widget == "switch" else "knob")
    if overlay not in OVERLAYS:
        fail(where, '"overlay" must be "knob" or "menu", got %s' % json.dumps(overlay))
    polarity = spec.get("polarity", "auto")
    if polarity not in POLARITIES:
        fail(where, '"polarity" must be one of %s, got %s' % (", ".join('"%s"' % p for p in POLARITIES), json.dumps(polarity)))
    if "polarity" in spec and widget not in ("knob", "fader"):
        fail(where, '"polarity" only applies to "knob" and "fader" widgets (this control is a "%s")' % widget)
    visible = parse_visible_when(spec["visibleWhen"], table, where) if "visibleWhen" in spec else None
    if widget == "empty":
        return Control("empty", visible=visible)
    if widget == "text":
        text = spec.get("text")
        if not isinstance(text, str):
            fail(where, '"widget": "text" needs a "text" string (the heading to show in this slot)')
        return Control("text", text=text, visible=visible)
    if "param" not in spec:
        fail(where, 'control needs a "param" (parameter name); use "widget": "empty" for a blank slot')
    param = table.lookup(spec["param"], where)
    if widget == "switch":
        if not param.is_discrete:
            fail(where, '"switch" needs a bool or choice parameter; "%s" is kind "%s".' % (param.name, param.kind),
                 'Use "knob" or "fader" for continuous parameters, or make the parameter a choiceParam()/boolParam() in the code.')
        if len(param.choices) > MAX_SWITCH_CHOICES:
            fail(where, '"switch" fits at most %d choices in a slot; "%s" has %d.' % (MAX_SWITCH_CHOICES, param.name, len(param.choices)),
                 'Use {"param": "%s", "widget": "slider", "overlay": "menu"} instead (a bar that steps through the choices with a menu on double-tap).' % param.name)
    return Control(widget, param=param, visible=visible, overlay=overlay, polarity=polarity)


def load_ui(path, table):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        fail(None, "cannot read ui.json: %s" % e)
    except ValueError as e:
        fail(path, "not valid JSON (%s)" % e, "Check for trailing commas or comments; ui.json must be strict JSON.")
    if not isinstance(data, dict):
        fail(path, 'top level must be an object like {"pages": [...]}')
    check_keys(path, data, UI_KEYS)
    pages_spec = data.get("pages")
    if not isinstance(pages_spec, list) or not pages_spec:
        fail(path, '"pages" must be a non-empty array of pages ({"name": "...", "controls": [...]})')
    if len(pages_spec) > MAX_PAGES:
        fail(path, "%d pages given; the MPC supports at most %d tabs." % (len(pages_spec), MAX_PAGES))

    style = dict(DEFAULT_STYLE)
    if "style" in data:
        if not isinstance(data["style"], dict):
            fail(path + ' "style"', "must be an object of ARGB hex colours")
        check_keys(path + ' "style"', data["style"], tuple(DEFAULT_STYLE))
        for k, v in data["style"].items():
            if not (isinstance(v, str) and len(v) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in v)):
                fail(path + ' "style"', '"%s" must be an 8-digit ARGB hex string like "ff1e1d1e", got %s' % (k, json.dumps(v)))
            style[k] = v.lower()

    kit = parse_kit(data.get("kit"), path, "accent" in data.get("style", {}), style)

    knob_image = data.get("knobImage")
    if knob_image is not None and (not isinstance(knob_image, str) or not knob_image.strip()):
        fail(path, '"knobImage" must be a file name (a PNG film strip in the project\'s assets/ folder, which build.py copies into Plugin Skins/)')
    frames = data.get("knobFrames", 127)
    if not isinstance(frames, int) or isinstance(frames, bool) or frames < 2:
        fail(path, '"knobFrames" must be an integer >= 2 (number of frames in the film strip), got %s' % json.dumps(frames))

    pages = []
    for pi, pspec in enumerate(pages_spec):
        where = "%s pages[%d]" % (path, pi)
        if not isinstance(pspec, dict):
            fail(where, 'each page must be an object like {"name": "OSC", "controls": [...]}')
        check_keys(where, pspec, PAGE_KEYS)
        name = pspec.get("name", str(pi + 1))
        if not isinstance(name, str) or not name:
            fail(where, '"name" must be a non-empty string (the tab label)')
        if len(name) > 8:
            print('gen_ui: WARNING: %s name "%s" is longer than 8 characters and may be truncated on the tab.' % (where, name))
        controls_spec = pspec.get("controls", [])
        if not isinstance(controls_spec, list):
            fail(where, '"controls" must be an array')
        if len(controls_spec) > MAX_CONTROLS_PER_PAGE:
            fail(where, "has %d controls but a page is a 4 x 4 grid with at most %d slots."
                 % (len(controls_spec), MAX_CONTROLS_PER_PAGE),
                 "Split the controls over more pages (add another entry to \"pages\").")
        controls = [parse_control(c, table, "%s.controls[%d]" % (where, ci)) for ci, c in enumerate(controls_spec)]
        pages.append(Page(name, controls))

    uses_kit = any(c.widget in KIT_WIDGETS for pg in pages for c in pg.controls)
    if kit is None and uses_kit:
        kit = {"theme": "mpc-red", "accent": style["accent"] if "accent" in data.get("style", {}) else None}
    elif kit is not None and not uses_kit:
        print('gen_ui: NOTE: "kit" is set but no control uses "knob", "fader" or "switch"; nothing from the kit is drawn.')
    return pages, style, knob_image, frames, kit


def parse_kit(spec, path, accent_in_style, style):
    """Returns None or {"theme": name, "accent": ARGB-or-None}. Also aligns the focus-ring accent with the kit."""
    if spec is None:
        return None
    try:
        import make_ui_kit
    except ImportError:
        fail(path, '"kit" needs tools/make_ui_kit.py next to gen_ui.py')
    where = path + ' "kit"'
    if isinstance(spec, str):
        spec = {"theme": spec}
    if not isinstance(spec, dict):
        fail(where, 'must be a theme name like "mpc-red" or an object like {"theme": "amber"} / {"accent": "ff19c6e0"}')
    check_keys(where, spec, KIT_KEYS)
    theme = spec.get("theme", "mpc-red")
    if theme not in make_ui_kit.THEMES:
        fail(where, 'unknown theme %s.' % json.dumps(theme), "Themes: " + ", ".join('"%s"' % t for t in sorted(make_ui_kit.THEMES)))
    accent = spec.get("accent")
    if accent is not None:
        if not (isinstance(accent, str) and len(accent) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in accent)):
            fail(where, '"accent" must be an 8-digit ARGB hex string like "ff19c6e0", got %s' % json.dumps(accent))
        accent = accent.lower()
    elif accent_in_style:
        accent = style["accent"]
    if not accent_in_style:
        # The focus ring follows the kit colour unless style.accent was set explicitly.
        style["accent"] = accent or make_ui_kit.THEMES[theme].lower()
    return {"theme": theme, "accent": accent}


def auto_pages(params):
    pages = []
    for start in range(0, len(params), MAX_CONTROLS_PER_PAGE):
        chunk = params[start:start + MAX_CONTROLS_PER_PAGE]
        pages.append(Page(str(len(pages) + 1), [Control("slider", param=p) for p in chunk]))
    if len(pages) > MAX_PAGES:
        fail(None, "the plugin has %d parameters, which needs %d pages; the MPC supports at most %d tabs."
             % (len(params), len(pages), MAX_PAGES), "Write a ui.json that lays out only the parameters you need.")
    return pages


# --------------------------------------------------------------------------------------------
# TUI.json building blocks (shapes mirror the firmware's generic skins)
# --------------------------------------------------------------------------------------------
def action(on_action, handler, handle_name, additional=""):
    return obj(("version", 1), ("onAction", on_action), ("handler", handler),
               ("handleName", handle_name), ("additionalData", additional))


def background_data(colour):
    def side():
        return obj(("version", 1), ("colour", colour), ("image", ""))
    return obj(("version", 1), ("focussed", side()), ("unfocussed", side()))


def component_definition(actions, children, bg_colour, ignore_mouse=False):
    return obj(("version", 2), ("actions", actions), ("backgroundData", background_data(bg_colour)),
               ("ignoreMousePresses", ignore_mouse), ("disableCoarseDataWheel", False),
               ("componentsData", children))


def entry(name, type_name, data, bounds, accepts_focus, show_invalid, when_visible, remap=(), invalidating=None):
    bounds_pairs = [("version", 2 if invalidating else 1), ("acceptsHWFocus", accepts_focus),
                    ("showWhenDataModelInvalid", show_invalid), ("whenVisible", when_visible),
                    ("boundsType", "Absolute"), ("bounds", bounds)]
    if invalidating:
        bounds_pairs.append(("additionalInvalidatingHandles", list(invalidating)))
    return obj(("version", 2),
               ("componentData", obj(("version", 1), ("name", name), ("type", type_name), ("data", data))),
               ("handle remapping", obj(("version", 1), ("map", [obj(("key", k), ("value", v)) for k, v in remap]))),
               ("bounds", Obj(bounds_pairs)))


def slider_data():
    return obj(("version", 1), ("handleName", "Data"))


def knob_filmstrip_data(image, frames, drag=None):
    pairs = [("version", 3 if drag else 2), ("knobType", "FilmStrip"), ("filmStrip", image), ("numFrames", frames),
             ("invert", False)]
    if drag:
        pairs.append(("dragOrientation", drag))
    pairs.append(("handleName", "Data"))
    return Obj(pairs)


def button_data(on_image, off_image, button_id, group_size):
    return obj(("version", 2), ("onImage", on_image), ("offImage", off_image), ("buttonId", button_id),
               ("numButtonsInGroup", group_size), ("handleName", "Data"), ("gestureBehaviour", "Instant"))


def label_data(label_type, colour, height=30.0, justification="left", text=None):
    pairs = [("version", 1),
             ("textStyle", obj(("version", 1),
                               ("font", obj(("version", 1), ("name", FONT_NAME), ("style", "Regular"), ("height", float(height)))),
                               ("colour", colour), ("justification", justification), ("case", "Original"))),
             ("type", label_type), ("handleName", "Data")]
    if text is not None:
        pairs.append(("text", text))
    return Obj(pairs)


def focus_data(accent):
    return obj(("version", 1), ("backgroundColour", "19ffffff"), ("outlineColour", accent),
               ("backgroundInset", 4.0), ("outlineThickness", 1.0))


def focus_lost_action():
    return action("Focus Lost", "Focus Lost", "Focus Lost", "")


def knob_overlay_data(bipolar=False):
    return obj(("version", 2), ("version", 1), ("actions", [focus_lost_action()]),
               ("overlayType", "Knob With Header"), ("scaleType", "Linear"),
               ("polarityType", "Bipolar" if bipolar else "Unipolar"),
               ("showMinusSign", True), ("handleName", "Data"))


def menu_overlay_data():
    return obj(("version", 1), ("version", 1), ("actions", [focus_lost_action()]),
               ("withNumbers", False), ("handleName", "Data"))


def control_actions(overlay_name, double_click=True):
    acts = [action("Mouse Down", "Q-Link", "Data", "")]
    if double_click:
        acts.append(action("Double Click", "Show Overlay", "Data", overlay_name))
    acts.append(action("Enter Pressed", "Show Overlay", "Data", overlay_name))
    return acts


def slot_bounds_str():
    return "0 0 %d %d" % (SLOT_W, SLOT_H)


def focus_child(style):
    return entry("focus", "Focus", focus_data(style["accent"]), slot_bounds_str(), "No", "Show", "WhenFocussed")


def slider_component(style, overlay_name):
    children = [focus_child(style),
                entry("slider", "Slider", slider_data(), "15 15 248 77", "No", "Show", "Always")]
    return component_definition(control_actions(overlay_name), children, "00000000")


def knob_component(style, overlay_name, knob_image, frames, bipolar=False):
    """A knob slot. knob_image: (file, frames, size) for a film strip, or None for the generic knob."""
    if knob_image:
        image, frames, size = knob_image
        knob = knob_filmstrip_data(image, frames)
        kx = ky = (107 - size) // 2 if size <= 107 else 0
        tx, tw = kx + size + 12, 266 - kx - size - 12
        children = [focus_child(style),
                    entry("knob", "Knob", knob, "%d %d %d %d" % (kx, ky, size, size), "No", "Show", "Always"),
                    entry("name", "Label", label_data("Name", style["muted"], 26.0), "%d 12 %d 36" % (tx, tw), "No", "Show", "Always"),
                    entry("value", "Label", label_data("Value", style["text"], 30.0), "%d 52 %d 42" % (tx, tw), "No", "Show", "Always")]
    else:
        knob = obj(("version", 5), ("knobType", "GenericLegacy"), ("polarity", "BiPolar" if bipolar else "UniPolar"),
                   ("dragOrientation", "Vertical"), ("handleName", "Data"))
        children = [focus_child(style),
                    entry("knob", "Knob", knob, "15 15 77 77", "No", "Show", "Always"),
                    entry("name", "Label", label_data("Name", style["muted"], 26.0), "104 14 160 38", "No", "Show", "Always"),
                    entry("value", "Label", label_data("Value", style["text"], 30.0), "104 54 160 40", "No", "Show", "Always")]
    return component_definition(control_actions(overlay_name), children, "00000000")


def fader_component(style, overlay_name, image, frames):
    """A horizontal film-strip fader (248 x 36) under a name / value header. Drawn by the Knob widget with a horizontal drag."""
    children = [focus_child(style),
                entry("name", "Label", label_data("Name", style["muted"], 24.0), "15 8 135 34", "No", "Show", "Always"),
                entry("value", "Label", label_data("Value", style["text"], 26.0, "right"), "150 8 113 34", "No", "Show", "Always"),
                entry("fader", "Knob", knob_filmstrip_data(image, frames, "Horizontal"), "15 54 248 36", "No", "Show", "Always")]
    return component_definition(control_actions(overlay_name), children, "00000000")


def toggle_component(style, overlay_name):
    """A bool switch: name and value text on the left, a pill toggle (Button widget) on the right."""
    children = [focus_child(style),
                entry("name", "Label", label_data("Name", style["muted"], 26.0), "15 12 170 36", "No", "Show", "Always"),
                entry("value", "Label", label_data("Value", style["text"], 30.0), "15 52 170 42", "No", "Show", "Always"),
                entry("toggle", "Button", button_data("toggle_on.png", "toggle_off.png", 1, 1), "186 20 84 68", "No", "Show", "Always")]
    return component_definition(control_actions(overlay_name, double_click=False), children, "00000000")


def radio_component(style, overlay_name, n):
    """An n-way choice switch: name on top, n segment buttons below (their captions are added by the tab)."""
    width, xs = SEGMENTS[n]
    children = [focus_child(style),
                entry("name", "Label", label_data("Name", style["muted"], 24.0), "15 8 248 34", "No", "Show", "Always")]
    for i, x in enumerate(xs):
        children.append(entry("seg %d" % i, "Button", button_data("seg%d_on.png" % n, "seg%d_off.png" % n, i, n),
                              "%d 54 %d 40" % (x, width), "No", "Show", "Always"))
    return component_definition(control_actions(overlay_name, double_click=False), children, "00000000")


def overlay_component(type_name, data, bg_colour):
    child = entry("overlay", type_name, data, "0 0 1280 800", "No", "Show", "Always")
    return component_definition([], [child], bg_colour, ignore_mouse=True)


# --------------------------------------------------------------------------------------------
# Assembling the two files
# --------------------------------------------------------------------------------------------
def tab_component(page, style):
    children = []
    for i, ctl in enumerate(page.controls):
        row, col = divmod(i, 4)
        bounds = "%d %d %d %d" % (SLOT_X[col], SLOT_Y[row], SLOT_W, SLOT_H)
        invalidating = None
        if ctl.visible:
            vp, idx = ctl.visible
            invalidating = ["IndexedEnabling/%d/%d/%s" % (idx, len(vp.choices), vp.handle)]
        if ctl.widget == "empty":
            continue
        if ctl.widget == "text":
            children.append(entry("text %d" % i, "Label", label_data("Text", style["text"], 30.0, "left", ctl.text),
                                  bounds, "No", "Show", "Always", invalidating=invalidating))
            continue
        children.append(entry(ctl.param.name, ctl.definition, obj(("version", 1), ("handleName", "Data")), bounds,
                              "Yes", "Hide", "Always", remap=[("Data", ctl.param.handle)], invalidating=invalidating))
        if ctl.definition.startswith("radio"):
            # Segment captions live in the tab (the definition is shared by every n-way switch).
            width, xs = SEGMENTS[len(ctl.param.choices)]
            for ci, (choice, x) in enumerate(zip(ctl.param.choices, xs)):
                children.append(entry("%s seg %d" % (ctl.param.name, ci), "Label",
                                      label_data("Text", style["text"], 22.0, "horizontallyCentred verticallyCentred", choice),
                                      "%d %d %d 40" % (SLOT_X[col] + x, SLOT_Y[row] + 54, width),
                                      "No", "Hide", "Always", invalidating=invalidating))
    return component_definition([], children, style["background"])


def build_tui(pages, params, style, knob_image, frames, kit_assets):
    """kit_assets: dict asset name -> (file, frames, (w, h)) for the kit files available, or {} without a kit."""
    used = []
    for page in pages:
        for ctl in page.controls:
            if ctl.param is not None and ctl.definition not in used:
                used.append(ctl.definition)
    if "sliderWithFocus" not in used:
        used.insert(0, "sliderWithFocus")  # always present, so hand edits have a template to copy

    def kit(name):
        if name not in kit_assets:
            fail(None, "the UI kit has no asset %s (tools/make_ui_kit.py --list shows them)" % name)
        return kit_assets[name]

    defs = []
    for name in used:
        menu = name.endswith("Menu")
        overlay = "menu overlay" if menu else ("knob overlay bipolar" if "Bi" in name else "knob overlay")
        base = name[:-4] if menu else name
        if base == "sliderWithFocus":
            comp = slider_component(style, overlay)
        elif base in ("knobUni", "knobBi"):
            if knob_image:
                comp = knob_component(style, overlay, (knob_image, frames, 77), frames, bipolar=(base == "knobBi"))
            elif kit_assets:
                f, n, size = kit("knob_uni" if base == "knobUni" else "knob_bi")
                comp = knob_component(style, overlay, (f, n, size[0]), n, bipolar=(base == "knobBi"))
            else:
                comp = knob_component(style, overlay, None, frames, bipolar=(base == "knobBi"))
        elif base in ("faderUni", "faderBi"):
            f, n, _ = kit("fader_h_uni" if base == "faderUni" else "fader_h_bi")
            comp = fader_component(style, overlay, f, n)
        elif base == "toggle":
            kit("toggle_on"), kit("toggle_off")
            comp = toggle_component(style, overlay)
        elif base.startswith("radio"):
            n = int(base[5:])
            kit("seg%d_on" % n), kit("seg%d_off" % n)
            comp = radio_component(style, overlay, n)
        else:
            fail(None, "internal error: no builder for component %s" % name)
        defs.append((name, comp))
    defs.append(("knob overlay", overlay_component("KnobOverlay", knob_overlay_data(False), style["background"])))
    defs.append(("knob overlay bipolar", overlay_component("KnobOverlay", knob_overlay_data(True), style["background"])))
    defs.append(("menu overlay", overlay_component("MenuOverlay", menu_overlay_data(), style["background"])))

    # replacementStrings: every discrete parameter referenced by a control or a visibleWhen.
    referenced = {}
    for page in pages:
        for ctl in page.controls:
            if ctl.param is not None:
                referenced[ctl.param.index] = ctl.param
            if ctl.visible:
                referenced[ctl.visible[0].index] = ctl.visible[0]
    replacements = [obj(("key", p.handle), ("value", list(p.choices)))
                    for _, p in sorted(referenced.items()) if p.is_discrete]

    tabs = []
    for ti, page in enumerate(pages):
        tabs.append(obj(("version", 1), ("tabName", page.name), ("fnKeyIndex", ti), ("fnKeySubIndex", 0),
                        ("componentDefinition", tab_component(page, style)),
                        ("qlinkBoundsData", list(QLINK_BOUNDS))))

    page_data = obj(
        ("version", 1),
        ("componentDefinitions", obj(("version", 2), ("importFiles", []),
                                     ("localComponentDefinitions", [obj(("key", k), ("value", v)) for k, v in defs]))),
        ("replacementStrings", replacements),
        ("info", obj(("version", 1), ("type", "CompleteDescription"))),
        ("tabs", tabs))
    return obj(("pageData", page_data))


def build_qlinks(pages, params):
    entries = []
    for ti, page in enumerate(pages):
        assigned = {}
        for i, ctl in enumerate(page.controls):
            if ctl.param is not None:
                row, col = divmod(i, 4)
                assigned[slot_qlink(row, col)] = ctl.param.index
        qmap = Obj(("Q-Link %d" % q, assigned.get(q, -1)) for q in range(1, 17))
        entries.append(obj(("Tab", ti + 1), ("SubTab", 1), ("Bank Direction", "Column"), ("Q-Links", qmap)))
    by_index = {p.index: p for p in params}
    first16 = sorted(by_index)[:16]
    program = Obj(("Q-Link %d" % (q + 1), first16[q] if q < len(first16) else -1) for q in range(16))
    return obj(("version", 4),
               ("info", obj(("version", 1), ("type", "CompleteDescription"))),
               ("Screen Mode Q-Links", obj(("version", 4), ("map", entries))),
               ("Program Mode Q-Links", program))


def kit_dir_for(kit, cache_dir, out_dir):
    """Render (or reuse) the kit and return (directory holding the PNGs, assets dict from kit.json)."""
    import make_ui_kit
    tag = "ui-kit-%s%s" % (kit["theme"], ("-" + kit["accent"]) if kit["accent"] else "")
    target = os.path.join(cache_dir, tag) if cache_dir else out_dir
    manifest_path = os.path.join(target, "kit.json")
    fresh = False
    if cache_dir and os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            want_accent = (kit["accent"] or make_ui_kit.THEMES[kit["theme"]]).lower()
            fresh = (m.get("kitVersion") == make_ui_kit.KIT_VERSION and m.get("theme") == kit["theme"]
                     and m.get("colours", {}).get("accent") == want_accent
                     and all(os.path.isfile(os.path.join(target, a["file"])) for a in m.get("assets", {}).values()))
        except (OSError, ValueError):
            fresh = False
    if not fresh:
        make_ui_kit.render_kit(target, kit["theme"], kit["accent"], quiet=True)
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    assets = {name: (a["file"], a["frames"], (a["width"], a["height"])) for name, a in m["assets"].items()}
    return target, assets


def referenced_images(value, acc):
    """Collect every image file name used in the generated TUI (filmStrip / onImage / offImage / image keys)."""
    if isinstance(value, Obj):
        for k, v in value:
            if k in ("filmStrip", "onImage", "offImage", "image") and isinstance(v, str) and v:
                acc.add(v)
            referenced_images(v, acc)
    elif isinstance(value, (list, tuple)):
        for v in value:
            referenced_images(v, acc)
    return acc


def generate(params_path, ui_path, out_dir, assets_dir=None, quiet=False, kit_cache=None):
    params = load_params(params_path)
    table = ParamTable(params)
    if ui_path:
        pages, style, knob_image, frames, kit = load_ui(ui_path, table)
    else:
        pages, style, knob_image, frames, kit = auto_pages(params), dict(DEFAULT_STYLE), None, 127, None
    if knob_image:
        knob_image = os.path.basename(knob_image)
        if assets_dir and not os.path.isfile(os.path.join(assets_dir, knob_image)):
            print('gen_ui: WARNING: knobImage "%s" not found in %s; the knob will render blank on the MPC.'
                  ' (tools/make_knob.py can generate one.)' % (knob_image, assets_dir))
    if len(pages) > 6 and not quiet:
        print("gen_ui: NOTE: %d pages; the MPC screen has six function keys, so tabs beyond the sixth are untested." % len(pages))

    os.makedirs(out_dir, exist_ok=True)
    kit_dir, kit_assets = (kit_dir_for(kit, kit_cache, out_dir) if kit else (None, {}))
    tui = build_tui(pages, params, style, knob_image, frames, kit_assets)
    qlinks = build_qlinks(pages, params)
    if kit_dir and os.path.abspath(kit_dir) != os.path.abspath(out_dir):
        for img in sorted(referenced_images(tui, set())):
            src = os.path.join(kit_dir, img)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(out_dir, img))
    elif kit_dir:
        # Rendered straight into the output: drop the files no control uses.
        keep = referenced_images(tui, set())
        for name in list(kit_assets):
            f = kit_assets[name][0]
            if f not in keep and os.path.isfile(os.path.join(out_dir, f)):
                os.remove(os.path.join(out_dir, f))
        if os.path.isfile(os.path.join(out_dir, "kit.json")):
            os.remove(os.path.join(out_dir, "kit.json"))
    tui_path = os.path.join(out_dir, "TUI.json")
    q_path = os.path.join(out_dir, "Q-Links.json")
    with open(tui_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_json(tui) + "\n")
    with open(q_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_json(qlinks) + "\n")
    if not quiet:
        n_controls = sum(1 for pg in pages for c in pg.controls if c.param is not None)
        print("gen_ui: wrote %s and %s (%d page%s, %d bound control%s, %s%s)"
              % (tui_path, q_path, len(pages), "" if len(pages) == 1 else "s",
                 n_controls, "" if n_controls == 1 else "s",
                 "layout from " + ui_path if ui_path else "auto layout",
                 ", kit " + kit["theme"] + (" accent " + kit["accent"] if kit["accent"] else "") if kit else ""))
        bip = [c.param.name for pg in pages for c in pg.controls if c.widget in ("knob", "fader") and c.polarity == "auto" and c.bipolar]
        if bip:
            print('gen_ui: NOTE: drawn bipolar (centre-zero) by auto-detection: %s. Set "polarity" to override.'
                  % ", ".join('"%s"' % n for n in bip))
    return tui_path, q_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate MPC Plugin Skins (TUI.json + Q-Links.json) from params.json and an optional ui.json.")
    ap.add_argument("--params", required=True, help="build/params.json written by host_sim dump")
    ap.add_argument("--ui", help="ui.json layout spec (omit for an automatic layout of all parameters)")
    ap.add_argument("--out", required=True, help='output directory, normally "<plugin folder>/Plugin Skins"')
    ap.add_argument("--assets-dir", help="folder holding knobImage (only used to warn when the file is missing)")
    ap.add_argument("--kit-cache", help="directory to cache the rendered UI kit between builds (e.g. the project's build/)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        generate(args.params, args.ui, args.out, args.assets_dir, args.quiet, args.kit_cache)
    except UiError as e:
        print("gen_ui: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
