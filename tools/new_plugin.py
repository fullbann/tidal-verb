#!/usr/bin/env python3
"""Create a new MPC plugin project from a template.

Usage:
    python tools/new_plugin.py "Wobble Bass" --vendor "Acme Audio" [--kind instrument|effect] [--dir plugins]

Copies templates/<kind>/ to <dir>/<Name>/ and fills in the placeholders:
    {{NAME}} {{CLASS}} {{VENDOR}} {{UID}} {{SLUG}} {{KIND}} {{IS_INSTRUMENT}} {{NUM_INPUTS}} {{NUM_OUTPUTS}}

Standard library only. Python 3.9+.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

SDK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(SDK_ROOT, "templates")

ILLEGAL_FILENAME_CHARS = '"#@,;:<>*^|?\\/'  # the exact set the MPC strips when it builds the folder name


# ---------------------------------------------------------------------------
# Naming helpers (kept identical to build.py / package.py)
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    """Lowercase name with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^a-z0-9]", "-", name.lower())


def make_class_name(name: str) -> str:
    """'wobble bass 2' -> 'WobbleBass2'. Always a valid C++ identifier."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    cls = "".join(w[:1].upper() + w[1:] for w in words)
    if not cls:
        cls = "MyPlugin"
    if cls[0].isdigit():
        cls = "Plugin" + cls
    return cls


def make_uid(vendor: str, name: str) -> str:
    """Four printable characters derived deterministically from vendor + name.

    First character: an uppercase letter (the vendor's initial when it has one).
    Remaining three: letters/digits from a SHA-256 of "vendor\\nname".
    """
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    alnum = letters + "abcdefghijklmnopqrstuvwxyz0123456789"
    digest = hashlib.sha256((vendor + "\n" + name).encode("utf-8")).digest()
    first = next((c.upper() for c in vendor if c.isalpha() and c.isascii()), None)
    if first is None:
        first = letters[digest[0] % len(letters)]
    rest = "".join(alnum[digest[i] % len(alnum)] for i in (1, 2, 3))
    return first + rest


def legal_filename(s: str) -> str:
    return "".join(c for c in s if c not in ILLEGAL_FILENAME_CHARS)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

def escape_for(ext: str, value: str) -> str:
    """Escape a substitution value so it stays valid inside .json and .cpp string literals."""
    if ext == ".json":
        return json.dumps(value)[1:-1]
    if ext in (".cpp", ".h", ".hpp", ".c"):
        return value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def substitute(text: str, subs: dict, ext: str) -> str:
    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", escape_for(ext, value))
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a new MPC plugin project from a template.")
    ap.add_argument("name", help='Plugin name as shown on the MPC, e.g. "Wobble Bass"')
    ap.add_argument("--vendor", required=True, help='Manufacturer / your name, e.g. "Acme Audio"')
    ap.add_argument("--kind", choices=["instrument", "effect"], default="instrument",
                    help="instrument (receives MIDI, makes sound) or effect (processes audio). Default: instrument")
    ap.add_argument("--dir", default=os.path.join(SDK_ROOT, "plugins"),
                    help="Parent folder for the new project (default: <sdk>/plugins)")
    args = ap.parse_args()

    name = args.name.strip()
    vendor = args.vendor.strip()
    if not name:
        print("ERROR: the plugin name must not be empty.")
        return 1
    if not vendor:
        print("ERROR: --vendor must not be empty.")
        return 1
    if len(name) > 32:
        print("WARNING: plugin names longer than 32 characters are truncated by the VST2 host. Consider a shorter name.")
    if len(vendor) > 64:
        print("WARNING: manufacturer names longer than 64 characters are truncated by the VST2 host.")

    template_dir = os.path.join(TEMPLATES_DIR, args.kind)
    if not os.path.isdir(template_dir):
        print("ERROR: template folder not found: %s" % template_dir)
        return 1

    folder_name = legal_filename(name)
    if not folder_name:
        print("ERROR: the plugin name contains no characters usable in a folder name.")
        return 1
    dest = os.path.abspath(os.path.join(args.dir, folder_name))
    if os.path.exists(dest):
        print("ERROR: %s already exists. Refusing to overwrite it." % dest)
        print("       Choose a different name, or delete/rename that folder first.")
        return 1

    is_instrument = args.kind == "instrument"
    subs = {
        "NAME": name,
        "CLASS": make_class_name(name),
        "VENDOR": vendor,
        "UID": make_uid(vendor, name),
        "SLUG": make_slug(name),
        "KIND": args.kind,
        "IS_INSTRUMENT": "true" if is_instrument else "false",
        "NUM_INPUTS": "0" if is_instrument else "2",
        "NUM_OUTPUTS": "2",
    }

    created = []
    for root, dirs, files in os.walk(template_dir):
        dirs.sort()
        rel_root = os.path.relpath(root, template_dir)
        for d in dirs:
            os.makedirs(os.path.join(dest, substitute(os.path.normpath(os.path.join(rel_root, d)), subs, "")), exist_ok=True)
        for f in sorted(files):
            if f == ".DS_Store":
                continue
            src_path = os.path.join(root, f)
            rel_path = os.path.normpath(os.path.join(rel_root, f))
            out_rel = substitute(rel_path, subs, "")
            out_path = os.path.join(dest, out_rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            ext = os.path.splitext(f)[1].lower()
            try:
                with open(src_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except UnicodeDecodeError:
                shutil.copyfile(src_path, out_path)   # binary asset: copy as-is
            else:
                with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(substitute(text, subs, ext))
            created.append(out_rel)

    rel_dest = os.path.relpath(dest, os.getcwd())
    if rel_dest.startswith(".."):
        rel_dest = dest
    sdk_rel = os.path.relpath(SDK_ROOT, os.getcwd())
    if sdk_rel == ".":
        tools_prefix = "tools"
    else:
        tools_prefix = os.path.join(sdk_rel if not sdk_rel.startswith("..") else SDK_ROOT, "tools")

    print("Created %s plugin project: %s" % (args.kind, dest))
    for c in created:
        print("  " + c)
    print()
    print("  Name:         %s" % name)
    print("  Manufacturer: %s" % vendor)
    print("  Unique ID:    %s   (plugin.json \"uid\" and the C++ uniqueId - keep them equal)" % subs["UID"])
    print("  Class:        %s   (src/%s.cpp)" % (subs["CLASS"], subs["CLASS"]))
    print("  Output:       build/%s - VST - %s/" % (legal_filename(vendor), folder_name))
    print()
    print("Next steps:")
    print("  1. Build it:    python %s %s" % (os.path.join(tools_prefix, "build.py"), _quote(rel_dest)))
    print("  2. Package it:  python %s %s" % (os.path.join(tools_prefix, "package.py"), _quote(rel_dest)))
    print("  3. Edit src/%s.cpp to make it your own (look for the HOW TO comments)." % subs["CLASS"])
    return 0


def _quote(s: str) -> str:
    return '"%s"' % s if " " in s else s


if __name__ == "__main__":
    sys.exit(main())
