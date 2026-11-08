#!/usr/bin/env python3
"""mpc_firmware.py - update the firmware of an Akai MPC over the network.

Usage:
    mpc_firmware.py ADDRESS IMAGE.img [options]       copy the image to the MPC, then install it
    mpc_firmware.py ADDRESS --net URL [options]       have the MPC download the update itself
    mpc_firmware.py ADDRESS --usb [options]           reboot the MPC into USB update mode

The MPC's address can also be given as --address ADDRESS (or the MPC_ADDRESS environment variable).

With an image file the tool copies it (scp) to /data/az01-update.img on the MPC and runs
    az01-update --file /data/az01-update.img
With --net it runs
    az01-update --net <URL>
and nothing is copied.

Options:
    --net URL       download URL for the update; handed to az01-update --net
    --usb           reboot the MPC into USB update mode (runs `reboot loader` on arm32 or `reboot update` on arm64 over SSH)
    --keep          do not delete the copied image afterwards
    --password PW   SSH password if the MPC needs one (or set MPC_SSH_PASSWORD)
    --user NAME     SSH user (default root)
    --port N        SSH port (default 22)
    --yes           do not ask for confirmation
    --no-color      plain output (also honours NO_COLOR)

The MPC must be on your network with developer mode on (that is what starts its SSH server).
The MPC will usually reboot when the update finishes, so the connection dropping at the end is normal.
Do not switch the MPC off while it is updating.

Uses your computer's `ssh` and `scp` commands and reuses the connection code from mpc_remote.py.
Standard library only. Python 3.9+.
"""

import argparse
import os
import shlex
import subprocess
import sys

import mpc_remote as mr
from mpc_remote import (EXIT_FAIL, EXIT_OK, EXIT_SETUP, Remote, S, SshError, Step, ask, confirm, explain,
                        fail_panel, human_size, info_rows, interactive, panel, say)

UPDATER = "az01-update"
REMOTE_IMAGE = "/data/az01-update.img"  # where the image is put on the MPC (/tmp is too small)
# the command that puts the MPC into USB update mode depends on its processor
USB_MODE_CMDS = {"arm32": "reboot loader", "arm64": "reboot update"}
SSH_LOST = 255  # ssh's own exit status when the connection fails or drops


def scp_command(remote, local, dest):
    """scp command line that shares the ssh options (and open connection) of `remote`."""
    scp = os.environ.get("MPC_SCP_BIN") or mr.shutil.which("scp")
    if not scp:
        raise SshError("noclient")
    ssh_cmd = remote._command("true")  # [ssh, -p N, -o ..., user@host, "true"]
    opts = []
    i = 1
    while i < len(ssh_cmd) - 2:
        if ssh_cmd[i] == "-p":
            opts += ["-P", ssh_cmd[i + 1]]
        else:
            opts += [ssh_cmd[i], ssh_cmd[i + 1]]
        i += 2
    if not sys.stdout.isatty():
        opts.append("-q")
    return [scp] + opts + [local, "%s@%s:%s" % (remote.user, remote.host, dest)]


def copy_image(remote, local, dest):
    """Copy the image to the MPC. scp draws its own progress meter on a terminal. Returns True on success."""
    cmd = scp_command(remote, local, dest)
    say("  %s Copying %s %s %s" % (S.arrow, S.bold(os.path.basename(local)), S.arrow, dest))
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, env=remote._env(), stderr=subprocess.PIPE,
                           start_new_session=(os.name != "nt" and remote.password is not None))
    except FileNotFoundError:
        raise SshError("noclient")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        if p.returncode == SSH_LOST:
            raise SshError(mr.classify_ssh_error(err), err)
        say("  %s %s" % (S.badc(S.bad), "Copy failed: " + (err.splitlines()[-1] if err else "scp exit %d" % p.returncode)))
        return False
    say("  %s %s" % (S.good(S.ok), "Image copied"))
    return True


def stream_remote(remote, script):
    """Run a script on the MPC, echoing its output live. Returns the exit status (255 if the link dropped)."""
    cmd = remote._command("sh -c " + shlex.quote(script + " 2>&1"))
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=remote._env(), start_new_session=(os.name != "nt" and remote.password is not None))
    try:
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            say("    " + S.dim(line))
        return p.wait()
    except KeyboardInterrupt:
        p.terminate()  # only stops our ssh session; the MPC may carry on updating
        raise


def free_kb(remote, directory):
    rc, out, err = remote.text("df -Pk %s | tail -n 1" % shlex.quote(directory))
    try:
        return int(out.split()[3])
    except (IndexError, ValueError):
        return None


def action_usb(remote, host, info, assume_yes):
    """Reboot the MPC into USB update mode: `reboot loader` on arm32 devices, `reboot update` on arm64."""
    arch = mr.device_arch(info)
    cmd = USB_MODE_CMDS.get(arch)
    if not cmd:
        fail_panel("Can't tell which USB mode command to use", [
            "The MPC reports its processor as '%s', which is neither arm32 nor arm64." % (info.get("arch") or "unknown"),
        ], "warn")
        return False
    panel("USB update mode", [
        S.warnc(S.warn) + " " + S.bold("The MPC will restart into USB update mode."),
        "",
        "It leaves its normal software and waits for an update over USB.",
        "Save your work on it first. Anything unsaved is lost.",
    ], "warn")
    say()
    if not confirm("Reboot %s into USB update mode now?" % host, False, assume_yes):
        say("  %s %s" % (S.warnc(S.warn), "Not rebooted%s." % ("" if interactive() else " (pass --yes to confirm)")))
        return False
    say("  %s Running %s %s" % (S.arrow, S.bold(cmd), S.dim("(%s)" % arch)))
    rc = stream_remote(remote, cmd)
    say()
    if rc not in (0, SSH_LOST):  # the reboot normally kills our session
        fail_panel("%s failed" % cmd, ["It exited with status %d. See its output above." % rc])
        return False
    panel("Rebooting into USB update mode", [
        "The MPC is restarting into its loader. Connect it to your computer with a USB cable",
        "and run the USB updater. Do not switch it off while it updates.",
    ], "good")
    return True


def build_parser():
    p = argparse.ArgumentParser(prog="mpc_firmware.py", description="Update the firmware of an Akai MPC over the network.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Give either an image file or --net URL.")
    p.add_argument("address", nargs="?", metavar="ADDRESS", default=os.environ.get("MPC_ADDRESS") or os.environ.get("MPC_HOST"),
                   help="IP address or name of the MPC (or set MPC_ADDRESS)")
    p.add_argument("image", nargs="?", metavar="IMAGE", help="firmware update image (.img) to copy to the MPC")
    p.add_argument("--net", metavar="URL", help="download URL for the update, passed to az01-update --net")
    p.add_argument("--usb", action="store_true", help="reboot the MPC into USB update mode (`reboot loader` on arm32, `reboot update` on arm64)")
    p.add_argument("--keep", action="store_true", help="leave the copied image on the MPC")
    p.add_argument("--password", help="SSH password if needed (or set MPC_SSH_PASSWORD)")
    p.add_argument("--user", default="root", help="SSH user (default root)")
    p.add_argument("--port", type=int, default=22, help="SSH port (default 22)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--no-color", action="store_true", help="plain output")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    mr._enable_vt()
    color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    mr.S = S_ = mr.Style(color)
    globals()["S"] = S_

    mr.banner("Manage your MPC firmware")
    host, image = args.address, args.image
    if not host:
        if not interactive():
            say("  %s %s" % (S.badc(S.bad), "Give the MPC's IP address as the first argument."))
            return EXIT_SETUP
        host = ask("MPC IP address")
        if not host:
            return EXIT_FAIL
    if args.usb and (args.net or image):
        say("  %s %s" % (S.badc(S.bad), "--usb takes no image file and no --net."))
        return EXIT_FAIL
    if args.net and image:
        say("  %s %s" % (S.badc(S.bad), "Give an image file or --net, not both."))
        return EXIT_FAIL
    if not args.net and not image and not args.usb:
        if not interactive():
            say("  %s %s" % (S.badc(S.bad), "Give an update image file (.img) or --net URL."))
            return EXIT_FAIL
        image = mr.parse_paths(ask("Firmware image file (.img)", path=True))[0]
    if image:
        image = os.path.expanduser(image)
        if not os.path.isfile(image):
            say("  %s %s" % (S.badc(S.bad), "No such file: %s" % image))
            return EXIT_FAIL
        if not image.lower().endswith(".img"):
            say("  %s %s" % (S.warnc(S.warn), "That file does not end in .img; check it is the right one."))

    password = args.password if args.password is not None else os.environ.get("MPC_SSH_PASSWORD")
    remote = Remote(host, args.user, args.port, password)
    remote_file = None
    try:
        try:
            mr.connect(remote)
        except SshError as e:
            explain(e, host, args.port, remote.password is not None)
            return EXIT_SETUP

        with Step("Reading device details") as st:
            info = mr.get_info(remote)
            st.done(info.get("dt.model") or "")
        say()
        panel("Your MPC", info_rows(host, info))
        say()

        if args.usb:
            return EXIT_OK if action_usb(remote, host, info, args.yes) else EXIT_FAIL

        with Step("Looking for %s" % UPDATER) as st:
            rc, out, err = remote.text("command -v %s" % UPDATER)
            if rc != 0 or not out.strip():
                st.fail("not found")
                fail_panel("This MPC has no %s" % UPDATER, [
                    "The updater was not found on the device, so the firmware cannot be updated this way.",
                    "Update it from the MPC itself instead, or use a newer firmware that includes the updater.",
                ], "warn")
                return EXIT_SETUP
            st.done(out.strip().splitlines()[0])

        if image:
            size = os.path.getsize(image)
            tmpdir = os.path.dirname(REMOTE_IMAGE)
            with Step("Checking space in %s" % tmpdir) as st:
                free = free_kb(remote, tmpdir)
                if free is None:
                    st.warn("could not tell")
                elif free * 1024 < size + (8 << 20):
                    st.fail("%s free, image is %s" % (human_size(free * 1024), human_size(size)))
                    fail_panel("Not enough temporary space", [
                        "%s on the MPC has %s free but the image needs %s." % (tmpdir, human_size(free * 1024), human_size(size)),
                        "Free some space on the MPC's internal storage and try again.",
                    ], "warn")
                    return EXIT_FAIL
                else:
                    st.done("%s free, image is %s" % (human_size(free * 1024), human_size(size)))
            remote_file = REMOTE_IMAGE
            update_args = "--file %s" % shlex.quote(remote_file)
            what = "%s (%s)" % (os.path.basename(image), human_size(size))
        else:
            update_args = "--net %s" % shlex.quote(args.net)
            what = args.net

        say()
        panel("Firmware update", [
            S.warnc(S.warn) + " " + S.bold("Do not switch the MPC off or unplug it while it updates."),
            "",
            "Update from: " + what,
            "The MPC will probably restart when it has finished. Save your work on it first.",
        ], "warn")
        say()
        if not confirm("Update the firmware of %s now?" % host, False, args.yes):
            say("  %s %s" % (S.warnc(S.warn), "Not updated%s." % ("" if interactive() else " (pass --yes to confirm)")))
            return EXIT_FAIL

        if image:
            try:
                if not copy_image(remote, image, remote_file):
                    remote_file = None
                    return EXIT_FAIL
            except SshError:
                remote_file = None
                raise

        say()
        say("  %s Running %s %s" % (S.arrow, S.bold(UPDATER), update_args))
        rc = stream_remote(remote, "%s %s" % (UPDATER, update_args))
        say()
        if rc == SSH_LOST:
            # the updater usually reboots the MPC, which kills our session
            panel("Connection closed", [
                "The MPC dropped the connection. That is normal if it is restarting to finish the update.",
                "Leave it alone for a few minutes; it must not be switched off while it updates.",
            ], "warn")
            remote_file = None  # we cannot reach the MPC to tidy up now
            return EXIT_OK
        if rc != 0:
            fail_panel("%s failed" % UPDATER, ["It exited with status %d. See its output above." % rc])
            return EXIT_FAIL
        panel("Update finished", ["%s completed successfully." % UPDATER,
                                  "The MPC may restart by itself; if it does not, restart it from its power button."], "good")
        return EXIT_OK
    except SshError as e:
        explain(e, host, args.port, remote.password is not None)
        return EXIT_SETUP
    except KeyboardInterrupt:
        say()
        say("  " + S.dim("Interrupted. If the update had started, the MPC may still be installing it; do not switch it off."))
        return EXIT_FAIL
    finally:
        if remote_file and not args.keep:
            try:
                remote.text("rm -f %s" % shlex.quote(remote_file), timeout=15)
            except Exception:
                pass
        remote.close()


if __name__ == "__main__":
    sys.exit(main())
