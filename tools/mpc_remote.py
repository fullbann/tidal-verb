#!/usr/bin/env python3
"""mpc_remote.py - manage VST2 plugins on an Akai MPC over the network.

Usage:
    mpc_remote.py [ADDRESS] [options] [command ...]

The MPC's address can also be given as --address ADDRESS (or the MPC_ADDRESS environment variable).

    mpc_remote.py 192.168.1.50                       interactive menu
    mpc_remote.py 192.168.1.50 info                  show the device and its drives
    mpc_remote.py 192.168.1.50 list
    mpc_remote.py 192.168.1.50 add plugins/MySynth   an SDK project (uses its build/ output) ...
    mpc_remote.py 192.168.1.50 add Wobble-1.0-mpc.zip              ... or a downloaded zip
    mpc_remote.py 192.168.1.50 update plugins/MySynth              replace an installed plugin
    mpc_remote.py 192.168.1.50 remove "Acme - VST - Wobble"
    mpc_remote.py 192.168.1.50 export --all --out ~/Backups        always written as zip files
    mpc_remote.py 192.168.1.50 doctor                check the MPC is set up correctly
    mpc_remote.py 192.168.1.50 restart               restart the MPC app to rescan VSTs (unsaved data is lost)

Options:
    --password PW   SSH password if the MPC needs one (or set MPC_SSH_PASSWORD; avoids showing it in `ps`)
    --user NAME     SSH user (default root)
    --port N        SSH port (default 22)
    --drive D       which drive to manage: its number, name or mount point (asked if several)
    --yes           do not ask for confirmation (remove / replace)
    --no-color      plain output (also honours NO_COLOR)

The MPC must be on your network with developer mode on (that is what starts its SSH server) and
VST2 Support enabled. Everything this tool writes lives inside the "VST2 Plugins" folder of the drive
you pick (plus one short-lived hidden staging folder at the drive root); it never touches system files.

It uses your computer's `ssh` command and streams tar archives over it, so nothing but OpenSSH is
required on your side and nothing but a standard shell + tar is needed on the MPC.

Standard library only. Python 3.9+.
"""

import argparse
import getpass
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

ILLEGAL_FILENAME_CHARS = '"#@,;:<>*^|?\\/'  # the exact set the MPC strips when it builds the folder name
PLUGINS_DIR = "VST2 Plugins"
INTERNAL_DRIVE_PREFIXES = ("az01-internal", "acvs-synths", "acvs-content", "nvme")
NVME_MOUNT = "/nvme"  # internal NVMe on units that have one; "VST2 Plugins" lives at its root
CPU_PARTS = {  # ARM "CPU part" ids as shown in /proc/cpuinfo
    "0xc07": "Cortex-A7", "0xc0f": "Cortex-A15", "0xc0e": "Cortex-A17", "0xd03": "Cortex-A53",
    "0xd04": "Cortex-A35", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
    "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76", "0xd0d": "Cortex-A77",
}
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
MAX_ZIP_BYTES = 1 << 30
MPC_SERVICE = "acvs"  # the systemd unit that runs the MPC application

EXIT_OK, EXIT_FAIL, EXIT_SETUP = 0, 1, 2
COMMANDS = ("info", "drives", "list", "doctor", "restart", "add", "update", "remove", "export")

# ----------------------------------------------------------------------------------------------
# Terminal styling
# ----------------------------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _enable_vt():
    if os.name == "nt":
        os.system("")  # switches the Windows console into VT mode


class Style:
    """Colour and symbol helper. Degrades to plain ASCII when output is not a capable terminal."""

    def __init__(self, color):
        self.color = color
        enc = (getattr(sys.stdout, "encoding", None) or "").lower()
        self.unicode = "utf" in enc
        u = self.unicode
        self.ok = "✔" if u else "+"
        self.bad = "✘" if u else "x"
        self.warn = "▲" if u else "!"
        self.info = "●" if u else "*"
        self.arrow = "›" if u else ">"
        self.dot = "·" if u else "-"
        self.spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if u else "|/-\\"
        self.box = ("╭", "╮", "╰", "╯", "─", "│") if u else ("+", "+", "+", "+", "-", "|")

    def paint(self, text, *codes):
        if not self.color or not codes:
            return text
        return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)

    def fg(self, n):
        return "38;5;%d" % n

    # semantic colours
    def good(self, t): return self.paint(t, self.fg(78))
    def badc(self, t): return self.paint(t, self.fg(203))
    def warnc(self, t): return self.paint(t, self.fg(221))
    def accent(self, t): return self.paint(t, self.fg(213))
    def cyan(self, t): return self.paint(t, self.fg(80))
    def dim(self, t): return self.paint(t, "2")
    def bold(self, t): return self.paint(t, "1")


S = Style(False)


def visible_len(s):
    return len(ANSI_RE.sub("", s))


def pad(s, width):
    return s + " " * max(0, width - visible_len(s))


def say(text=""):
    print(text)


def banner(subtitle="install and manage VST2 plugins on your MPC over the network"):
    art = [
        "┏┳┓┏━┓┏━╸  ┏━┓┏━╸┏┳┓┏━┓╺┳╸┏━╸",
        "┃┃┃┣━┛┃    ┣┳┛┣╸ ┃┃┃┃ ┃ ┃ ┣╸ ",
        "╹ ╹╹  ┗━╸  ╹┗╸┗━╸╹ ╹┗━┛ ╹ ┗━╸",
    ]
    ramp = [201, 171, 141, 111, 81, 51]
    say()
    for row in art:
        if S.unicode:
            out = "".join(S.paint(ch, S.fg(ramp[min(len(ramp) - 1, i * len(ramp) // len(row))]), "1") if ch != " " else " "
                          for i, ch in enumerate(row))
        else:
            out = ""
        if out:
            say("  " + out)
    if not S.unicode:
        say("  " + S.bold("MPC REMOTE"))
    say("  " + S.dim(subtitle))
    say()


def panel(title, rows, tone=None):
    """Rounded box with a title. rows is a list of strings (may hold ANSI)."""
    tl, tr, bl, br, h, v = S.box
    paint = {"bad": S.badc, "warn": S.warnc, "good": S.good,
             "amber": lambda t: S.paint(t, S.fg(214)), "violet": lambda t: S.paint(t, S.fg(141))}.get(tone, S.cyan)
    width = max([visible_len(r) for r in rows] + [visible_len(title) + 4, 40])
    width = min(width, max(40, shutil.get_terminal_size((100, 24)).columns - 6))
    say(paint(tl + h + " ") + S.bold(title) + " " + paint(h * max(1, width - visible_len(title) - 1) + tr))
    for r in rows:
        say(paint(v) + " " + pad(r, width) + " " + paint(v))
    say(paint(bl + h * (width + 2) + br))


def kv_rows(pairs):
    keyw = max(len(k) for k, _ in pairs) if pairs else 0
    return ["%s  %s" % (S.dim(k.ljust(keyw)), v) for k, v in pairs]


def table(headers, rows):
    widths = [visible_len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], visible_len(c))
    say("  " + "  ".join(S.dim(pad(h, widths[i])) for i, h in enumerate(headers)))
    say("  " + S.dim("  ".join("-" * w for w in widths)))
    for r in rows:
        say("  " + "  ".join(pad(c, widths[i]) for i, c in enumerate(r)))


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


class Step:
    """A progress line: spinner while running, then a tick or cross."""

    def __init__(self, text):
        self.text = text
        self.finished = False
        self._stop = threading.Event()
        self._thread = None
        self.live = sys.stdout.isatty()

    def __enter__(self):
        if self.live:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            say("  %s %s ..." % (S.arrow, self.text))
        return self

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = S.spin[i % len(S.spin)]
            sys.stdout.write("\r\x1b[2K  %s %s" % (S.accent(frame), self.text))
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)

    def _end(self, mark, detail):
        self.finished = True
        self._stop.set()
        if self._thread:
            self._thread.join()
            sys.stdout.write("\r\x1b[2K")
        say("  %s %s%s" % (mark, self.text, ("  " + S.dim(detail)) if detail else ""))

    def done(self, detail=""):
        self._end(S.good(S.ok), detail)

    def warn(self, detail=""):
        self._end(S.warnc(S.warn), detail)

    def fail(self, detail=""):
        self._end(S.badc(S.bad), detail)

    def __exit__(self, et, ev, tb):
        if not self.finished:
            if et:
                self.fail(str(ev) if not isinstance(ev, SystemExit) else "")
            else:
                self.done()
        return False


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


try:
    import readline  # tab completion and line editing; not available on Windows
except ImportError:
    readline = None

BOX_WIDTH = 60


def complete_path(text, state):
    """readline completer: local filenames, with ~ and spaces in names handled."""
    if text == "~":
        matches = ["~/"]
    else:
        typed_dir, _, base = text.rpartition("/")
        typed_dir = typed_dir + "/" if "/" in text else ""
        real_dir = os.path.expanduser(typed_dir) or "."
        try:
            names = sorted(os.listdir(real_dir))
        except OSError:
            names = []
        matches = []
        for n in names:
            if n.startswith(base) and (base.startswith(".") or not n.startswith(".")):
                matches.append(typed_dir + n + ("/" if os.path.isdir(os.path.join(real_dir, n)) else ""))
    return matches[state] if state < len(matches) else None


def _readline_safe(prompt):
    """Wrap colour codes so readline measures the prompt width correctly."""
    if readline is None:
        return prompt
    return re.sub(r"(\x1b\[[0-9;]*m)", "\x01\\1\x02", prompt)


def ask(prompt, default=None, secret=False, path=False):
    """A titled input box. Falls back to a plain prompt when not on a terminal."""
    tl, tr, bl, br, h, v = S.box
    suffix = "  (Enter for %s)" % default if default else ""
    if interactive():
        title = prompt
        width = max(BOX_WIDTH, visible_len(title + suffix) + 6)
        width = min(width, max(40, shutil.get_terminal_size((100, 24)).columns - 4))
        say(S.accent(tl + h + " ") + S.bold(title) + S.dim(suffix) + " " + S.accent(h * max(2, width - visible_len(title + suffix) - 3)))
        text = S.accent(v) + " " + S.accent(S.arrow) + " "
    else:
        width = 0
        text = "%s%s: " % (prompt, suffix)
    old_completer = None
    if path and readline is not None and interactive():
        old_completer = readline.get_completer()
        readline.set_completer(complete_path)
        readline.set_completer_delims("\n")  # a path may contain spaces
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    try:
        if secret:
            val = getpass.getpass(ANSI_RE.sub("", text))
        else:
            val = input(_readline_safe(text))
    except (EOFError, KeyboardInterrupt):
        say()
        raise SystemExit(EXIT_FAIL)
    finally:
        if path and readline is not None and interactive():
            readline.set_completer(old_completer)
    if width:
        say(S.accent(bl + h * (width - 1)))
    val = val.strip()
    return val if val or default is None else default


def parse_paths(val):
    """One path (spaces allowed, quotes or backslashes optional) or several quoted ones."""
    v = val.strip()
    if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    v = v.replace("\\ ", " ")
    if os.path.exists(os.path.expanduser(v)):
        return [v]
    try:
        return shlex.split(val)
    except ValueError:
        return [v]


def confirm(prompt, default=False, assume_yes=False):
    if assume_yes:
        return True
    if not interactive():
        return False
    val = ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    if not val:
        return default
    return val.startswith("y")


def choose(title, items, allow_back=False, back_label="back"):
    """items: list of (label, detail), shown in a box. Returns the chosen index or None for back."""
    rows = []
    numw = len(str(len(items)))
    labelw = max([visible_len(label) for label, _ in items] + [0])
    for i, (label, detail) in enumerate(items, 1):
        rows.append("%s  %s%s" % (S.accent(str(i).rjust(numw)), pad(label, labelw) if detail else label,
                                  ("  " + S.dim(detail)) if detail else ""))
    if allow_back:
        rows.append("%s  %s" % (S.accent("0".rjust(numw)), S.dim(back_label)))
    panel(title, rows)
    while True:
        val = ask("Choose a number")
        if allow_back and val in ("0", "q", ""):
            return None
        if val.isdigit() and 1 <= int(val) <= len(items):
            return int(val) - 1
        say("  " + S.warnc("Enter a number from the list."))


def fail_panel(title, lines, tone="bad"):
    say()
    panel(title, lines, tone)
    say()


# ----------------------------------------------------------------------------------------------
# Setup help (shared by diagnostics)
# ----------------------------------------------------------------------------------------------

def setup_steps():
    return [
        S.bold("1. Update the MPC") + " to MPC OS 3.10.1 or later (Preferences → Info shows the version).",
        S.bold("2. Enable the beta features:") + " Preferences → Enhancements (Beta) →",
        "     turn on " + S.cyan("Enable Enhancements") + " and then " + S.cyan("VST2 Support") + " (read the warning).",
        S.bold("3. Switch on developer mode:") + " Preferences → Info. Tap the version value 3 times,",
        "     then the serial number value 2 times, then the firmware version once.",
        "     Developer mode starts the MPC's SSH server.",
        S.bold("4. Join the network:") + " the MPC and this computer must be on the same Wi-Fi / Ethernet.",
        "     Find the MPC's IP address in Preferences: the Wi-Fi tab (hold Shift to reach it) or the Ethernet tab (or your router's device list).",
    ]


class SshError(Exception):
    def __init__(self, kind, detail=""):
        Exception.__init__(self, detail)
        self.kind = kind  # noclient | dns | unreachable | refused | auth | closed | timeout | other
        self.detail = detail


def explain(err, host, port, has_password):
    k = err.kind
    if k == "noclient":
        fail_panel("No ssh command found", [
            "This tool uses your computer's OpenSSH client.",
            "macOS / Linux: it is normally installed already. Windows 10/11: Settings → Apps → Optional features → OpenSSH Client.",
        ])
    elif k == "dns":
        fail_panel("Can't find '%s'" % host, [
            "That name does not resolve. Use the MPC's IP address instead (for example 192.168.1.50).",
            "Find it on the MPC under Preferences → Network.",
        ])
    elif k == "unreachable":
        fail_panel("Can't reach %s" % host, [
            "Nothing answered at that address (timed out).",
            "",
            "• Is the MPC switched on and on the same network as this computer?",
            "• Is the IP address right? Check Preferences on the MPC: Wi-Fi tab (hold Shift) or Ethernet tab.",
            "• Guest Wi-Fi and some routers stop devices talking to each other (client isolation).",
        ])
    elif k == "refused":
        fail_panel("The MPC is there, but its SSH server is off", [
            "%s:%d refused the connection. That almost always means developer mode is not on," % (host, port),
            "which is what starts the MPC's SSH server. To set the MPC up:",
            "",
        ] + setup_steps(), "warn")
    elif k == "auth":
        lines = [
            "The MPC is there and SSH is on, but it did not accept the login.",
            "",
        ]
        if has_password:
            lines.append("• The password was refused. Check it, or try an empty password:  --password \"\"")
        else:
            lines.append("• It wants a password. Run again with  --password YOURPASSWORD  (or set MPC_SSH_PASSWORD).")
        lines.append("• To log in as another user: --user NAME")
        fail_panel("Login refused", lines, "warn")
    elif k == "closed":
        fail_panel("The connection was closed straight away", [
            "The SSH server may still be starting. Wait ten seconds and try again.",
            "If this keeps happening, turn developer mode off and on again on the MPC.",
        ], "warn")
    elif k == "timeout":
        fail_panel("The MPC stopped responding", ["The connection timed out. Check the network and try again."])
    else:
        fail_panel("SSH problem", [err.detail or "unknown error"])


# ----------------------------------------------------------------------------------------------
# SSH transport
# ----------------------------------------------------------------------------------------------

class Remote:
    def __init__(self, host, user="root", port=22, password=None):
        self.host = host
        self.user = user
        self.port = port
        self.password = password
        self.ssh = os.environ.get("MPC_SSH_BIN") or shutil.which("ssh")
        self._tmp = None
        self._askpass = None
        self._control = None

    # -- setup ------------------------------------------------------------------------------
    def _ensure_tmp(self):
        if self._tmp is None:
            base = "/tmp" if os.name != "nt" and os.path.isdir("/tmp") else None  # keeps the control socket path short
            self._tmp = tempfile.mkdtemp(prefix="mpcr-", dir=base)
            os.chmod(self._tmp, 0o700)
        return self._tmp

    def _askpass_script(self):
        if self._askpass is None:
            d = self._ensure_tmp()
            if os.name == "nt":
                path = os.path.join(d, "askpass.bat")
                with open(path, "w") as f:
                    f.write("@echo off\r\necho.%MPC_ASKPASS_PW%\r\n")
            else:
                path = os.path.join(d, "askpass.sh")
                with open(path, "w") as f:
                    f.write("#!/bin/sh\nprintf '%s\\n' \"$MPC_ASKPASS_PW\"\n")
                os.chmod(path, 0o700)
            self._askpass = path
        return self._askpass

    def _command(self, remote_cmd):
        if not self.ssh:
            raise SshError("noclient")
        null = "NUL" if os.name == "nt" else "/dev/null"
        cmd = [self.ssh, "-p", str(self.port),
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + null,
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
               "-o", "NumberOfPasswordPrompts=1"]
        if self.password is None:
            cmd += ["-o", "BatchMode=yes"]
        if os.name != "nt" and not os.environ.get("MPC_SSH_BIN"):
            if self._control is None:
                self._control = os.path.join(self._ensure_tmp(), "cm")
            cmd += ["-o", "ControlMaster=auto", "-o", "ControlPath=" + self._control, "-o", "ControlPersist=120"]
        cmd += ["%s@%s" % (self.user, self.host), remote_cmd]
        return cmd

    def _env(self):
        env = dict(os.environ)
        if self.password is not None:
            env["MPC_ASKPASS_PW"] = self.password
            env["SSH_ASKPASS"] = self._askpass_script()
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
        return env

    # -- running ----------------------------------------------------------------------------
    def run(self, script, stdin=None, timeout=60):
        """Run a shell script remotely. Returns (returncode, stdout bytes, stderr str)."""
        cmd = self._command("sh -c " + shlex.quote(script))
        try:
            p = subprocess.run(cmd, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=self._env(),
                               start_new_session=(os.name != "nt" and self.password is not None))
        except FileNotFoundError:
            raise SshError("noclient")
        except subprocess.TimeoutExpired:
            raise SshError("timeout")
        err = p.stderr.decode("utf-8", "replace").strip()
        if p.returncode == 255:
            raise SshError(classify_ssh_error(err), err)
        return p.returncode, p.stdout, err

    def text(self, script, timeout=60):
        rc, out, err = self.run(script, timeout=timeout)
        return rc, out.decode("utf-8", "replace"), err

    def close(self):
        if self._control and self.ssh and os.path.exists(self._control):
            try:
                subprocess.run([self.ssh, "-S", self._control, "-O", "exit", "x"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
            except Exception:
                pass
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)


def classify_ssh_error(err):
    e = err.lower()
    if "permission denied" in e or "authentication failed" in e or "too many authentication" in e:
        return "auth"
    if "refused" in e:
        return "refused"
    if "could not resolve" in e or "name or service not known" in e or "nodename nor servname" in e:
        return "dns"
    if "timed out" in e or "no route" in e or "unreachable" in e or "host is down" in e:
        return "unreachable"
    if "closed by remote" in e or "connection reset" in e or "kex_exchange" in e or "banner" in e:
        return "closed"
    return "other"


def probe_port(host, port):
    """Cheap TCP check first so we can give a precise diagnosis. Raises SshError."""
    try:
        with socket.create_connection((host, port), timeout=5):
            return
    except socket.gaierror:
        raise SshError("dns")
    except ConnectionRefusedError:
        raise SshError("refused")
    except socket.timeout:
        raise SshError("unreachable")
    except OSError as e:
        raise SshError("unreachable", str(e))


def connect(remote):
    """Open the connection (with a password prompt if needed). Raises SshError on failure."""
    with Step("Reaching %s:%d" % (remote.host, remote.port)) as st:
        if os.environ.get("MPC_SSH_BIN") is None:
            probe_port(remote.host, remote.port)
        st.done("SSH server is up")
    with Step("Logging in as %s" % remote.user) as st:
        attempts = 0
        while True:
            try:
                rc, out, err = remote.text("echo mpc-remote-ok")
                if "mpc-remote-ok" in out:
                    st.done("ok" if remote.password is None else "password accepted")
                    return
                raise SshError("other", err or "unexpected reply from the MPC")
            except SshError as e:
                if e.kind == "auth" and remote.password is None and interactive() and attempts < 2:
                    attempts += 1
                    st._stop.set()
                    if st._thread:
                        st._thread.join()
                        sys.stdout.write("\r\x1b[2K")
                    remote.password = ask("SSH password for %s@%s (Enter for none)" % (remote.user, remote.host), secret=True)
                    st = Step("Logging in as %s" % remote.user).__enter__()
                    continue
                st.fail(e.kind)
                raise


# ----------------------------------------------------------------------------------------------
# Device info and drives
# ----------------------------------------------------------------------------------------------

INFO_SCRIPT = r"""
B=/sys/firmware/devicetree/base
for f in model inmusic,product-code serial-number compatible; do
  printf 'dt.%s=' "$f"
  if [ -r "$B/$f" ]; then tr '\000' ',' < "$B/$f"; fi
  echo
done
printf 'app=%s\n' "$(head -n 1 /tmp/com.akaipro.mpc.version 2>/dev/null)"
printf 'uname=%s\n' "$(uname -srm 2>/dev/null)"
printf 'arch=%s\n' "$(uname -m 2>/dev/null)"
printf 'hostname=%s\n' "$(hostname 2>/dev/null)"
printf 'uptime=%s\n' "$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
if [ -r /etc/os-release ]; then sed 's/^/os./' /etc/os-release; fi
if [ -e /data/dev-mode ]; then echo devmode.file=yes; fi
for f in /etc/az0x-release /etc/az01-release; do
  if [ -r "$f" ]; then sed -n 's/^\(AZ0[X1]_VERSION\)=/firmware=/p' "$f" | head -n 1; fi
done
printf 'cpu.count=%s\n' "$(grep -c '^processor' /proc/cpuinfo 2>/dev/null)"
sed -n 's/^model name[[:space:]]*: */cpu.model=/p;s/^Hardware[[:space:]]*: */cpu.hardware=/p;s/^CPU part[[:space:]]*: */cpu.part=/p' /proc/cpuinfo 2>/dev/null | sort -u
printf 'cpu.maxkhz=%s\n' "$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null)"
"""


def get_info(remote):
    rc, out, err = remote.text(INFO_SCRIPT)
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in info:  # first value wins (cpu.part repeats per core type)
                info[k] = v.strip().strip(",").strip().strip('"').strip("'")
    return info


def cpu_summary(info):
    """e.g. 'Cortex-A55 x4 @ 1.8 GHz'. Falls back to whatever the kernel reports."""
    name = CPU_PARTS.get((info.get("cpu.part") or "").lower()) or info.get("cpu.model") or info.get("cpu.hardware") or ""
    parts = []
    if name:
        parts.append(name + (" ×%s" % info["cpu.count"] if info.get("cpu.count", "0") not in ("", "0", "1") and S.unicode else
                             (" x%s" % info["cpu.count"] if info.get("cpu.count", "0") not in ("", "0", "1") else "")))
    try:
        parts.append("@ %.1f GHz" % (int(info["cpu.maxkhz"]) / 1e6))
    except (KeyError, ValueError):
        pass
    arch = info.get("arch") or ""
    text = " ".join(parts) or arch
    if arch and arch not in text:
        text += " (%s)" % arch
    return text


def info_rows(host, info):
    model = info.get("dt.model") or ""
    code = info.get("dt.inmusic,product-code") or ""
    pairs = [("Address", host)]
    pairs.append(("Model", S.bold(model) if model else S.dim("(not reported)")))
    if code:
        pairs.append(("Hardware code", code))
    pairs.append(("Serial number", info.get("dt.serial-number") or S.dim("(not reported)")))
    if info.get("hostname"):
        pairs.append(("Hostname", info["hostname"]))
    pairs.append(("MPC version", S.bold(info["app"]) if info.get("app") else S.dim("(not reported; is the MPC app running?)")))
    if info.get("firmware"):
        pairs.append(("Firmware version", S.bold(info["firmware"])))
    else:
        pairs.append(("Firmware version", S.dim("(not reported)")))
    cpu = cpu_summary(info)
    if cpu:
        pairs.append(("CPU", cpu))
    arch = device_arch(info)
    if arch:
        pairs.append(("Architecture", "64-bit" if arch == "arm64" else "32-bit"))
    system = info.get("os.PRETTY_NAME") or info.get("os.NAME") or ""
    if info.get("os.VERSION_ID") and info["os.VERSION_ID"] not in system:
        system = (system + " " + info["os.VERSION_ID"]).strip()
    if system:
        pairs.append(("System", system))
    if info.get("uname"):
        pairs.append(("Kernel", info["uname"]))
    if info.get("uptime"):
        try:
            secs = int(float(info["uptime"]))
            pairs.append(("Uptime", "%dh %02dm" % (secs // 3600, secs % 3600 // 60)))
        except ValueError:
            pass
    return kv_rows(pairs)


def device_arch(info):
    a = (info.get("arch") or "").lower()
    if a in ("aarch64", "arm64"):
        return "arm64"
    if a.startswith("arm"):
        return "arm32"
    return None


class Drive:
    def __init__(self, device, mount, fstype, options):
        self.device = device
        self.mount = mount
        self.fstype = fstype
        self.options = options.split(",")
        self.name = os.path.basename(mount.rstrip("/"))
        self.total = None
        self.free = None

    @property
    def internal(self):
        return self.name.startswith(INTERNAL_DRIVE_PREFIXES)

    @property
    def recommended(self):
        """The internal user-content storage: the best place for plugins (survives removing cards)."""
        return self.name.startswith("az01-internal-sd") or self.mount == NVME_MOUNT

    def mark(self):
        return S.good("●" if S.unicode else "*") if self.recommended else " "

    @property
    def writable(self):
        return "rw" in self.options

    @property
    def noexec(self):
        return "noexec" in self.options

    @property
    def plugins_path(self):
        return self.mount.rstrip("/") + "/" + PLUGINS_DIR

    def describe(self):
        bits = ["internal" if self.internal else "removable", self.fstype]
        if self.free is not None:
            bits.append("%s free of %s" % (human_size(self.free), human_size(self.total)))
        if not self.writable:
            bits.append(S.badc("read-only"))
        if self.noexec:
            bits.append(S.warnc("noexec"))
        return S.dim(" · " if S.unicode else " - ").join(b if "\x1b" in b else S.dim(b) for b in bits)


def unescape_mount(path):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def discover_drives(remote):
    rc, out, err = remote.text("cat /proc/mounts; echo '@@DF'; df -Pk 2>/dev/null")
    mounts, df = out.split("@@DF", 1) if "@@DF" in out else (out, "")
    drives = []
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mount = unescape_mount(parts[1])
        if parts[2] in ("tmpfs", "proc", "sysfs", "devtmpfs", "devpts"):
            continue
        if mount == NVME_MOUNT or mount.startswith("/media/"):
            drives.append(Drive(parts[0], mount, parts[2], parts[3]))
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = " ".join(parts[5:])
        for d in drives:
            if d.mount == mount:
                try:
                    d.total, d.free = int(parts[1]) * 1024, int(parts[3]) * 1024
                except ValueError:
                    pass
    drives.sort(key=lambda d: (not d.internal, d.mount))
    return drives


def show_drives(drives):
    numw = len(str(len(drives)))
    namew = max(visible_len(d.name) for d in drives)
    rows = ["%s  %s %s  %s" % (S.accent(str(i).rjust(numw)), d.mark(), S.bold(pad(d.name, namew)), d.describe())
            for i, d in enumerate(drives, 1)]
    if any(d.recommended for d in drives):
        rows.append(S.good("●" if S.unicode else "*") + " " + S.dim("recommended: internal storage"))
    panel("Drives found", rows, "amber")


def pick_drive(drives, wanted, assume_yes):
    if not drives:
        return None
    if wanted:
        w = wanted.lower()
        for i, d in enumerate(drives, 1):
            if w == str(i) or w == d.mount.lower() or w == d.name.lower():
                return d
        matches = [d for d in drives if w in d.name.lower() or w in d.mount.lower()]
        if len(matches) == 1:
            return matches[0]
        raise UsageError("No single drive matches '%s'. Drives: %s" % (wanted, ", ".join("%d=%s" % (i, d.name) for i, d in enumerate(drives, 1))))
    if len(drives) == 1:
        return drives[0]
    if not interactive():
        raise UsageError("Several drives found; choose one with --drive (%s)" % ", ".join("%d=%s" % (i, d.name) for i, d in enumerate(drives, 1)))
    say()
    idx = choose("Which drive do you want to manage?", [(d.mark() + " " + d.name, d.describe()) for d in drives])
    return drives[idx]


class UsageError(Exception):
    pass


# ----------------------------------------------------------------------------------------------
# Plugins on the MPC
# ----------------------------------------------------------------------------------------------

LIST_SCRIPT = r"""
cd %(dir)s 2>/dev/null || { echo '@@MISSING'; exit 0; }
for d in */; do
  [ -d "$d" ] || continue
  d=${d%%/}
  echo "@@PLUGIN $d"
  cat "$d/plugin.json" 2>/dev/null
  echo
  echo "@@SIZE $(du -sk "$d" 2>/dev/null | cut -f1)"
done
echo '@@END'
"""


class InstalledPlugin:
    def __init__(self, folder, manifest, size_kb):
        self.folder = folder
        self.manifest = manifest or {}
        self.size = (size_kb or 0) * 1024

    @property
    def name(self):
        return self.manifest.get("name") or self.folder

    def label(self):
        return self.folder


def list_plugins(remote, drive):
    """Returns (exists, [InstalledPlugin])."""
    rc, out, err = remote.text(LIST_SCRIPT % {"dir": shlex.quote(drive.plugins_path)})
    if "@@MISSING" in out:
        return False, []
    plugins = []
    cur = None
    body = []
    size = 0
    for line in out.splitlines():
        if line.startswith("@@PLUGIN "):
            cur, body, size = line[9:], [], 0
        elif line.startswith("@@SIZE ") and cur is not None:
            try:
                size = int(line[7:].strip() or 0)
            except ValueError:
                size = 0
            manifest = None
            try:
                manifest = json.loads("\n".join(body))
            except ValueError:
                pass
            plugins.append(InstalledPlugin(cur, manifest, size))
            cur = None
        elif cur is not None:
            body.append(line)
    return True, plugins


def show_plugins(plugins, drive, exists):
    if not exists:
        say("  %s %s" % (S.warnc(S.warn), "There is no \"%s\" folder on %s yet (it is created when you add a plugin)." % (PLUGINS_DIR, drive.name)))
        return
    if not plugins:
        say("  %s %s" % (S.dim(S.info), "No plugins installed on %s." % drive.name))
        return
    rows = []
    for p in plugins:
        m = p.manifest
        if not m:
            rows.append([S.warnc(p.folder), "", "", "", S.warnc("no valid plugin.json"), human_size(p.size)])
            continue
        archs = []
        if m.get("file64"):
            archs.append("arm64")
        if m.get("file32"):
            archs.append("arm32")
        rows.append([S.bold(p.folder), m.get("manufacturer", ""), str(m.get("version", "")),
                     m.get("kind", ""), "/".join(archs), human_size(p.size)])
    table(["Folder", "Manufacturer", "Ver", "Kind", "Builds", "Size"], rows)


def find_installed(plugins, query):
    q = query.lower()
    exact = [p for p in plugins if q in (p.folder.lower(), p.name.lower())]
    if len(exact) == 1:
        return exact[0]
    part = [p for p in plugins if q in p.folder.lower() or q in p.name.lower()]
    if len(part) == 1:
        return part[0]
    if len(part) > 1:
        raise UsageError("'%s' matches several plugins: %s" % (query, ", ".join(p.folder for p in part)))
    raise UsageError("No installed plugin matches '%s'." % query)


def safe_folder_name(name):
    return bool(name) and name not in (".", "..") and "/" not in name and "\x00" not in name and not name.startswith("-")


# ----------------------------------------------------------------------------------------------
# Local plugin sources (zip files and SDK project folders)
# ----------------------------------------------------------------------------------------------

def legal_filename(s):
    return "".join(c for c in s if c not in ILLEGAL_FILENAME_CHARS)


class LocalPlugin:
    def __init__(self, path, manifest):
        self.path = path
        self.manifest = manifest
        self.folder = legal_filename("%s - VST - %s" % (manifest["manufacturer"], manifest["name"]))
        self.warnings = []
        self.errors = []
        self.builds = {}

    def check(self):
        m = self.manifest
        for key in ("name", "manufacturer", "uid"):
            if not m.get(key):
                self.errors.append("plugin.json has no \"%s\"" % key)
        for arch, key, machine in (("arm64", "file64", 0xB7), ("arm32", "file32", 0x28)):
            rel = m.get(key)
            if not rel:
                continue
            full = os.path.normpath(os.path.join(self.path, rel))
            if not full.startswith(os.path.normpath(self.path)) or not os.path.isfile(full):
                self.errors.append("%s is listed as %s but missing" % (rel, key))
                continue
            with open(full, "rb") as f:
                head = f.read(20)
            if head[:4] != b"\x7fELF":
                self.errors.append("%s is not an ELF binary" % rel)
            elif struct.unpack_from("<H", head, 18)[0] != machine:
                self.errors.append("%s is not a %s binary" % (rel, arch))
            else:
                self.builds[arch] = rel
        if not self.builds and not self.errors:
            self.errors.append("no ARM binary listed in plugin.json (file32 / file64)")
        if not os.path.isfile(os.path.join(self.path, "version.xml")):
            self.warnings.append("no version.xml")
        if os.path.basename(self.path.rstrip("/")) != self.folder and not self.path.startswith(tempfile.gettempdir()):
            self.warnings.append("folder will be installed as \"%s\"" % self.folder)


def read_manifest(folder):
    try:
        with open(os.path.join(folder, "plugin.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def scan_for_plugins(root):
    """Find plugin folders in an unzipped tree or a project directory."""
    root = os.path.abspath(root)
    if read_manifest(root):
        # An SDK project also has a plugin.json at its root; the deployable folder is under build/.
        built = [] if os.path.isdir(os.path.join(root, "VST")) else built_folders(os.path.join(root, "build"))
        return built or [root]
    found = []
    build = os.path.join(root, "build")
    for base in ([build] if os.path.isdir(build) else []) + [root]:
        for dirpath, dirnames, _ in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__MACOSX" and d not in ("native", "node_modules"))
            depth = dirpath[len(base):].count(os.sep)
            if dirpath != base and read_manifest(dirpath):
                found.append(dirpath)
                dirnames[:] = []
            elif depth >= 2:
                dirnames[:] = []
        if found:
            break
    return found


def built_folders(directory):
    """Child folders of `directory` that are complete plugin folders (plugin.json plus a VST/ folder)."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names
            if os.path.isdir(os.path.join(directory, n, "VST")) and read_manifest(os.path.join(directory, n))]


def plugin_zip_manifest(path):
    """The plugin.json inside a zip (at the top or one folder down), or None if it is not a plugin zip."""
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                parts = name.split("/")
                if parts[-1] == "plugin.json" and len(parts) <= 2 and "__MACOSX" not in parts:
                    data = json.loads(z.read(name).decode("utf-8"))
                    return data if isinstance(data, dict) and data.get("name") else None
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        pass
    return None


def zips_in(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names
            if n.lower().endswith(".zip") and os.path.isfile(os.path.join(directory, n))]


class Candidate:
    def __init__(self, path, kind, manifest):
        self.path = path
        self.kind = kind  # "folder" | "zip"
        self.manifest = manifest
        self.mtime = os.path.getmtime(path)

    def glyph(self):
        """Dark blue folder-like box for a folder, amber stacked-squares for a zip."""
        if self.kind == "folder":
            return S.paint("▤" if S.unicode else "D", S.fg(27))
        return S.paint("⧉" if S.unicode else "Z", S.fg(214))

    def label(self):
        m = self.manifest
        return "%s%s" % (m.get("name", os.path.basename(self.path)), (" " + str(m["version"])) if m.get("version") else "")

    def detail(self, base):
        shown_path = os.path.dirname(self.path) + os.sep if self.kind == "folder" else self.path  # the folder name repeats the plugin name
        rel = os.path.relpath(shown_path, base) + (os.sep if self.kind == "folder" else "")
        shown = shown_path if rel.startswith("..") else rel
        age = time.time() - self.mtime
        when = ("%d min ago" % max(1, age // 60)) if age < 3600 else ("%d h ago" % (age // 3600)) if age < 86400 else ("%d days ago" % (age // 86400))
        return "%s  (%s, %s)" % (shown, self.kind, when)


def find_candidates(cwd=None, extra_roots=()):
    """Look for deployable plugins: built folders and plugin zips in the current folder and in the
    build/ and dist/ folders of projects under ./plugins and ./examples (also those next to this script)."""
    cwd = os.path.abspath(cwd or os.getcwd())
    sdk = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spots = [cwd, os.path.join(cwd, "build"), os.path.join(cwd, "dist")]
    for base in dict.fromkeys([cwd, sdk] + list(extra_roots)):
        for group in ("plugins", "examples"):
            gdir = os.path.join(base, group)
            try:
                projects = sorted(os.listdir(gdir))
            except OSError:
                continue
            for proj in projects:
                spots += [os.path.join(gdir, proj, "build"), os.path.join(gdir, proj, "dist")]
    found = {}
    for spot in spots:
        if not os.path.isdir(spot):
            continue
        for f in built_folders(spot):
            m = read_manifest(f)
            if m and m.get("name") and m.get("manufacturer"):
                found[os.path.realpath(f)] = Candidate(f, "folder", m)
        for z in zips_in(spot):
            m = plugin_zip_manifest(z)
            if m:
                found[os.path.realpath(z)] = Candidate(z, "zip", m)
    return sorted(found.values(), key=lambda c: -c.mtime)[:15]


def extract_zip(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_ZIP_BYTES:
            raise UsageError("%s expands to %s, which is too big for a plugin" % (zpath, human_size(total)))
        base = os.path.realpath(dest)
        for info in z.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if target != base and not target.startswith(base + os.sep):
                raise UsageError("%s contains an unsafe path (%s); refusing to unpack it" % (zpath, info.filename))
        z.extractall(dest)
        for info in z.infolist():  # keep the executable bit on binaries
            if info.external_attr >> 16 & 0o100:
                try:
                    os.chmod(os.path.join(dest, info.filename), 0o755)
                except OSError:
                    pass


def load_sources(source, workdir):
    """Resolve a zip file or folder into a list of LocalPlugin."""
    source = os.path.expanduser(source)
    if not os.path.exists(source):
        raise UsageError("%s does not exist" % source)
    if os.path.isfile(source):
        if not zipfile.is_zipfile(source):
            raise UsageError("%s is not a zip file" % source)
        dest = tempfile.mkdtemp(prefix="src-", dir=workdir)
        extract_zip(source, dest)
        folders = scan_for_plugins(dest)
    else:
        folders = scan_for_plugins(source)
    plugins = []
    for f in folders:
        m = read_manifest(f)
        if not m or not m.get("name") or not m.get("manufacturer"):
            continue
        lp = LocalPlugin(f, m)
        lp.check()
        plugins.append(lp)
    if not plugins:
        hint = ""
        if os.path.isdir(source) and os.path.isdir(os.path.join(source, "src")):
            hint = " This looks like an unbuilt SDK project: run  python tools/build.py %s  first." % source
        raise UsageError("No plugin found in %s (looking for a folder with plugin.json).%s" % (source, hint))
    return plugins


def build_tar(lp):
    """Tar the plugin folder with normalised owners and modes. Returns (bytes, {relpath: size})."""
    buf = io.BytesIO()
    sizes = {}
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        def add(path, arc):
            ti = tf.gettarinfo(path, arc)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            if ti.isdir():
                ti.mode = 0o755
                tf.addfile(ti)
            elif ti.isreg():
                with open(path, "rb") as fh:
                    head = fh.read(4)
                ti.mode = 0o755 if head == b"\x7fELF" else 0o644
                with open(path, "rb") as fh:
                    tf.addfile(ti, fh)
                sizes[arc[len(lp.folder) + 1:]] = ti.size

        add(lp.path, lp.folder)
        for dirpath, dirnames, filenames in os.walk(lp.path):
            dirnames[:] = sorted(d for d in dirnames if d != "__MACOSX" and not d.startswith("._"))
            for name in sorted(dirnames) + sorted(filenames):
                if name in JUNK_NAMES or name.startswith("._"):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, lp.path).replace(os.sep, "/")
                add(full, lp.folder + "/" + rel)
    return buf.getvalue(), sizes


# ----------------------------------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------------------------------

class Context:
    def __init__(self, remote, info, drive, assume_yes, workdir):
        self.remote = remote
        self.info = info
        self.drive = drive
        self.assume_yes = assume_yes
        self.workdir = workdir
        self.arch = device_arch(info)


def require_writable(ctx):
    d = ctx.drive
    if not d.writable:
        fail_panel("%s is read-only" % d.name, [
            "The MPC has mounted this drive read-only, so nothing can be copied to it.",
            "Choose another drive (--drive), or check the card's write-protect switch.",
        ])
        return False
    return True


def action_list(ctx):
    with Step("Reading %s/%s" % (ctx.drive.name, PLUGINS_DIR)) as st:
        exists, plugins = list_plugins(ctx.remote, ctx.drive)
        st.done("%d plugin%s" % (len(plugins), "" if len(plugins) == 1 else "s"))
    say()
    show_plugins(plugins, ctx.drive, exists)
    say()
    return plugins


def drive_free(remote, drive):
    """Bytes free on a drive right now (asked again, as the figure from discovery goes stale). None if unknown."""
    rc, out, err = remote.text("df -Pk %s | tail -n 1" % shlex.quote(drive.mount))
    try:
        drive.free = int(out.split()[3]) * 1024
    except (IndexError, ValueError):
        pass
    return drive.free


def install(ctx, lp, replace):
    """Copy one plugin to the MPC. Returns True on success."""
    d = ctx.drive
    vst = d.plugins_path
    name = lp.folder
    if not safe_folder_name(name):
        say("  %s %s" % (S.badc(S.bad), "Unsafe plugin folder name: %r" % name))
        return False
    for w in lp.warnings:
        say("  %s %s" % (S.warnc(S.warn), w))
    if lp.errors:
        for e in lp.errors:
            say("  %s %s" % (S.badc(S.bad), e))
        return False
    if ctx.arch and ctx.arch not in lp.builds:
        say("  %s %s" % (S.badc(S.bad), "This MPC needs an %s build and %s only contains %s." % (
            ctx.arch, lp.folder, "/".join(sorted(lp.builds)) or "nothing usable")))
        return False
    data, sizes = build_tar(lp)
    # The plugin is streamed as a tar and unpacked straight into a staging folder on the drive, so no zip
    # is ever copied over. While it is staged any old copy still occupies its space, so the whole new
    # plugin (plus filesystem block rounding) must fit in what is free right now.
    need = len(data) + 4096 * (len(sizes) + 1) + (1 << 20)
    with Step("Checking space on %s" % d.name) as st:
        free = drive_free(ctx.remote, d)
        if free is None:
            st.warn("could not tell; carrying on")
        elif free < need:
            st.fail("%s free, needs %s" % (human_size(free), human_size(need)))
            say("  %s %s" % (S.badc(S.bad), "Not enough space on %s to unpack %s. Free some space or choose another drive (--drive)." % (d.name, name)))
            return False
        else:
            st.done("%s free, needs about %s" % (human_size(free), human_size(need)))

    exists, plugins = list_plugins(ctx.remote, d)
    present = any(p.folder == name for p in plugins)
    if present and not replace:
        if not confirm("%s is already installed on %s. Replace it?" % (name, d.name), False, ctx.assume_yes):
            say("  %s %s" % (S.warnc(S.warn), "Left %s alone (use the update command to replace it)." % name))
            return False

    stage = d.mount.rstrip("/") + "/.mpc-remote-stage"
    q = shlex.quote
    script = (
        "set -e\n"
        "V=%(vst)s; N=%(name)s; T=%(stage)s\n"
        "rm -rf \"$T\"; mkdir -p \"$V\" \"$T\"\n"
        "tar -xof - -C \"$T\"\n"
        "[ -f \"$T/$N/plugin.json\" ] || { rm -rf \"$T\"; echo 'upload incomplete' >&2; exit 3; }\n"
        "if [ -e \"$V/$N\" ]; then mv \"$V/$N\" \"$T/.old\"; fi\n"
        "mv \"$T/$N\" \"$V/$N\"\n"
    ) % {"vst": q(vst), "name": q(name), "stage": q(stage)}
    with Step("Copying %s %s" % (S.bold(name), S.dim("(%s)" % human_size(len(data))))) as st:
        rc, out, err = ctx.remote.run(script, stdin=data, timeout=900)
        if rc != 0:
            ctx.remote.text("rm -rf %s" % q(stage))
            st.fail(err.splitlines()[-1] if err else "copy failed (exit %d)" % rc)
            return False
        st.done()
    with Step("Verifying files") as st:
        rc, out, err = ctx.remote.text("cd %s && find . -type f -exec wc -c {} \\;" % q(vst + "/" + name))
        remote_sizes = {}
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+\./(.*)$", line)
            if m:
                remote_sizes[m.group(2)] = int(m.group(1))
        bad = [k for k, v in sizes.items() if remote_sizes.get(k) != v]
        if bad:
            ctx.remote.text("set -e; rm -rf %(v)s/%(n)s; if [ -e %(t)s/.old ]; then mv %(t)s/.old %(v)s/%(n)s; fi; rm -rf %(t)s" % {
                "v": q(vst), "n": q(name), "t": q(stage)})
            st.fail("%s did not arrive intact; previous version restored" % bad[0])
            return False
        ctx.remote.text("rm -rf %s; sync" % q(stage))
        st.done("%d files match" % len(sizes))
    return True


def post_install_notes(ctx):
    notes = []
    if ctx.drive.noexec:
        notes.append(S.warnc(S.warn) + " " + ctx.drive.name + " is mounted noexec: the MPC may refuse to load plugins from it. "
                     "If the plugin does not appear, try the internal drive.")
    notes.append(S.dim(S.info) + " On the MPC: Preferences → Enhancements (Beta) → Enable Enhancements + VST2 Support must be on.")
    notes.append(S.dim(S.info) + " Then open a plugin browser: it is listed under External VST Plugins. "
                 "If it was already loaded on a track, remove and re-add it (or restart the MPC).")
    say()
    for n in notes:
        say("  " + n)
    say()


def pick_source(replace):
    """Offer plugins found near the current folder, or let the user type a path. Returns a list of paths."""
    cands = find_candidates()
    if cands:
        items = [(c.glyph() + " " + c.label(), c.detail(os.getcwd())) for c in cands] + [
            ("  " + ("Enter a file path…" if S.unicode else "Enter a file path..."), "a zip or an SDK project folder")]
        idx = choose("Which plugin do you want to %s?" % ("update" if replace else "add"), items, allow_back=True)
        if idx is None:
            return []
        if idx < len(cands):
            return [cands[idx].path]
    else:
        say("  " + S.dim("No built plugins or plugin zips found nearby (looked in this folder, plugins/ and examples/)."))
    val = ask("Plugin zip or folder (Tab completes)", path=True)
    return parse_paths(val) if val else []


def action_add(ctx, sources, replace=False):
    if not require_writable(ctx):
        return False
    if not sources:
        if not interactive():
            raise UsageError("Give a zip file or an SDK plugin folder to %s." % ("update" if replace else "add"))
        sources = pick_source(replace)
        if not sources:
            return False
    all_ok = True
    installed = 0
    for src in sources:
        plugins = load_sources(src, ctx.workdir)
        chosen = plugins
        if len(plugins) > 1 and interactive() and not ctx.assume_yes:
            say()
            say("  %s contains %d plugins." % (S.bold(src), len(plugins)))
            idx = choose("Install which?", [(p.folder, "v%s" % p.manifest.get("version", "?")) for p in plugins] + [("All of them", "")])
            chosen = plugins if idx == len(plugins) else [plugins[idx]]
        for lp in chosen:
            say()
            say("  %s %s %s" % (S.accent(S.arrow), S.bold(lp.manifest.get("name", lp.folder)),
                                S.dim("by %s · v%s · %s" % (lp.manifest.get("manufacturer"), lp.manifest.get("version", "?"), lp.manifest.get("kind", "?")))))
            if install(ctx, lp, replace):
                installed += 1
            else:
                all_ok = False
    if installed:
        say()
        say("  %s %s" % (S.good(S.ok), S.bold("Installed %d plugin%s on %s" % (installed, "" if installed == 1 else "s", ctx.drive.name))))
        post_install_notes(ctx)
        if interactive() and not ctx.assume_yes:
            action_restart(ctx, after_install=True)  # shows the unsaved-data warning and asks first
        else:  # never restart unattended: it throws away the open project
            say("  %s %s" % (S.dim(S.info), "Run the restart command to make the MPC rescan its plugins (unsaved data is lost)."))
            say()
    return all_ok


def action_remove(ctx, names):
    if not require_writable(ctx):
        return False
    exists, plugins = list_plugins(ctx.remote, ctx.drive)
    if not plugins:
        say("  %s %s" % (S.dim(S.info), "Nothing to remove on %s." % ctx.drive.name))
        return True
    targets = []
    if names:
        for n in names:
            targets.append(find_installed(plugins, n))
    elif interactive():
        say()
        show_plugins(plugins, ctx.drive, exists)
        say()
        idx = choose("Remove which plugin?", [(p.folder, "") for p in plugins], allow_back=True)
        if idx is None:
            return True
        targets.append(plugins[idx])
    else:
        raise UsageError("Say which plugin to remove.")
    ok = True
    for p in targets:
        if not safe_folder_name(p.folder):
            say("  %s %s" % (S.badc(S.bad), "Refusing to touch %r" % p.folder))
            ok = False
            continue
        if not confirm("Delete %s from %s? This cannot be undone." % (S.bold(p.folder), ctx.drive.name), False, ctx.assume_yes):
            say("  %s %s" % (S.warnc(S.warn), "Kept %s (pass --yes to skip this question)." % p.folder))
            ok = ok and interactive()  # a scripted run that was not told --yes has not done its job
            continue
        with Step("Removing %s" % p.folder) as st:
            rc, out, err = ctx.remote.text("rm -rf %s && sync" % shlex.quote(ctx.drive.plugins_path + "/" + p.folder))
            if rc != 0:
                st.fail(err or "rm failed")
                ok = False
            else:
                st.done()
    return ok


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s-%d%s" % (base, n, ext)):
        n += 1
    return "%s-%d%s" % (base, n, ext)


def action_export(ctx, names, export_all, out_dir):
    exists, plugins = list_plugins(ctx.remote, ctx.drive)
    if not plugins:
        say("  %s %s" % (S.dim(S.info), "Nothing to export on %s." % ctx.drive.name))
        return True
    if export_all:
        targets = plugins
    elif names:
        targets = [find_installed(plugins, n) for n in names]
    elif interactive():
        say()
        show_plugins(plugins, ctx.drive, exists)
        say()
        idx = choose("Export which plugin?", [(p.folder, "") for p in plugins] + [("All of them", "")], allow_back=True)
        if idx is None:
            return True
        targets = plugins if idx == len(plugins) else [plugins[idx]]
        if interactive():
            out_dir = ask("Save zip files to (Tab completes)", out_dir or os.getcwd(), path=True)
    else:
        raise UsageError("Say which plugin to export, or use --all.")
    out_dir = os.path.expanduser(out_dir or os.getcwd())
    os.makedirs(out_dir, exist_ok=True)
    ok = True
    for p in targets:
        if not safe_folder_name(p.folder):
            ok = False
            continue
        with Step("Exporting %s" % p.folder) as st:
            rc, data, err = ctx.remote.run("tar -cf - -C %s %s" % (shlex.quote(ctx.drive.plugins_path), shlex.quote(p.folder)), timeout=900)
            if rc != 0 or not data:
                st.fail(err or "tar failed")
                ok = False
                continue
            version = str(p.manifest.get("version", "")).strip()
            fname = legal_filename(p.folder) + ((" " + version) if version else "") + ".zip"
            dest = unique_path(os.path.join(out_dir, fname))
            files = 0
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                for m in tf.getmembers():
                    arc = m.name.lstrip("./")
                    parts = arc.split("/")
                    if not arc or ".." in parts or any(x in JUNK_NAMES or x.startswith("._") for x in parts):
                        continue
                    if m.isdir():
                        z.writestr(arc.rstrip("/") + "/", b"")
                    elif m.isreg():
                        zi = zipfile.ZipInfo(arc, time.localtime(m.mtime)[:6])
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zi.external_attr = (stat.S_IFREG | (0o755 if m.mode & 0o100 else 0o644)) << 16
                        z.writestr(zi, tf.extractfile(m).read())
                        files += 1
            st.done("%d files %s %s" % (files, S.arrow, dest))
    return ok


def action_restart(ctx, after_install=False):
    """Restart the MPC application so it rescans VST2 plugins. Everything unsaved in the open project is lost."""
    say()
    panel("Restart the MPC to rescan VSTs?" if after_install else "Restart the MPC application?", [
        S.warnc(S.warn) + " " + S.bold("Any unsaved data will be lost."),
        "",
        "The MPC only looks for new VST2 plugins on an internal drive when its app starts."
        if after_install else "The MPC app will close and start again, which makes it rescan its VST2 plugins.",
        *(["Restarting it now makes it rescan automatically, so your new plugin appears without you doing anything else."] if after_install else []),
        "Save your project on the MPC first. Audio stops while it restarts (about a minute).",
    ], "warn")
    say()
    if not confirm("Restart the MPC now so it rescans VSTs?" if after_install else "Restart the MPC now?", False, ctx.assume_yes):
        say("  %s %s" % (S.warnc(S.warn), "Not restarted%s." % ("" if interactive() else " (pass --yes to confirm)")))
        return interactive()
    unit = shlex.quote(MPC_SERVICE)
    rc, out, err = ctx.remote.text("systemctl cat %s >/dev/null 2>&1" % unit)
    if rc != 0:
        say("  %s %s" % (S.badc(S.bad), "This MPC has no '%s' service, so I can't restart it. Power-cycle it from the MPC instead." % MPC_SERVICE))
        return False
    with Step("Restarting the MPC application") as st:
        rc, out, err = ctx.remote.text("systemctl restart %s" % unit, timeout=120)
        if rc != 0:
            st.fail(err.splitlines()[-1] if err else "systemctl failed (exit %d)" % rc)
            return False
        state = ""
        for _ in range(60):
            rc, out, err = ctx.remote.text("systemctl is-active %s" % unit)
            state = out.strip()
            if state == "active":
                break
            time.sleep(1)
        if state != "active":
            st.fail("service is '%s'; check the MPC screen" % (state or "unknown"))
            return False
        st.done("running again")
    say("  %s %s" % (S.dim(S.info), "The MPC takes a little longer to finish loading. Your plugins appear under External VST Plugins."))
    say()
    return True


# ----------------------------------------------------------------------------------------------
# Doctor
# ----------------------------------------------------------------------------------------------

def action_doctor(ctx, drives):
    problems = 0
    rows = []

    def row(state, text, hint=""):
        mark = {"ok": S.good(S.ok), "warn": S.warnc(S.warn), "bad": S.badc(S.bad)}[state]
        rows.append("%s %s" % (mark, text))
        if hint:
            rows.append("    " + S.dim(hint))

    row("ok", "SSH server reachable and login works")
    if ctx.info.get("devmode.file"):
        row("ok", "Developer mode marker present")
    else:
        row("warn", "No developer-mode marker file found", "Fine if SSH works; otherwise repeat the developer-mode tap sequence on the MPC.")
    if ctx.arch:
        row("ok", "Processor: %s (plugins need the %s build)" % (ctx.info.get("arch"), ctx.arch))
    else:
        row("warn", "Could not tell the processor type")
    if not drives:
        row("bad", "No drives mounted under /media or /nvme", "Insert a USB drive or SD card, or check the internal storage is mounted.")
        problems += 1
    for d in drives:
        exists, plugins = list_plugins(ctx.remote, d)
        state = "ok"
        notes = []
        if not d.writable:
            state, problems = "bad", problems + 1
            notes.append("read-only")
        if d.noexec:
            state = "warn" if state == "ok" else state
            notes.append("mounted noexec: plugins may not load from here")
        if d.free is not None and d.free < 20 * 1024 * 1024:
            state = "warn" if state == "ok" else state
            notes.append("under 20 MB free")
        notes.append("%s folder %s" % (PLUGINS_DIR, ("with %d plugin%s" % (len(plugins), "" if len(plugins) == 1 else "s")) if exists else "not created yet"))
        row(state, "%s: %s" % (d.name, ", ".join(notes)))
    row("warn", "Enhancements and VST2 Support switches cannot be read remotely",
        "On the MPC: Preferences → Enhancements (Beta) → Enable Enhancements, then VST2 Support.")
    say()
    panel("MPC health check", rows, "bad" if problems else "good")
    say()
    return problems == 0


# ----------------------------------------------------------------------------------------------
# Menu
# ----------------------------------------------------------------------------------------------

def menu(ctx, drives):
    entries = [
        ("List plugins", "see what is installed"),
        ("Add a plugin", "from a zip or an SDK project folder"),
        ("Update a plugin", "replace an installed plugin with a newer build"),
        ("Remove a plugin", ""),
        ("Export a plugin", "save it from the MPC as a zip"),
        ("Restart MPC to rescan VSTs", "restarts the MPC app; unsaved data is lost"),
        ("Check this MPC", "diagnostics"),
        ("Switch drive", ctx.drive.name),
    ]
    while True:
        say()
        idx = choose("What would you like to do?", entries[:-1] + ([entries[-1]] if len(drives) > 1 else []), allow_back=True, back_label="exit")
        say()
        if idx is None:
            say("  " + S.dim("Bye!"))
            return True
        try:
            if idx == 0:
                action_list(ctx)
            elif idx == 1:
                action_add(ctx, [])
            elif idx == 2:
                action_add(ctx, [], replace=True)
            elif idx == 3:
                action_remove(ctx, [])
            elif idx == 4:
                action_export(ctx, [], False, None)
            elif idx == 5:
                action_restart(ctx)
            elif idx == 6:
                action_doctor(ctx, drives)
            elif idx == 7:
                ctx.drive = pick_drive(drives, None, False)
                entries[-1] = ("Switch drive", ctx.drive.name)
        except UsageError as e:
            say("  %s %s" % (S.badc(S.bad), e))
        except SshError as e:
            explain(e, ctx.remote.host, ctx.remote.port, ctx.remote.password is not None)


# ----------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)  # same options accepted after the command too
    p = argparse.ArgumentParser(prog="mpc_remote.py", description="Manage VST2 plugins on an Akai MPC over the network.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Run with just an address for an interactive menu.")
    p.add_argument("--address", "--host", dest="address", metavar="ADDRESS",
                   default=os.environ.get("MPC_ADDRESS") or os.environ.get("MPC_HOST"),
                   help="IP address or name of the MPC; normally given as the first argument (or set MPC_ADDRESS)")
    for target, default in ((p, True), (common, False)):
        d = (lambda v: v) if default else (lambda v: argparse.SUPPRESS)
        target.add_argument("--password", default=d(None), help="SSH password if needed (or set MPC_SSH_PASSWORD)")
        target.add_argument("--user", default=d("root"), help="SSH user (default root)")
        target.add_argument("--port", type=int, default=d(22), help="SSH port (default 22)")
        target.add_argument("--drive", default=d(None), help="drive number, name or mount point to manage")
        target.add_argument("--yes", "-y", action="store_true", default=d(False), help="do not ask for confirmation")
        target.add_argument("--no-color", action="store_true", default=d(False), help="plain output")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("info", parents=[common], help="show the MPC and its drives")
    sub.add_parser("drives", parents=[common], help="list drives")
    sub.add_parser("list", parents=[common], help="list installed plugins")
    sub.add_parser("doctor", parents=[common], help="check the MPC is set up correctly")
    sub.add_parser("restart", parents=[common], help="restart the MPC app so it rescans VST2 plugins (unsaved data is lost)")
    a = sub.add_parser("add", parents=[common], help="install plugins from zip files or SDK project folders")
    a.add_argument("sources", nargs="*")
    u = sub.add_parser("update", parents=[common], help="install, replacing any existing copy without asking")
    u.add_argument("sources", nargs="*")
    r = sub.add_parser("remove", parents=[common], help="delete installed plugins")
    r.add_argument("names", nargs="*")
    e = sub.add_parser("export", parents=[common], help="save installed plugins from the MPC as zip files")
    e.add_argument("names", nargs="*")
    e.add_argument("--all", action="store_true", help="export every plugin")
    e.add_argument("--out", help="folder for the zip files (default: current folder)")
    return p


def main(argv=None):
    global S
    argv = list(sys.argv[1:] if argv is None else argv)
    host_arg = None
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        host_arg = argv.pop(0)  # `mpc_remote.py 192.168.1.50 list`: argparse cannot take an optional positional before subcommands
    args = build_parser().parse_args(argv)
    _enable_vt()
    color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    S = Style(color)

    banner()
    host = host_arg or args.address
    if not host:
        if not interactive():
            say("  %s %s" % (S.badc(S.bad), "Give the MPC's IP address as the first argument."))
            return EXIT_USAGE_ERR
        host = ask("MPC IP address")
        if not host:
            return EXIT_FAIL
    password = args.password if args.password is not None else os.environ.get("MPC_SSH_PASSWORD")
    remote = Remote(host, args.user, args.port, password)
    workdir = tempfile.mkdtemp(prefix="mpcr-work-")
    try:
        try:
            connect(remote)
        except SshError as e:
            explain(e, host, args.port, remote.password is not None)
            return EXIT_SETUP

        with Step("Reading device details") as st:
            info = get_info(remote)
            st.done(info.get("dt.model") or "")
        say()
        panel("Your MPC", info_rows(host, info))
        say()

        if args.command == "restart":  # needs no drive
            return EXIT_OK if action_restart(Context(remote, info, None, args.yes, workdir)) else EXIT_FAIL

        with Step("Looking for drives") as st:
            drives = discover_drives(remote)
            st.done("%d found" % len(drives))
        if not drives:
            fail_panel("No drives found", [
                "The MPC has nothing mounted under /media (or /nvme).",
                "Insert a USB drive or SD card (formatted FAT32 / exFAT / ext4) and run this again.",
            ], "warn")
            return EXIT_SETUP
        say()
        show_drives(drives)
        say()
        drive = pick_drive(drives, args.drive, args.yes)
        say("  %s Using %s %s" % (S.good(S.ok), S.bold(drive.name), S.dim(drive.mount)))
        say()

        ctx = Context(remote, info, drive, args.yes, workdir)
        cmd = args.command
        if cmd in ("info", "drives"):
            return EXIT_OK
        if cmd == "list":
            action_list(ctx)
            return EXIT_OK
        if cmd == "doctor":
            return EXIT_OK if action_doctor(ctx, drives) else EXIT_SETUP
        if cmd in ("add", "update"):
            return EXIT_OK if action_add(ctx, args.sources, replace=(cmd == "update")) else EXIT_FAIL
        if cmd == "remove":
            return EXIT_OK if action_remove(ctx, args.names) else EXIT_FAIL
        if cmd == "export":
            return EXIT_OK if action_export(ctx, args.names, args.all, args.out) else EXIT_FAIL
        if interactive():
            action_list(ctx)
            menu(ctx, drives)
            return EXIT_OK
        action_list(ctx)
        say("  " + S.dim("Pass a command (add, update, remove, export, doctor) to do more."))
        return EXIT_OK
    except UsageError as e:
        say("  %s %s" % (S.badc(S.bad), e))
        return EXIT_FAIL
    except SshError as e:
        explain(e, host, args.port, remote.password is not None)
        return EXIT_SETUP
    except KeyboardInterrupt:
        say()
        say("  " + S.dim("Interrupted."))
        return EXIT_FAIL
    finally:
        remote.close()
        shutil.rmtree(workdir, ignore_errors=True)


EXIT_USAGE_ERR = EXIT_SETUP

if __name__ == "__main__":
    sys.exit(main())
