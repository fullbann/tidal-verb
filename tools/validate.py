#!/usr/bin/env python3
"""validate.py - check a built MPC plugin folder before copying it to the hardware.

Usage:
    validate.py "<build>/<Manufacturer> - VST - <Name>"  [--params build/params.json] [--strict]
    validate.py <project dir>                            (uses <project>/build/<folder> and build/params.json)

Checks (printed as OK / WARN / ERROR lines; exit code 1 when any ERROR, --strict makes WARN fatal):
  * plugin.json: valid, required keys, kind, uid format, numOutputs > 0, file32/file64 relative paths inside the folder
  * folder name equals "<manufacturer> - VST - <name>" (the MPC looks skins up by that name)
  * each .so: ELF magic, class, machine (ARM / AArch64), ET_DYN, DT_NEEDED libraries, GLIBC / GLIBCXX
    symbol-version requirements, exported VSTPluginMain   (pure-python ELF parsing, no dependencies)
  * params.json (what the binary reports) agrees with plugin.json: uid, name, vendor, kind, I/O counts
  * Plugin Skins: TUI.json / Q-Links.json structure, parameter handles in range, Q-Link values, tabs, images
  * version.xml: parses; <uiversion> must be <= 0.1.0.0

Standard library only; Python 3.9+.
"""

import argparse
import json
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET

ALLOWED_LIBS = {"libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1", "libgcc_s.so.1",
                "ld-linux-armhf.so.3", "ld-linux-aarch64.so.1"}
WARN_LIBS = {"libstdc++.so.6", "libatomic.so.1"}
GLIBC_MAX = (2, 39)
GLIBC_WARN_ABOVE = (2, 36)
GLIBCXX_MAX = (3, 4, 32)
UIVERSION_MAX = (0, 1, 0, 0)
MAX_TABS = 32
ILLEGAL_FILENAME_CHARS = '"#@,;:<>*^|?\\/'  # the exact set the MPC strips when it builds the folder name
REQUIRED_PLUGIN_KEYS = ("name", "manufacturer", "version", "kind", "uid", "numInputs", "numOutputs", "file32", "file64")

EM_ARM, EM_AARCH64 = 40, 183
ET_DYN = 3
SHT_DYNSYM, SHT_DYNAMIC, SHT_GNU_VERNEED = 11, 6, 0x6FFFFFFE
DT_NEEDED = 1


class Report:
    def __init__(self):
        self.errors = 0
        self.warnings = 0

    def ok(self, msg):
        print("OK     " + msg)

    def warn(self, msg):
        self.warnings += 1
        print("WARN   " + msg)

    def error(self, msg):
        self.errors += 1
        print("ERROR  " + msg)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def legal_file_name(s):
    return "".join(ch for ch in s if ch not in ILLEGAL_FILENAME_CHARS)


def expected_folder_name(manufacturer, name):
    return legal_file_name("%s - VST - %s" % (manufacturer, name))


def fourcc_int(uid):
    """plugin.json uid -> 32-bit int (MPC_FOURCC: first char is the most significant byte). None if invalid."""
    if isinstance(uid, bool):
        return None
    if isinstance(uid, int):
        return uid & 0xFFFFFFFF if 0 <= uid <= 0xFFFFFFFF else None
    if isinstance(uid, str):
        if re.fullmatch(r"0[xX][0-9a-fA-F]{1,8}", uid):
            return int(uid, 16)
        if len(uid) == 4 and all(32 <= ord(ch) < 127 for ch in uid):
            v = 0
            for ch in uid:
                v = (v << 8) | ord(ch)
            return v
    return None


def load_json_last_wins(path):
    """json.load that tolerates duplicate keys (the last value wins), as the firmware's own skins have them."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=lambda pairs: dict(pairs))


def version_tuple(text, max_parts):
    parts = text.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts) or len(parts) > max_parts:
        return None
    return tuple(int(p) for p in parts)


# --------------------------------------------------------------------------------------------
# Minimal ELF reader
# --------------------------------------------------------------------------------------------
class ElfError(Exception):
    pass


class Elf:
    def __init__(self, data):
        self.data = data
        if len(data) < 64 or data[:4] != b"\x7fELF":
            raise ElfError("not an ELF file (bad magic); is this really a Linux shared object?")
        self.cls = data[4]              # 1 = ELFCLASS32, 2 = ELFCLASS64
        self.little = data[5] == 1
        if self.cls not in (1, 2) or data[5] not in (1, 2):
            raise ElfError("unsupported ELF class/endianness")
        self.e = "<" if self.little else ">"
        self.is64 = self.cls == 2
        if self.is64:
            (self.e_type, self.e_machine, _ver, _entry, _phoff, self.e_shoff, _flags, _ehsize, _phentsize,
             _phnum, self.e_shentsize, self.e_shnum, self.e_shstrndx) = struct.unpack_from(self.e + "HHIQQQIHHHHHH", data, 16)
        else:
            (self.e_type, self.e_machine, _ver, _entry, _phoff, self.e_shoff, _flags, _ehsize, _phentsize,
             _phnum, self.e_shentsize, self.e_shnum, self.e_shstrndx) = struct.unpack_from(self.e + "HHIIIIIHHHHHH", data, 16)
        self.sections = self._read_sections()

    def _read_sections(self):
        secs = []
        if self.e_shoff == 0 or self.e_shnum == 0:
            return secs
        fmt = self.e + ("IIQQQQIIQQ" if self.is64 else "IIIIIIIIII")
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            if off + struct.calcsize(fmt) > len(self.data):
                raise ElfError("truncated section header table")
            (name, typ, flags, addr, offset, size, link, info, align, entsize) = struct.unpack_from(fmt, self.data, off)
            secs.append({"name_off": name, "type": typ, "offset": offset, "size": size, "link": link,
                         "info": info, "entsize": entsize, "addr": addr})
        if self.e_shstrndx < len(secs):
            strtab = secs[self.e_shstrndx]
            for s in secs:
                s["name"] = self._cstr(strtab["offset"] + s["name_off"])
        else:
            for s in secs:
                s["name"] = ""
        return secs

    def _cstr(self, off):
        end = self.data.find(b"\0", off)
        if off < 0 or end < 0 or off > len(self.data):
            return ""
        return self.data[off:end].decode("utf-8", "replace")

    def section_by_type(self, typ):
        return [s for s in self.sections if s["type"] == typ]

    def section_bytes(self, s):
        return self.data[s["offset"]:s["offset"] + s["size"]]

    def strtab_of(self, s):
        link = s["link"]
        if link >= len(self.sections):
            raise ElfError("section %r links to a missing string table" % s.get("name"))
        return self.sections[link]["offset"]

    def needed(self):
        out = []
        for s in self.section_by_type(SHT_DYNAMIC):
            strbase = self.strtab_of(s)
            fmt = self.e + ("qQ" if self.is64 else "iI")
            size = struct.calcsize(fmt)
            blob = self.section_bytes(s)
            for off in range(0, len(blob) - size + 1, size):
                tag, val = struct.unpack_from(fmt, blob, off)
                if tag == 0:
                    break
                if tag == DT_NEEDED:
                    out.append(self._cstr(strbase + val))
        return out

    def required_versions(self):
        """Names from .gnu.version_r, e.g. ['GLIBC_2.17', 'GLIBCXX_3.4.21']."""
        names = []
        for s in self.section_by_type(SHT_GNU_VERNEED):
            strbase = self.strtab_of(s)
            base = s["offset"]
            off = base
            for _ in range(s["info"] or 0x10000):
                if off + 16 > len(self.data):
                    break
                vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from(self.e + "HHIII", self.data, off)
                aoff = off + vn_aux
                for _ in range(vn_cnt):
                    if aoff + 16 > len(self.data):
                        break
                    _hash, _flags, _other, vna_name, vna_next = struct.unpack_from(self.e + "IHHII", self.data, aoff)
                    names.append(self._cstr(strbase + vna_name))
                    if vna_next == 0:
                        break
                    aoff += vna_next
                if vn_next == 0:
                    break
                off += vn_next
        return names

    def dynamic_symbols(self):
        """Yield (name, binding, defined) for every .dynsym entry."""
        for s in self.section_by_type(SHT_DYNSYM):
            strbase = self.strtab_of(s)
            blob = self.section_bytes(s)
            if self.is64:
                fmt, size = self.e + "IBBHQQ", 24
            else:
                fmt, size = self.e + "IIIBBH", 16
            for off in range(0, len(blob) - size + 1, size):
                if self.is64:
                    st_name, st_info, _other, st_shndx, _value, _size = struct.unpack_from(fmt, blob, off)
                else:
                    st_name, _value, _size, st_info, _other, st_shndx = struct.unpack_from(fmt, blob, off)
                yield self._cstr(strbase + st_name), st_info >> 4, st_shndx != 0


def check_binary(rep, path, want64, label):
    try:
        with open(path, "rb") as f:
            data = f.read()
        elf = Elf(data)
    except OSError as e:
        rep.error("%s: cannot read %s (%s)" % (label, path, e))
        return
    except ElfError as e:
        rep.error("%s: %s: %s" % (label, path, e))
        return
    except struct.error:
        rep.error("%s: %s: malformed ELF headers" % (label, path))
        return

    want_cls = 2 if want64 else 1
    if elf.cls != want_cls:
        rep.error("%s: %s is a %d-bit ELF but %s must be %d-bit" % (label, path, 64 if elf.is64 else 32, label, 64 if want64 else 32))
    want_machine, want_name = (EM_AARCH64, "AArch64") if want64 else (EM_ARM, "ARM")
    if elf.e_machine != want_machine:
        rep.error("%s: %s is built for machine type %d, expected %s (%d). Check the cross-compiler -target."
                  % (label, path, elf.e_machine, want_name, want_machine))
    if elf.e_type != ET_DYN:
        rep.error("%s: %s is not a shared object (ELF type %d, expected ET_DYN=3). Link with -shared -fPIC." % (label, path, elf.e_type))
    if not elf.sections:
        rep.warn("%s: %s has no section headers; cannot check libraries, GLIBC versions or exports" % (label, path))
        return
    if elf.cls == want_cls and elf.e_machine == want_machine and elf.e_type == ET_DYN:
        rep.ok("%s: %s is a %d-bit %s shared object" % (label, os.path.basename(path), 64 if want64 else 32, want_name))

    try:
        needed = elf.needed()
        versions = elf.required_versions()
        symbols = list(elf.dynamic_symbols())
    except (ElfError, struct.error) as e:
        rep.error("%s: %s: cannot parse dynamic section (%s)" % (label, path, e))
        return

    for lib in needed:
        if lib in ALLOWED_LIBS:
            continue
        if lib in WARN_LIBS:
            rep.warn("%s: dynamic %s: safer to link statically (-static-libstdc++ -static-libgcc); the MPC's copy may differ" % (label, lib.split(".so")[0]))
        else:
            rep.error("%s: needs shared library %s which is not on the MPC. Link it statically or remove the dependency." % (label, lib))
    rep.ok("%s: needed libraries: %s" % (label, ", ".join(needed) if needed else "(none)"))

    glibc = [version_tuple(v[len("GLIBC_"):], 3) for v in versions if v.startswith("GLIBC_")]
    glibc = [g for g in glibc if g]
    if glibc:
        top = max(glibc)
        top_s = ".".join(str(x) for x in top)
        if top > GLIBC_MAX:
            rep.error("%s: requires GLIBC_%s but the MPC firmware ships glibc %s. Build against an older glibc "
                      "(zig -target ...-linux-gnueabihf.2.31 / aarch64-linux-gnu.2.31)." % (label, top_s, ".".join(map(str, GLIBC_MAX))))
        elif top > GLIBC_WARN_ABOVE:
            rep.warn("%s: requires GLIBC_%s; older MPC firmware (glibc %s) will not load it" % (label, top_s, ".".join(map(str, GLIBC_WARN_ABOVE))))
        else:
            rep.ok("%s: max GLIBC requirement %s" % (label, top_s))
    glibcxx = [version_tuple(v[len("GLIBCXX_"):], 3) for v in versions if v.startswith("GLIBCXX_")]
    glibcxx = [g for g in glibcxx if g]
    if glibcxx:
        top = max(glibcxx)
        top_s = ".".join(str(x) for x in top)
        if top > GLIBCXX_MAX:
            rep.error("%s: requires GLIBCXX_%s (max supported %s). Link libstdc++ statically." % (label, top_s, ".".join(map(str, GLIBCXX_MAX))))
        else:
            rep.ok("%s: max GLIBCXX requirement %s" % (label, top_s))

    exported = [n for n, binding, defined in symbols if n == "VSTPluginMain" and defined and binding in (1, 2)]
    if exported:
        rep.ok("%s: exports VSTPluginMain" % label)
    else:
        rep.error("%s: VSTPluginMain is not an exported dynamic symbol. Use MPC_PLUGIN_ENTRY(YourClass) and do not hide it "
                  "(the framework marks it visible; check -fvisibility settings / version script)." % label)


# --------------------------------------------------------------------------------------------
# plugin.json
# --------------------------------------------------------------------------------------------
def check_plugin_json(rep, folder):
    path = os.path.join(folder, "plugin.json")
    if not os.path.isfile(path):
        rep.error("plugin.json missing in %s" % folder)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            pj = json.load(f)
    except ValueError as e:
        rep.error("plugin.json is not valid JSON: %s" % e)
        return None
    if not isinstance(pj, dict):
        rep.error("plugin.json must be a JSON object")
        return None
    missing = [k for k in REQUIRED_PLUGIN_KEYS if k not in pj]
    if missing:
        rep.error("plugin.json is missing required keys: %s" % ", ".join(missing))
    for k in ("name", "manufacturer", "version", "file32", "file64"):
        if k in pj and (not isinstance(pj[k], str) or not pj[k].strip()):
            rep.error('plugin.json "%s" must be a non-empty string' % k)
    if "kind" in pj and pj["kind"] not in ("instrument", "effect"):
        rep.error('plugin.json "kind" must be "instrument" or "effect", got %s' % json.dumps(pj["kind"]))
    for k in ("numInputs", "numOutputs"):
        if k in pj and (not isinstance(pj[k], int) or isinstance(pj[k], bool) or pj[k] < 0):
            rep.error('plugin.json "%s" must be a non-negative integer' % k)
    if isinstance(pj.get("numOutputs"), int) and pj["numOutputs"] == 0:
        rep.error("plugin.json numOutputs is 0; the MPC does not list plugins without audio outputs")
    if "uid" in pj and fourcc_int(pj["uid"]) is None:
        rep.error('plugin.json "uid" must be a 4-character string (e.g. "AcWb"), an integer, or "0x" hex, got %s' % json.dumps(pj["uid"]))
    if "version" in pj and isinstance(pj["version"], str) and version_tuple(pj["version"], 4) is None:
        rep.warn('plugin.json "version" should look like "1.0.0" (digits and dots), got %s' % json.dumps(pj["version"]))
    if not missing:
        rep.ok("plugin.json: %s by %s (%s, uid %s)" % (pj["name"], pj["manufacturer"], pj.get("kind"), pj.get("uid")))
    return pj


def resolve_binary(rep, folder, pj, key):
    rel = pj.get(key)
    if not isinstance(rel, str) or not rel:
        return None
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        rep.error('plugin.json "%s" must be a path relative to the plugin folder, got %s' % (key, rel))
        return None
    if ".." in rel.replace("\\", "/").split("/"):
        rep.error('plugin.json "%s" must not contain "..": %s' % (key, rel))
        return None
    full = os.path.normpath(os.path.join(folder, rel.replace("\\", "/")))
    root = os.path.normpath(folder)
    if os.path.commonpath([root, full]) != root:
        rep.error('plugin.json "%s" points outside the plugin folder: %s' % (key, rel))
        return None
    if not os.path.isfile(full):
        rep.error('plugin.json "%s" = %s does not exist in the plugin folder' % (key, rel))
        return None
    return full


# --------------------------------------------------------------------------------------------
# params.json vs plugin.json
# --------------------------------------------------------------------------------------------
def check_params(rep, params_path, pj):
    try:
        with open(params_path, "r", encoding="utf-8") as f:
            params = json.load(f)
    except (OSError, ValueError) as e:
        rep.error("cannot read params.json (%s): %s" % (params_path, e))
        return None
    if not isinstance(params, dict) or not isinstance(params.get("params"), list):
        rep.error("params.json has no \"params\" array")
        return None
    if pj is None:
        return params
    bin_uid = params.get("uidInt")
    if bin_uid is None and isinstance(params.get("uid"), str):
        bin_uid = fourcc_int(params["uid"])
    want_uid = fourcc_int(pj.get("uid"))
    if bin_uid is not None and want_uid is not None:
        if bin_uid != want_uid:
            rep.error('uid mismatch: plugin.json says %s (0x%08x) but the binary reports %s (0x%08x). '
                      'Make PluginInfo.uniqueId = MPC_FOURCC(...) match plugin.json "uid".'
                      % (json.dumps(pj.get("uid")), want_uid, json.dumps(params.get("uid")), bin_uid))
        else:
            rep.ok("uid matches the binary (%s)" % pj.get("uid"))
    for pkey, bkey in (("name", "name"), ("manufacturer", "vendor")):
        if pkey in pj and bkey in params:
            if pj[pkey] != params[bkey]:
                rep.error('%s mismatch: plugin.json "%s" = %s but the binary reports %s. The MPC finds skins by these strings; '
                          'make PluginInfo.%s and plugin.json agree exactly.' % (pkey, pkey, json.dumps(pj[pkey]), json.dumps(params[bkey]), bkey))
            else:
                rep.ok("%s matches the binary (%s)" % (pkey, pj[pkey]))
    if "kind" in pj and "isInstrument" in params:
        want = pj["kind"] == "instrument"
        if bool(params["isInstrument"]) != want:
            rep.error('kind mismatch: plugin.json kind "%s" but the binary reports isInstrument=%s'
                      % (pj["kind"], json.dumps(bool(params["isInstrument"]))))
        else:
            rep.ok("kind matches the binary (%s)" % pj["kind"])
    for k in ("numInputs", "numOutputs"):
        if k in pj and k in params:
            if pj[k] != params[k]:
                rep.error("%s mismatch: plugin.json says %s but the binary reports %s" % (k, pj[k], params[k]))
            else:
                rep.ok("%s matches the binary (%s)" % (k, pj[k]))
    return params


# --------------------------------------------------------------------------------------------
# Plugin Skins
# --------------------------------------------------------------------------------------------
PARAM_HANDLE_RE = re.compile(r"(?:^|/)Parameter (\d+)$")


def walk(value, path=""):
    """Yield (path, value) for every node in a parsed JSON tree."""
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            for item in walk(v, path + "/" + str(k)):
                yield item
    elif isinstance(value, list):
        for i, v in enumerate(value):
            for item in walk(v, path + "[%d]" % i):
                yield item


def check_skins(rep, skins_dir, nparams):
    tui_path = os.path.join(skins_dir, "TUI.json")
    q_path = os.path.join(skins_dir, "Q-Links.json")
    tabs_in_tui = None
    if not os.path.isfile(tui_path):
        rep.error("Plugin Skins/TUI.json missing (a Plugin Skins folder needs TUI.json and Q-Links.json)")
    else:
        try:
            tui = load_json_last_wins(tui_path)
        except ValueError as e:
            rep.error("Plugin Skins/TUI.json is not valid JSON: %s" % e)
            tui = None
        if isinstance(tui, dict):
            page = tui.get("pageData")
            if not isinstance(page, dict):
                rep.error('TUI.json: top level must be {"pageData": {...}}')
            else:
                if page.get("version") != 1:
                    rep.error("TUI.json: pageData.version must be 1, got %s" % json.dumps(page.get("version")))
                tabs = page.get("tabs")
                if not isinstance(tabs, list) or not tabs:
                    rep.error("TUI.json: pageData.tabs must be a non-empty array")
                else:
                    tabs_in_tui = len(tabs)
                    if len(tabs) > MAX_TABS:
                        rep.error("TUI.json: %d tabs; at most %d are supported" % (len(tabs), MAX_TABS))
                    seen = {}
                    for i, t in enumerate(tabs):
                        if not isinstance(t, dict):
                            rep.error("TUI.json: tabs[%d] is not an object" % i)
                            continue
                        key = (t.get("fnKeyIndex"), t.get("fnKeySubIndex", 0))
                        if key in seen:
                            rep.error("TUI.json: tabs[%d] and tabs[%d] both use fnKeyIndex %s / fnKeySubIndex %s"
                                      % (seen[key], i, key[0], key[1]))
                        seen[key] = i
                        if not isinstance(t.get("tabName"), str):
                            rep.warn("TUI.json: tabs[%d] has no tabName" % i)
                # Every "Parameter N" handle in range, and every image referenced present.
                bad = []
                images = set()
                for path, v in walk(tui):
                    if isinstance(v, str):
                        m = PARAM_HANDLE_RE.search(v)
                        if m and nparams is not None and int(m.group(1)) >= nparams:
                            bad.append((path, v))
                        if path.endswith(("/filmStrip", "/image", "/onImage", "/offImage")) and v:
                            images.add(v)
                if bad:
                    for path, v in bad[:10]:
                        rep.error('TUI.json: "%s" at %s refers to a parameter the plugin does not have (it has %d: indices 0..%d)'
                                  % (v, path, nparams, nparams - 1))
                    if len(bad) > 10:
                        rep.error("TUI.json: ... and %d more out-of-range parameter handles" % (len(bad) - 10))
                elif nparams is not None:
                    rep.ok("TUI.json: all parameter handles are within 0..%d" % (nparams - 1))
                for img in sorted(images):
                    if os.path.isfile(os.path.join(skins_dir, img)):
                        rep.ok("TUI.json: image %s present" % img)
                    else:
                        rep.error("TUI.json: references image %s which is not in Plugin Skins/" % img)
                if tabs_in_tui:
                    rep.ok("TUI.json: %d tab%s" % (tabs_in_tui, "" if tabs_in_tui == 1 else "s"))
    if not os.path.isfile(q_path):
        rep.error("Plugin Skins/Q-Links.json missing")
    else:
        try:
            q = load_json_last_wins(q_path)
        except ValueError as e:
            rep.error("Plugin Skins/Q-Links.json is not valid JSON: %s" % e)
            q = None
        if isinstance(q, dict):
            if q.get("version") != 4:
                rep.error("Q-Links.json: version must be 4, got %s" % json.dumps(q.get("version")))
            screen = q.get("Screen Mode Q-Links")
            entries = screen.get("map") if isinstance(screen, dict) else None
            if not isinstance(entries, list):
                rep.error('Q-Links.json: "Screen Mode Q-Links".map must be an array')
                entries = []
            if tabs_in_tui is not None and len(entries) != tabs_in_tui:
                rep.warn("Q-Links.json has %d tab entries but TUI.json has %d tabs" % (len(entries), tabs_in_tui))
            bad = 0

            def check_qmap(qmap, where):
                nonlocal bad
                if not isinstance(qmap, dict):
                    rep.error("Q-Links.json: %s is not an object" % where)
                    bad += 1
                    return
                for k, v in qmap.items():
                    if not isinstance(v, int) or isinstance(v, bool) or v < -1 or (nparams is not None and v >= nparams):
                        rep.error("Q-Links.json: %s %s = %s is not -1 or a valid parameter index%s"
                                  % (where, k, json.dumps(v), (" (0..%d)" % (nparams - 1)) if nparams else ""))
                        bad += 1

            for i, e in enumerate(entries):
                if not isinstance(e, dict):
                    rep.error("Q-Links.json: map[%d] is not an object" % i)
                    bad += 1
                    continue
                check_qmap(e.get("Q-Links"), "Tab %s" % e.get("Tab", i + 1))
            if "Program Mode Q-Links" in q:
                check_qmap(q["Program Mode Q-Links"], "Program Mode")
            if not bad:
                rep.ok("Q-Links.json: %d tab map%s, all values in range" % (len(entries), "" if len(entries) == 1 else "s"))


# --------------------------------------------------------------------------------------------
# version.xml
# --------------------------------------------------------------------------------------------
def check_version_xml(rep, folder, has_skins):
    path = os.path.join(folder, "version.xml")
    if not os.path.isfile(path):
        rep.warn("version.xml missing (build.py normally writes it)")
        return
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        rep.error("version.xml does not parse: %s" % e)
        return
    ver = root.find("version")
    if ver is None or not (ver.text or "").strip():
        rep.warn("version.xml has no <version> element")
    ui = root.find("uiversion")
    if ui is not None:
        text = (ui.text or "").strip()
        tup = version_tuple(text, 4)
        if tup is None or tup > UIVERSION_MAX:
            rep.error("version.xml <uiversion> is \"%s\" but the MPC's scripted UI version is 0.1.0.0; write 0.1.0.0, not 1. "
                      "A higher value makes the plugin refuse to load." % text)
        else:
            rep.ok("version.xml uiversion %s" % text)
        if not has_skins:
            rep.warn("version.xml has <uiversion> but there is no Plugin Skins folder")
    elif has_skins:
        rep.warn("version.xml has no <uiversion>; the MPC may ignore the Plugin Skins. build.py writes <uiversion>0.1.0.0</uiversion>.")
    else:
        rep.ok("version.xml parses")


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------
def locate(target):
    """Return (plugin folder, default params.json path or None)."""
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        return None, None
    is_project = os.path.isfile(os.path.join(target, "plugin.json")) and os.path.isdir(os.path.join(target, "src")) \
        and not os.path.isdir(os.path.join(target, "VST"))
    if is_project:
        try:
            with open(os.path.join(target, "plugin.json"), "r", encoding="utf-8") as f:
                pj = json.load(f)
            folder = os.path.join(target, "build", expected_folder_name(str(pj["manufacturer"]), str(pj["name"])))
        except (ValueError, KeyError, OSError):
            return None, None
        return folder, os.path.join(target, "build", "params.json")
    return target, os.path.join(os.path.dirname(target), "params.json")


def validate(target, params_path=None, strict=False):
    rep = Report()
    folder, default_params = locate(target)
    if folder is None:
        print("ERROR  %s is not a plugin folder or a plugin project directory" % target)
        return 1
    if not os.path.isdir(folder):
        print("ERROR  plugin folder not found: %s (run tools/build.py first)" % folder)
        return 1
    print("Validating " + folder)
    pj = check_plugin_json(rep, folder)

    if pj and isinstance(pj.get("manufacturer"), str) and isinstance(pj.get("name"), str):
        want = expected_folder_name(pj["manufacturer"], pj["name"])
        actual = os.path.basename(os.path.normpath(folder))
        if actual != want:
            rep.error('folder is named "%s" but must be "%s" (manufacturer - VST - name); the MPC will not find the skins otherwise'
                      % (actual, want))
        else:
            rep.ok('folder name "%s"' % actual)

    if pj:
        for key, want64 in (("file32", False), ("file64", True)):
            full = resolve_binary(rep, folder, pj, key)
            if full:
                check_binary(rep, full, want64, key)

    nparams = None
    if params_path is None and default_params and os.path.isfile(default_params):
        params_path = default_params
    if params_path:
        if os.path.isfile(params_path):
            params = check_params(rep, params_path, pj)
            if params:
                nparams = len(params["params"])
        else:
            rep.warn("params.json not found at %s; skipping binary/manifest comparison" % params_path)

    skins = os.path.join(folder, "Plugin Skins")
    has_skins = os.path.isdir(skins)
    if has_skins:
        check_skins(rep, skins, nparams)
    else:
        rep.ok("no Plugin Skins folder (the MPC will show its generic parameter UI)")
    check_version_xml(rep, folder, has_skins)

    content = os.path.join(folder, "Content")
    if os.path.isdir(content):
        files = [os.path.join(r, f) for r, _d, fs in os.walk(content) for f in fs]
        total = sum(os.path.getsize(f) for f in files)
        rep.ok("Content/: %d file%s, %.1f MB (loaded by the plugin at run time, see framework/mpc_resources.h)"
               % (len(files), "" if len(files) == 1 else "s", total / 1048576.0))
        if total > 256 * 1048576:
            rep.warn("Content/ is over 256 MB; large sample sets load slowly and use the MPC's limited RAM")

    print("%d error%s, %d warning%s" % (rep.errors, "" if rep.errors == 1 else "s", rep.warnings, "" if rep.warnings == 1 else "s"))
    if rep.errors or (strict and rep.warnings):
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate a built MPC plugin folder (or a plugin project's build output).")
    ap.add_argument("target", help='"<Manufacturer> - VST - <Name>" folder, or a plugin project directory')
    ap.add_argument("--params", help="params.json dumped by host_sim (default: build/params.json next to the folder)")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = ap.parse_args(argv)
    return validate(args.target, args.params, args.strict)


if __name__ == "__main__":
    sys.exit(main())
