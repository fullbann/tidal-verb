#!/usr/bin/env python3
"""Zip a built MPC plugin for sharing.

Usage:
    python tools/package.py <project> [--out dist/]

Takes <project>/build/"<Manufacturer> - VST - <Name>"/ (made by tools/build.py), adds INSTALL.txt
(and the project's README.md if there is one) and writes <out>/<slug>-<version>-mpc.zip.
The zip contains the plugin folder at the top level so users can unzip it straight into "VST2 Plugins".

Standard library only. Python 3.9+.
"""

import argparse
import json
import os
import re
import sys
import zipfile

ILLEGAL_FILENAME_CHARS = '"#@,;:<>*^|?\\/'  # the exact set the MPC strips when it builds the folder name


def make_slug(name):
    return re.sub(r"[^a-z0-9]", "-", name.lower())


def legal_filename(s):
    return "".join(c for c in s if c not in ILLEGAL_FILENAME_CHARS)


def output_folder_name(manifest):
    return legal_filename("%s - VST - %s" % (manifest["manufacturer"], manifest["name"]))


def install_text(manifest, folder):
    name = manifest["name"]
    kind = manifest.get("kind", "")
    description = manifest.get("description")
    version = manifest.get("version", "")
    lines = []
    lines.append("%s%s - plugin for Akai MPC standalone" % (name, (" " + str(version)) if version else ""))
    lines.append("by %s" % manifest["manufacturer"])
    lines.append("=" * 72)
    lines.append("")
    if isinstance(description, str) and description.strip():
        lines.append(description.strip())
        lines.append("")
    if kind == "instrument":
        lines.append("This is an INSTRUMENT plugin (it makes sound and is played from the pads / keys / sequencer).")
    elif kind == "effect":
        lines.append("This is an EFFECT plugin (it processes audio as an insert on a track or program).")
    lines.append("")
    lines.append("HOW TO INSTALL")
    lines.append("--------------")
    lines.append("")
    lines.append("1. Copy the folder")
    lines.append("")
    lines.append('       "%s"' % folder)
    lines.append("")
    lines.append('   into a folder named exactly   VST2 Plugins   at the root (top level) of a USB drive,')
    lines.append("   an SD card, or the MPC's internal drive. Create the \"VST2 Plugins\" folder if it does not")
    lines.append("   exist yet. The result should look like this:")
    lines.append("")
    lines.append("       <drive>/VST2 Plugins/%s/plugin.json" % folder)
    lines.append("       <drive>/VST2 Plugins/%s/version.xml" % folder)
    lines.append("       <drive>/VST2 Plugins/%s/VST/..." % folder)
    lines.append("")
    lines.append("   Keep the folder name exactly as it is - the MPC uses it to find the plugin.")
    lines.append("")
    lines.append("2. On the MPC, open   Preferences > Enhancements (Beta)   and switch on both")
    lines.append('   "Enable Enhancements" and "VST2 Support". Plug in the drive (or restart the MPC if the')
    lines.append("   plugin does not appear).")
    lines.append("")
    lines.append('3. The plugin now appears in the plugin browser under   "External VST Plugins".')
    if kind == "instrument":
        lines.append("   Create a Plugin program and pick it from the list - then play it from the pads or a keyboard.")
    elif kind == "effect":
        lines.append("   Add it as an insert effect on a track, program, pad or mixer channel.")
    else:
        lines.append("   Instruments appear when you create a Plugin program; effects appear as insert effects.")
    lines.append("")
    lines.append("TO UNINSTALL")
    lines.append("------------")
    lines.append("")
    lines.append('Delete the "%s" folder from "VST2 Plugins". Projects that used the plugin will' % folder)
    lines.append("still open, but the plugin's sound/effect will be missing.")
    lines.append("")
    lines.append("PLEASE READ - IMPORTANT")
    lines.append("-----------------------")
    lines.append("")
    lines.append("* Third-party plugins are NOT supported by inMusic / Akai. If something goes wrong, inMusic")
    lines.append("  support cannot help with it.")
    lines.append("* A plugin runs inside the MPC software. A faulty plugin can make the MPC freeze or crash.")
    lines.append("  SAVE YOUR WORK OFTEN, and do not rely on third-party plugins for a live show until you")
    lines.append("  have tested them thoroughly.")
    lines.append("* Requires MPC OS 3.10.1 or later. Older MPC OS versions ignore this folder entirely;")
    lines.append("  update the MPC first (Preferences > Info shows the version).")
    lines.append("* VST plugins must be enabled on the device: Preferences > Enhancements (Beta) >")
    lines.append("  Enable Enhancements and VST2 Support. Until both are on, the plugin stays hidden.")
    lines.append("* If the MPC crashes or the plugin never appears, remove the folder from \"VST2 Plugins\"")
    lines.append("  and restart the MPC.")
    lines.append("")
    if isinstance(manifest.get("url"), str) and manifest["url"].strip():
        lines.append("More information: %s" % manifest["url"].strip())
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zip a built MPC plugin folder for sharing.")
    ap.add_argument("project", help="plugin project folder (must have been built with tools/build.py)")
    ap.add_argument("--out", default=None, help="output folder for the zip (default: <project>/dist)")
    args = ap.parse_args(argv)

    project = os.path.abspath(args.project)
    manifest_path = os.path.join(project, "plugin.json")
    if not os.path.isfile(manifest_path):
        print("ERROR: no plugin.json in %s - is this a plugin project folder?" % project)
        return 1
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except ValueError as e:
        print("ERROR: plugin.json is not valid JSON: %s" % e)
        return 1
    for key in ("name", "manufacturer"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            print("ERROR: plugin.json is missing the required string \"%s\"" % key)
            return 1

    folder = output_folder_name(manifest)
    built = os.path.join(project, "build", folder)
    if not os.path.isdir(built):
        print("ERROR: nothing to package: %s does not exist." % built)
        print("       Build the plugin first:  python tools/build.py %s" % args.project)
        return 1

    problems = []
    for key, default in (("file32", "VST/%s-arm32.so" % make_slug(manifest["name"])),
                         ("file64", "VST/%s-arm64.so" % make_slug(manifest["name"]))):
        rel = manifest.get(key) if isinstance(manifest.get(key), str) else default
        if not os.path.isfile(os.path.join(built, rel.replace("/", os.sep))):
            problems.append(rel)
    if problems:
        print("WARNING: the built folder is missing %s. The plugin will not work on every MPC model." % ", ".join(problems))
        print("         Run  python tools/build.py %s --target all  to build both architectures." % args.project)
    if not os.path.isfile(os.path.join(built, "version.xml")):
        print("WARNING: version.xml is missing from the built folder; rebuild with tools/build.py")

    version = str(manifest.get("version", "1.0.0")).strip() or "1.0.0"
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(project, "dist")
    os.makedirs(out_dir, exist_ok=True)
    zip_name = "%s-%s-mpc.zip" % (make_slug(manifest["name"]), legal_filename(version).replace(" ", "-"))
    zip_path = os.path.join(out_dir, zip_name)

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(built):
            dirs.sort()
            for f in sorted(files):
                if f in (".DS_Store", "Thumbs.db"):
                    continue
                full = os.path.join(root, f)
                arc = os.path.join(folder, os.path.relpath(full, built)).replace(os.sep, "/")
                zf.write(full, arc)
                count += 1
        zf.writestr("INSTALL.txt", install_text(manifest, folder))
        count += 1
        readme = os.path.join(project, "README.md")
        if os.path.isfile(readme):
            zf.write(readme, "README.md")
            count += 1

    print("Packaged %d files into:" % count)
    print("  " + zip_path)
    print("Share that zip. Users unzip it and copy the \"%s\" folder into \"VST2 Plugins\" on their MPC drive" % folder)
    print("(full instructions are in INSTALL.txt inside the zip).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
