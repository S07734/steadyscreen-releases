#!/usr/bin/env python3
"""Display System client: a barebones browser with a built-in HTTP admin backend.

One process: a fullscreen WebKit view with no browser chrome at all, plus a
small HTTP server (default :8080) for setting the URL, reloading and
restarting it from another machine on the LAN.

Config lives in /etc/kiosk/config.json and is rewritten whenever a setting is
changed through the admin page, so changes survive a reboot.
"""

import base64
import io
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, WebKit2  # noqa: E402

CONFIG_PATH = os.environ.get("KIOSK_CONFIG", "/etc/kiosk/config.json")
IDENTITY_PATH = os.environ.get("KIOSK_IDENTITY", "/etc/kiosk/identity.json")
VERSION = "b1.12.1"
STARTED = time.time()

DEFAULTS = {
    "url": "about:blank",
    "admin_port": 8080,
    "admin_port_alt": 80,          # also listen here; 0 disables. See start_admin

    "admin_bind": "0.0.0.0",
    "admin_password": "",          # empty = no auth (LAN only)
    "zoom": 1.0,
    "hardware_acceleration": False,  # Cherry Trail GL is weak; off is steadier
    "refresh_seconds": 0,            # >0 = periodic auto-reload
    "idle_reset_seconds": 0,         # >0 = return to url after N idle seconds
    "hide_cursor": True,
    # On-screen keyboard for the configured page. "auto" turns it on when the
    # machine has a touchscreen and no keyboard -- which is what a wall-mounted
    # scanner is. "on"/"off" override it.
    "osk": "auto",
    # How the on-screen keyboard starts: "lock" caps lock on, "first" the
    # first letter capitalised then lower case, "off" lower case throughout.
    "osk_caps": "lock",
    # Nudge the keys away from a dead patch on the digitiser, in pixels.
    # x positive moves right, y positive lifts the keys off the bottom edge.
    # Needed on a real panel whose glass had stopped registering presses in
    # one band, whichever key happened to be sitting there.
    "osk_offset_x": 0,
    "osk_offset_y": 0,
    "retry_seconds": 15,             # retry interval after a failed load
    "rotation": "normal",            # normal | left | right | inverted
    "resolution": "auto",            # auto, or WxH (scaled if not native)
    "wait_for_network": True,        # show the network page until reachable
    "offline_after_seconds": 45,     # then give up waiting and use the cache
    "offline_border": True,          # 2px red edge while the site is unreachable
    "splash_managed": True,          # branding is server-controlled; see below
    "clear_cache_on_start": False,   # purge the disk cache at every start
    "allow_location": False,         # answer the page's geolocation request
    "print_enabled": True,           # window.print() prints, with no dialog
    "printer": "",                   # CUPS queue; empty = system default
    "print_margins_mm": 0,           # receipt paper has no margins to give
    "print_width_mm": 80,            # 80 mm roll; 58 for the narrow ones
    "screenshot_cache_secs": 1.5,    # reuse a frame this fresh instead of
                                     # grabbing the screen again

    "splash_enabled": True,          # boot splash

    "audio_output": "auto",          # "auto", or "card:device" e.g. "0:3"
    "volume": 60,                    # 0-100, applied in software (see below)
    "audio_muted": False,

    # Seconds a newly-booted slot must survive before it is committed. Short
    # enough that a reboot does not undo a good update; long enough that a
    # system which dies on its face is never trusted.
    "os_commit_grace": 120,
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except Exception as exc:                                  # noqa: BLE001
        print(f"ds: bad config {CONFIG_PATH}: {exc}", file=sys.stderr)
    return cfg


def save_config(cfg):
    """Write the config so a power cut cannot leave it half-written.

    These machines are switched off by pulling the plug -- that is simply how
    a display in a restaurant gets turned off at closing time. os.replace is
    atomic, but without the fsync the rename can reach the disk while the data
    has not, and the client comes back to an empty config file it cannot parse.
    Syncing the directory too is what makes the rename itself durable.
    """
    tmp = CONFIG_PATH + ".tmp"
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_PATH)
    try:
        fd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass                      # best effort; the write itself is durable


def save_identity(ident):
    """Write identity.json durably. Same reasoning as save_config: these
    machines lose power without warning, and a half-written identity is a
    client that comes back as a stranger."""
    os.makedirs(os.path.dirname(IDENTITY_PATH), exist_ok=True)
    tmp = IDENTITY_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(ident, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, IDENTITY_PATH)
    try:
        fd = os.open(os.path.dirname(IDENTITY_PATH), os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


DS_NAMESPACE = uuid.UUID("808ddfb2-5d75-50f4-a718-706309d959cf")


def locally_admin(mac):
    """Is this MAC locally administered -- i.e. invented rather than burned in?

    NetworkManager randomises the wifi MAC while scanning and only settles on
    the permanent one once associated, so a fingerprint taken during boot can
    contain a MAC that will never be seen again. That is not theoretical: one
    client produced three different client ids in a day, a new one at every
    boot, and none of its later MAC combinations could reproduce the boot
    value -- because the address in it no longer existed.

    Every randomised or virtual address (bridges, veth, docker) has the
    locally-administered bit set in the first octet; real burned-in hardware
    does not. Skipping them is what makes the identity survive a reboot.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


HWID_HELPER = "/usr/local/sbin/kiosk-hwid"


def hw_fingerprint():
    """A stable, unique string for this hardware.

    Computed by kiosk-hwid, which the installer also calls. One program, so
    the two cannot drift -- they have twice, and the second time both were
    wrong in the same way, which no amount of comparing them to each other
    would have caught.

    Empty when the machine has nothing burned in to go on (a VM with a
    randomised NIC, an LVM root). That is not an error: the identity is then a
    value we mint rather than one we discover, and it lives on /data.
    """
    try:
        p = subprocess.run([HWID_HELPER], capture_output=True, text=True,
                           timeout=20)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:                                         # noqa: BLE001
        return ""


def derived_id(fingerprint):
    """A stable id for this hardware, or "" when there is no hardware to go on.

    Never derive from an empty fingerprint. uuid5(namespace, "") is a perfectly
    valid, perfectly *constant* uuid -- 88019707-cf81-516b-ae13-6c35b9e89001 --
    so every machine that could not read its own hardware would claim the same
    id. Two machines reported exactly that, which is a worse collision than the
    per-model one this work set out to fix: universal rather than confined to
    one board.

    The caller mints a random id instead, and marks it not-derived. That is the
    honest answer -- an identity we assigned rather than one we discovered.
    """
    if not fingerprint:
        return ""
    return str(uuid.uuid5(DS_NAMESPACE, fingerprint))


def identity_check():
    """Does the stored identity still belong to this hardware, right now?

    load_identity() only runs when this process starts, so an update that
    replaces a helper without restarting the browser would never re-check.
    This is callable at any time and is what kiosk-update consults before it
    installs anything.
    """
    fp = hw_fingerprint()
    stored = (IDENTITY.get("hardware") or {}).get("fingerprint") or ""
    expect = derived_id(fp)
    out = {
        "id": IDENTITY.get("id", ""),
        "expected_id": expect,
        "fingerprint": fp[:12],
        "derived": bool(IDENTITY.get("derived")),
        "ok": True,
        "detail": "",
    }
    if not IDENTITY.get("derived"):
        # Installed before ids were derived from hardware. Not wrong, just
        # unverifiable -- say so rather than claiming a pass.
        out["ok"] = True
        out["detail"] = "id predates hardware derivation; cannot be verified"
        return out
    if not fp:
        # kiosk-hwid missing or the hardware unreadable. That is a gap in what
        # we can see, not evidence the machine changed, and calling it a
        # mismatch would stop updates on a client that is perfectly fine --
        # kiosk-update refuses to run when identity.ok is false.
        out["detail"] = "cannot read this hardware right now; not verified"
        return out
    if stored and stored != fp:
        out["ok"] = False
        out["detail"] = ("hardware does not match the stored fingerprint "
                         "(%s -> %s)" % (stored[:12], fp[:12]))
    elif out["id"] != expect:
        out["ok"] = False
        out["detail"] = ("id does not match this hardware; expected %s"
                         % expect)
    return out


def load_identity():
    """Who this client is. A management server will key on `id`, not on name.

    Self-healing: a box built before identity.json existed gets one written
    the first time the app starts, so no client is ever without an id.
    """
    try:
        with open(IDENTITY_PATH) as fh:
            ident = json.load(fh)
        # An identity file can arrive on hardware it was not made for: a disk
        # cloned to a second machine, or a data partition moved. The id is
        # derived from the hardware, so recompute and compare rather than
        # trusting the file -- otherwise two machines would claim to be the
        # same client and a licence would be counted once for both.
        fp = hw_fingerprint()
        stored_fp = (ident.get("hardware") or {}).get("fingerprint") or ""
        # `fp` must be non-empty. With an unreadable fingerprint this branch
        # would rewrite a good identity with a derived-from-nothing value --
        # which is how every affected machine ended up claiming the same id.
        # Re-derive when the hardware moved OR when the stored id simply is
        # not what this hardware derives. The second case is not hypothetical:
        # clients that computed an id while their fingerprint was unreadable
        # stored a derived-from-nothing value, and their stored fingerprint is
        # empty -- so a check that only compares fingerprints would leave them
        # on the wrong id forever.
        if ident.get("derived") and fp and (
                (stored_fp and stored_fp != fp)
                or ident.get("id") != derived_id(fp)):
            want = derived_id(fp)
            print("ds: re-deriving id (%s -> %s): %s"
                  % (stored_fp[:12] or "none", fp[:12], want),
                  file=sys.stderr)
            ident["id"] = want
            ident["name"] = ident.get("name") or ""
            ident["derived"] = True
            ident.setdefault("hardware", {})["fingerprint"] = fp
            ident["hardware"]["short"] = fp[:6]
            ident["recloned"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            try:
                save_identity(ident)
            except Exception:                                 # noqa: BLE001
                pass
            return ident
        if ident.get("id"):
            return ident
    except (FileNotFoundError, ValueError):
        pass
    except Exception as exc:                                  # noqa: BLE001
        print("ds: bad identity file: %s" % exc, file=sys.stderr)

    ident = {
        "id": str(uuid.uuid4()),
        "name": socket.gethostname(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hardware": {},
    }
    try:
        with open("/sys/block/mmcblk0/device/cid") as fh:
            ident["hardware"]["emmc_cid"] = fh.read().strip()
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(IDENTITY_PATH), exist_ok=True)
        tmp = IDENTITY_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(ident, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, IDENTITY_PATH)
        print("ds: generated identity %s (%s)" % (ident["id"], ident["name"]),
              flush=True)
    except OSError as exc:
        print("ds: could not write %s: %s" % (IDENTITY_PATH, exc),
              file=sys.stderr)
    return ident


# Everything legal in a URL stays as it is; a space and its friends do not.
# `%` is deliberately in the safe set so an already-encoded URL survives being
# normalised twice instead of turning %20 into %2520.
URL_SAFE = "%:/?#[]@!$&'()*+,;=-._~"


# The wordmark, written once. Wherever the name appears as text it is set the
# same way as the boot splash: "Steady" white, "Screen" in the splash's cyan.
# Two places styling it by hand is how a brand ends up with two brands.
BRAND_CYAN = "#6fe3f2"          # CYAN in build/make-splash.py
BRAND_CYAN_DIM = "#2f9fd6"      # CYAN_DIM, the stand
BRAND_CSS = ".bw{color:#fff}.bs{color:" + BRAND_CYAN + "}"
BRAND_HTML = "<span class=bw>Steady</span><span class=bs>Screen</span>"

# The splash mark reduced to something legible at 16px, as a data URI so every
# built-in page carries it without another endpoint to serve or fail.
#
# The SVG uses SINGLE quotes throughout. It sits inside an HTML href="...",
# so a double quote of its own ends the attribute early and the icon silently
# does not load -- which is exactly what the first version did.
FAVICON = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='6' y='10' width='52' height='34' rx='4' ry='4' fill='none' stroke='%236fe3f2' stroke-width='4'/><path d='M14 30 q4 -9 7 0 t7 0 t7 0 h9' fill='none' stroke='%236fe3f2' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/><path d='M26 44 h12 l3 8 h-18 z' fill='%232f9fd6'/><rect x='18' y='52' width='28' height='5' rx='2' fill='%232f9fd6'/></svg>"
FAVICON_TAG = '<link rel="icon" href="' + FAVICON + '">'


def brandify(page):
    """Put the mark, its colours and the favicon into a built-in page.

    One pass over every page rather than four hand-styled copies. The tokens
    are HTML comments so the pages stay valid and readable on their own, and so
    a page that forgets one degrades to plain text instead of showing markup.
    """
    return (page
            .replace("<!--FAVICON-->", FAVICON_TAG)
            .replace("<!--BRANDCSS-->", BRAND_CSS)
            .replace("<!--BRAND-->", BRAND_HTML))


UNSTAMPED = "1.0"          # the literal in source; a release rewrites it


def software_version():
    """What to call the software this client is running.

    VERSION is stamped by make-release.sh and build-rootfs.sh. A file put in
    place by hand keeps the source literal, so the client would report "1.0" --
    a version that was never released and tells you nothing. Saying "-dev"
    makes a hand-deployed build visible as one, which is the truth and is the
    whole point of printing a version on the screen.
    """
    return VERSION + "-dev" if VERSION == UNSTAMPED else VERSION


OSK_JS_PATH = "/usr/local/share/steadyscreen/osk.js"


def osk_script():
    """The on-screen keyboard, read from disk so it can be updated on its own.

    Returns "" if it is not installed, and the caller then injects nothing --
    a missing keyboard is a missing feature, not a broken page.
    """
    try:
        with open(OSK_JS_PATH) as fh:
            return fh.read()
    except Exception:                                          # noqa: BLE001
        return ""


def osk_wanted(cfg):
    """Whether to inject it, given the setting and what is plugged in."""
    mode = str(cfg.get("osk", "auto")).lower()
    if mode in ("on", "true", "yes", "1"):
        return True
    if mode in ("off", "false", "no", "0"):
        return False
    inp = input_devices()
    return bool(inp.get("touch")) and not inp.get("keyboard")


def os_version():
    """Which OS this slot is, as a dotted string: "1", "1.1", "1.10".

    A STRING, not a number. Read as a decimal, 1.10 would be smaller than 1.9 --
    the oldest version bug there is. Comparison happens component-wise in the
    updaters; this only has to report it faithfully.

    Anything that is not digits-and-dots is a client from before the OS and the
    software had separate versions. It reports "0", older than every numbered
    OS, which is exactly what it is.
    """
    try:
        with open("/etc/steadyscreen-os-version") as fh:
            v = fh.read().strip()
    except Exception:                                          # noqa: BLE001
        return "0"
    if v and all(c.isdigit() or c == "." for c in v) \
            and not v.startswith(".") and not v.endswith("."):
        return v
    return "0"


def is_unconfigured(cfg):
    """No page has been chosen yet.

    A fresh install is expected to be in this state: the installer no longer
    asks for a URL, because the admin page can set it and whoever is standing
    at the machine during an install usually does not know it yet. So this is
    not an error condition to be recovered from -- it is where a client
    legitimately starts, and it must show something useful rather than a blank
    page or an endless wait for a site that was never named.
    """
    u = (cfg.get("url") or "").strip()
    return u in ("", "about:blank")


def normalise_url(url):
    """Tidy a typed URL: add a scheme, and encode what a browser would reject.

    The encoding is here rather than left to the caller because a URL with a
    space in it is the ordinary case -- a query string built from a store name
    -- and neither the operator filling in an answer file nor the person
    pasting into the admin page should have to know that a space is %20. The
    installer does the same thing to the same rules; see `encode_url` in
    kiosk-install.
    """
    from urllib.parse import quote

    url = (url or "").strip()
    if not url:
        return "about:blank"
    if "://" not in url and not url.startswith("about:"):
        url = "http://" + url
    return quote(url, safe=URL_SAFE)


def target_endpoint(url):
    """(host, port) the configured page lives on, for a plain reachability test."""
    try:
        from urllib.parse import urlparse
        u = urlparse(normalise_url(url))
        if not u.hostname:
            return None, None
        port = u.port or (443 if u.scheme == "https" else 80)
        return u.hostname, port
    except Exception:                                         # noqa: BLE001
        return None, None


def can_reach(host, port, timeout=4):
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def nm_connectivity():
    ok, out, _ = nmcli("-t", "networking", "connectivity")
    return out if ok and out else "unknown"


def saved_wifi_networks():
    """Saved wifi profiles with whether each is currently active."""
    ok, out, _ = nmcli("-t", "-f", "NAME,TYPE,ACTIVE,DEVICE", "connection", "show")
    if not ok:
        return []
    nets = []
    for line in out.splitlines():
        f = nm_split(line)
        if len(f) >= 3 and f[1] == "802-11-wireless":
            nets.append({"name": f[0], "active": f[2] == "yes",
                         "device": f[3] if len(f) > 3 else ""})
    return nets



def input_devices():
    """What can actually drive this screen.

    Some clients are pure menu displays: no touchscreen, no keyboard, no
    mouse. Showing them buttons is worse than useless, so the on-screen
    pages hide every control until something can operate them.

    udev is the honest source here: it sets ID_INPUT_KEY on anything with a
    button (power buttons, lid switches, HID hotkeys) but ID_INPUT_KEYBOARD
    only on something you could actually type on.
    """
    caps = {"touch": False, "keyboard": False, "pointer": False}
    touch_paths = []
    try:
        import glob
        for node in glob.glob("/dev/input/event*"):
            try:
                p = subprocess.run(["udevadm", "info", "-q", "property",
                                    "-n", node],
                                   capture_output=True, text=True, timeout=5)
            except Exception:                                 # noqa: BLE001
                continue
            props = p.stdout
            if "ID_INPUT_TOUCHSCREEN=1" in props:
                caps["touch"] = True
                for line in props.splitlines():
                    if line.startswith("DEVPATH="):
                        touch_paths.append(line.split("=", 1)[1])
            if "ID_INPUT_KEYBOARD=1" in props:
                caps["keyboard"] = True
            if "ID_INPUT_MOUSE=1" in props or "ID_INPUT_TOUCHPAD=1" in props:
                caps["pointer"] = True
    except Exception as exc:                                  # noqa: BLE001
        print("ds: input probe failed: %s" % exc, file=sys.stderr)
    # A keyboard can drive the controls perfectly well (tab, enter), so it
    # counts as interactive. `clickable` is kept separate only to decide
    # whether the on-screen keyboard is worth showing. Note a keyboard-wedge
    # barcode scanner also appears as ID_INPUT_KEYBOARD and udev cannot tell
    # the two apart, so keyboard-only is not proof a person can navigate.
    caps["clickable"] = caps["touch"] or caps["pointer"]
    caps["typable"] = caps["keyboard"]
    caps["interactive"] = caps["clickable"] or caps["typable"]
    caps["touch_activity"] = touch_activity(touch_paths) if caps["touch"] else None
    return caps


def irq_counts():
    """{device name: interrupts since boot} from /proc/interrupts."""
    out = {}
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3 or not parts[0].rstrip(":").isdigit():
                    continue
                total, i = 0, 1
                while i < len(parts) and parts[i].isdigit():
                    total += int(parts[i]); i += 1     # per-CPU counts
                # then the controller and the hw-irq descriptor, e.g.
                #   chv-gpio 19 GDIX1001:00
                #   IO-APIC  23-fasteoi  ehci_hcd:usb1, i801_smbus
                # everything after those two is the device name list.
                for name in " ".join(parts[i + 2:]).split(","):
                    name = name.strip()
                    if name:
                        out[name] = out.get(name, 0) + total
    except Exception:                                         # noqa: BLE001
        pass
    return out


def touch_activity(devpaths):
    """Has a touchscreen's interrupt ever fired?

    A panel can be registered, bound by X and completely dead: udev sets
    ID_INPUT_TOUCHSCREEN from the device's *capabilities*, so `touch: true`
    says the hardware is present, not that it works. On a unit tested
    2026-08-27 the controller answered I2C perfectly and its interrupt had
    fired zero times, while an identical machine showed 61 (hazard 21).

    Returns {"irq": name, "count": n} or None when it cannot be determined --
    and None is the honest answer for anything on a shared line, notably USB,
    where a zero would say nothing about this device. Absence of evidence is
    not reported as evidence of absence.

    A count of zero is "nothing seen since boot", not "broken": a display
    nobody touches reads zero and is perfectly healthy. It is only damning
    when someone is standing there tapping.
    """
    counts = irq_counts()
    for dp in devpaths:
        m = re.search(r"/([^/]+)/input/input\d+", dp or "")
        if not m:
            continue
        node = m.group(1)
        if node.startswith("usb") or "-" not in node and ":" not in node:
            continue                       # shared or unmappable, say nothing
        for cand in (node, re.sub(r"^i2c-", "", node)):
            if cand in counts:
                return {"irq": cand, "count": counts[cand]}
    return None


def primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:                                         # noqa: BLE001
        return "?"



# --------------------------------------------------------------------------
# network (nmcli via sudo; see /etc/sudoers.d/kiosk)
# --------------------------------------------------------------------------

NMCLI = ["sudo", "-n", "/usr/bin/nmcli"]


def nmcli(*args, timeout=25):
    """Run nmcli. Returns (ok, stdout, stderr)."""
    try:
        p = subprocess.run(NMCLI + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "timed out"
    except FileNotFoundError:
        return False, "", "nmcli not installed"


def nm_split(line):
    """Split one -t (terse) nmcli line on unescaped colons."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def wifi_device():
    ok, out, _ = nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device", "status")
    if ok:
        for line in out.splitlines():
            f = nm_split(line)
            if len(f) >= 3 and f[1] == "wifi":
                return f[0], f[2]
    return None, "unavailable"


def net_status():
    dev, state = wifi_device()
    ok, out, _ = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION,IP4-CONNECTIVITY",
                       "device", "status")
    wired, wifi = None, None
    if ok:
        for line in out.splitlines():
            f = nm_split(line)
            if len(f) < 4:
                continue
            entry = {"device": f[0], "state": f[2], "connection": f[3] or ""}
            if f[1] == "ethernet" and wired is None:
                wired = entry
            elif f[1] == "wifi" and wifi is None:
                wifi = entry
    for entry in (wired, wifi):
        if entry and entry["state"] == "connected":
            ok2, out2, _ = nmcli("-t", "-f", "IP4.ADDRESS", "device", "show",
                                 entry["device"])
            if ok2 and out2:
                entry["ip"] = nm_split(out2.splitlines()[0])[-1]
    radio_ok, radio, _ = nmcli("-t", "radio", "wifi")
    return {
        "wired": wired,
        "wifi": wifi,
        "wifi_device": dev,
        "wifi_radio": (radio == "enabled") if radio_ok else False,
    }


def wifi_scan(rescan=True):
    dev, _ = wifi_device()
    if not dev:
        return None, "no wifi device"
    args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    ok, out, err = nmcli(*args, timeout=40)
    if not ok:
        return None, err or "scan failed"
    best = {}
    for line in out.splitlines():
        f = nm_split(line)
        if len(f) < 4 or not f[1]:
            continue                       # skip hidden / unnamed networks
        ssid = f[1]
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        entry = {"ssid": ssid, "signal": signal,
                 "security": f[3] or "open", "in_use": f[0] == "*"}
        # the same SSID shows up once per band/channel: keep the strongest
        if ssid not in best or entry["signal"] > best[ssid]["signal"]:
            best[ssid] = entry
        if entry["in_use"]:
            best[ssid]["in_use"] = True
    nets = sorted(best.values(), key=lambda n: -n["signal"])
    return nets, None


def wifi_wait_ready(dev, ssid, timeout=20):
    """Wait until the radio is usable and the SSID has actually been seen.

    `nmcli radio wifi on` returns immediately, but the device then spends
    several seconds "unavailable" with no completed scan behind it -- so a
    connect issued straight afterwards fails with "no network with SSID
    found", and the same password entered a second time works. That is a
    confusing failure to hand someone standing at a counter, and it was
    reported from the field on 2026-08-28.

    Returns True if the SSID was seen. False is not fatal: a hidden network
    never appears in a scan, so the caller still tries.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, out, _ = nmcli("-t", "-f", "DEVICE,STATE", "device")
        state = ""
        for line in (out or "").splitlines():
            f = nm_split(line)
            if len(f) >= 2 and f[0] == dev:
                state = f[1]
        if state and state not in ("unavailable", "unmanaged"):
            break
        time.sleep(1)

    nmcli("device", "wifi", "rescan", timeout=20)
    while time.time() < deadline:
        ok, out, _ = nmcli("-t", "-f", "SSID", "device", "wifi", "list")
        if ok and any(nm_split(l)[:1] == [ssid] for l in (out or "").splitlines()):
            return True
        time.sleep(1.5)
    return False


AUTH_HINTS = ("secrets were required", "no secrets provided",
              "802.1x supplicant", "authentication", "psk", "password")


def wifi_connect(ssid, password="", hidden=False):
    """Join a network, replacing any saved profile for it.

    The delete-first matters and is not tidiness. `nmcli device wifi connect`
    ACTIVATES AN EXISTING PROFILE when one exists for that SSID, and the
    password argument is then ignored -- so one typo becomes permanent: every
    later attempt re-activates the bad profile and fails identically, and the
    network cannot be re-entered from the screen. Deleting first means the
    password on screen is always the password that gets used.

    On failure any half-made profile is removed too, so a wrong password never
    lingers to be auto-retried in the background.
    """
    dev, _ = wifi_device()
    if not dev:
        return False, "no wifi device", False
    nmcli("radio", "wifi", "on")
    wifi_wait_ready(dev, ssid)

    for name in wifi_saved(ssid):
        nmcli("connection", "delete", name, timeout=20)

    args = ["device", "wifi", "connect", ssid, "ifname", dev]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    ok, out, err = nmcli(*args, timeout=60)
    detail = (err or out or "").strip()

    if not ok and "no network with ssid" in (detail or "").lower():
        # The scan had not caught up. Rescan properly and try once more,
        # rather than making someone type the password again for a reason
        # that had nothing to do with the password.
        print("ds: %s not visible yet; rescanning and retrying" % ssid,
              file=sys.stderr)
        nmcli("device", "wifi", "rescan", timeout=20)
        time.sleep(4)
        ok, out, err = nmcli(*args, timeout=60)
        detail = (err or out or "").strip()

    if not ok:
        # Do not leave a failed profile behind: NetworkManager will keep
        # retrying it, which is what made the screen flap between connecting
        # and connected.
        for name in wifi_saved(ssid):
            nmcli("connection", "delete", name, timeout=20)
        low = detail.lower()
        bad_password = any(h in low for h in AUTH_HINTS)
        if bad_password and not detail.lower().startswith("error: no network"):
            detail = "that password was not accepted"
        return False, detail, bad_password

    return True, detail, False


def wifi_saved(ssid):
    ok, out, _ = nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if not ok:
        return []
    return [nm_split(l)[0] for l in out.splitlines()
            if len(nm_split(l)) >= 2 and nm_split(l)[1] == "802-11-wireless"
            and (ssid is None or nm_split(l)[0] == ssid)]



# --------------------------------------------------------------------------
# ssh (authorized keys are ours to write; the sshd toggle goes through sudo)
# --------------------------------------------------------------------------

SSH_DIR = os.path.expanduser("~/.ssh")
AUTH_KEYS = os.path.join(SSH_DIR, "authorized_keys")
SSHD_DROPIN = "/etc/ssh/sshd_config.d/99-kiosk.conf"


def key_info(line):
    """(type, bits, fingerprint, comment) for one public key line, or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    try:
        p = subprocess.run(["ssh-keygen", "-l", "-f", "-"], input=line + "\n",
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        bits, fp, rest = (p.stdout.strip().split(" ", 2) + ["", ""])[:3]
        ktype = rest.rsplit("(", 1)[-1].rstrip(")") if "(" in rest else "?"
        comment = rest.rsplit("(", 1)[0].strip() if "(" in rest else rest.strip()
        return {"type": ktype, "bits": bits, "fingerprint": fp,
                "comment": comment if comment != "no comment" else ""}
    except Exception:                                         # noqa: BLE001
        return None


def read_auth_keys():
    try:
        with open(AUTH_KEYS) as fh:
            return [l.rstrip("\n") for l in fh if l.strip()
                    and not l.startswith("#")]
    except FileNotFoundError:
        return []


def write_auth_keys(lines):
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    tmp = AUTH_KEYS + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("".join(l.rstrip("\n") + "\n" for l in lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_KEYS)


def add_auth_key(key):
    info = key_info(key)
    if not info:
        return False, "that does not look like an SSH public key", None
    lines = read_auth_keys()
    for existing in lines:
        got = key_info(existing)
        if got and got["fingerprint"] == info["fingerprint"]:
            return False, "that key is already installed", info
    lines.append(key.strip())
    write_auth_keys(lines)
    return True, "key added", info


def remove_auth_key(fingerprint):
    lines = read_auth_keys()
    kept = [l for l in lines
            if (key_info(l) or {}).get("fingerprint") != fingerprint]
    if len(kept) == len(lines):
        return False, "no key with that fingerprint"
    write_auth_keys(kept)
    return True, "key removed"


def sshd_password_auth():
    """What our drop-in says; sshd's compiled default is yes."""
    try:
        with open(SSHD_DROPIN) as fh:
            return "no" not in fh.read().lower().split("passwordauthentication")[1]
    except (FileNotFoundError, IndexError):
        return True


def ssh_status():
    try:
        active = subprocess.run(["systemctl", "is-active", "ssh"],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except Exception:                                         # noqa: BLE001
        active = "unknown"
    host_keys = []
    for name in sorted(os.listdir("/etc/ssh")):
        if name.startswith("ssh_host_") and name.endswith("_key.pub"):
            try:
                with open("/etc/ssh/" + name) as fh:
                    info = key_info(fh.read())
                if info:
                    host_keys.append({"type": info["type"],
                                      "fingerprint": info["fingerprint"]})
            except Exception:                                 # noqa: BLE001
                pass
    keys = [k for k in (key_info(l) for l in read_auth_keys()) if k]
    return {
        "active": active,
        "user": os.environ.get("USER") or "kiosk",
        "password_auth": sshd_password_auth(),
        "host_keys": host_keys,
        "authorized_keys": keys,
    }


# --------------------------------------------------------------------------
# screen rotation (xrandr for the panel, xinput for the touchscreen)
# --------------------------------------------------------------------------

ROTATIONS = ("normal", "left", "right", "inverted")

# Rotating the panel without also transforming the touchscreen leaves taps
# landing in the wrong place, so the two always move together.
TOUCH_MATRIX = {
    "normal":   ("1", "0", "0", "0", "1", "0", "0", "0", "1"),
    "left":     ("0", "-1", "1", "1", "0", "0", "0", "0", "1"),
    "right":    ("0", "1", "0", "-1", "0", "1", "0", "0", "1"),
    "inverted": ("-1", "0", "1", "0", "-1", "1", "0", "0", "1"),
}


def x_run(*args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:                                  # noqa: BLE001
        return False, "", str(exc)


def connected_outputs():
    ok, out, _ = x_run("xrandr", "--query")
    if not ok:
        return []
    return [l.split()[0] for l in out.splitlines() if " connected" in l]


def touch_devices():
    """xinput ids of touchscreens, as (id, name).

    Selected by id, not by name: this panel's Goodix registers twice -- once
    as a slave pointer and once as a slave keyboard -- and `xinput set-prop`
    refuses an ambiguous name ("There are multiple devices matching ...").
    Only slave *pointers* carry a meaningful transformation matrix, so the
    keyboard twin is skipped.
    """
    ok, out, _ = x_run("xinput", "list", "--short")
    if not ok:
        return []
    found = []
    for line in out.splitlines():
        m = re.search(r"(.+?)\s+id=(\d+)\s+\[slave\s+pointer", line)
        if not m:
            continue
        name = m.group(1).strip(" \u21b3\u2192\u23a1\u23a2\u23a3\u239c\u239f|")
        if "touch" in name.lower():
            found.append((m.group(2), name))
    return found


def apply_rotation(rotation):
    """Rotate every connected output and match the touch matrix to it."""
    if rotation not in ROTATIONS:
        return False, "unknown rotation"
    problems = []
    outputs = connected_outputs()
    if not outputs:
        problems.append("no connected output found")
    for output in outputs:
        ok, _, err = x_run("xrandr", "--output", output, "--rotate", rotation)
        if not ok:
            problems.append(f"{output}: {err}")
    matrix = TOUCH_MATRIX[rotation]
    touched = touch_devices()
    if not touched:
        problems.append("no touchscreen found to transform")
    for dev_id, name in touched:
        ok, _, err = x_run("xinput", "set-prop", dev_id,
                           "Coordinate Transformation Matrix", *matrix)
        if not ok:
            problems.append(f"{name} (id {dev_id}): {err}")
    detail = "; ".join(problems) if problems else (
        "rotated %s; touch remapped on %s"
        % (rotation, ", ".join(n for _, n in touched)))
    return (not problems), detail


# --------------------------------------------------------------------------
# clock and boot splash (both need root, via the helpers in /usr/local/sbin)
# --------------------------------------------------------------------------

SPLASH_HELPER = "/usr/local/sbin/kiosk-splash"
SPLASH_IMAGE = "/usr/share/plymouth/themes/kiosk/splash.png"


_ZONE_CACHE = []

# The ones these devices will actually be in, so nobody has to scroll 600
# entries to pick the obvious answer.
COMMON_ZONES = [
    "America/Chicago", "America/New_York", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
    "Pacific/Honolulu", "UTC",
]


def timezones():
    """Every zone this system knows, from timedatectl. Cached: it never
    changes while we are running and the list is ~600 lines."""
    global _ZONE_CACHE
    if _ZONE_CACHE:
        return _ZONE_CACHE
    try:
        p = subprocess.run(["timedatectl", "list-timezones"],
                           capture_output=True, text=True, timeout=15)
        zones = [z.strip() for z in p.stdout.splitlines() if z.strip()]
    except Exception:                                         # noqa: BLE001
        zones = []
    if not zones:
        try:
            zones = sorted(
                os.path.relpath(os.path.join(root, f), "/usr/share/zoneinfo")
                for root, _, files in os.walk("/usr/share/zoneinfo")
                for f in files
                if not f.endswith((".tab", ".list")) and "/" in
                os.path.relpath(os.path.join(root, f), "/usr/share/zoneinfo"))
        except OSError:
            zones = []
    _ZONE_CACHE = zones or list(COMMON_ZONES)
    return _ZONE_CACHE


def time_status():
    out = {}
    try:
        p = subprocess.run(["timedatectl", "show"], capture_output=True,
                           text=True, timeout=10)
        for line in p.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    except Exception as exc:                                  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "timezone": out.get("Timezone", "?"),
        "ntp": out.get("NTP") == "yes",
        "synchronized": out.get("NTPSynchronized") == "yes",
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def set_time_settings(timezone=None, ntp=None):
    notes = []
    if timezone:
        if not re.fullmatch(r"[A-Za-z0-9+_/-]{1,64}", timezone):
            return False, "that does not look like a time zone"
        if not os.path.exists("/usr/share/zoneinfo/" + timezone):
            return False, "unknown time zone: " + timezone
        ok, out, err = run_root("timedatectl", "set-timezone", timezone)
        if ok:
            # This process cached the zone at startup, so without tzset() the
            # admin page would keep reporting the old one -- the system clock
            # is right but the readout lies.
            try:
                time.tzset()
            except AttributeError:                            # not on POSIX
                pass
        notes.append("timezone " + timezone if ok else "timezone failed: "
                     + (err or out))
    if ntp is not None:
        ok, out, err = run_root("timedatectl", "set-ntp",
                                "true" if ntp else "false")
        notes.append(("ntp on" if ntp else "ntp off") if ok
                     else "ntp failed: " + (err or out))
    if not notes:
        return False, "nothing to change"
    return (not any("failed" in n for n in notes)), "; ".join(notes)


def run_root(*args, timeout=30):
    try:
        p = subprocess.run(["sudo", "-n"] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:                                  # noqa: BLE001
        return False, "", str(exc)


def set_login_password(password):
    """Set the kiosk account's password (used for ssh and for sudo)."""
    password = (password or "").strip()
    if len(password) < 8:
        return False, "password must be at least 8 characters"
    try:
        p = subprocess.run(["sudo", "-n", "/usr/local/sbin/kiosk-passwd"],
                           input=password, capture_output=True, text=True,
                           timeout=20)
        ok = p.returncode == 0
        return ok, (p.stderr or p.stdout).strip() or ("done" if ok else "failed")
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc)


CEC_HELPER = "/usr/local/sbin/kiosk-cec"


PRINTER_HELPER = "/usr/local/sbin/kiosk-printer"
AUDIO_HELPER = "/usr/local/sbin/kiosk-audio"
OSUPDATE_HELPER = "/usr/local/sbin/kiosk-osupdate"
OS_COMMIT_GRACE = 120
DIAG_HELPER = "/usr/local/sbin/kiosk-diag"
DIAG_SECTIONS = ("fs", "boot", "disk", "net", "power", "update", "log", "all")


def diagnostics(section):
    """Read-only diagnostics from the helper.

    The section is checked against a fixed tuple before it goes anywhere near
    a command line: this runs through sudo, so an unvalidated argument here
    would be the whole ballgame.
    """
    if section not in DIAG_SECTIONS:
        return False, "unknown section"
    try:
        p = subprocess.run(["sudo", "-n", DIAG_HELPER, section],
                           capture_output=True, text=True, timeout=60)
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[:60000]


UPDATE_HELPER = "/usr/local/sbin/kiosk-update"
UPDATE_STATE = "/var/lib/kiosk/update-state.json"


def update_state():
    """What the last update run did, if anything has run."""
    try:
        with open(UPDATE_STATE) as f:
            return json.load(f)
    except Exception:                                         # noqa: BLE001
        return {}


def update_command(action):
    """Run kiosk-update through sudo. `check` is read-only; `apply` is not.

    Deliberately no timeout shorter than the helper's own: it downloads and
    checksums every changed component before installing anything, and killing
    it half way would leave the operator with no idea whether it had started.
    """
    if action not in ("check", "apply"):
        return False, "unknown action", {}
    cmd = ["sudo", "-n", UPDATE_HELPER, action]
    if action == "apply":
        cmd.append("--force")          # the button IS the human decision
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "timed out", {}
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc), {}
    out = (p.stdout or "").strip()
    if action == "check":
        try:
            return p.returncode == 0, "", json.loads(out)
        except Exception:                                     # noqa: BLE001
            return False, (p.stderr or out or "no output").strip()[:400], {}
    return p.returncode == 0, (out or p.stderr or "").strip()[:400], {}


TMT88V_PPD = ("/usr/share/cups/model/epson-thermal-printer/"
              "epson-tm-t88v-rastertotmt88v.ppd")
TMT88V_FILTER = "/usr/lib/cups/filter/rastertotmt88v"


def printer_status():
    """CUPS queues, and whether the TM-T88V driver is actually present.

    Checked rather than assumed: the card used to state flatly that the PPD
    was missing, which stayed on screen after the driver was installed.
    """
    st = {"cups": False, "queues": [], "default": "", "detail": "",
          "tmt88v": (os.path.exists(TMT88V_PPD)
                     and os.access(TMT88V_FILTER, os.X_OK))}
    try:
        p = subprocess.run(["lpstat", "-a"], capture_output=True, text=True,
                           timeout=10)
        st["cups"] = True
        st["queues"] = [l.split()[0] for l in p.stdout.splitlines() if l.strip()]
        d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True,
                           timeout=10).stdout
        st["default"] = d.split(":")[-1].strip() if ":" in d else ""
    except FileNotFoundError:
        st["detail"] = "cups-client not installed"
    except Exception as exc:                                  # noqa: BLE001
        st["detail"] = str(exc)
    return st


def printer_command(action, name=""):
    if action not in ("list", "detect", "test", "default", "add-tmt88v"):
        return False, "unknown action"
    args = [PRINTER_HELPER, action]
    if name:
        args.append(name)
    ok, out, err = run_root(*args, timeout=45)
    return ok, (err or out or "done")[-600:]


def cec_status():
    """Whether this machine has a usable HDMI-CEC adapter.

    Timed, because the honest answer on a machine with no adapter arrives in
    a couple of milliseconds and that looks like the button did nothing.

    Most x86 boards do not wire the CEC pin, so `adapters=0` is the normal
    answer and not a fault. A USB CEC adapter shows up here as /dev/cecN.
    """
    t0 = time.time()
    ok, out, err = run_root(CEC_HELPER, "detect")
    st = {"ok": ok, "tool": False, "adapters": 0, "devices": [],
          "took_ms": int((time.time() - t0) * 1000),
          "detail": (out or err)[-400:]}
    for line in out.splitlines():
        if line.startswith("tool="):
            st["tool"] = line.split("=", 1)[1] == "ok"
        elif line.startswith("adapters="):
            try:
                st["adapters"] = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("adapter="):
            st["devices"].append(line[len("adapter="):])
        elif line.startswith("hdmi="):
            st.setdefault("hdmi", []).append(line[len("hdmi="):])
    st["available"] = st["tool"] and st["adapters"] > 0
    return st


def cec_command(action):
    if action not in ("on", "off", "scan"):
        return False, "unknown action"
    ok, out, err = run_root(CEC_HELPER, action, timeout=30)
    return ok, (err or out or "done")[-400:]


def audio_status():
    """Where sound goes, how loud, and what else it could come out of.

    Volume is a software control. HDMI carries no mixer -- the Pipo's card
    exposes nothing but jack detect and IEC958 switches, and the HDA boxes
    are the same -- so there is no "Master" to move. kiosk-audio inserts an
    ALSA softvol plugin to create one.
    """
    ok, out, err = run_root(AUDIO_HELPER, "status", timeout=20)
    st = {"ok": ok, "card": 0, "device": 0, "volume": 0, "muted": False,
          "routed": False, "control": False, "outputs": [],
          "detail": (err or "")[-300:]}
    for line in out.splitlines():
        if line.startswith("output "):
            o = {}
            for field in line[len("output "):].split(" "):
                if "=" in field:
                    key, _, val = field.partition("=")
                    o[key] = val
            # the trailing name/label fields contain spaces, so re-parse them
            for key in ("name", "label"):
                m = re.search(key + r"=(.*?)(?= (?:name|label)=|$)", line)
                if m:
                    o[key] = m.group(1).strip()
            if "card" in o and "device" in o:
                o["id"] = "%s:%s" % (o["card"], o["device"])
                o["live"] = o.get("live") == "yes"
                st["outputs"].append(o)
        elif "=" in line:
            key, _, val = line.partition("=")
            if key in ("card", "device", "volume"):
                try:
                    st[key] = int(val)
                except ValueError:
                    pass
            elif key in ("routed", "control", "muted"):
                st[key] = val == "yes"
    st["current"] = "%s:%s" % (st["card"], st["device"])
    return st


def audio_set_output(target):
    """target is "auto" or "card:device"; anything else is refused here."""
    if target == "auto":
        ok, out, err = run_root(AUDIO_HELPER, "set-output", "auto", timeout=25)
        return ok, (err or out or "done")[-300:]
    if not re.fullmatch(r"\d{1,2}:\d{1,2}", target or ""):
        return False, "output must be auto or card:device"
    card, _, dev = target.partition(":")
    ok, out, err = run_root(AUDIO_HELPER, "set-output", card, dev, timeout=25)
    return ok, (err or out or "done")[-300:]


def audio_set_volume(level):
    try:
        level = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return False, "volume must be a number"
    ok, out, err = run_root(AUDIO_HELPER, "volume", str(level), timeout=25)
    return ok, (err or out or "done")[-300:]


def audio_set_mute(on):
    ok, out, err = run_root(AUDIO_HELPER, "mute", "on" if on else "off",
                            timeout=25)
    return ok, (err or out or "done")[-300:]


def audio_test():
    # The tone itself is under two seconds, but the first play on a machine
    # renders it, and the fallback path can sit through a 20 s speaker-test on
    # an output with nothing attached. The generous timeout is for those, not
    # for the tone.
    ok, out, err = run_root(AUDIO_HELPER, "test", timeout=30)
    return ok, (err or out or "done")[-300:]


# In the kiosk user's home, NOT /var/lib/kiosk. That directory is root-owned
# and kiosk.py runs unprivileged, so the write failed silently and the "once"
# guarantee never held: the first-boot update re-ran on every browser restart,
# fetching the manifest and reapplying the release each time. /home/kiosk is
# bind-mounted from /data, so this still survives an OS update.
FIRST_UPDATE_MARK = os.path.expanduser("~/.steadyscreen-first-update")


def first_boot_update():
    """Bring a brand new install up to the current release, once, by itself.

    An image is out of date the day after it is built, and the person doing the
    install is standing in a shop, not reading release notes. Waiting for the
    nightly window means a display runs old software all day for no reason --
    and the very first thing anyone does with a fresh machine is look at it.

    Once only, and recorded on /data so it survives a slot switch: after that
    the normal timer owns updates. Failures are not recorded, so a machine that
    was offline at first boot tries again the next time it starts.
    """
    if os.path.exists(FIRST_UPDATE_MARK):
        return
    deadline = time.time() + 1800
    while time.time() < deadline:
        time.sleep(20)
        if nm_connectivity() != "full":
            continue
        # The marker goes down BEFORE the update, not after. A successful
        # update replaces kiosk.py and restarts it, which kills this very
        # thread -- so "write it afterwards" never writes it, and the machine
        # would do a first-boot update on every boot for ever. If the update
        # fails, the nightly timer picks it up; that is what the timer is for.
        # Record it BEFORE updating, and refuse to update if it cannot be
        # recorded. A guard that silently fails to write is not a guard: this
        # one swallowed a permission error and turned "once, on first boot"
        # into "every time the browser restarts".
        try:
            with open(FIRST_UPDATE_MARK, "w") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        except Exception as exc:                               # noqa: BLE001
            print("ds: cannot record the first-boot update at %s (%s); "
                  "skipping it rather than repeating it on every restart. "
                  "The nightly timer will catch this client up."
                  % (FIRST_UPDATE_MARK, exc), file=sys.stderr)
            return
        print("ds: first boot is online -- catching up to the current release",
              file=sys.stderr)
        try:
            p = subprocess.run(["sudo", "-n", UPDATE_HELPER, "apply", "--force"],
                               capture_output=True, text=True, timeout=900)
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            print("ds: first-boot update: %s" % (out[-500:] or "no output"),
                  file=sys.stderr)
        except Exception as exc:                               # noqa: BLE001
            print("ds: first-boot update could not run: %s" % exc,
                  file=sys.stderr)
        return
    print("ds: no network within 30 minutes of first boot; "
          "leaving the update to the timer", file=sys.stderr)


def commit_os_slot():
    """Keep the slot we booted into, once this client is actually working.

    An A/B update arms one attempt at the other slot. If nothing commits it,
    the next boot returns to the committed slot -- which is the rollback, and
    is exactly right when the new system is broken. But it also means a
    *successful* update silently undoes itself unless something says so.

    The grace period is the whole point. Committing at startup would commit a
    slot that panics a minute later, and the machine would then have no way
    back. Waiting means the new system has to stay up before it is trusted.
    """
    try:
        time.sleep(OS_COMMIT_GRACE)
        p = subprocess.run(["sudo", "-n", OSUPDATE_HELPER, "commit"],
                           capture_output=True, text=True, timeout=30)
        out = (p.stdout or p.stderr).strip()
        if out and "already committed" not in out:
            print("ds: %s" % out, file=sys.stderr)
    except Exception as exc:                                  # noqa: BLE001
        print("ds: could not commit the boot slot: %s" % exc, file=sys.stderr)


def audio_apply(cfg):
    """Put the saved audio settings back after a boot or an update.

    Routing lives in /etc/asound.conf and survives, but the softvol level does
    not: the control is created when the device is first opened and starts at
    full scale, so a saved volume has to be written back or every reboot is a
    surprise for whoever is standing next to the screen.
    """
    try:
        ok, detail = audio_set_output(cfg.get("audio_output", "auto"))
        if not ok:
            print("ds: audio: could not route output: %s" % detail,
                  file=sys.stderr)
        if cfg.get("audio_muted"):
            audio_set_mute(True)
        else:
            audio_set_volume(cfg.get("volume", 60))
    except Exception as exc:                                  # noqa: BLE001
        print("ds: audio: apply failed: %s" % exc, file=sys.stderr)


def splash_status():
    ok, out, _ = run_root(SPLASH_HELPER, "status")
    st = {"enabled": False, "has_image": False, "detail": out}
    for line in out.splitlines():
        if line.startswith("enabled="):
            st["enabled"] = line.split("=", 1)[1].strip() == "yes"
        elif line.startswith("image="):
            st["has_image"] = line.split("=", 1)[1].strip() == "yes"
        elif line.startswith("busy="):
            st["busy"] = line.split("=", 1)[1].strip() == "yes"
    st["ok"] = ok
    return st


def set_splash(enabled):
    if enabled is None:
        return False, "nothing to change"
    ok, out, err = run_root(SPLASH_HELPER,
                            "enable" if enabled else "disable", timeout=300)
    return ok, (err or out or "done")[-400:]


def clear_splash_image():
    ok, out, err = run_root(SPLASH_HELPER, "clear-image", timeout=300)
    return ok, (err or out or "done")[-400:]


def set_splash_image(data):
    """Accept an uploaded image, normalise it, hand it to the helper."""
    if not data:
        return False, "empty upload"
    tmp = "/tmp/kiosk-splash-upload"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        # Normalise to PNG and cap the size: the splash lives in the initramfs
        # and a 12 MB phone photo there costs boot time on every start.
        try:
            from PIL import Image as PILImage
            im = PILImage.open(tmp)
            im = im.convert("RGB")
            im.thumbnail((1920, 1200))
            im.save(tmp + ".png", "PNG", optimize=True)
            src = tmp + ".png"
        except ImportError:
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                return False, "install python3-pil, or upload a PNG"
            src = tmp
        except Exception as exc:                              # noqa: BLE001
            return False, "could not read that image: %s" % exc
        ok, out, err = run_root(SPLASH_HELPER, "set-image", src, timeout=300)
        return ok, (err or out or "done")[-400:]
    finally:
        for f in (tmp, tmp + ".png"):
            try:
                os.unlink(f)
            except OSError:
                pass



# --------------------------------------------------------------------------
# screen resolution
# --------------------------------------------------------------------------

# Sizes worth offering, grouped so we only ever suggest ones with the same
# shape as the panel. Stretching a 16:9 image onto a 16:10 panel is exactly
# what "scale correctly" is meant to avoid.
COMMON_MODES = [
    (3840, 2160), (2560, 1440), (1920, 1080), (1600, 900), (1366, 768),
    (1280, 720),
    (2560, 1600), (1920, 1200), (1680, 1050), (1440, 900), (1280, 800),
    (1600, 1200), (1400, 1050), (1024, 768), (800, 600),
]


def display_info():
    """Connected outputs with their current, preferred and available modes."""
    ok, out, _ = x_run("xrandr", "--query")
    if not ok:
        return []
    outputs, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^(\S+) connected (primary )?(\d+)x(\d+)", line)
        if m:
            cur = {"name": m.group(1), "primary": bool(m.group(2)),
                   "current": "%sx%s" % (m.group(3), m.group(4)),
                   "preferred": None, "modes": []}
            outputs.append(cur)
            continue
        if re.match(r"^\S+ (dis)?connected", line):
            cur = None
            continue
        if cur is not None:
            m = re.match(r"^\s+(\d+)x(\d+)\s+(.*)$", line)
            if m:
                mode = "%sx%s" % (m.group(1), m.group(2))
                if mode not in cur["modes"]:
                    cur["modes"].append(mode)
                if "+" in m.group(3):
                    cur["preferred"] = mode
    for o in outputs:
        o["preferred"] = o["preferred"] or o["current"]
        o["suggested"] = suggested_modes(o)
    return outputs


def suggested_modes(output):
    """Preferred mode first, then smaller sizes of the same aspect ratio.

    Anything the output does not natively support is still offered: it is
    rendered at that size and scaled to the panel with --scale-from, which
    is how you get a clean 1080p desktop on a 4K screen.
    """
    try:
        pw, ph = (int(x) for x in output["preferred"].split("x"))
    except Exception:                                         # noqa: BLE001
        return []
    if not ph:
        return []
    aspect = pw / ph
    out = [{"mode": output["preferred"], "label":
            "Recommended (%s)" % output["preferred"], "native": True}]
    seen = {output["preferred"]}
    cands = [(w, h) for (w, h) in COMMON_MODES] + \
            [tuple(int(x) for x in m.split("x")) for m in output["modes"]]
    for w, h in sorted(set(cands), key=lambda t: -(t[0] * t[1])):
        mode = "%dx%d" % (w, h)
        if mode in seen or not h:
            continue
        if w > pw or h > ph:
            continue                       # never offer more than the panel has
        if w < 1024 or h < 600:
            continue                       # a stray tap should not make the
                                           # kiosk unreadable
        if abs((w / h) - aspect) > 0.02:
            continue                       # different shape: would distort
        seen.add(mode)
        native = mode in output["modes"]
        out.append({"mode": mode, "native": native,
                    "label": "%s%s" % (mode, "" if native else "  (scaled)")})
    return out


def apply_resolution(mode):
    """Set the resolution, natively when possible and scaled when not."""
    outputs = display_info()
    if not outputs:
        return False, "no connected output"
    problems = []
    for o in outputs:
        name = o["name"]
        if not mode or mode == "auto":
            ok, _, err = x_run("xrandr", "--output", name, "--auto",
                               "--scale", "1x1")
            if not ok:
                problems.append("%s: %s" % (name, err))
            continue
        if mode in o["modes"]:
            ok, _, err = x_run("xrandr", "--output", name, "--mode", mode,
                               "--scale", "1x1")
        else:
            # render at `mode` and let the GPU scale it onto the panel
            ok, _, err = x_run("xrandr", "--output", name,
                               "--mode", o["preferred"], "--scale-from", mode)
        if not ok:
            problems.append("%s: %s" % (name, err))
    if problems:
        return False, "; ".join(problems)
    return True, ("resolution set to %s" % (mode or "auto"))


# --------------------------------------------------------------------------
# error page
# --------------------------------------------------------------------------

# The boot splash is #12141a. Everything that can be on screen before the page
# paints uses the same value, so plymouth hands over to X and X hands over to
# the page with no visible seam.
SPLASH_HEX = "#12141a"
BG_CSS_NORMAL = b"window { background: #12141a; }"
BG_CSS_OFFLINE = b"window { background: #c0392b; }"
SPLASH_RGBA = Gdk.RGBA()
SPLASH_RGBA.parse(SPLASH_HEX)


WELCOME_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteadyScreen</title>
<!--FAVICON-->
<style>
 <!--BRANDCSS-->
 /* Read from across a room: this is on a display, not a laptop. Sizes scale
    with the viewport so a 15" panel and a 55" screen both look deliberate. */
 :root{color-scheme:dark;--bg:#12141a;--card:#1b1f27;--line:#2b313c;
   --fg:#eef1f5;--dim:#94a0b0;--acc:#4c8dff;--ok:#3ecf8e}
 *{box-sizing:border-box}
 html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--fg);
   font:400 1rem/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
   display:flex;align-items:center;justify-content:center;padding:4vmin;
   -webkit-user-select:none;user-select:none;position:relative;overflow:hidden}
 /* The boot splash, as the page's own mark rather than a watermark behind
    it. Full-bleed at center/contain it was the height of the screen and read
    straight through the cards; sized and placed at the top it is the same
    artwork doing the job the wordmark was doing badly, and the boot becomes
    one continuous image -- splash, then this. */
 .mark{display:block;margin:0 auto 1.2rem;max-width:min(780px,88vw);
   max-height:39vh;width:auto;height:auto}
 .wrap{position:relative;z-index:1;width:100%;max-width:1100px;text-align:center}
 /* Only shown if the splash image cannot be loaded -- a page with neither a
    mark nor a name would be anonymous, which is worse than plain. */
 h1{font-size:clamp(2rem,6vmin,4.2rem);margin:0 0 .4em;letter-spacing:-.02em}
 .lede{color:var(--dim);font-size:clamp(1rem,2.2vmin,1.5rem);margin:0 0 2em}
 .grid{display:flex;gap:3vmin;justify-content:center;flex-wrap:wrap}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
   padding:clamp(1rem,3vmin,2rem);min-width:min(420px,92vw);flex:1 1 380px}
 .label{color:var(--dim);text-transform:uppercase;letter-spacing:.09em;
   font-size:clamp(.7rem,1.3vmin,.9rem);margin-bottom:.7em}
 .addr{font-size:clamp(1.3rem,3.6vmin,2.6rem);font-weight:600;color:var(--acc);
   word-break:break-all;line-height:1.2}
 .hint{color:var(--dim);font-size:clamp(.8rem,1.5vmin,1rem);margin-top:.9em}
 /* The pairing code is not built yet. Showing the space it will occupy is
    honest and stops the page being redesigned around it later. */
 .code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
   font-size:clamp(1.4rem,4vmin,3rem);letter-spacing:.18em;color:var(--dim)}
 .soon{display:inline-block;border:1px dashed var(--line);border-radius:9px;
   padding:.5em .9em;color:var(--dim)}
 .foot{margin-top:3vmin;color:var(--dim);font-size:clamp(.75rem,1.4vmin,.95rem)}
 /* Dimmer than the footer: useful when someone asks what a display is
    running, not something a customer should read first. */
 .ver{margin-top:.5em;color:#5c6774;font-size:clamp(.72rem,1.25vmin,.9rem);
   font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
 .dot{width:.6em;height:.6em;border-radius:50%;background:var(--ok);
   display:inline-block;margin-right:.5em;vertical-align:middle}
</style></head><body><div class=wrap>

<img class=mark src="/api/splash/image.png" alt="SteadyScreen"
     onerror="this.style.display='none';document.getElementById('wordmark').style.display=''">
<h1 id=wordmark style="display:none"><!--BRAND--></h1>
<div class=lede><span class=dot></span><span id=lede>This display is ready.</span></div>

<div class=grid>
  <div class=card>
    <div class=label>Set it up from any device on this network</div>
    <div class=addr id=addr>&nbsp;</div>
    <div class=hint>Or press <b>Ctrl</b>+<b>Alt</b>+<b>A</b> on this screen.</div>
  </div>
  <div class=card>
    <div class=label>Link to an account</div>
    <div class=code><span class=soon id=code>coming soon</span></div>
    <div class=hint id=codehint>A code will appear here. Enter it at
      <b>pair.steadyscreen.com</b> to connect this display to your
      management account.</div>
  </div>
</div>

<div class=foot><span id=who>&nbsp;</span> &middot; steadyscreen.com</div>
<div class=ver id=ver>&nbsp;</div>
</div>
<script>
function esc(s){ return String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
async function tick(){
  let d; try { d = await (await fetch('/api/startup-state')).json(); } catch(e){ return; }
  // Prefer the address without a port -- it is the one a person can repeat
  // out loud. Fall back to whatever port is actually being served.
  const ports = d.ports || [];
  const p = ports.includes(80) ? 80 : (ports[0] || d.admin_port || 8080);
  const a = document.getElementById('addr');
  if(d.admin_ip){
    a.textContent = 'http://' + d.admin_ip + (p === 80 ? '' : ':' + p);
  } else {
    a.textContent = 'waiting for a network address';
  }
  document.getElementById('who').innerHTML = esc(d.client_name || '');
  const v = document.getElementById('ver');
  if(v){
    v.textContent = 'software ' + (d.software_version || '?')
                  + '   \u00b7   os ' + (d.os_version === undefined ? '?' : d.os_version);
  }
  const lede = document.getElementById('lede');
  // NOT keyed off phase. An unconfigured client can never reach 'ready' --
  // can_reach(None, None) is always false -- so this line permanently read
  // "Waiting for the network" on a machine that was connected and fine, next
  // to a green dot saying otherwise. The same mistake that kept this page from
  // appearing at all, left behind in its text when the gate was fixed.
  const dot = document.querySelector('.dot');
  const linked = !!d.admin_ip;
  if(!linked){
      lede.textContent = 'Waiting for a network connection.';
      if(dot) dot.style.background = '#d0902f';
  } else if(d.connectivity === 'none'){
      lede.textContent = 'On the network, but it has no route out yet.';
      if(dot) dot.style.background = '#d0902f';
  } else {
      lede.textContent = 'This display is ready. It has no page to show yet.';
      if(dot) dot.style.background = '';
  }
  if(d.pairing_code){
    const c = document.getElementById('code');
    c.className = ''; c.textContent = d.pairing_code;
    document.getElementById('codehint').innerHTML =
        'Enter this code at <b>pair.steadyscreen.com</b> to connect this '
        + 'display to your management account.';
  }
}
tick(); setInterval(tick, 5000);
</script></body></html>
"""


ERROR_PAGE = """<!doctype html><meta charset="utf-8">
<style>
 html,body{{height:100%;margin:0;background:#111;color:#eee;
   font:16px/1.5 system-ui,sans-serif;display:flex;align-items:center;
   justify-content:center;text-align:center}}
 .b{{max-width:34em;padding:2em}}
 h1{{font-size:1.4em;font-weight:600;margin:0 0 .6em}}
 code{{background:#222;padding:.15em .4em;border-radius:3px;word-break:break-all}}
 .m{{color:#888;margin-top:1.4em;font-size:.85em}}
</style>
<div class=b>
 <h1>Can't reach the page</h1>
 <p><code>{url}</code></p>
 <p style="color:#f0a">{error}</p>
 <p class=m>Retrying every {retry}s.<br>
 Admin: <code>http://{ip}:{port}/</code></p>
</div>"""


# --------------------------------------------------------------------------
# the client window
# --------------------------------------------------------------------------

class DisplayClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_error = ""
        self.last_rotation_detail = ""
        self.last_resolution_detail = ""
        self.last_cache_detail = ""
        self.last_permission = ""
        self.last_print = ""
        self._shot_sem = threading.Semaphore(1)
        self._shot_lock = threading.Lock()
        self._shot_cache = (None, 0.0, None)
        self.shot_cache_secs = float(cfg.get("screenshot_cache_secs", 1.5))
        self.load_ok = False
        # network phase: waiting-network -> waiting-site -> ready
        self.net_phase = "starting"
        self.started = time.time()
        # The offline fallback fires at most once per outage. Without
        # this it loops: falling back navigates, the load fails with no
        # network, the failure handler returns to the network page and
        # wakes the watcher immediately, and the watcher falls back
        # again -- the screen pulses between the two forever.
        self._fell_back = False
        self._net_wake = threading.Event()
        self.net_detail = {}
        self.showing_startup = False
        self.showing_welcome = False
        self.retry_id = None
        self.refresh_id = None
        self.idle_id = None
        self.last_input = time.time()

        self.window = Gtk.Window()
        self.window.set_title("display-system")
        self.window.set_decorated(False)
        self.window.connect("destroy", lambda *_: self.quit())

        geom = screen_geometry(self.window)
        self.window.set_default_size(geom.width, geom.height)
        self.window.move(0, 0)
        self.window.fullscreen()

        ctx = WebKit2.WebContext.get_default()
        ctx.set_cache_model(WebKit2.CacheModel.WEB_BROWSER)

        self.view = WebKit2.WebView()
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_write_console_messages_to_stdout(False)
        settings.set_media_playback_requires_user_gesture(False)
        settings.set_enable_page_cache(True)
        settings.set_javascript_can_open_windows_automatically(False)
        settings.set_hardware_acceleration_policy(
            WebKit2.HardwareAccelerationPolicy.ALWAYS
            if cfg["hardware_acceleration"]
            else WebKit2.HardwareAccelerationPolicy.NEVER
        )
        self.view.set_zoom_level(float(cfg["zoom"]))

        # no right-click menu, no popups: keep every navigation in this view
        self.view.connect("context-menu", lambda *_: True)
        self.view.connect("create", self._on_create)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("web-process-terminated", self._on_web_crash)
        self.view.connect("permission-request", self._on_permission)
        self.view.connect("print", self._on_print)

        # any input resets the idle timer
        self.window.add_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.TOUCH_MASK
        )
        self.window.connect("key-press-event", self._on_key)
        for sig in ("motion-notify-event", "button-press-event",
                    "touch-event"):
            self.window.connect(sig, self._on_input)

        # The view sits inside the window with a margin, and the window's own
        # background shows through it. That is the offline border: nothing is
        # injected into the page, so a site cannot style it away or collide
        # with it.
        #
        # The window background used to be red permanently, so that the margin
        # would be red when it appeared. But the window is painted before the
        # browser has anything to draw, so every boot flashed a full red screen
        # between the splash and the first page. It is the splash colour now
        # and only turns red while the border is actually wanted.
        self.offline = False
        self._css = Gtk.CssProvider()
        self._css.load_from_data(BG_CSS_NORMAL)
        Gtk.StyleContext.add_provider_for_screen(
            self.window.get_screen(), self._css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # ...and the same colour behind the page itself, so the gap before
        # WebKit paints is the splash colour rather than a white flash.
        try:
            self.view.set_background_color(SPLASH_RGBA)
        except Exception:                                     # noqa: BLE001
            pass

        self.window.add(self.view)
        self.window.show_all()
        self.window.present()

        # Cursor policy is decided per page, not once at startup. Deciding it
        # here raced USB enumeration: a mouse that finished enumerating a
        # moment after the browser started counted as "no pointer", the cursor
        # was blanked, and the mouse then worked invisibly on the setup screen
        # -- clicks landed, focus moved, and nothing could be seen. Reported
        # from the field on 2026-08-29.
        self.apply_cursor()

        # a rotation or resolution saved earlier has to be re-applied on
        # every start; X comes up at the panel default otherwise
        if cfg.get("resolution", "auto") not in ("", "auto"):
            apply_resolution(cfg["resolution"])
        if cfg.get("rotation", "normal") != "normal":
            apply_rotation(cfg["rotation"])
        if (cfg.get("rotation", "normal") != "normal"
                or cfg.get("resolution", "auto") not in ("", "auto")):
            GLib.timeout_add(800, self._refit)

        # Routing and volume, off the main loop: applying them means running
        # aplay and amixer, and a page that has to wait several seconds for
        # sound it may never play is a worse trade than sound arriving a
        # moment after the screen does.
        threading.Thread(target=audio_apply, args=(cfg,), daemon=True,
                         name="audio").start()

        # If this boot was an A/B attempt, keep it -- but only after it has
        # proven it can stay up. See commit_os_slot().
        global OS_COMMIT_GRACE
        OS_COMMIT_GRACE = int(cfg.get("os_commit_grace", 120) or 120)
        # A new install catches itself up as soon as it has a network. The
        # updater replaces kiosk.py and restarts it, which on a first boot
        # disturbs nobody.
        threading.Thread(target=first_boot_update, daemon=True,
                         name="first-boot-update").start()
        threading.Thread(target=commit_os_slot, daemon=True,
                         name="os-commit").start()
        self.window.get_screen().connect("size-changed",
                                         lambda *_: self._refit())

        # Do not load the page until something can actually answer. Booting
        # straight into "can't reach the page" while wifi is still associating
        # is technically true and completely useless to whoever is standing
        # in front of it.
        if cfg.get("clear_cache_on_start", False):
            self.clear_cache(everything=False, then_reload=False)

        if is_unconfigured(cfg):
            # Fresh install: show the setup page and keep watching the network,
            # so the address on screen is right once it has one.
            self.show_startup("Not set up yet")
            threading.Thread(target=self._net_loop, daemon=True,
                             name="kiosk-net").start()
        elif cfg.get("wait_for_network", True):
            self.show_startup("Starting up")
            threading.Thread(target=self._net_loop, daemon=True,
                             name="kiosk-net").start()
        else:
            self.navigate(cfg["url"])
        self._arm_refresh()
        self._arm_idle()

    # -- events ------------------------------------------------------------

    def _on_input(self, *_):
        self.last_input = time.time()
        return False

    def _on_key(self, widget, event):
        """Ctrl+Alt+A toggles the admin page.

        The device has no browser chrome, so this is the only way to reach the
        admin page from the machine itself when there is no second computer
        on the LAN. One key both ways: a separate "go back" shortcut is one
        more thing to remember and to print on a card taped to the counter.
        """
        self.last_input = time.time()
        want = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        if event.state & want != want:
            return False
        name = (Gdk.keyval_name(event.keyval or 0) or "").lower()
        if name == "a":
            admin = "http://127.0.0.1:%d/" % int(self.cfg["admin_port"])
            here = self.view.get_uri() or ""
            if here.startswith("http://127.0.0.1:%d" % int(self.cfg["admin_port"])):
                self.navigate(self.cfg["url"])       # already there: go back
            else:
                self.view.load_uri(admin)
            return True
        return False

    def _on_create(self, view, nav_action):
        """A target=_blank / window.open(): load it here instead."""
        req = nav_action.get_request()
        if req is not None:
            GLib.idle_add(self.navigate, req.get_uri())
        return None

    def _on_load_changed(self, view, event):
        if event == WebKit2.LoadEvent.FINISHED and self.load_ok:
            self._cancel_retry()
            self._inject_osk()

    def _inject_osk(self):
        """Put the on-screen keyboard into the page that just loaded.

        Only somebody else's page: our own built-in screens have their own
        keyboard and would end up with two.

        Injected per load rather than through a UserContentManager because the
        WebView is already built by the time we know the setting, and because a
        page that reloads itself -- which a scanner page does -- gets it again
        without any extra machinery. The script makes itself idempotent.
        """
        if self.showing_startup or self.showing_welcome:
            return
        if not osk_wanted(self.cfg):
            return
        js = osk_script()
        if not js:
            return
        mode = str(self.cfg.get("osk_caps", "lock")).lower()
        if mode not in ("lock", "first", "off"):
            mode = "lock"
        # Set before the script runs, so the first keyboard shown already has
        # the right case rather than correcting itself a moment later.
        try:
            ox = int(self.cfg.get("osk_offset_x", 0) or 0)
            oy = int(self.cfg.get("osk_offset_y", 0) or 0)
        except (TypeError, ValueError):
            ox = oy = 0
        js = ("window.__dsOskCaps=%r;window.__dsOskOffset={x:%d,y:%d};\n%s"
              % (mode, ox, oy, js))
        try:
            self.view.run_javascript(js, None, None, None)
        except Exception as exc:                               # noqa: BLE001
            print("ds: could not inject the on-screen keyboard: %s" % exc,
                  file=sys.stderr)

    def _on_load_failed(self, view, event, failing_uri, error):
        ignorable = (
            (WebKit2.NetworkError.quark(), WebKit2.NetworkError.CANCELLED),
            (WebKit2.PolicyError.quark(),
             WebKit2.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE),
        )
        if any(error.matches(q, c) for q, c in ignorable):
            return False
        self.load_ok = False
        self.last_error = error.message
        # Distinguish "the network is not up yet" from "the site is broken".
        # Only the second one is the user's problem to read about.
        if self.cfg.get("wait_for_network", True):
            # load-failed means a network-level problem (DNS, refused, TLS,
            # timeout); an HTTP 404 or 500 still renders a page. So the
            # network page is the honest thing to show, with the reason on it.
            self.show_startup("load failed")
            self._net_wake.set()
            self._arm_retry()
        else:
            self._show_error(failing_uri, error.message)
            self._arm_retry()
        return True                     # we rendered our own page

    def _on_permission(self, view, request):
        """WebKit denies permission requests unless something answers them.

        Only geolocation is ever granted, and only when switched on. Anything
        else (camera, microphone, notifications) is refused outright: a device
        has no business granting those to a page.
        """
        if isinstance(request, WebKit2.GeolocationPermissionRequest):
            if self.cfg.get("allow_location", False):
                request.allow()
                self.last_permission = "location: allowed"
            else:
                request.deny()
                self.last_permission = "location: denied (off in settings)"
        else:
            request.deny()
            self.last_permission = "%s: denied" % type(request).__name__
        print("ds: %s" % self.last_permission, flush=True)
        return True

    def _on_print(self, view, print_operation):
        """window.print() must produce a receipt, not a dialog.

        Returning True tells WebKit we have handled printing ourselves, which
        suppresses the print dialog entirely. Margins are forced to zero and
        the paper is a continuous roll; WebKitGTK adds no header or footer of
        its own, so there is nothing to switch off there.
        """
        if not self.cfg.get("print_enabled", True):
            print("ds: print request ignored (printing disabled)",
                  file=sys.stderr)
            return True
        try:
            width_mm = float(self.cfg.get("print_width_mm", 80) or 80)
            margin = float(self.cfg.get("print_margins_mm", 0) or 0)

            paper = Gtk.PaperSize.new_custom(
                "kiosk-roll", "Receipt roll",
                width_mm, 3276.0, Gtk.Unit.MM)      # long roll, cut by the printer
            setup = Gtk.PageSetup()
            setup.set_paper_size(paper)
            for edge in ("top", "bottom", "left", "right"):
                getattr(setup, "set_%s_margin" % edge)(margin, Gtk.Unit.MM)
            setup.set_orientation(Gtk.PageOrientation.PORTRAIT)
            print_operation.set_page_setup(setup)

            settings = Gtk.PrintSettings()
            name = (self.cfg.get("printer") or "").strip()
            if name:
                settings.set_printer(name)
            settings.set_use_color(False)
            print_operation.set_print_settings(settings)

            # print(), not run_dialog(): no window, no questions
            print_operation.print_()
            self.last_print = "sent to %s" % (name or "the default printer")
        except Exception as exc:                              # noqa: BLE001
            self.last_print = "print failed: %s" % exc
            print("ds: %s" % self.last_print, file=sys.stderr)
        return True

    def _on_web_crash(self, view, reason):
        print(f"ds: web process died ({reason}); reloading", file=sys.stderr)
        GLib.timeout_add_seconds(2, lambda: (self.navigate(self.cfg["url"]),
                                             False)[1])

    # -- network watch -----------------------------------------------------

    def _net_loop(self):
        """Off the main loop: nmcli and socket calls both block."""
        while True:
            host, port = target_endpoint(self.cfg["url"])
            state = {
                "connectivity": nm_connectivity(),
                "net": net_status(),
                "target_host": host,
                "target_port": port,
            }
            has_link = any(
                (state["net"].get(k) or {}).get("state") == "connected"
                for k in ("wired", "wifi"))
            state["has_link"] = has_link
            state["reachable"] = can_reach(host, port) if has_link else False
            GLib.idle_add(self._on_net_state, state)
            # Sleep, but wake immediately when the URL changes or a load
            # fails, so state is never decided from a 30-second-old check.
            self._net_wake.wait(5 if not state["reachable"] else 30)
            self._net_wake.clear()

    def _on_net_state(self, state):
        self.net_detail = state
        if state["reachable"]:
            phase = "ready"
        elif state["has_link"]:
            phase = "waiting-site"
        else:
            phase = "waiting-network"
        changed = phase != self.net_phase
        self.net_phase = phase
        self.set_offline(phase != "ready")

        if is_unconfigured(self.cfg):
            # Nothing to go to. Which built-in page depends on what is wrong:
            # no network means the network page, where wifi can be joined;
            # a working network with no page configured means the branded
            # welcome screen, which is what a customer would see standing in
            # front of a display that has just been plugged in.
            # "ready" means the *configured site* answered, and an
            # unconfigured client has no site -- can_reach(None, None) is
            # always false, so phase can never reach "ready" here and the
            # welcome page was unreachable by construction. What matters with
            # no URL is simply whether this machine is on a network.
            if state.get("has_link"):
                if not self.showing_welcome:
                    self.show_welcome()
            elif not self.showing_startup:
                self.show_startup("Network lost")
            return False

        if phase == "ready" and (self.showing_startup or changed):
            self._fell_back = False          # a new outage gets a new attempt
            if self.showing_startup:
                self.showing_startup = False
                self.navigate(self.cfg["url"])
        elif phase != "ready" and self.showing_startup:
            # A menu board that reboots during an outage must not sit on the
            # network page until someone fixes the internet. After a grace
            # period, load the page anyway: WebKit serves what it has cached,
            # so the board comes back showing yesterday's menu rather than a
            # diagnostic screen nobody in the building can act on.
            grace = int(self.cfg.get("offline_after_seconds", 45) or 0)
            if (grace > 0 and not self._fell_back
                    and (time.time() - self.started) > grace):
                # Once only. If nothing is cached the load fails, the failure
                # handler puts the network page back, and we stay there until
                # the site actually answers -- which is the honest outcome,
                # rather than flickering between the two.
                self._fell_back = True
                print("ds: no network after %ds; trying the cached page once"
                      % grace, file=sys.stderr)
                self.showing_startup = False
                self.navigate(self.cfg["url"])
        return False

    def set_offline(self, off):
        """Show or hide the offline border. Cheap and idempotent.

        Never while the built-in page is up. That page already says what the
        network is doing in words, so the border adds nothing -- and it costs
        something real: the border is the window showing through a margin, so
        before WebKit has painted, the window *is* the border. A boot into the
        network page therefore flashed a full red screen, and an unconfigured
        client sat behind a permanent red edge with no page to be offline
        from. Measured in QEMU at 130s: 100% of the frame was #c0392b.
        """
        off = bool(off) and bool(self.cfg.get("offline_border", True))
        if (getattr(self, "showing_startup", False)
                or getattr(self, "showing_welcome", False)):
            off = False
        if off == getattr(self, "offline", None):
            return
        self.offline = off
        m = 2 if off else 0
        for setter in ("set_margin_start", "set_margin_end",
                       "set_margin_top", "set_margin_bottom"):
            getattr(self.view, setter)(m)
        # Red only while the border is showing; otherwise the splash colour,
        # so nothing red is ever on screen at boot.
        try:
            self._css.load_from_data(BG_CSS_OFFLINE if off else BG_CSS_NORMAL)
        except Exception:                                     # noqa: BLE001
            pass

    def show_welcome(self):
        """The branded page a display shows when it has no page of its own.

        Deliberately not the network diagnostic screen. That one is for
        fixing a problem; this is what a customer sees on a display that was
        plugged in and works, and it carries the address to set it up -- plus
        the space where the account pairing code will go.
        """
        self.showing_welcome = True
        self.showing_startup = False
        self.load_ok = True
        self.set_offline(False)
        self.apply_cursor()
        self.view.load_uri("http://127.0.0.1:%d/welcome"
                           % int(self.cfg["admin_port"]))
        return False

    def show_startup(self, _reason=""):
        """Put the built-in network/status page on screen."""
        self.showing_welcome = False
        self.showing_startup = True
        self.load_ok = True
        self.apply_cursor()
        # Drop the border now rather than at the next poll, which can be 30 s
        # away -- otherwise going back to this page leaves a red edge framing a
        # screen that is explaining the problem in words anyway.
        self.set_offline(False)
        self.view.load_uri("http://127.0.0.1:%d/startup"
                           % int(self.cfg["admin_port"]))
        return False

    # -- actions -----------------------------------------------------------

    def apply_cursor(self):
        """Show the pointer on any page a person is meant to touch.

        On a configured display showing somebody's page, a visible arrow in the
        corner is clutter and there is usually nothing to click. On our own
        setup and welcome screens there is: they exist to be used, by whoever
        is standing in front of the machine with a mouse. Re-checked on every
        page change, so a mouse plugged in later is not treated as absent for
        ever.
        """
        try:
            win = self.window.get_window()
            if win is None:
                return
            builtin = self.showing_startup or self.showing_welcome
            hide = (self.cfg.get("hide_cursor", True)
                    and not builtin
                    and not input_devices()["pointer"])
            if hide:
                cur = Gdk.Cursor.new_for_display(Gdk.Display.get_default(),
                                                 Gdk.CursorType.BLANK_CURSOR)
            else:
                cur = None                      # None restores the default
            win.set_cursor(cur)
        except Exception:                                      # noqa: BLE001
            pass

    def navigate(self, url):
        url = normalise_url(url)
        self._cancel_retry()
        if not url.startswith("http://127.0.0.1"):
            self.showing_startup = False
            # And the welcome page. It was only ever cleared by show_startup(),
            # so after a client was configured from /welcome the flag stayed
            # true -- which also suppresses the offline border, because that is
            # deliberately hidden while a built-in page is up. A crash on an
            # unconfigured client was worse: normalise_url("") is about:blank,
            # which never fires load-failed, so nothing recovered and the screen
            # stayed blank with the client reporting healthy.
            self.showing_welcome = False
        GLib.idle_add(self.apply_cursor)
        self.load_ok = True
        self.last_error = ""
        self.view.load_uri(url)
        return False

    def clear_cache(self, everything=False, then_reload=True):
        """Purge WebKit's stored data.

        A browser restart does NOT do this: the cache is on disk under
        ~/.cache and outlives both the process and the machine rebooting.
        `everything` also drops cookies, localStorage and HSTS state.
        """
        try:
            dm = WebKit2.WebContext.get_default().get_website_data_manager()
            types = (WebKit2.WebsiteDataTypes.ALL if everything else
                     (WebKit2.WebsiteDataTypes.DISK_CACHE
                      | WebKit2.WebsiteDataTypes.MEMORY_CACHE))

            def done(mgr, result):
                try:
                    mgr.clear_finish(result)
                    self.last_cache_detail = ("cleared everything" if everything
                                              else "cleared the cache")
                except Exception as exc:                      # noqa: BLE001
                    self.last_cache_detail = "clear failed: %s" % exc
                print("ds: %s" % self.last_cache_detail, flush=True)
                if then_reload:
                    GLib.timeout_add(400, lambda: (self.reload(True), False)[1])

            dm.clear(types, 0, None, done)
            self.last_cache_detail = "clearing..."
        except Exception as exc:                              # noqa: BLE001
            self.last_cache_detail = "clear failed: %s" % exc
            print("ds: %s" % self.last_cache_detail, file=sys.stderr)
        return False

    def reload(self, bypass_cache=False):
        if not self.load_ok:                # sitting on our own error page
            return self.navigate(self.cfg["url"])
        if bypass_cache:
            self.view.reload_bypass_cache()
        else:
            self.view.reload()
        return False

    def set_url(self, url):
        self.cfg["url"] = normalise_url(url)
        save_config(self.cfg)
        self.net_phase = "starting"
        self._net_wake.set()
        return self.navigate(self.cfg["url"])

    def _refit(self):
        """Resize to the current screen geometry (there is no WM to do it)."""
        geom = screen_geometry(self.window)
        self.window.move(0, 0)
        self.window.resize(geom.width, geom.height)
        self.window.fullscreen()
        return False

    def persist_resolution(self, mode, detail):
        self.cfg["resolution"] = mode
        save_config(self.cfg)
        self.last_resolution_detail = detail
        GLib.timeout_add(800, self._refit)
        return False

    def persist_rotation(self, rotation, detail):
        self.cfg["rotation"] = rotation
        save_config(self.cfg)
        self.last_rotation_detail = detail
        GLib.timeout_add(800, self._refit)
        return False

    def capture(self, result, done, max_width=1024, fmt="png", quality=70):
        """Grab what is actually on screen, as PNG bytes. Main thread only."""
        try:
            root = Gdk.get_default_root_window()
            w, h = root.get_width(), root.get_height()
            pb = Gdk.pixbuf_get_from_window(root, 0, 0, w, h)
            if pb is None:
                raise RuntimeError("could not read the root window")
            if w > max_width:
                pb = pb.scale_simple(max_width, max(1, int(h * max_width / w)),
                                     GdkPixbuf.InterpType.BILINEAR)
            if fmt == "jpeg":
                ok, buf = pb.save_to_bufferv("jpeg", ["quality"],
                                             [str(int(quality))])
            else:
                ok, buf = pb.save_to_bufferv("png", [], [])
            result.append(bytes(buf) if ok else None)
        except Exception as exc:                              # noqa: BLE001
            print("ds: screenshot failed: %s" % exc, file=sys.stderr)
            result.append(None)
        finally:
            done.set()
        return False

    def screenshot(self, timeout=20, max_width=1024, fmt="png", quality=70):
        """Grab the screen, but never more than one grab at a time.

        The grab is a synchronous full-screen XGetImage on the GTK main loop.
        Anything else reading the same display -- a remote-control agent, for
        instance -- is competing for X, and overlapping grabs are how that
        turns into a freeze. So: one at a time, and a very short cache so
        several viewers (or a viewer plus a polling agent) cost the same as
        one.
        """
        key = (max_width, fmt, quality)
        now = time.time()
        with self._shot_lock:
            cached, when, ckey = self._shot_cache
            if cached and ckey == key and (now - when) < self.shot_cache_secs:
                return cached

        # Only one thread may hold the capture at a time; the others wait
        # briefly and then take whatever the winner just produced.
        if not self._shot_sem.acquire(timeout=timeout):
            with self._shot_lock:
                return self._shot_cache[0]
        try:
            result, done = [], threading.Event()
            GLib.idle_add(self.capture, result, done, max_width, fmt, quality)
            if not done.wait(timeout):
                return None
            img = result[0] if result else None
            if img:
                with self._shot_lock:
                    self._shot_cache = (img, time.time(), key)
            return img
        finally:
            self._shot_sem.release()

    def set_zoom(self, zoom):
        self.cfg["zoom"] = max(0.25, min(5.0, float(zoom)))
        save_config(self.cfg)
        self.view.set_zoom_level(self.cfg["zoom"])
        return False

    def set_option(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        if key == "refresh_seconds":
            self._arm_refresh()
        elif key == "idle_reset_seconds":
            self._arm_idle()
        elif key == "offline_border":
            # Re-evaluate now rather than at the next network poll, which can
            # be 30 s away: a setting that appears to do nothing for half a
            # minute reads as broken.
            want = self.net_phase != "ready"
            self.offline = None
            self.set_offline(want)
        return False

    def quit(self, code=0):
        Gtk.main_quit()
        # the session wrapper restarts us
        os._exit(code)

    def _show_error(self, url, message):
        page = ERROR_PAGE.format(
            url=url, error=message, retry=self.cfg["retry_seconds"],
            ip=primary_ip(), port=self.cfg["admin_port"])
        self.view.load_alternate_html(page, url, None)

    # -- timers ------------------------------------------------------------

    def _arm_retry(self):
        self._cancel_retry()
        secs = int(self.cfg["retry_seconds"]) or 15

        def retry():
            # Only retry from here when we are not already parked on the
            # network page -- there the watcher owns the navigation.
            if not self.showing_startup:
                self.navigate(self.cfg["url"])
            else:
                self._net_wake.set()
            return True

        self.retry_id = GLib.timeout_add_seconds(secs, retry)

    def _cancel_retry(self):
        if self.retry_id:
            GLib.source_remove(self.retry_id)
            self.retry_id = None

    def _arm_refresh(self):
        if self.refresh_id:
            GLib.source_remove(self.refresh_id)
            self.refresh_id = None
        secs = int(self.cfg["refresh_seconds"] or 0)
        if secs > 0:
            self.refresh_id = GLib.timeout_add_seconds(
                secs, lambda: (self.reload(), True)[1])

    def _arm_idle(self):
        if self.idle_id:
            GLib.source_remove(self.idle_id)
            self.idle_id = None
        if int(self.cfg["idle_reset_seconds"] or 0) > 0:
            self.idle_id = GLib.timeout_add_seconds(5, self._idle_tick)

    def _idle_tick(self):
        limit = int(self.cfg["idle_reset_seconds"] or 0)
        if limit <= 0:
            self.idle_id = None
            return False
        if time.time() - self.last_input > limit:
            current = self.view.get_uri() or ""
            if current.rstrip("/") != self.cfg["url"].rstrip("/"):
                self.navigate(self.cfg["url"])
            self.last_input = time.time()
        return True

    # -- status ------------------------------------------------------------

    def status(self):
        return {
            "version": software_version(),
            # Two versions, deliberately. The software moves often and arrives
            # as components; the OS is a plain integer that moves only when the
            # root filesystem must change, and arrives as a whole slot. One
            # number for both made an OS that simply had not been rebuilt look
            # like it was behind.
            "software_version": software_version(),
            "os_version": os_version(),
            # The admin page needs this to tell "no page chosen yet" apart from
            # "the chosen page is not answering". Without it a fresh install
            # reports a fault for doing exactly what it should.
            "configured": not is_unconfigured(self.cfg),
            "client_id": IDENTITY.get("id", ""),
            "identity": identity_check(),
            "client_name": IDENTITY.get("name", "") or socket.gethostname(),
            # Deprecated aliases, kept so an older admin page or script does
            # not break mid-upgrade. Remove once the server ships.
            "kiosk_id": IDENTITY.get("id", ""),
            "kiosk_name": IDENTITY.get("name", "") or socket.gethostname(),
            "configured_url": self.cfg["url"],
            "current_url": self.view.get_uri() or "",
            "title": self.view.get_title() or "",
            "loading": self.view.is_loading(),
            "last_error": self.last_error,
            "zoom": self.cfg["zoom"],
            "refresh_seconds": self.cfg["refresh_seconds"],
            "idle_reset_seconds": self.cfg["idle_reset_seconds"],
            "hardware_acceleration": self.cfg["hardware_acceleration"],
            "rotation": self.cfg.get("rotation", "normal"),
            "rotation_detail": self.last_rotation_detail,
            "resolution": self.cfg.get("resolution", "auto"),
            "resolution_detail": self.last_resolution_detail,
            "net_phase": self.net_phase,
            "offline": bool(getattr(self, "offline", False)),
            "offline_after_seconds": self.cfg.get("offline_after_seconds", 45),
            "offline_border": self.cfg.get("offline_border", True),
            "splash_managed": self.cfg.get("splash_managed", True),
            "input": input_devices(),
            "update": update_state(),
            "showing_startup": self.showing_startup,
            "wait_for_network": self.cfg.get("wait_for_network", True),
            "clear_cache_on_start": self.cfg.get("clear_cache_on_start", False),
            "cache_detail": self.last_cache_detail,
            "allow_location": self.cfg.get("allow_location", False),
            "osk": self.cfg.get("osk", "auto"),
            "osk_caps": self.cfg.get("osk_caps", "lock"),
            "osk_offset_x": self.cfg.get("osk_offset_x", 0),
            "osk_offset_y": self.cfg.get("osk_offset_y", 0),
            "last_permission": self.last_permission,
            "print_enabled": self.cfg.get("print_enabled", True),
            "printer": self.cfg.get("printer", ""),
            "last_print": self.last_print,
            "admin_protected": bool(self.cfg.get("admin_password")),
            "browser_uptime": int(time.time() - STARTED),
            "system_uptime": system_uptime(),
            "ip": primary_ip(),
            "hostname": socket.gethostname(),
            "load": os.getloadavg()[0],
            "mem_free_mb": mem_available_mb(),   # kept: MemAvailable
            "memory": meminfo(),
        }


def screen_geometry(window):
    """Size of the monitor this window is on, without the deprecated API."""
    display = window.get_display()
    monitor = (display.get_primary_monitor()
               or display.get_monitor_at_window(window.get_screen().get_root_window())
               or display.get_monitor(0))
    return monitor.get_geometry()


def system_uptime():
    try:
        with open("/proc/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except Exception:                                         # noqa: BLE001
        return 0


def meminfo():
    """Memory in MB.

    `available` is MemAvailable, not MemFree: what a new program could take
    without swapping. MemFree on a healthy Linux box is always small because
    the kernel keeps the page cache full, so reporting it would look alarming
    and mean nothing. `used` is total - available, which is the number that
    matches what people expect.
    """
    vals = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                try:
                    vals[k] = int(rest.split()[0]) // 1024
                except (IndexError, ValueError):
                    pass
    except OSError:
        return {"total": 0, "available": 0, "used": 0,
                "swap_total": 0, "swap_used": 0}
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    swap_total = vals.get("SwapTotal", 0)
    swap_free = vals.get("SwapFree", 0)
    return {
        "total": total,
        "available": avail,
        "used": max(0, total - avail),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


def mem_available_mb():
    return meminfo()["available"]


# --------------------------------------------------------------------------
# admin HTTP server
# --------------------------------------------------------------------------

ADMIN_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteadyScreen Admin</title>
<!--FAVICON-->
<style>
 <!--BRANDCSS-->
 :root{color-scheme:dark;--bg:#15171a;--card:#1e2126;--line:#2e333a;
   --fg:#e8eaed;--dim:#9aa3ad;--acc:#4c8dff;--ok:#3ecf8e;--warn:#ffb454}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
   font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:1.2rem}
 /* One column at about tablet-portrait width. Every card is then exactly
    the same width, which is most of what "uniform" means here, and it reads
    well on the device's own touchscreen too. */
 .wrap{max-width:780px;margin:0 auto}
 /* tabs: ten stacked cards was too much to scroll through */
 .tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 1.1rem;
   border-bottom:1px solid var(--line);padding-bottom:.7rem}
 .tabs button{flex:0 0 auto;min-width:0;min-height:2.6rem;padding:.5rem 1.1rem;
   background:transparent;border:1px solid transparent;border-radius:8px;
   color:var(--dim);font-size:.95rem}
 .tabs button:hover{color:var(--fg);background:#22262d}
 .tabs button.on{background:#22262d;border-color:var(--line);color:var(--fg);
   font-weight:600}
 .cards{display:grid;gap:1rem;grid-template-columns:1fr}
 .cards .card{margin:0}
 .strip{display:grid;gap:.5rem;margin:.2rem 0 1.1rem;
   grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
 .stat{background:var(--card);border:1px solid var(--line);border-radius:9px;
   padding:.55rem .7rem;min-width:0}
 .stat .k{display:block;font-size:.7rem;text-transform:uppercase;
   letter-spacing:.05em;color:var(--dim);margin-bottom:.15rem}
 .stat .v{display:block;font-size:.95rem;font-weight:600;
   overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .banner{grid-column:1/-1;background:#3a2f1d;border:1px solid #5b4a2b;
   color:var(--warn);border-radius:9px;padding:.55rem .7rem;font-size:.85rem}
 h1{font-size:1.15rem;margin:0 0 .2rem;font-weight:600}
 .sub{color:var(--dim);font-size:.85rem;margin-bottom:1.2rem}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:1.1rem 1.15rem;margin-bottom:1rem}
 .card > h2:first-child{margin-top:0}
 .card dl + .row, .card dl + .grid2{margin-top:.9rem}
 label{display:block;font-size:.8rem;color:var(--dim);margin-bottom:.35rem;
   text-transform:uppercase;letter-spacing:.04em}
 input[type=text],input[type=number]{width:100%;padding:.7rem .8rem;
   background:#111316;border:1px solid var(--line);border-radius:7px;
   color:var(--fg);font:inherit}
 input:focus{outline:none;border-color:var(--acc)}
 .row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:.8rem}
 /* equal-width buttons so a row of two and a row of four still line up */
 button{flex:1 1 0;min-width:8.5rem;min-height:2.9rem;padding:.7rem 1rem;
   background:#2a2f36;color:var(--fg);border:1px solid var(--line);
   border-radius:7px;font:inherit;cursor:pointer}
 button:hover{background:#333941}
 button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
 button.primary:hover{background:#3d7be8}
 button.danger{background:#3a2325;border-color:#5b3033;color:#ffb0b0}
 button.danger:hover{background:#4a2b2e}
 dl{display:grid;grid-template-columns:auto 1fr;gap:.35rem .9rem;margin:0;
   font-size:.88rem}
 dt{color:var(--dim)}
 dd{margin:0;word-break:break-all;font-family:ui-monospace,monospace}
 .ok{color:var(--ok)} .warn{color:var(--warn)}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}
 .msg{margin-top:.8rem;font-size:.85rem;min-height:1.2em;color:var(--ok)}
 /* Card titles carry the section. They used to be the same weight and colour
    as a field label, so a card read as a loose pile of controls rather than a
    thing with a name. */
 h2{font-size:.95rem;text-transform:uppercase;letter-spacing:.09em;
   color:var(--fg);margin:0 0 1rem;font-weight:700;
   padding-bottom:.6rem;border-bottom:1px solid var(--line);
   display:flex;align-items:center;gap:.6rem}
 h2::before{content:"";width:3px;height:1.05em;border-radius:2px;
   background:var(--acc);flex:0 0 auto}
 /* A sub-heading inside a card is not another card. No accent bar, no rule,
    dimmer and lighter -- it groups a few rows, it does not name the section. */
 .savedrow{display:flex;align-items:center;gap:.6rem;padding:.55rem .7rem;
   border:1px solid var(--line);border-radius:8px;margin-bottom:.4rem;
   background:#15181c}
 .savedrow .ssid{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
   white-space:nowrap}
 .savedacts{display:flex;gap:.4rem;flex:0 0 auto}
 button.mini{min-height:2.1rem;padding:.3rem .8rem;font-size:.85rem;
   width:auto;flex:0 0 auto}
 .card h3{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;
   color:var(--dim);font-weight:600;margin:1.2rem 0 .6rem;
   padding:0;border:0}
 .net{margin-top:.8rem}
 .net button.row-net{display:flex;align-items:center;gap:.7rem;width:100%;
   text-align:left;margin:0 0 .4rem;padding:.7rem .8rem;min-height:3rem}
 .bars{display:inline-flex;align-items:flex-end;gap:2px;height:1em;flex:none}
 .bars i{width:3px;background:#4c5560;border-radius:1px}
 .bars i.on{background:var(--acc)}
 .ssid{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tag{font-size:.7rem;padding:.1rem .4rem;border-radius:4px;background:#2f3742;
   color:var(--dim);flex:none}
 .tag.cur{background:#1d4030;color:var(--ok)}
 .join{padding:.7rem .8rem;background:#111316;border:1px solid var(--line);
   border-radius:7px;margin:0 0 .6rem}
 .shotwrap{margin-top:1rem}
 .shothead{display:flex;align-items:center;justify-content:space-between;
   gap:.6rem;margin-bottom:.5rem}
 #shotstamp{font-size:.8rem;color:var(--dim);text-transform:uppercase;
   letter-spacing:.04em}
 button.small{flex:0 0 auto;min-width:0;min-height:2.2rem;padding:.35rem .8rem;
   font-size:.85rem}
 /* A reveal belongs beside the field it reveals, not stacked under it: below,
    it reads as another action in the form rather than part of the input. */
 .pwrow{display:flex;gap:.5rem;align-items:stretch;margin:.15rem 0 0}
 .pwrow input{flex:1 1 auto;min-width:0}
 .pwrow button{flex:0 0 auto;min-height:0;padding:.35rem .9rem;font-size:.85rem}
 #shot{display:block;width:100%;max-height:46vh;object-fit:contain;
   border:1px solid var(--line);border-radius:8px;background:#0b0d0f;
   min-height:80px}
 #shot.stale{opacity:.45}

</style></head><body><div class=wrap>
<h1><!--BRAND--> Admin</h1>
<div class=sub id=host>&nbsp;</div>
<div class=strip id=strip></div>
<div class=tabs id=tabs></div>

<div class=card>
 <h2>Page</h2>
 <label for=url>URL</label>
 <input type=text id=url spellcheck=false autocapitalize=off autocomplete=off>
 <div class=row>
  <button class=primary onclick="setUrl()">Set &amp; go</button>
  <button onclick="post('/api/reload')">Reload</button>
  <button onclick="post('/api/reload-hard')">Hard reload</button>
  <button onclick="post('/api/restart','Restarting browser...')">Restart browser</button>
 </div>
 <div class=row>
  <button onclick="clearCache(false)">Clear cache</button>
  <button class=danger onclick="clearCache(true)">Clear all site data</button>
 </div>
 <div class=msg id=msg></div>
 <div class=shotwrap>
  <div class=shothead>
   <span id=shotstamp>Screen</span>
   <label style="display:flex;align-items:center;gap:.4rem;font-size:.85rem;
                 color:var(--dim);cursor:pointer">
     <input type=checkbox id=shotauto style="width:auto;margin:0"> live
   </label>
   <button class=small onclick="shot()" id=shotbtn>Refresh screenshot</button>
  </div>
  <img id=shot alt="live screenshot of the device's display">
 </div>
</div>

<div class=card>
 <h2>Status</h2>
 <dl id=status></dl>
</div>

<div class=card>
 <h2>Network</h2>
 <dl id=netstatus></dl>
 <div class=row>
  <button onclick="scanWifi()" id=scanbtn>Scan for Wi-Fi</button>
  <button onclick="post('/api/wifi/disconnect','Wi-Fi disconnected')">Disconnect Wi-Fi</button>
 </div>
 <h3>Saved networks</h3>
 <div id=savedlist class=net></div>
 <h3>In range</h3>
 <div id=wifilist></div>
 <div class=msg id=netmsg></div>
 <details style="margin-top:.8rem">
  <summary style="cursor:pointer;color:var(--dim);font-size:.85rem">Hidden network</summary>
  <div style="margin-top:.6rem">
   <input type=text id=hssid placeholder="Network name" autocomplete=off spellcheck=false>
   <input type=password id=hpass placeholder="Password (blank if open)" autocomplete=off
          spellcheck=false style="margin-top:.5rem">
   <div class=row><button onclick="joinHidden()">Join hidden network</button></div>
  </div>
 </details>
</div>

<div class=card>
 <h2>Display</h2>
 <dl id=displayinfo></dl>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=zoom>Zoom</label>
   <input type=number id=zoom step=0.1 min=0.25 max=5></div>
  <div><label for=refresh>Auto-reload (s, 0=off)</label>
   <input type=number id=refresh min=0 step=10></div>
  <div><label for=idle>Idle reset (s, 0=off)</label>
   <input type=number id=idle min=0 step=10></div>
  <div><label for=resolution>Screen resolution</label>
   <select id=resolution style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=auto>Recommended</option></select></div>
  <div><label for=rotation>Screen rotation</label>
   <select id=rotation style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=normal>normal (landscape)</option>
    <option value=left>90&deg; left</option>
    <option value=right>90&deg; right</option>
    <option value=inverted>180&deg; upside down</option></select></div>
  <div><label for=hwaccel>GPU acceleration</label>
   <select id=hwaccel style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=0>off (steadier)</option><option value=1>on</option></select></div>
 </div>
 <dl id=cecinfo style="margin-top:1rem"></dl>
 <div class=row>
  <button onclick="refreshCec()" id=cecbtn>Re-check HDMI-CEC</button>
 </div>
 <div class=row id=cecrow style="display:none">
  <button onclick="cec('on')">Wake display (CEC)</button>
  <button onclick="cec('off')">Display standby (CEC)</button>
 </div>
 <div class=row><button class=primary onclick="saveDisplay()">Apply display settings</button></div>
</div>

<div class=card>
 <h2>Remote access (SSH)</h2>
 <dl id=sshstatus></dl>
 <label for=newkey style="margin-top:.9rem">Add an authorized public key</label>
 <input type=text id=newkey autocomplete=off spellcheck=false
        placeholder="ssh-ed25519 AAAA... you@laptop">
 <div class=row>
  <button class=primary onclick="addKey()">Add key</button>
  <button onclick="togglePwAuth()" id=pwauthbtn>Password login: ...</button>
 </div>
 <label for=sshpw style="margin-top:.9rem">Set the ssh password</label>
 <div class=pwrow>
  <input type=password id=sshpw autocomplete=off spellcheck=false
         placeholder="at least 8 characters">
  <button type=button class=small onclick="pwToggle('sshpw', this)"
          aria-pressed=false>Show</button>
 </div>
 <div class=row><button onclick="setSshPassword()">Set password</button></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:-.2rem">
  This is the <code>kiosk</code> account password, used for ssh and for
  <code>sudo</code>. Turning password login on does not change it.
 </div>
 <div id=keylist class=net></div>
 <div class=msg id=sshmsg></div>
</div>

<div class=card>
 <h2>Admin page password</h2>
 <label for=adminpw>Password (leave blank for no password)</label>
 <div class=pwrow>
  <input type=password id=adminpw autocomplete=off spellcheck=false
         placeholder="no password set">
  <button type=button class=small onclick="pwToggle('adminpw', this)"
          aria-pressed=false>Show</button>
 </div>
 <div class=row><button onclick="setAdminPw()">Apply</button></div>
 <div class=msg id=pwmsg></div>
</div>

<div class=card>
 <h2>Clock</h2>
 <dl id=timestatus></dl>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=tz>Time zone</label>
   <select id=tz style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);
     font:inherit"></select></div>
  <div><label for=ntpsel>Network time (NTP)</label>
   <select id=ntpsel style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=1>on</option><option value=0>off</option></select></div>
 </div>
 <div class=row><button onclick="saveTime()">Apply clock settings</button></div>
 <div class=msg id=timemsg></div>
</div>

<div class=card>
 <h2>Software update</h2>
 <dl id=updinfo></dl>
 <div class=row>
  <button onclick="updCheck()" id=updcheckbtn>Check for updates</button>
  <button class=primary onclick="updApply()" id=updapplybtn style="display:none">
    Install now</button>
 </div>
 <div class=msg id=updmsg></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem">
  Updates are signed and are checked against that signature before anything is
  installed. Installing restarts the browser; a failed update rolls itself back.
 </div>
</div>

<div class=card>
 <h2>Printing</h2>
 <dl id=printinfo></dl>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=printer>Printer</label>
   <select id=printer style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value="">system default</option></select></div>
  <div><label for=printwidth>Paper width (mm)</label>
   <input type=number id=printwidth min=40 max=210 step=1></div>
 </div>
 <div class=row>
  <button onclick="savePrint()">Apply printing settings</button>
  <button onclick="printerCmd('test')">Test print</button>
  <button onclick="addTmt88v()" id=addtmbtn style="display:none">Add TM-T88V</button>
 </div>
 <div class=msg id=printmsg></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem" id=printnote>
  The page prints with <b>no dialog, no header or footer and zero margins</b>.
 </div>
</div>

<div class=card>
 <h2>Audio</h2>
 <dl id=audioinfo></dl>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=audioout>Output</label>
   <select id=audioout style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value="auto">automatic (prefer HDMI)</option></select></div>
  <div><label for=volrange>Volume &mdash; <span id=vollabel>...</span></label>
   <input type=range id=volrange min=0 max=100 step=1
     style="width:100%;margin-top:.9rem"
     oninput="document.getElementById('vollabel').textContent=this.value+'%'"
     onchange="setVolume(this.value)"></div>
 </div>
 <div class=row>
  <button onclick="setOutput()">Apply output</button>
  <button onclick="toggleMute()" id=mutebtn>Mute</button>
  <button onclick="audioTest()">Test tone</button>
 </div>
 <div class=msg id=audiomsg></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem">
  Sound follows the screen: the default is the HDMI output with a display on
  it. <b>HDMI provides no volume control of its own</b>, so the level here is
  applied in software on the way to the card &mdash; which also means it is
  the only volume control, as the display's own remote will not change it.
  A page has to be playing audio for anything to come out; use
  <b>Test tone</b> to check the wiring on its own.
 </div>
</div>

<div class=card>
 <h2>Keyboard</h2>
 <div style="color:var(--dim);font-size:.9rem;margin-bottom:.9rem">
  The on-screen keyboard for touch displays. It appears when someone touches a
  text field on the page being shown.
 </div>
 <div class=grid2>
  <div><label for=osk>On-screen keyboard</label>
   <select id=osk style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=auto>only when no keyboard is attached</option>
    <option value=on>always on</option>
    <option value=off>off</option></select></div>
  <div><label for=oskcaps>Capitals</label>
   <select id=oskcaps style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=lock>caps lock on</option>
    <option value=first>first letter only</option>
    <option value=off>lower case</option></select></div>
  <div><label for=oskdx>Nudge across (px, -left / +right)</label>
   <input type=number id=oskdx step=10></div>
  <div><label for=oskdy>Nudge up (px)</label>
   <input type=number id=oskdy step=10></div>
 </div>
 <div style="color:var(--dim);font-size:.85rem;margin-top:.7rem">
  Nudge moves the keys away from a dead patch on the touchscreen. Only the keys
  move &mdash; a press beside them still will not dismiss the keyboard.
 </div>
 <div class=row><button class=primary onclick="saveKeyboard()">Apply keyboard settings</button></div>
 <div class=msg id=kbdmsg></div>
</div>

<div class=card>
 <h2>Permissions</h2>
 <div style="color:var(--dim);font-size:.9rem;margin-bottom:.9rem">
  What the page being shown is allowed to ask this device for.
 </div>
 <div class=grid2>
  <div><label for=geo>Location</label>
   <select id=geo style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=0>off (page cannot ask)</option>
    <option value=1>on (page may use location)</option></select></div>
 </div>
 <div class=row><button class=primary onclick="savePermissions()">Apply permissions</button></div>
 <div class=msg id=permmsg></div>
</div>

<div class=card>
 <h2>Boot splash</h2>
 <div style="color:var(--dim);font-size:.86rem">
  Every client shows the SteadyScreen boot splash. Setting your own is part of
  a paid licence, and is managed centrally so it is the same on every screen in
  an account rather than something each machine is set up with by hand.
 </div>
</div>

<div class=card>
 <h2>Startup behaviour</h2>
 <dl id=netphase></dl>
 <div class=row>
  <button onclick="window.open('/startup','_blank')">Open the network page</button>
  <button onclick="toggleClearOnStart()" id=cosbtn>Clear cache at boot: ...</button>
 </div>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=offsecs>Give up waiting after (seconds)</label>
   <input type=number id=offsecs min=0 max=3600 step=5></div>
  <div><label for=offborder>Offline border</label>
   <button onclick="toggleOffBorder()" id=offborderbtn
     style="width:100%">...</button></div>
 </div>
 <div class=row>
  <button onclick="saveOffline()">Apply offline settings</button>
 </div>
 <div class=msg id=offmsg></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem">
  While there is no network the device shows its own network page instead of a
  &ldquo;cannot reach the page&rdquo; error, and loads the real page as soon as
  the site answers. <b>After the timeout above it stops waiting and loads the
  page from cache instead</b> &mdash; a menu board that reboots during an
  outage comes back showing the last menu it saw, rather than a diagnostic
  screen nobody in the building can act on. Set 0 to wait indefinitely.
  While the site is unreachable a 2px red edge marks the screen as offline.
 </div>
</div>

<div class=card>
 <h2>Power</h2>
 <div class=row>
  <button class=danger onclick="confirmPost('/api/reboot','Reboot the device?')">Reboot</button>
  <button class=danger onclick="armShutdown()" id=sdstart>Shut down</button>
 </div>

 <!-- Shutting a display down remotely is the one action here that cannot be
      undone remotely: nothing on the network can switch it back on. A confirm
      dialog is one absent-minded OK away from a site visit, so this is a
      deliberate sequence instead: read it, tick it, then hold. -->
 <div id=sdconfirm style="display:none;margin-top:.9rem;padding:.9rem 1rem;
   border:1px solid #7c2d2d;border-radius:10px;background:#1e1416">
  <div style="font-weight:600;color:#ff9b9b;margin-bottom:.5rem">
   This device cannot be switched back on remotely.
  </div>
  <div style="color:var(--dim);font-size:.92rem;line-height:1.5">
   Shutting down cuts the admin page, ssh and everything else. Unless somebody
   is standing next to the machine and can reach its power button, it stays off
   until someone travels to it. If you only need it back in a working state,
   use <b>Reboot</b> instead.
  </div>
  <label style="display:flex;align-items:center;gap:.6rem;margin:.9rem 0 .2rem;
    text-transform:none;letter-spacing:0;font-size:.95rem;color:var(--fg)">
   <input type=checkbox id=sdack style="width:auto;min-height:0;margin:0">
   <span>Someone can physically reach this device to switch it back on.</span>
  </label>
  <div class=row>
   <button id=sdhold class=danger disabled
     style="position:relative;overflow:hidden">Hold to shut down &mdash; 10s</button>
   <button onclick="cancelShutdown()">Cancel</button>
  </div>
  <div class=msg id=sdmsg></div>
 </div>
</div>
</div>
<script>
// Escape anything that came from the client before it goes near innerHTML.
// The admin page had no esc() -- the startup page did, and I used it here from
// memory. The ReferenceError killed refresh() at its first line and every
// status card below it silently failed to render.
function esc(t){ return String(t == null ? '' : t)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;'); }

// The cards stay in document order in the HTML; they are sorted into tabs
// here so adding a card never means rewriting the layout.
const TABS = [
  ['page',    'Page',    ['Page', 'Permissions']],
  ['network', 'Network', ['Network', 'Startup behaviour']],
  ['display',  'Display', ['Display', 'Keyboard', 'Boot splash']],
  ['system',   'System',  ['Status', 'Software update', 'Printing', 'Audio', 'Clock',
                          'Remote access (SSH)',
                          'Admin page password', 'Power']],
];

const WIDE = ['Page', 'Network', 'Remote access (SSH)'];

function buildTabs(){
  const wrap = document.querySelector('.wrap');
  const bar = document.getElementById('tabs');
  const cards = Array.from(document.querySelectorAll('.card'));
  const byTitle = new Map();
  for(const c of cards){
    const h = c.querySelector('h2');
    if(h) byTitle.set(h.textContent.trim(), c);
  }
  for(const [id, label, titles] of TABS){
    const pane = document.createElement('div');
    pane.className = 'cards'; pane.id = 'tab-' + id; pane.style.display = 'none';
    for(const t of titles){
      const c = byTitle.get(t);
      if(c){ if(WIDE.includes(t)) c.classList.add('wide');
             pane.appendChild(c); byTitle.delete(t); }
    }
    wrap.appendChild(pane);
    const b = document.createElement('button');
    b.textContent = label; b.dataset.tab = id;
    b.onclick = () => showTab(id);
    bar.appendChild(b);
  }
  // anything unclaimed (a card added later) lands on the first tab rather
  // than vanishing
  // Anything not named in TABS lands on the first tab. That is a reasonable
  // fallback and a terrible silence: "Software update" sat on the Page tab for
  // weeks because nobody had listed it. Say so.
  const first = document.getElementById('tab-' + TABS[0][0]);
  for(const [title, c] of byTitle.entries()){
    console.warn('ds: card "' + title + '" is in no tab; putting it on ' +
                 TABS[0][1] + '. Add it to TABS.');
    first.appendChild(c);
  }

  let start = TABS[0][0];
  try { start = localStorage.getItem('ds-admin-tab') || start; } catch(e){}
  const fromHash = (location.hash || '').replace('#', '');
  if(fromHash) start = fromHash;
  if(!document.getElementById('tab-' + start)) start = TABS[0][0];
  showTab(start);

  // Changing only the #fragment is a same-document navigation: the page is
  // never reloaded, so the hash has to be watched rather than just read once.
  window.addEventListener('hashchange', () => {
    const id = (location.hash || '').replace('#', '');
    if(id && document.getElementById('tab-' + id)) showTab(id);
  });
}

function showTab(id){
  for(const [t] of TABS){
    const pane = document.getElementById('tab-' + t);
    if(pane) pane.style.display = (t === id) ? '' : 'none';
  }
  for(const b of document.querySelectorAll('.tabs button'))
    b.classList.toggle('on', b.dataset.tab === id);
  try { localStorage.setItem('ds-admin-tab', id); } catch(e){}
  if(location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
  // the screenshot is half a megabyte; only fetch it when it is on screen
  if(id === 'page' && !document.getElementById('shot').src) shot();
}

let dirty = false;
for (const id of ['url','zoom','refresh','idle','rotation','hwaccel','tz','ntpsel','resolution','geo','printer','printwidth'])
  for (const ev of ['input','change'])
    document.getElementById(id).addEventListener(ev, () => dirty = true);

function msg(t, cls){ const m=document.getElementById('msg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

async function post(path, note){
  msg(note || 'Working...');
  try { const r = await fetch(path,{method:'POST'});
        msg(r.ok ? (note || 'Done') : 'Failed: '+r.status, r.ok?'':'warn'); }
  catch(e){ msg('Failed: '+e, 'warn'); }
  setTimeout(refresh, 600);
}
async function toggleClearOnStart(){
  const on = document.getElementById('cosbtn').textContent.endsWith('OFF');
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({clear_cache_on_start: on})});
    await r.json();
  }catch(e){}
  refreshPhase();
}

async function clearCache(everything){
  if(everything && !confirm('Clear ALL site data?\n\nCache, cookies, '
      + 'localStorage and saved logins for every site. Anything the page '
      + 'remembered about this device is lost.')) return;
  msg(everything ? 'Clearing all site data...' : 'Clearing cache...');
  try{
    const r = await fetch('/api/cache/clear',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({everything: !!everything})});
    const d = await r.json();
    msg(d.detail || 'done');
  }catch(e){ msg('Failed: '+e,'warn'); }
  setTimeout(refresh, 2500);
}

async function setUrl(){
  const url = document.getElementById('url').value;
  msg('Loading...');
  try { const r = await fetch('/api/url',{method:'POST',
          headers:{'content-type':'application/json'},
          body:JSON.stringify({url})});
        msg(r.ok?'Set':'Failed: '+r.status, r.ok?'':'warn'); dirty=false; }
  catch(e){ msg('Failed: '+e,'warn'); }
  setTimeout(refresh, 800);
}
/* Each card applies its own settings. One "Apply" that reached across three
 * unrelated cards was how Display ended up owning the keyboard and the page's
 * location permission in the first place. */
async function saveCard(body, msgId, label){
  const m = document.getElementById(msgId);
  if(m){ m.textContent = 'Applying...'; m.className = 'msg'; }
  try {
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    if(m){ m.textContent = r.ok ? (label + ' applied') : ('Failed: ' + r.status);
           m.className = 'msg' + (r.ok ? '' : ' warn'); }
  } catch(e){
    if(m){ m.textContent = 'Failed: ' + e; m.className = 'msg warn'; }
  }
  setTimeout(refresh, 700);
}

function saveKeyboard(){
  saveCard({
    osk: document.getElementById('osk').value,
    osk_caps: document.getElementById('oskcaps').value,
    osk_offset_x: parseInt(document.getElementById('oskdx').value||0),
    osk_offset_y: parseInt(document.getElementById('oskdy').value||0)
  }, 'kbdmsg', 'Keyboard');
}

function savePermissions(){
  saveCard({
    allow_location: document.getElementById('geo').value === '1'
  }, 'permmsg', 'Permissions');
}

async function saveDisplay(){
  const body = {
    zoom: parseFloat(document.getElementById('zoom').value),
    refresh_seconds: parseInt(document.getElementById('refresh').value||0),
    idle_reset_seconds: parseInt(document.getElementById('idle').value||0),
    hardware_acceleration: document.getElementById('hwaccel').value === '1'
  };
  const rot = document.getElementById('rotation').value;
  const res = document.getElementById('resolution').value;
  const notes = [];
  msg('Applying...');

  // resolution first: rotation is applied to whatever mode ends up active
  try {
    const r = await fetch('/api/resolution',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({mode: res})});
    const d = await r.json();
    notes.push(r.ok ? ('resolution ' + res) : ('resolution FAILED: '
               + (d.detail || d.error || '')));
  } catch(e){ notes.push('resolution FAILED: '+e); }
  try {
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify(body)});
    notes.push(r.ok ? 'settings saved' : 'settings FAILED');
  } catch(e){ notes.push('settings FAILED: '+e); }

  // rotation lives in this card too, so this one button applies it as well
  try {
    const r = await fetch('/api/rotation',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({rotation: rot})});
    const d = await r.json();
    notes.push(r.ok ? ('rotation ' + rot)
                    : ('rotation FAILED: ' + (d.detail || d.error || '')));
  } catch(e){ notes.push('rotation FAILED: '+e); }

  const bad = notes.some(n => n.includes('FAILED'));
  msg(notes.join(' \u00b7 '), bad ? 'warn' : '');
  dirty = false;
  setTimeout(() => { refresh(); refreshDisplay(); shot(); }, 1600);
}
function confirmPost(path, q){ if(confirm(q)) post(path, 'Sent'); }

/* Shutdown: read it, tick it, hold it.
 *
 * This is the only control on the page whose effect cannot be undone from the
 * page. A confirm() dialog sits one absent-minded OK away from someone
 * driving to a shop to press a power button, so the sequence is deliberate --
 * and the hold has to be continuous, because a ten-second press is not
 * something anybody does by accident.
 */
var sdTimer = null, sdLeft = 0;

function armShutdown(){
  document.getElementById('sdconfirm').style.display = '';
  document.getElementById('sdstart').disabled = true;
  var ack = document.getElementById('sdack');
  ack.checked = false;
  document.getElementById('sdhold').disabled = true;
  ack.onchange = function(){
    document.getElementById('sdhold').disabled = !ack.checked;
  };
  resetHold();
}

function cancelShutdown(){
  stopHold();
  document.getElementById('sdconfirm').style.display = 'none';
  document.getElementById('sdstart').disabled = false;
  document.getElementById('sdmsg').textContent = '';
}

function resetHold(){
  sdLeft = 10;
  var b = document.getElementById('sdhold');
  b.textContent = 'Hold to shut down — 10s';
  b.style.background = '';
}

function stopHold(){
  if(sdTimer){ clearInterval(sdTimer); sdTimer = null; }
  resetHold();
}

function startHold(e){
  if(e){ e.preventDefault(); }
  var b = document.getElementById('sdhold');
  if(b.disabled || sdTimer){ return; }
  sdLeft = 10;
  b.textContent = 'Keep holding — ' + sdLeft + 's';
  sdTimer = setInterval(function(){
    sdLeft -= 1;
    if(sdLeft > 0){
      b.textContent = 'Keep holding — ' + sdLeft + 's';
      /* fills left to right as the count runs down */
      var pct = Math.round((10 - sdLeft) * 10);
      b.style.background =
        'linear-gradient(to right,#c0392b 0%,#c0392b ' + pct + '%,' +
        '#3a1d1d ' + pct + '%,#3a1d1d 100%)';
      return;
    }
    clearInterval(sdTimer); sdTimer = null;
    b.textContent = 'Shutting down...';
    b.disabled = true;
    document.getElementById('sdmsg').textContent =
      'Sent. The device is powering off.';
    post('/api/shutdown', 'Shutting down');
  }, 1000);
}

function endHold(e){
  if(e){ e.preventDefault(); }
  if(!sdTimer){ return; }
  stopHold();
  document.getElementById('sdmsg').textContent =
    'Released early — nothing was sent. Hold the full ten seconds.';
}

(function(){
  var b = document.getElementById('sdhold');
  if(!b){ return; }
  ['pointerdown','touchstart','mousedown'].forEach(function(ev){
    b.addEventListener(ev, function(e){
      if(ev === 'pointerdown' || !window.PointerEvent){ startHold(e); }
      else { e.preventDefault(); }
    }, {passive:false});
  });
  ['pointerup','pointercancel','pointerleave','touchend','touchcancel',
   'mouseup','mouseleave'].forEach(function(ev){
    b.addEventListener(ev, endHold);
  });
})();



function dur(s){ s=Math.max(0,s|0);
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600),
        m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`: h?`${h}h ${m}m`: `${m}m ${s%60}s`; }

async function refresh(){
  let s; try { s = await (await fetch('/api/status')).json(); } catch(e){ return; }

  const host = document.getElementById('host');
  // innerHTML, not textContent, so the wordmark can carry its colours -- and
  // every dynamic part is escaped, because client_name is set by whoever has
  // the admin page and would otherwise be a way to inject markup here.
  host.innerHTML =
    esc(s.client_name || s.kiosk_name || s.hostname) + ' \u00b7 ' + esc(s.ip)
    + ' \u00b7 <!--BRAND--> ' + esc(s.version);
  const cid = s.client_id || s.kiosk_id;
  host.title = cid ? 'id ' + cid : '';

  const m = s.memory || {used:0, total:0, available:s.mem_free_mb || 0,
                        swap_used:0, swap_total:0};
  // An unconfigured display has not failed to reach anything -- there is
  // nothing to reach yet. Calling that "site unreachable" reports a fault on a
  // machine that is working exactly as intended on its first boot.
  const phase = s.configured === false
    ? '<span class=ok>ready for setup</span>'
    : ({'ready':'<span class=ok>page loaded</span>',
        'waiting-site':'<span class=warn>site unreachable</span>',
        'waiting-network':'<span class=warn>no network</span>',
        'starting':'starting'}[s.net_phase] || s.net_phase);
  const stat = (k, v, title) =>
    `<div class=stat${title ? ` title="${title}"` : ''}>` +
    `<span class=k>${k}</span><span class=v>${v}</span></div>`;

  document.getElementById('strip').innerHTML =
    stat('Status', phase) +
    stat('Uptime', dur(s.system_uptime)) +
    stat('Memory used', m.used + ' MB',
         'total minus available: what programs are actually holding') +
    stat('Memory total', m.total + ' MB') +
    stat('Showing', (s.title || s.current_url || '-')) +
    (s.admin_protected ? ''
      : '<div class=banner>This admin page has no password &mdash; anything '
        + 'on the network can change this device. Set one under System.</div>');
  const state = s.last_error ? `<span class=warn>error</span>`
              : s.loading ? 'loading' : '<span class=ok>loaded</span>';
  document.getElementById('status').innerHTML = `
    <dt>State</dt><dd>${state}</dd>
    <dt>Showing</dt><dd>${s.current_url||'-'}</dd>
    <dt>Title</dt><dd>${s.title||'-'}</dd>
    ${s.last_error?`<dt>Error</dt><dd class=warn>${s.last_error}</dd>`:''}
    <dt>Browser up</dt><dd>${dur(s.browser_uptime)}</dd>
    <dt>System up</dt><dd>${dur(s.system_uptime)}</dd>
    <dt>Load</dt><dd>${s.load.toFixed(2)}</dd>
    <dt>Memory</dt><dd>${m.used} MB used · ${m.available} MB available ·
        ${m.total} MB total</dd>
    <dt>Swap</dt><dd>${m.swap_total
        ? `${m.swap_used} MB of ${m.swap_total} MB`
        : 'none'}</dd>
    <dt>Input</dt><dd>${(s.input && s.input.interactive)
        ? [s.input.touch?('touchscreen' + touchNote(s)):null,
           s.input.keyboard?'keyboard':null,
           s.input.pointer?'mouse':null].filter(Boolean).join(', ')
        : '<span class=warn>none (display only)</span>'}</dd>`;
  if(!dirty){
    document.getElementById('url').value = s.configured_url;
    document.getElementById('zoom').value = s.zoom;
    document.getElementById('refresh').value = s.refresh_seconds;
    document.getElementById('idle').value = s.idle_reset_seconds;
    document.getElementById('hwaccel').value = s.hardware_acceleration?'1':'0';
    document.getElementById('rotation').value = s.rotation || 'normal';
    document.getElementById('geo').value = s.allow_location ? '1' : '0';
    document.getElementById('osk').value = s.osk || 'auto';
    document.getElementById('oskcaps').value = s.osk_caps || 'lock';
    document.getElementById('oskdx').value = s.osk_offset_x || 0;
    document.getElementById('oskdy').value = s.osk_offset_y || 0;
  }
}

function netmsg(t, cls){ const m=document.getElementById('netmsg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

// A touchscreen can be present and completely dead: udev reports it from the
// device's capabilities, not from whether it works. If its interrupt has never
// fired we say so rather than claiming a working panel -- but "no activity" is
// not "broken", because a display nobody touches reads zero too.
function touchNote(s){
  const a = s.input && s.input.touch_activity;
  if (!a) return '';                       // shared/unmappable line: say nothing
  return a.count > 0 ? '' : ' (no activity since boot)';
}

async function saveOffline(){
  const secs = parseInt(document.getElementById('offsecs').value, 10);
  if(!(secs >= 0)){ offmsg('enter a number of seconds, or 0 to wait forever', 'warn'); return; }
  try{
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({offline_after_seconds: secs})});
    offmsg(r.ok ? 'saved' : 'failed', r.ok ? '' : 'warn');
    refresh();
  }catch(e){ offmsg('failed: '+e, 'warn'); }
}

async function toggleOffBorder(){
  const on = !(lastStatus && lastStatus.offline_border);
  try{
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({offline_border: on})});
    offmsg(r.ok ? ('offline border ' + (on ? 'on' : 'off')) : 'failed',
           r.ok ? '' : 'warn');
    refresh();
  }catch(e){ offmsg('failed: '+e, 'warn'); }
}

function offmsg(s, cls){
  const e = document.getElementById('offmsg');
  e.textContent = s; e.className = 'msg ' + (cls || '');
}

async function updCheck(){
  const b = document.getElementById('updcheckbtn');
  b.disabled = true; umsg('checking...');
  try{
    const r = await fetch('/api/update', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'check'})});
    const d = await r.json();
    if(!r.ok){ umsg(d.detail || d.error || 'check failed', 'warn'); return; }
    renderUpd(d.result);
  }catch(e){ umsg('check failed: '+e, 'warn'); }
  finally{ b.disabled = false; }
}

async function updApply(){
  const avail = (lastUpd && lastUpd.available) || 'the published release';
  if(!confirm('Install ' + avail + ' now?\n\nThe browser restarts. If the new '
            + 'version fails to come up it is rolled back automatically.')) return;
  const b = document.getElementById('updapplybtn');
  b.disabled = true; umsg('installing -- this can take a minute...');
  try{
    const r = await fetch('/api/update', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'apply'})});
    const d = await r.json();
    umsg(d.detail || (r.ok ? 'installed' : 'failed'), r.ok ? '' : 'warn');
    if(r.ok) setTimeout(() => { refresh(); updCheck(); }, 4000);
  }catch(e){
    // The browser restarting mid-request looks exactly like a network error,
    // so do not call it a failure -- say what is actually known.
    umsg('connection lost, which is expected if the browser restarted. '
       + 'Re-check in a moment.', 'warn');
  }
  finally{ b.disabled = false; }
}

function umsg(s, cls){
  const e = document.getElementById('updmsg');
  e.textContent = s; e.className = 'msg ' + (cls || '');
}

let lastStatus = null;
let lastUpd = null;
function renderUpd(u){
  lastUpd = u;
  const dl = document.getElementById('updinfo');
  const btn = document.getElementById('updapplybtn');
  if(!u){ dl.innerHTML = ''; return; }
  const changed = (u.changed || []);
  const down = u.direction === 'older';
  dl.innerHTML =
    `<dt>Installed</dt><dd>${u.installed || 'unknown'}</dd>`
  + `<dt>Published</dt><dd>${u.available || 'unknown'} (${u.channel || '?'})</dd>`
  + `<dt>Status</dt><dd>${!u.update_available ? 'up to date'
        : down
          ? '<span class=warn>the published release is OLDER than what is '
            + 'installed &mdash; installing it would downgrade this client</span>'
          : '<span class=warn>' + changed.length + ' component'
            + (changed.length === 1 ? '' : 's') + ' differ' + (changed.length === 1 ? 's' : '')
            + '</span>'}</dd>`
  + (u.update_available ? `<dt>Changed</dt><dd>${changed.join(', ')}</dd>` : '')
  + `<dt>Automatic</dt><dd>${u.auto
        ? ('yes, between ' + (u.window || '03:00-05:00')
           + (u.in_window ? ' (window open now)' : ''))
        : 'no, manual only'}</dd>`;
  // A downgrade is not offered as a one-click action. The updater refuses it
  // too; this keeps the button from claiming something the backend will reject.
  btn.style.display = (u.update_available && !down) ? '' : 'none';
  btn.textContent = 'Install ' + (u.available || 'update');
  umsg(!u.update_available ? 'nothing to install'
        : down ? 'not offering this: it would move this client backwards.' : '');
}

// Password fields are masked by default and can be revealed deliberately.
// Reveal is per-field and never sticky: it resets on every page refresh, so a
// screen left on a counter does not quietly keep showing a wifi key.
function pwToggle(id, btn){
  const i = document.getElementById(id);
  if(!i) return;
  const show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  btn.textContent = show ? 'Hide' : 'Show';
  btn.setAttribute('aria-pressed', show ? 'true' : 'false');
  i.focus();
}

function bars(sig){
  const n = sig>=75?4 : sig>=50?3 : sig>=25?2 : 1;
  let h=''; for(let i=1;i<=4;i++)
    h += `<i class="${i<=n?'on':''}" style="height:${i*25}%"></i>`;
  return `<span class=bars>${h}</span>`;
}

// Every saved profile, including ones nowhere near this device -- otherwise
// there is no way to clear out an old site's wifi.
function renderSaved(saved){
  const box = document.getElementById('savedlist');
  if(!box) return;
  if(!saved || !saved.length){
    box.innerHTML = '<div class=msg>No saved networks.</div>';
    return;
  }
  box.innerHTML = '';
  /* One line per network: name, state, and its actions on the right. A saved
     network is a list entry, not a panel -- a full-width Forget under every
     one made three remembered networks look like three sections, and put the
     most destructive control in the largest target on the card. */
  for(const n of saved){
    const row = document.createElement('div');
    row.className = 'savedrow';
    row.innerHTML =
      `<span class=ssid>${n.name.replace(/[<&]/g, c=>({'<':'&lt;','&':'&amp;'}[c]))}</span>` +
      (n.active ? '<span class="tag cur">connected</span>'
                : '<span class=tag>saved</span>');
    const actions = document.createElement('span');
    actions.className = 'savedacts';
    if(!n.active){
      const c = document.createElement('button');
      c.className = 'mini';
      c.textContent = 'Connect';
      c.onclick = () => connectSavedAdmin(n.name);
      actions.appendChild(c);
    }
    const f = document.createElement('button');
    f.className = 'mini danger';
    f.textContent = 'Forget';
    f.onclick = () => forgetNetwork(n.name, n.active);
    actions.appendChild(f);
    row.appendChild(actions);
    box.appendChild(row);
  }
}

async function connectSavedAdmin(name){
  netmsg('Connecting to ' + name + '...');
  try{
    const r = await fetch('/api/wifi/connect-saved',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({ssid:name})});
    const d = await r.json();
    netmsg(d.ok ? 'Connected to '+name : 'Failed: '+(d.detail||''), d.ok?'':'warn');
  }catch(e){ netmsg('Failed: '+e,'warn'); }
  refreshNet();
}

async function refreshNet(){
  let n; try { n = await (await fetch('/api/net')).json(); } catch(e){ return; }
  renderSaved(n.saved);
  const line = d => d ? `${d.state}${d.connection?' · '+d.connection:''}` +
                        `${d.ip?' · '+d.ip:''}` : 'none';
  document.getElementById('netstatus').innerHTML = `
    <dt>Wired</dt><dd>${line(n.wired)}</dd>
    <dt>Wi-Fi</dt><dd>${line(n.wifi)}</dd>
    <dt>Radio</dt><dd>${n.wifi_radio?'<span class=ok>on</span>':'<span class=warn>off</span>'}</dd>`;
}

async function scanWifi(){
  const btn = document.getElementById('scanbtn');
  btn.disabled = true; btn.textContent = 'Scanning...';
  netmsg('');
  try {
    const r = await fetch('/api/wifi/scan');
    const d = await r.json();
    if(!r.ok){ netmsg(d.error||'scan failed','warn'); return; }
    const saved = new Set(d.saved||[]);
    const box = document.getElementById('wifilist');
    box.className = 'net';
    box.innerHTML = d.networks.length ? '' : '<div class=msg>No networks found</div>';
    for(const nw of d.networks){
      const open = !nw.security || nw.security === 'open';
      const b = document.createElement('button');
      b.className = 'row-net';
      b.innerHTML = bars(nw.signal) +
        `<span class=ssid>${nw.ssid.replace(/[<&]/g, c=>({'<':'&lt;','&':'&amp;'}[c]))}</span>` +
        (nw.in_use ? '<span class="tag cur">connected</span>' : '') +
        (saved.has(nw.ssid) && !nw.in_use ? '<span class=tag>saved</span>' : '') +
        `<span class=tag>${open ? 'open' : nw.security}</span>`;
      b.onclick = () => pickNetwork(nw, saved.has(nw.ssid));
      box.appendChild(b);
    }
  } catch(e){ netmsg('Scan failed: '+e,'warn'); }
  finally { btn.disabled = false; btn.textContent = 'Scan for Wi-Fi'; }
}

function pickNetwork(nw, isSaved){
  const box = document.getElementById('wifilist');
  const open = !nw.security || nw.security === 'open';
  const old = document.getElementById('joinbox');
  if(old) old.remove();
  const div = document.createElement('div');
  div.id = 'joinbox'; div.className = 'join';
  div.innerHTML = `<label>${nw.ssid}</label>` +
    (open || isSaved ? '' :
      '<div class=pwrow><input type=password id=wpass placeholder="Password"'
       + ' autocomplete=off spellcheck=false>'
       + '<button type=button class=small aria-pressed=false '
       + 'onclick="pwToggle(&quot;wpass&quot;, this)">Show</button></div>') +
    `<div class=row>
       <button class=primary onclick="joinNetwork('${nw.ssid.replace(/'/g,"\\'")}')">
         ${isSaved ? 'Connect' : 'Join'}</button>` +
    (isSaved ? `<button class=danger onclick="forgetNetwork('${nw.ssid.replace(/'/g,"\\'")}')">Forget</button>` : '') +
    `</div>`;
  box.prepend(div);
  const pw = document.getElementById('wpass'); if(pw) pw.focus();
}

async function joinNetwork(ssid){
  const pw = document.getElementById('wpass');
  netmsg('Joining ' + ssid + '... (this can take 20s)');
  try {
    const r = await fetch('/api/wifi/connect',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({ssid, password: pw ? pw.value : ''})});
    const d = await r.json();
    netmsg(d.ok ? 'Connected to ' + ssid : 'Failed: ' + (d.detail||''),
           d.ok ? '' : 'warn');
    if(d.ok){ const j=document.getElementById('joinbox'); if(j) j.remove(); }
  } catch(e){ netmsg('Failed: '+e,'warn'); }
  refreshNet();
}

async function joinHidden(){
  const ssid = document.getElementById('hssid').value.trim();
  if(!ssid){ netmsg('Enter a network name','warn'); return; }
  netmsg('Joining ' + ssid + '...');
  try {
    const r = await fetch('/api/wifi/connect',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({ssid, password:document.getElementById('hpass').value,
                           hidden:true})});
    const d = await r.json();
    netmsg(d.ok ? 'Connected to ' + ssid : 'Failed: ' + (d.detail||''),
           d.ok ? '' : 'warn');
  } catch(e){ netmsg('Failed: '+e,'warn'); }
  refreshNet();
}

async function forgetNetwork(ssid, active){
  const q = active
    ? 'Forget ' + ssid + '?\n\nThis device is CONNECTED to it right now and '
      + 'will drop off the network. If it has no other way on, you will need '
      + 'physical access to reconnect it.'
    : 'Forget ' + ssid + '?';
  if(!confirm(q)) return;
  try {
    const r = await fetch('/api/wifi/forget',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({ssid})});
    const d = await r.json();
    netmsg(d.ok ? 'Forgot ' + ssid : 'Failed: ' + (d.detail||d.error||''),
           d.ok ? '' : 'warn');
  } catch(e){ netmsg('Failed: '+e,'warn'); }
  refreshNet();
}


function sshmsg(t, cls){ const m=document.getElementById('sshmsg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

async function refreshSsh(){
  let d; try { d = await (await fetch('/api/ssh')).json(); } catch(e){ return; }
  const st = d.active === 'active' ? '<span class=ok>running</span>'
                                   : `<span class=warn>${d.active}</span>`;
  const host = d.host_keys.map(k => `${k.type} ${k.fingerprint}`).join('<br>');
  document.getElementById('sshstatus').innerHTML = `
    <dt>sshd</dt><dd>${st}</dd>
    <dt>Log in as</dt><dd>ssh ${d.user}@${location.hostname}</dd>
    <dt>Password login</dt><dd>${d.password_auth
        ? 'enabled' : '<span class=ok>disabled (keys only)</span>'}</dd>
    <dt>Host keys</dt><dd>${host||'-'}</dd>`;
  document.getElementById('pwauthbtn').textContent =
    'Password login: ' + (d.password_auth ? 'ON' : 'OFF');
  const box = document.getElementById('keylist');
  box.innerHTML = d.authorized_keys.length ? '' :
    '<div class=msg>No authorized keys installed yet.</div>';
  for(const k of d.authorized_keys){
    const row = document.createElement('div');
    row.className = 'join';
    row.innerHTML =
      `<div style="font-family:ui-monospace,monospace;font-size:.8rem;
                   word-break:break-all">${k.type} ${k.fingerprint}</div>
       <div style="color:var(--dim);font-size:.8rem">${k.comment||'(no comment)'}</div>
       <div class=row><button class=danger
         onclick="delKey('${k.fingerprint}')">Remove</button></div>`;
    box.appendChild(row);
  }
}

async function addKey(){
  const inp = document.getElementById('newkey');
  if(!inp.value.trim()){ sshmsg('Paste a public key first','warn'); return; }
  try {
    const r = await fetch('/api/ssh/key',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({key: inp.value})});
    const d = await r.json();
    sshmsg(d.detail, d.ok ? '' : 'warn');
    if(d.ok) inp.value = '';
  } catch(e){ sshmsg('Failed: '+e,'warn'); }
  refreshSsh();
}

async function delKey(fp){
  if(!confirm('Remove this key?')) return;
  try {
    const r = await fetch('/api/ssh/key/delete',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({fingerprint: fp})});
    const d = await r.json(); sshmsg(d.detail, d.ok ? '' : 'warn');
  } catch(e){ sshmsg('Failed: '+e,'warn'); }
  refreshSsh();
}

async function setSshPassword(){
  const inp = document.getElementById('sshpw');
  const pw = inp.value;
  if(pw.length < 8){ sshmsg('Password must be at least 8 characters','warn'); return; }
  try{
    const r = await fetch('/api/ssh/password',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({password: pw})});
    const d = await r.json();
    sshmsg(d.ok ? 'Password set for the kiosk account' : ('Failed: '+(d.detail||'')),
           d.ok ? '' : 'warn');
    if(d.ok) inp.value = '';
  }catch(e){ sshmsg('Failed: '+e,'warn'); }
  refreshSsh();
}

async function togglePwAuth(){
  const on = document.getElementById('pwauthbtn').textContent.endsWith('OFF');
  if(!on && !confirm('Turn OFF password login? Make sure a working key is '
                     + 'installed first, or you will be locked out of ssh.')) return;
  if(on) sshmsg('Password login on. Set the password below if you do not know it.');
  try {
    const r = await fetch('/api/ssh/password-auth',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({on})});
    const d = await r.json();
    sshmsg(d.ok ? ('Password login ' + (on?'enabled':'disabled'))
                : ('Failed: '+(d.detail||'')), d.ok?'':'warn');
  } catch(e){ sshmsg('Failed: '+e,'warn'); }
  refreshSsh();
}

async function setAdminPw(){
  const pw = document.getElementById('adminpw').value;
  const m = document.getElementById('pwmsg');
  try {
    const r = await fetch('/api/admin-password',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({password: pw})});
    const d = await r.json();
    m.className = 'msg';
    m.textContent = d.protected
      ? 'Password set. The page will ask for it on the next load '
        + '(any username).'
      : 'Password cleared. The admin page is open to the LAN again.';
  } catch(e){ m.className='msg warn'; m.textContent = 'Failed: '+e; }
}


let shotTimer = null, shotBusy = false, shotAbort = null;

function stopShotAuto(){
  if(shotTimer){ clearInterval(shotTimer); shotTimer = null; }
  // Unchecking must also drop a request that is already in the air, or the
  // the device keeps grabbing its screen after you asked it to stop.
  if(shotAbort){ try { shotAbort.abort(); } catch(e){} shotAbort = null; }
  shotBusy = false;
  const stamp = document.getElementById('shotstamp');
  if(stamp) stamp.textContent = 'Screen (live off)';
}

function toggleShotAuto(){
  const on = document.getElementById('shotauto').checked;
  try { localStorage.setItem('ds-shot-auto', on ? '1' : '0'); } catch(e){}
  stopShotAuto();
  if(on){
    shot(true);
    // 2s feels live; the server caches for 1.5s so extra viewers are free
    shotTimer = setInterval(() => shot(true), 2000);
  }
}

// A hidden tab polling a device forever is pure waste, and on a machine that
// something else is also screen-grabbing it is worse than waste.
document.addEventListener('visibilitychange', () => {
  const cb = document.getElementById('shotauto');
  if(!cb) return;
  if(document.hidden) { if(shotTimer){ clearInterval(shotTimer); shotTimer = null; } }
  else if(cb.checked && !shotTimer) toggleShotAuto();
});

async function shot(auto){
  if(auto && shotBusy) return;          // never stack requests
  shotBusy = true;
  const img = document.getElementById('shot');
  const btn = document.getElementById('shotbtn');
  const stamp = document.getElementById('shotstamp');
  if(!auto){ btn.disabled = true; btn.textContent = 'Capturing...';
             img.classList.add('stale'); }
  try {
    // jpeg while live-refreshing: a fraction of the bytes and the CPU
    const endpoint = auto ? '/api/screenshot.jpg?w=800&t='
                          : '/api/screenshot.png?t=';
    if(auto){ shotAbort = new AbortController(); }
    const r = await fetch(endpoint + Date.now(),
                          auto ? {signal: shotAbort.signal} : undefined);
    if(!r.ok) throw new Error('capture failed (' + r.status + ')');
    const blob = await r.blob();
    if(img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url = URL.createObjectURL(blob);
    img.dataset.url = url; img.src = url;
    img.classList.remove('stale');
    stamp.textContent = (auto ? 'Live \u00b7 ' : 'Screen at ')
      + new Date().toLocaleTimeString();
  } catch(e){
    if(e.name !== 'AbortError') stamp.textContent = 'Screenshot: ' + e.message;
  } finally {
    shotBusy = false;
    btn.disabled = false; btn.textContent = 'Refresh screenshot';
  }
}



async function refreshTime(){
  let d; try { d = await (await fetch('/api/time')).json(); } catch(e){ return; }
  document.getElementById('timestatus').innerHTML = `
    <dt>Local time</dt><dd>${d.local_time||'-'}</dd>
    <dt>Time zone</dt><dd>${d.timezone||'-'}</dd>
    <dt>NTP</dt><dd>${d.ntp ? '<span class=ok>on</span>' : '<span class=warn>off</span>'}
      ${d.synchronized ? '&middot; <span class=ok>synchronised</span>'
                       : '&middot; <span class=warn>not synchronised</span>'}</dd>`;
  const tz = document.getElementById('tz');
  if(!tz.dataset.filled && (d.zones || []).length){
    // grouped by region, with the likely ones first
    const mk = (label) => {
      const g = document.createElement('optgroup');
      g.label = label; return g;
    };
    const add = (g, z) => {
      const o = document.createElement('option');
      o.value = z; o.textContent = z.replace(/_/g, ' ');
      g.appendChild(o);
    };
    tz.innerHTML = '';
    if((d.common || []).length){
      const g = mk('Common');
      for(const z of d.common) add(g, z);
      tz.appendChild(g);
    }
    const groups = {};
    for(const z of d.zones){
      const region = z.includes('/') ? z.split('/')[0] : 'Other';
      if(!groups[region]) { groups[region] = mk(region); }
      add(groups[region], z);
    }
    for(const region of Object.keys(groups).sort())
      tz.appendChild(groups[region]);
    tz.dataset.filled = '1';
  }
  if(!dirty){
    if(d.timezone) tz.value = d.timezone;
    document.getElementById('ntpsel').value = d.ntp ? '1' : '0';
  }
}

async function saveTime(){
  const m = document.getElementById('timemsg');
  m.className='msg'; m.textContent='Applying...';
  try{
    const r = await fetch('/api/time',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({timezone: document.getElementById('tz').value,
                           ntp: document.getElementById('ntpsel').value==='1'})});
    const d = await r.json();
    m.className = 'msg' + (d.ok?'':' warn');
    m.textContent = d.detail || (d.ok?'done':'failed');
    dirty=false;
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
  refreshTime();
}

let resLoaded = false;
async function refreshDisplay(){
  let d; try { d = await (await fetch('/api/display')).json(); } catch(e){ return; }
  const sel = document.getElementById('resolution');
  const out = (d.outputs || [])[0];
  if(!out) return;
  if(!resLoaded || !dirty){
    sel.innerHTML = '';
    for(const s of (out.suggested || [])){
      const o = document.createElement('option');
      o.value = s.mode === out.preferred ? 'auto' : s.mode;
      o.textContent = s.label;
      sel.appendChild(o);
    }
    sel.value = d.configured || 'auto';
    if(!sel.value) sel.value = 'auto';
    resLoaded = true;
  }
  const info = document.getElementById('displayinfo');
  if(info) info.innerHTML =
    `<dt>Output</dt><dd>${out.name}</dd>` +
    `<dt>Now showing</dt><dd>${out.current}</dd>` +
    `<dt>Panel native</dt><dd>${out.preferred}</dd>`;
}

async function refreshCec(){
  const b = document.getElementById('cecbtn');
  if(b){ b.disabled = true; b.textContent = 'Checking...'; }
  let d;
  try { d = await (await fetch('/api/cec')).json(); }
  catch(e){ if(b){ b.disabled=false; b.textContent='Re-check HDMI-CEC'; } return; }
  if(b){ b.disabled = false; b.textContent = 'Re-check HDMI-CEC'; }
  const row = document.getElementById('cecrow');
  const info = document.getElementById('cecinfo');
  if(!info) return;
  if(d.available){
    row.style.display = '';
    info.innerHTML = `<dt>HDMI-CEC</dt><dd><span class=ok>available</span> &middot; `
      + `${d.devices.join(', ')}</dd>`
      + `<dt>Checked</dt><dd>${new Date().toLocaleTimeString()}`
      + (d.took_ms !== undefined ? ` (${d.took_ms} ms)` : '') + '</dd>';
  } else {
    row.style.display = 'none';
    const hdmi = d.hdmi || [];
    const anyConnected = hdmi.some(h => h.includes('status=connected'));
    let why;
    if(!d.tool) why = 'cec-ctl not installed';
    else if(!hdmi.length) why = 'no HDMI output on this machine';
    else if(!anyConnected)
      why = 'no adapter found, but nothing is plugged into HDMI &mdash; '
          + 'this is inconclusive. Connect a display and check again.';
    else
      why = 'no adapter, with a display connected &mdash; this HDMI port '
          + 'does not expose CEC. A USB CEC adapter would appear here.';
    info.innerHTML = '<dt>HDMI-CEC</dt><dd>' + why
      + (hdmi.length ? '<br><span style="color:var(--dim)">'
          + hdmi.join(', ') + '</span>' : '') + '</dd>'
      + `<dt>Checked</dt><dd>${new Date().toLocaleTimeString()}`
      + (d.took_ms !== undefined ? ` (${d.took_ms} ms)` : '') + '</dd>';
  }
}

async function cec(action){
  msg('CEC: ' + action + '...');
  try{
    const r = await fetch('/api/cec',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action})});
    const d = await r.json();
    msg(d.detail || 'done', d.ok ? '' : 'warn');
  }catch(e){ msg('Failed: '+e,'warn'); }
}

async function audioPost(body, note){
  const m = document.getElementById('audiomsg');
  m.className='msg'; m.textContent = note || 'Working...';
  try{
    const r = await fetch('/api/audio',{method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    m.className='msg'+(d.ok?'':' warn'); m.textContent = d.detail||'done';
    return d.ok;
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; return false; }
}
function setOutput(){
  const v = document.getElementById('audioout').value;
  audioPost({action:'output', output:v}, 'Routing...').then(refreshAudio);
}
function setVolume(v){
  audioPost({action:'volume', volume:Number(v)}, 'Setting volume...')
    .then(refreshAudio);
}
function toggleMute(){
  const on = document.getElementById('mutebtn').textContent === 'Mute';
  audioPost({action:'mute', muted:on}, on?'Muting...':'Unmuting...')
    .then(refreshAudio);
}
function audioTest(){
  // ~6 seconds of tone; say so rather than let it look hung
  audioPost({action:'test'}, 'Playing a tone...');
}
async function refreshAudio(){
  let d;
  try { d = await (await fetch('/api/audio')).json(); } catch(e){ return; }
  const info = document.getElementById('audioinfo');
  if(!info) return;
  const cur = d.outputs.find(o => o.id === d.current);
  const live = d.outputs.filter(o => o.live);
  info.innerHTML =
    `<dt>Playing through</dt><dd>${cur
        ? (cur.label || cur.name) + ' <span style="color:var(--dim)">(card ' + cur.card
          + ', device ' + cur.device + ')</span>'
        : '<span class=warn>not routed yet</span>'}</dd>` +
    `<dt>Display detected</dt><dd>${cur
        ? (cur.live ? '<span class=ok>yes</span>'
                    : '<span class=warn>no</span> &middot; this output has '
                      + 'nothing plugged into it, so there will be silence')
        : '-'}</dd>` +
    `<dt>Volume</dt><dd>${d.muted ? '<span class=warn>muted</span>'
                                  : d.volume + '%'}</dd>` +
    `<dt>Outputs found</dt><dd>${d.outputs.length}`
      + (live.length ? ' &middot; ' + live.length + ' with something attached'
                     : '') + '</dd>';
  const sel = document.getElementById('audioout');
  if(sel && !dirty){
    const want = d.configured || 'auto';
    sel.innerHTML = '<option value="auto">automatic (prefer HDMI)</option>';
    for(const o of d.outputs){
      const el = document.createElement('option');
      el.value = o.id;
      el.textContent = (o.label || o.name)
        + ' \u2014 ' + o.kind.toUpperCase()
        + (o.live ? '' : ' (nothing attached)');
      sel.appendChild(el);
    }
    sel.value = want;
  }
  const vr = document.getElementById('volrange');
  if(vr && !dirty && document.activeElement !== vr){
    vr.value = d.muted ? 0 : d.volume;
    document.getElementById('vollabel').textContent =
      d.muted ? 'muted' : d.volume + '%';
  }
  const mb = document.getElementById('mutebtn');
  if(mb) mb.textContent = d.muted ? 'Unmute' : 'Mute';
}

async function refreshPrinter(){
  let d, s;
  try {
    d = await (await fetch('/api/printer')).json();
    s = await (await fetch('/api/status')).json();
  } catch(e){ return; }
  const info = document.getElementById('printinfo');
  if(!info) return;
  info.innerHTML =
    `<dt>CUPS</dt><dd>${d.cups ? '<span class=ok>installed</span>'
                              : '<span class=warn>not installed</span>'}</dd>` +
    `<dt>TM-T88V driver</dt><dd>${d.tmt88v
        ? '<span class=ok>installed</span> &middot; can render HTML'
        : '<span class=warn>not installed</span> &middot; only raw ESC/POS'}</dd>` +
    `<dt>Queues</dt><dd>${d.queues.length ? d.queues.join(', ')
                                          : 'none configured'}</dd>` +
    `<dt>Default</dt><dd>${d.default || '-'}</dd>` +
    (s.last_print ? `<dt>Last print</dt><dd>${s.last_print}</dd>` : '');
  const btn = document.getElementById('addtmbtn');
  if(btn) btn.style.display = (d.tmt88v && !d.queues.length) ? '' : 'none';
  // Say what is true NOW. This used to tell you to press "Add TM-T88V" even
  // after the queue existed, which reads as though nothing had happened.
  const note = document.getElementById('printnote');
  if(note){
    let extra;
    if(d.queues.length){
      extra = ' Printing goes to <b><span id=pqname></span></b>, which is set '
            + 'up and ready.';
    } else if(d.tmt88v){
      extra = ' The TM-T88V driver is installed but has no queue yet &mdash; '
            + 'press <b>Add TM-T88V</b> to create one.';
    } else {
      extra = ' No print driver is installed, so only a raw ESC/POS queue is '
            + 'possible, which cannot render HTML.';
    }
    note.innerHTML =
      'The page prints with <b>no dialog, no header or footer and zero '
      + 'margins</b>.' + extra
      + '<br><br><b>Other printers.</b> A network printer that speaks '
      + '<b>IPP Everywhere</b> (AirPrint) needs no driver &mdash; add it by '
      + 'address. Automatic discovery is not available on this image, which '
      + 'ships no mDNS responder, so you have to type the address. Any '
      + 'thermal printer that speaks <b>ESC/POS</b> works as a raw queue, '
      + 'sending bytes rather than rendering a page. Printers that need a '
      + 'vendor driver are <b>not</b> supported: this image ships no PPDs '
      + 'beyond the TM-T88V.';
    const pq = document.getElementById('pqname');
    if(pq) pq.textContent = d.default || d.queues[0] || '';
  }
  const sel = document.getElementById('printer');
  if(sel && !dirty){
    const want = s.printer || '';
    sel.innerHTML = '<option value="">system default</option>';
    for(const q of d.queues){
      const o = document.createElement('option');
      o.value = q; o.textContent = q; sel.appendChild(o);
    }
    sel.value = want;
    const w = document.getElementById('printwidth');
    if(w && !w.value) w.value = 80;
  }
}

async function addTmt88v(){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent='Looking for a TM-T88V...';
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'add-tmt88v'})});
    const d = await r.json();
    m.className='msg'+(d.ok?'':' warn'); m.textContent=d.detail||'done';
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
  refreshPrinter();
}

async function savePrint(){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent='Saving...';
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({
        printer: document.getElementById('printer').value,
        print_width_mm: parseInt(document.getElementById('printwidth').value||80),
        print_margins_mm: 0, print_enabled: true})});
    await r.json(); m.textContent='Saved'; dirty=false;
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
  refreshPrinter();
}

async function printerCmd(action){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent=action+'...';
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action, name: document.getElementById('printer').value})});
    const d = await r.json();
    m.className='msg'+(d.ok?'':' warn'); m.textContent=d.detail||'done';
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
}

async function refreshPhase(){
  let s; try { s = await (await fetch('/api/status')).json(); } catch(e){ return; }
  const nice = s.configured === false
    ? '<span class=ok>ready for setup &mdash; no page chosen yet</span>'
    : ({'ready':'<span class=ok>page loaded</span>',
        'waiting-site':'<span class=warn>on the network, site unreachable</span>',
        'waiting-network':'<span class=warn>waiting for a network</span>',
        'starting':'starting'}[s.net_phase] || s.net_phase);
  document.getElementById('netphase').innerHTML = `
    <dt>Phase</dt><dd>${nice}</dd>
    <dt>Showing</dt><dd>${s.showing_startup?'the network page':'the configured page'}</dd>
    <dt>Cache at boot</dt><dd>${s.clear_cache_on_start
        ? 'cleared every start' : 'kept between starts'}</dd>`;
  const cb = document.getElementById('cosbtn');
  if(cb) cb.textContent = 'Clear cache at boot: '
    + (s.clear_cache_on_start ? 'ON' : 'OFF');
  lastStatus = s;
  const os = document.getElementById('offsecs');
  if(os && document.activeElement !== os)
    os.value = (s.offline_after_seconds !== undefined) ? s.offline_after_seconds : 45;
  const ob = document.getElementById('offborderbtn');
  if(ob) ob.textContent = s.offline_border ? 'ON' : 'OFF';
  const np = document.getElementById('netphase');
  if(np && s.offline)
    np.innerHTML += '<dt>Offline</dt><dd><span class=warn>yes &mdash; red edge '
                  + 'is showing</span></dd>';
}

buildTabs();
(() => {
  const cb = document.getElementById('shotauto');
  if(!cb) return;
  cb.addEventListener('change', toggleShotAuto);
  try { cb.checked = localStorage.getItem('ds-shot-auto') === '1'; } catch(e){}
  if(cb.checked) toggleShotAuto();
})();
refresh(); setInterval(refresh, 5000);
refreshTime(); setInterval(refreshTime, 20000);
refreshDisplay();
refreshCec();
refreshPrinter();
refreshAudio();
refreshPhase(); setInterval(refreshPhase, 5000);
refreshSsh(); setInterval(refreshSsh, 15000);
refreshNet(); setInterval(refreshNet, 10000);
</script></body></html>"""


SELFTEST_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteadyScreen self-test</title>
<!--FAVICON-->
<style>
 <!--BRANDCSS-->
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;background:#15171a;color:#e8eaed;
   font:16px/1.5 system-ui,-apple-system,sans-serif;padding:1.5rem}
 .wrap{max-width:760px;margin:0 auto}
 h1{font-size:1.2rem;margin:0 0 1rem}
 .card{background:#1e2126;border:1px solid #2e333a;border-radius:10px;
   padding:1rem;margin-bottom:1rem}
 h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;
   color:#9aa3ad;margin:0 0 .7rem;font-weight:600}
 input{width:100%;padding:1rem;font:1.3rem ui-monospace,monospace;
   background:#111316;border:2px solid #4c8dff;border-radius:8px;color:#fff}
 .scan{font:1.1rem ui-monospace,monospace;margin-top:.8rem}
 .scan div{padding:.35rem 0;border-bottom:1px solid #2e333a}
 .fast{color:#3ecf8e} .slow{color:#ffb454}
 .hint{color:#9aa3ad;font-size:.85rem;margin-top:.6rem}
 #pad{height:150px;background:#111316;border:1px dashed #3a4048;
   border-radius:8px;position:relative;touch-action:none}
 .dot{position:absolute;width:26px;height:26px;margin:-13px 0 0 -13px;
   border-radius:50%;background:#4c8dff88;border:2px solid #4c8dff}
 dl{display:grid;grid-template-columns:auto 1fr;gap:.3rem .9rem;margin:0;
   font-size:.9rem}
 dt{color:#9aa3ad} dd{margin:0;font-family:ui-monospace,monospace}
</style></head><body><div class=wrap>
<h1><!--BRAND--> self-test</h1>

<div class=card>
 <h2>Barcode scanner / keyboard</h2>
 <input id=inp autocomplete=off autocapitalize=off spellcheck=false
        placeholder="Scan a barcode or type here">
 <div class=hint>The box keeps focus by itself. A scan arrives as a fast burst
   of keystrokes ending in Enter &mdash; bursts under 30&nbsp;ms/char are
   flagged <span class=fast>scanner</span>, slower ones
   <span class=slow>typed</span>.</div>
 <div class=scan id=scans></div>
</div>

<div class=card>
 <h2>Touch</h2>
 <div id=pad></div>
 <div class=hint id=touchinfo>Tap or drag in the box above.</div>
</div>

<div class=card>
 <h2>Display</h2>
 <dl id=info></dl>
</div>
</div>
<script>
const inp = document.getElementById('inp');
const scans = document.getElementById('scans');
let first = 0, count = 0;

function keepFocus(){ if(document.activeElement !== inp) inp.focus(); }
inp.addEventListener('blur', () => setTimeout(keepFocus, 0));
setInterval(keepFocus, 1000); keepFocus();

inp.addEventListener('keydown', e => {
  const now = performance.now();
  if(!count) first = now;
  count++;
  if(e.key === 'Enter'){
    const value = inp.value;
    const per = count > 1 ? (now - first) / (count - 1) : 999;
    const fast = per < 30;
    const row = document.createElement('div');
    row.className = fast ? 'fast' : 'slow';
    row.textContent = `${fast ? 'scanner' : 'typed  '}  ${value}   ` +
      `(${value.length} chars, ${per.toFixed(1)} ms/char)`;
    scans.prepend(row);
    while(scans.children.length > 8) scans.lastChild.remove();
    inp.value = ''; count = 0;
    e.preventDefault();
  }
});

const pad = document.getElementById('pad');
const ti = document.getElementById('touchinfo');
function mark(x, y, kind, extra){
  const r = pad.getBoundingClientRect();
  const d = document.createElement('div');
  d.className = 'dot';
  d.style.left = (x - r.left) + 'px'; d.style.top = (y - r.top) + 'px';
  pad.appendChild(d);
  setTimeout(() => d.remove(), 1200);
  ti.textContent = `${kind} at ${Math.round(x)},${Math.round(y)}${extra||''}`;
}
pad.addEventListener('pointerdown', e => {
  pad.setPointerCapture(e.pointerId);
  mark(e.clientX, e.clientY, e.pointerType,
       ` \u00b7 id ${e.pointerId}`);
});
pad.addEventListener('pointermove', e => {
  if(e.buttons) mark(e.clientX, e.clientY, e.pointerType + ' drag');
});

document.getElementById('info').innerHTML = `
  <dt>Window</dt><dd>${innerWidth} x ${innerHeight} css px</dd>
  <dt>Screen</dt><dd>${screen.width} x ${screen.height}</dd>
  <dt>Pixel ratio</dt><dd>${devicePixelRatio}</dd>
  <dt>Touch points</dt><dd>${navigator.maxTouchPoints}</dd>
  <dt>Engine</dt><dd>${navigator.userAgent.match(/AppleWebKit\/[\d.]+/)||'?'}</dd>`;
</script></body></html>"""


STARTUP_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteadyScreen - network</title>
<!--FAVICON-->
<style>
 <!--BRANDCSS-->
 :root{color-scheme:dark;--bg:#12141a;--card:#1b1f27;--line:#2b313c;
   --fg:#eef1f5;--dim:#94a0b0;--acc:#4c8dff;--ok:#3ecf8e;--warn:#ffb454}
 *{box-sizing:border-box}
 html{height:100%}
 /* The page must SCROLL. It used to be a full-height flex box centred both
    ways, so a wifi list taller than the screen was simply unreachable -- the
    network you needed could sit above the top edge with no way to get to it. */
 body{margin:0;min-height:100%;background:var(--bg);color:var(--fg);
   font:20px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;
   display:flex;align-items:flex-start;justify-content:center;padding:2rem;
   overflow-y:auto;-webkit-user-select:none;user-select:none;position:relative}
 /* the boot splash image again, so plymouth handing over to X does not
    look like three unrelated screens in a row */
 body::before{content:"";position:fixed;inset:0;z-index:-1;
   background:var(--bg) url(/api/splash/image.png) center/contain no-repeat;
   opacity:.16}
 .wrap{width:100%;max-width:940px}
 .head{display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem}
 .dot{width:14px;height:14px;border-radius:50%;background:var(--warn);
   flex:none;animation:pulse 1.6s ease-in-out infinite}
 .dot.ok{background:var(--ok);animation:none}
 .dot.bad{background:#ff6b6b;animation:none}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
 h1{font-size:2rem;margin:0;font-weight:600}
 .sub{color:var(--dim);font-size:1rem;margin-top:.3rem}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
   padding:1.2rem;margin-bottom:1rem}
 h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.05em;
   color:var(--dim);margin:0 0 .9rem;font-weight:600}
 dl{display:grid;grid-template-columns:auto 1fr;gap:.4rem 1.2rem;margin:0;
   font-size:1rem}
 dt{color:var(--dim)} dd{margin:0;font-family:ui-monospace,monospace}
 button{font:inherit;color:var(--fg);background:#2a3039;border:1px solid var(--line);
   border-radius:10px;padding:.9rem 1.2rem;min-height:3.4rem;cursor:pointer}
 button:active{background:#333b46}
 button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
 .net{display:flex;align-items:center;gap:.8rem;width:100%;text-align:left;
   margin-bottom:.5rem}
 .net .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tag{font-size:.75rem;padding:.15rem .5rem;border-radius:6px;background:#333b46;
   color:var(--dim);flex:none}
 .tag.on{background:#17402f;color:var(--ok)}
 .bars{display:inline-flex;align-items:flex-end;gap:2px;height:1em;flex:none}
 .bars i{width:4px;background:#4c5560;border-radius:1px}
 .bars i.on{background:var(--acc)}
 .row{display:flex;gap:.7rem;flex-wrap:wrap;margin-top:.8rem}
 .msg{margin-top:.8rem;color:var(--dim);min-height:1.4em}
 .msg.warn{color:var(--warn)}
 input{width:100%;padding:1rem;font:1.2rem ui-monospace,monospace;
   background:#0e1015;border:2px solid var(--acc);border-radius:10px;
   color:#fff;margin-bottom:.6rem}
 .ident{margin:.2rem 0 1rem;color:var(--dim);
   font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
   font-size:.85rem;line-height:1.6}
 .kb{display:grid;grid-template-columns:repeat(10,1fr);gap:.4rem}
 .kb button{padding:.7rem 0;min-height:3rem;font-size:1rem}
.pwrow{display:flex;gap:.5rem;align-items:stretch;margin:.15rem 0 .2rem}
.pwrow input{flex:1 1 auto;min-width:0}
.pwrow button{flex:0 0 auto;padding:.5rem 1rem;font-size:.9rem}
 .kb .wide{grid-column:span 2}
 .hide{display:none}
 /* belt and braces: each list scrolls on its own too, so neither can push
    the buttons below it off the bottom of the screen */
 #scanlist,#saved{max-height:46vh;overflow-y:auto}
 .scrollnote{color:var(--dim);font-size:.85rem;margin-top:.4rem}
</style></head><body><div class=wrap>

<div class=head>
  <span class=dot id=dot></span>
  <div><h1 id=title>Starting up</h1><div class=sub id=sub>&nbsp;</div></div>
</div>

<!-- No "change this device" card here. This screen exists to get a network
     up, nothing else; once there is one the machine shows the welcome page,
     which carries the setup address. Two places telling you the same thing
     means one of them is wrong the day it changes. -->

<div class=card id=nonet>
  <h2>No network yet</h2>
  <div style="font-size:1.05rem" id=nonethow></div>
</div>

<div class=card>
  <h2>Connection</h2>
  <dl id=state></dl>
</div>

<div class=card id=savedcard>
  <h2>Saved networks</h2>
  <div id=saved></div>
</div>

<div class=card id=othercard>
  <h2>Other networks</h2>
  <div id=scanlist></div>
  <div class=row><button onclick="scan()" id=scanbtn>Scan for networks</button></div>
  <div class=msg id=msg></div>
</div>

<div class=ident id=ident>&nbsp;</div>

<div class=card id=noinput style="display:none">
  <h2>Display only</h2>
  <div style="color:var(--dim)" id=noinputtext>
    No touchscreen, keyboard or mouse is connected, so the controls are
    hidden. Plug one in and they appear, or configure this device from the
    admin page on another machine on the network.
  </div>
</div>

<div class=card id=pwcard style="display:none">
  <h2 id=pwtitle>Password</h2>
  <div class=pwrow>
    <input type=password id=pw autocomplete="off" autocapitalize="off"
           spellcheck="false" placeholder="type here, or use the keys below">
    <button type=button id=pwshow onmousedown="event.preventDefault()"
      onclick="pwToggle2()">Show</button>
    <button type=button id=kbtoggle onmousedown="event.preventDefault()"
      onclick="kbToggle()">Show keyboard</button>
  </div>
  <div class=kb id=kb></div>
  <div class=row>
    <button class=primary onclick="joinNow()">Connect</button>
    <button onclick="cancelPw()">Cancel</button>
  </div>
</div>

</div>
<script>
let scanning = false, pwSsid = null, shift = false;

function bars(sig){
  const n = sig>=75?4 : sig>=50?3 : sig>=25?2 : 1;
  let h=''; for(let i=1;i<=4;i++)
    h += `<i class="${i<=n?'on':''}" style="height:${i*25}%"></i>`;
  return `<span class=bars>${h}</span>`;
}
// Hide every control on a machine that has no way to operate them, and
// re-check on each poll so plugging a keyboard in makes them appear.
let lastInput = null;
function applyInputMode(inp){
  const key = JSON.stringify(inp);
  if(key === lastInput) return;
  lastInput = key;
  const on = !!inp.interactive;        // keyboard, mouse or touch will do
  document.getElementById('othercard').style.display = on ? '' : 'none';
  document.getElementById('noinput').style.display = on ? 'none' : '';
  document.getElementById('savedcard').dataset.interactive = on ? '1' : '0';
  if(!on) document.getElementById('pwcard').style.display = 'none';

  // A keyboard being DETECTED is not the same as a keyboard that works. One
  // that is plugged in but faulty leaves a mouse as the only way in, and the
  // on-screen keyboard was hidden precisely because a keyboard was "there".
  // So it is hidden by default when a keyboard exists, and always reachable.
  const pw = document.getElementById('pw');
  if(inp.keyboard){ pw.removeAttribute('readonly'); }
  else { pw.setAttribute('readonly',''); }
  const kb = document.getElementById('kb');
  const toggle = document.getElementById('kbtoggle');
  if(kbForced === null){
    kb.style.display = (inp.keyboard && !inp.touch) ? 'none' : '';
  } else {
    kb.style.display = kbForced ? '' : 'none';
  }
  if(toggle){
    toggle.textContent = kb.style.display === 'none'
        ? 'Show keyboard' : 'Hide keyboard';
  }
}

// null = follow what is detected; true/false = the operator decided.
var kbForced = null;
function kbToggle(){
  const kb = document.getElementById('kb');
  kbForced = (kb.style.display === 'none');
  kb.style.display = kbForced ? '' : 'none';
  document.getElementById('kbtoggle').textContent =
      kbForced ? 'Hide keyboard' : 'Show keyboard';
  if(kbForced){ kb.scrollIntoView({block:'nearest'}); }
}

function esc(t){ return (t||'').replace(/[<&"]/g, c=>({'<':'&lt;','&':'&amp;','"':'&quot;'}[c])); }
function msg(t, cls){ const m=document.getElementById('msg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

async function refresh(){
  let d; try { d = await (await fetch('/api/startup-state')).json(); } catch(e){ return; }
  applyInputMode(d.input || {});
  const dot = document.getElementById('dot');
  const title = document.getElementById('title');
  const sub = document.getElementById('sub');

  if(d.configured === false){
    // A fresh install has no page yet, and that is normal rather than broken.
    // Say what to do next instead of pretending to wait for something.
    dot.className = d.phase === 'waiting-network' ? 'dot' : 'dot ok';
    title.textContent = 'Ready to set up';
    sub.textContent = d.phase === 'waiting-network'
        ? 'Join a network below, then open the address in the card above.'
        : 'This display has no page yet. Open the address in the card above '
          + 'to choose one.';
  } else if(d.phase === 'ready'){
    dot.className = 'dot ok';
    title.textContent = 'Connected';
    sub.textContent = 'Loading ' + (d.target_host || 'the page') + '...';
  } else if(d.phase === 'waiting-site'){
    // On the network is not the same as on the internet, and neither is the
    // same as "that particular site is answering". Say which one it is.
    const online = d.connectivity === 'full';
    dot.className = online ? 'dot' : 'dot bad';
    title.textContent = online ? 'Waiting for the page'
                               : 'No internet connection';
    sub.textContent = (online
        ? 'Connected, but ' + (d.target_host||'the site') + ' has not answered yet.'
        : 'On a network, but there is no internet connection yet.')
        + (d.last_error ? '  (' + d.last_error + ')' : '');
  } else {
    dot.className = 'dot';
    title.textContent = 'Connecting to the network';
    sub.textContent = 'Waiting for a network connection...';
  }

  // With no network there is no admin page to send anyone to. Saying "open
  // http://<address>" when there is no address is worse than saying nothing:
  // it sends somebody to a machine that cannot be reached, and the field
  // report was exactly that. Say what will actually get a network up.
  const nonet = document.getElementById('nonet');
  if(nonet){
    if(d.admin_ip){
      nonet.style.display = 'none';
    } else {
      const kb = (d.input && d.input.keyboard);
      document.getElementById('nonethow').innerHTML =
          '<b>Plug in an ethernet cable</b> to set this display up from '
        + 'another computer &mdash; it will show the address here once it has '
        + 'one.'
        + '<div style="margin-top:.6rem">Or join a wi-fi network below. '
        + (kb ? 'Use the keyboard to type the password.'
              : 'With no keyboard attached, use <b>Show keyboard</b> to type '
                + 'the password on screen.')
        + '</div>';
      nonet.style.display = '';
    }
  }

  const ident = document.getElementById('ident');
  if(ident){
    const bits = [esc(d.client_name || '')];
    if(d.admin_ip){ bits.push('http://' + d.admin_ip
                              + ((d.admin_port||8080) === 80 ? '' : ':' + (d.admin_port||8080))); }
    bits.push('software ' + (d.software_version || '?')
              + '  \u00b7  os ' + (d.os_version === undefined ? '?' : d.os_version));
    ident.innerHTML = bits.join('<br>');
  }

  const line = x => x ? `${x.state}${x.connection?' &middot; '+esc(x.connection):''}`
                       + `${x.ip?' &middot; '+x.ip:''}` : 'not present';
  document.getElementById('state').innerHTML = `
    <dt>Wired</dt><dd>${line(d.net.wired)}</dd>
    <dt>Wi-Fi</dt><dd>${line(d.net.wifi)}</dd>
    <dt>Internet</dt><dd>${d.connectivity}</dd>
    <dt>Page</dt><dd>${esc(d.target_host||'-')}</dd>`;

  const sc = document.getElementById('savedcard');
  const box = document.getElementById('saved');
  if(!d.saved.length){ sc.style.display='none'; }
  else {
    sc.style.display='';
    box.innerHTML='';
    for(const n of d.saved){
      const b=document.createElement('button');
      b.className='net';
      b.innerHTML = `<span class=name>${esc(n.name)}</span>` +
        (n.active ? '<span class="tag on">connected</span>'
                  : '<span class=tag>saved</span>');
      const usable = document.getElementById('savedcard').dataset.interactive !== '0';
      if(!n.active && usable) b.onclick = () => connectSaved(n.name);
      if(!usable) b.style.pointerEvents = 'none';
      box.appendChild(b);
    }
  }
}

async function connectSaved(name){
  msg('Connecting to ' + name + '...');
  try{
    const r = await fetch('/api/wifi/connect-saved',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({ssid:name})});
    const d = await r.json();
    msg(d.ok ? 'Connected to '+name : 'Failed: '+(d.detail||''), d.ok?'':'warn');
  }catch(e){ msg('Failed: '+e,'warn'); }
  refresh();
}

async function scan(){
  if(scanning) return;
  scanning = true;
  const b=document.getElementById('scanbtn');
  b.disabled=true; b.textContent='Scanning...';
  try{
    const r = await fetch('/api/wifi/scan');
    const d = await r.json();
    const box=document.getElementById('scanlist');
    box.innerHTML='';
    for(const n of (d.networks||[])){
      const open = !n.security || n.security==='open';
      const el=document.createElement('button');
      el.className='net';
      el.innerHTML = bars(n.signal) + `<span class=name>${esc(n.ssid)}</span>` +
        `<span class=tag>${open?'open':'locked'}</span>`;
      el.onclick = () => open ? join(n.ssid,'') : askPassword(n.ssid);
      box.appendChild(el);
    }
    if(!d.networks || !d.networks.length) msg('No networks found');
    else if(d.networks.length > 6)
      msg(d.networks.length + ' networks found \u2014 the list scrolls');
  }catch(e){ msg('Scan failed: '+e,'warn'); }
  finally{ scanning=false; b.disabled=false; b.textContent='Scan for networks'; }
}

// These machines have a barcode scanner and a touchscreen, but usually no
// keyboard, so a wifi password has to be typeable on the glass.
const ROWS = ['1234567890','qwertyuiop','asdfghjkl','zxcvbnm'];
function buildKb(){
  const kb=document.getElementById('kb'); kb.innerHTML='';
  for(const row of ROWS)
    for(const ch of row){
      const b=document.createElement('button');
      b.textContent = shift ? ch.toUpperCase() : ch;
      b.type = 'button';
      // Keep focus in the field so a physical keyboard still works after
      // someone taps a key on the glass.
      b.onmousedown = ev => ev.preventDefault();
      b.onclick = () => { const i=document.getElementById('pw');
                          i.value += b.textContent; i.focus(); };
      kb.appendChild(b);
    }
  const mk=(label,fn,wide)=>{ const b=document.createElement('button');
    b.type='button'; b.textContent=label; if(wide) b.className='wide';
    b.onmousedown = ev => ev.preventDefault();      // never steal the caret
    b.onclick = () => { fn(); const i=document.getElementById('pw');
                        if(i) i.focus(); };
    kb.appendChild(b); };
  mk('shift', ()=>{ shift=!shift; buildKb(); }, true);
  mk('-', ()=>{ document.getElementById('pw').value+='-'; });
  mk('_', ()=>{ document.getElementById('pw').value+='_'; });
  mk('.', ()=>{ document.getElementById('pw').value+='.'; });
  mk('!', ()=>{ document.getElementById('pw').value+='!'; });
  mk('@', ()=>{ document.getElementById('pw').value+='@'; });
  mk('#', ()=>{ document.getElementById('pw').value+='#'; });
  mk('back', ()=>{ const i=document.getElementById('pw');
                   i.value=i.value.slice(0,-1); }, true);
}
function askPassword(ssid, why){
  pwSsid = ssid;
  document.getElementById('pwtitle').textContent = 'Password for ' + ssid;
  const p = document.getElementById('pw');
  p.value='';
  p.type = 'password';                      // never reopen already revealed
  const sb = document.getElementById('pwshow');
  if (sb) { sb.textContent = 'Show'; sb.setAttribute('aria-pressed','false'); }
  document.getElementById('pwcard').style.display='';
  shift=false; buildKb();
  document.getElementById('pwcard').scrollIntoView({behavior:'smooth'});
  if(why) msg(why, 'warn');
  // Focus it so a real keyboard just works. The field used to be readonly --
  // the on-screen keys wrote into it and nothing else could, which is why
  // typing only worked after clicking and Enter did nothing.
  setTimeout(() => { try { p.focus(); p.select(); } catch(e){} }, 60);
}
// The startup page has its own script, so it needs its own toggle. Typing a
// wifi key on a touchscreen keyboard with no way to check it is how a wrong
// password gets saved in the first place.
function pwToggle2(){
  const i = document.getElementById('pw'), b = document.getElementById('pwshow');
  const show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  b.textContent = show ? 'Hide' : 'Show';
  b.setAttribute('aria-pressed', show ? 'true' : 'false');
  i.focus();
}

function cancelPw(){
  pwSsid=null;
  document.getElementById('pwcard').style.display='none';
  const s=document.getElementById('scanbtn'); if(s) s.focus();
}
function joinNow(){ if(pwSsid) join(pwSsid, document.getElementById('pw').value); }

async function join(ssid, psk){
  cancelPw();
  msg('Joining ' + ssid + '... this can take 20 seconds');
  try{
    const r = await fetch('/api/wifi/connect',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({ssid, password: psk})});
    const d = await r.json();
    if(d.ok){ msg('Connected to '+ssid); }
    else if(d.bad_password){
      // Put the box straight back with the reason. Being told the password
      // was refused and then having no way to retype it is the worst of both.
      askPassword(ssid, 'That password was not accepted. Try again.');
    } else {
      msg('Failed: '+(d.detail||''), 'warn');
    }
  }catch(e){ msg('Failed: '+e,'warn'); }
  refresh();
}

// Enter connects, Escape cancels -- from anywhere in the password card.
document.addEventListener('keydown', e => {
  const card = document.getElementById('pwcard');
  if(!card || card.style.display === 'none') return;
  if(e.key === 'Enter'){ e.preventDefault(); joinNow(); }
  if(e.key === 'Escape'){ e.preventDefault(); cancelPw(); }
});

refresh();
setInterval(refresh, 4000);

// Give the page a focused control on arrival. Without this the first Tab goes
// nowhere useful and the Scan button could only be reached by guessing.
window.addEventListener('load', () => {
  setTimeout(() => {
    const b = document.getElementById('scanbtn');
    if (b && b.offsetParent !== null) { try { b.focus(); } catch(e){} }
  }, 150);
});
</script></body></html>"""

# One pass, after every page is defined. Doing it here rather than at serve
# time means a broken token shows up the moment the module loads, not on the
# screen of a machine in a shop.
WELCOME_PAGE = brandify(WELCOME_PAGE)
STARTUP_PAGE = brandify(STARTUP_PAGE)
ADMIN_PAGE = brandify(ADMIN_PAGE)
SELFTEST_PAGE = brandify(SELFTEST_PAGE)
for _p, _n in ((WELCOME_PAGE, "welcome"), (STARTUP_PAGE, "startup"),
               (ADMIN_PAGE, "admin"), (SELFTEST_PAGE, "selftest")):
    if "<!--BRAND" in _p or "<!--FAVICON" in _p:
        raise SystemExit("ds: %s page has an unsubstituted brand token" % _n)



class AdminHandler(BaseHTTPRequestHandler):
    kiosk = None          # set once the browser is built
    cfg = {}              # set before the server starts listening
    ports = []            # every port actually being served
    start_error = ""      # why the browser never came up, if it did not

    server_version = "SteadyScreen/" + VERSION
    kiosk = None

    def log_message(self, fmt, *args):        # quiet; journald gets the rest
        pass

    # -- helpers -----------------------------------------------------------

    # Paths that mean something before the browser exists. Everything else
    # needs the live client and answers 503 until it is there.
    # /api/startup-state belongs here: it is what BOTH built-in pages poll, and
    # the browser object is created after the server is listening, so without it
    # the first poll gets a 503 whose JSON body parses fine and renders every
    # field as undefined -- "waiting for a network address" on a machine that
    # has one. A wrong first frame is still a wrong frame.
    EARLY_OK = ("/", "/index.html", "/startup", "/welcome", "/healthz",
                "/selftest", "/api/splash/image.png", "/api/startup-state")

    def _ready(self):
        """The browser object is built after the server starts listening, so
        for a moment there is a server and no client. Say so honestly rather
        than throwing AttributeError at whoever is trying to get in."""
        if self.kiosk is not None:
            return True
        if self.start_error:
            self._json(503, {"error": "the display failed to start",
                             "detail": self.start_error})
            return False
        self._json(503, {"error": "starting up",
                         "detail": "the display is still starting. The admin "
                                   "page is up; this reading is not ready yet."})
        return False

    def _authed(self):
        # cfg is set before the server starts, so the password is enforced from
        # the first request even though the client is not built yet.
        base = self.kiosk.cfg if self.kiosk is not None else (self.cfg or {})
        want = base.get("admin_password") or ""
        if not want:
            return True
        got = self.headers.get("Authorization", "")
        if got.startswith("Basic "):
            try:
                raw = base64.b64decode(got[6:]).decode("utf-8", "replace")
                if raw.split(":", 1)[-1] == want:
                    return True
            except Exception:                                 # noqa: BLE001
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SteadyScreen"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                     # noqa: BLE001
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):                                         # noqa: N802
        if not self._authed():
            return
        path = self.path.split("?", 1)[0]
        if path not in self.EARLY_OK and not self._ready():
            return
        if path in ("/", "/index.html"):
            self._send(200, ADMIN_PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, self.kiosk.status())

        elif path == "/api/diag":
            from urllib.parse import parse_qs, urlparse
            q = urlparse(self.path).query
            section = (parse_qs(q).get("section") or ["all"])[0]
            ok, out = diagnostics(section)
            body = out.encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/screenshot.png", "/api/screenshot.jpg"):
            # jpeg + a smaller width for the auto-refreshing view: a 500 KB PNG
            # every two seconds is a lot of work for an Atom to do forever
            q = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
            jpeg = path.endswith(".jpg")
            try:
                width = max(320, min(1920, int(q.get("w", ["1024"])[0])))
            except ValueError:
                width = 1024
            img = self.kiosk.screenshot(max_width=width,
                                        fmt="jpeg" if jpeg else "png")
            if img:
                self._send(200, img, "image/jpeg" if jpeg else "image/png")
            else:
                self._json(503, {"error": "could not capture the screen"})
        elif path == "/api/net":
            st = net_status()
            st["saved"] = saved_wifi_networks()
            self._json(200, st)
        elif path == "/api/wifi/scan":
            nets, err = wifi_scan(rescan="norescan" not in self.path)
            if err:
                self._json(503, {"error": err})
            else:
                self._json(200, {"networks": nets,
                                 "saved": wifi_saved(None)})
        elif path == "/api/ssh":
            self._json(200, ssh_status())
        elif path == "/startup":
            self._send(200, STARTUP_PAGE, "text/html; charset=utf-8")
        elif path == "/welcome":
            self._send(200, WELCOME_PAGE, "text/html; charset=utf-8")
        elif path == "/api/startup-state":
            k = self.kiosk
            d = k.net_detail or {}
            self._json(200, {
                "phase": k.net_phase,
                "connectivity": d.get("connectivity", "unknown"),
                "net": d.get("net") or net_status(),
                "target_host": d.get("target_host"),
                # Where to go to fix this. The screen showing an IP is not the
                # same as the screen telling you what to do with it.
                "admin_ip": primary_ip(),
                "admin_port": k.cfg.get("admin_port", 8080),
                "configured": not is_unconfigured(k.cfg),
                "client_name": IDENTITY.get("name", ""),
                # Printed on both setup screens. Somebody standing at a display
                # asking "what is this running?" should not have to find a
                # laptop to answer it.
                "software_version": software_version(),
                "os_version": os_version(),
                "ports": list(getattr(AdminHandler, "ports", [])),
                # Filled in once account linking exists; the page already has
                # a place for it.
                "pairing_code": "",
                "url": k.cfg.get("url", ""),
                "gave_up": bool(getattr(k, "_fell_back", False)),
                "saved": saved_wifi_networks(),
                "input": input_devices(),
                "last_error": ("" if "cancel" in
                               (self.kiosk.last_error or "").lower()
                               else self.kiosk.last_error),
            })
        elif path == "/api/time":
            st = time_status()
            st["zones"] = timezones()
            st["common"] = [z for z in COMMON_ZONES if z in st["zones"]]
            self._json(200, st)
        elif path == "/api/cec":
            self._json(200, cec_status())
        elif path == "/api/printer":
            self._json(200, printer_status())
        elif path == "/api/audio":
            st = audio_status()
            st["configured"] = self.kiosk.cfg.get("audio_output", "auto")
            self._json(200, st)
        elif path == "/api/display":
            self._json(200, {"outputs": display_info(),
                             "configured": self.kiosk.cfg.get("resolution",
                                                              "auto")})
        elif path in ("/api/splash", "/api/splash/image",
                      "/api/splash/image/clear") and \
                k.cfg.get("splash_managed", True):
            # Enforced here, not merely hidden in the page: the endpoint is
            # reachable by anyone on the LAN with curl, so the button being
            # absent would protect nothing.
            self._json(403, {
                "ok": False,
                "error": "the boot splash is managed centrally",
                "detail": "Branding is set from the management server. "
                          "A client cannot change or remove it on its own.",
            })

        elif path == "/api/splash":
            self._json(200, splash_status())
        elif path == "/api/splash/image.png":
            try:
                with open(SPLASH_IMAGE, "rb") as fh:
                    self._send(200, fh.read(), "image/png")
            except OSError:
                self._send(404, "no splash image\n")
        elif path == "/selftest":
            self._send(200, SELFTEST_PAGE, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, "ok\n")
        else:
            self._send(404, "not found\n")

    def do_POST(self):                                        # noqa: N802
        if not self._authed():
            return
        if not self._ready():        # nothing here is meaningful without it
            return
        path = self.path.split("?", 1)[0]
        k = self.kiosk

        if path == "/api/url":
            url = self._body().get("url", "")
            if not url:
                return self._json(400, {"error": "no url"})
            GLib.idle_add(k.set_url, url)
            self._json(200, {"ok": True, "url": normalise_url(url)})

        elif path == "/api/reload":
            GLib.idle_add(k.reload, False)
            self._json(200, {"ok": True})

        elif path == "/api/cache/clear":
            everything = bool(self._body().get("everything"))
            GLib.idle_add(k.clear_cache, everything, True)
            self._json(200, {"ok": True,
                             "detail": ("clearing all site data"
                                        if everything else "clearing the cache")})

        elif path == "/api/reload-hard":
            GLib.idle_add(k.reload, True)
            self._json(200, {"ok": True})

        elif path == "/api/rotation":
            rot = (self._body().get("rotation") or "").strip()
            if rot not in ROTATIONS:
                return self._json(400, {"error": "rotation must be one of "
                                        + ", ".join(ROTATIONS)})
            # xrandr/xinput are plain subprocesses, safe to run off the main
            # loop, so the caller gets the real result rather than a guess
            ok, detail = apply_rotation(rot)
            if ok:
                GLib.idle_add(k.persist_rotation, rot, detail)
            self._json(200 if ok else 502,
                       {"ok": ok, "rotation": rot, "detail": detail})

        elif path == "/api/update":
            action = (self._body().get("action") or "check").strip()
            ok, detail, data = update_command(action)
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": detail, "result": data})

        elif path == "/api/printer":
            b = self._body()
            ok, detail = printer_command((b.get("action") or "").strip(),
                                         (b.get("name") or "").strip())
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/cec":
            ok, detail = cec_command((self._body().get("action") or "").strip())
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/audio":
            b = self._body()
            action = (b.get("action") or "").strip()
            if action == "test":
                ok, detail = audio_test()
            elif action == "output":
                target = (b.get("output") or "auto").strip()
                ok, detail = audio_set_output(target)
                if ok:
                    k.cfg["audio_output"] = target
                    save_config(k.cfg)
                    # A fresh softvol control starts at full scale, so the
                    # saved level has to be re-applied or changing output is
                    # also an unannounced jump to maximum volume.
                    if k.cfg.get("audio_muted"):
                        audio_set_mute(True)
                    else:
                        audio_set_volume(k.cfg.get("volume", 60))
            elif action == "volume":
                ok, detail = audio_set_volume(b.get("volume"))
                if ok:
                    k.cfg["volume"] = max(0, min(100, int(b.get("volume"))))
                    k.cfg["audio_muted"] = False
                    save_config(k.cfg)
            elif action == "mute":
                on = bool(b.get("muted"))
                ok, detail = audio_set_mute(on)
                if ok:
                    k.cfg["audio_muted"] = on
                    save_config(k.cfg)
                    if not on:
                        audio_set_volume(k.cfg.get("volume", 60))
            else:
                return self._json(400, {"error": "unknown action"})
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/resolution":
            mode = (self._body().get("mode") or "auto").strip()
            if mode != "auto" and not re.fullmatch(r"\d{3,5}x\d{3,5}", mode):
                return self._json(400, {"error": "mode must be auto or WxH"})
            ok, detail = apply_resolution(mode)
            if ok:
                GLib.idle_add(k.persist_resolution, mode, detail)
            self._json(200 if ok else 502,
                       {"ok": ok, "mode": mode, "detail": detail})

        elif path == "/api/settings":
            body = self._body()
            applied = {}
            if "zoom" in body:
                GLib.idle_add(k.set_zoom, body["zoom"])
                applied["zoom"] = body["zoom"]
            for key in ("refresh_seconds", "idle_reset_seconds",
                        "hardware_acceleration", "retry_seconds",
                        "hide_cursor", "admin_password",
                        "clear_cache_on_start", "wait_for_network",
                        "allow_location", "print_enabled", "printer",
                        "print_margins_mm", "print_width_mm",
                        "screenshot_cache_secs", "osk", "osk_caps",
                        "osk_offset_x", "osk_offset_y",
                        "offline_after_seconds", "offline_border"):
                # NOTE: splash_managed is deliberately NOT in this list. A
                # client that could unlock its own branding would make the
                # lock decorative.
                if key in body:
                    GLib.idle_add(k.set_option, key, body[key])
                    applied[key] = body[key]
            self._json(200, {"ok": True, "applied": applied})

        elif path == "/api/wifi/connect":
            body = self._body()
            ssid = (body.get("ssid") or "").strip()
            if not ssid:
                return self._json(400, {"error": "no ssid"})
            ok, detail, bad_password = wifi_connect(
                ssid, body.get("password", ""), bool(body.get("hidden")))
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": detail,
                        "bad_password": bad_password, "net": net_status()})

        elif path == "/api/wifi/connect-saved":
            ssid = (self._body().get("ssid") or "").strip()
            if not ssid:
                return self._json(400, {"error": "no ssid"})
            ok, out, err = nmcli("connection", "up", "id", ssid, timeout=60)
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": (err or out)[-300:]})

        elif path == "/api/time":
            body = self._body()
            ok, detail = set_time_settings(body.get("timezone"),
                                           body.get("ntp"))
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/splash":
            body = self._body()
            ok, detail = set_splash(body.get("enabled"))
            if ok and "enabled" in body:
                GLib.idle_add(k.set_option, "splash_enabled",
                              bool(body["enabled"]))
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/splash/image/clear":
            ok, detail = clear_splash_image()
            self._json(200 if ok else 502, {"ok": ok, "detail": detail})

        elif path == "/api/splash/image":
            # the browser posts the file's raw bytes as the body, which avoids
            # parsing multipart in a 200-line HTTP server
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > 12 * 1024 * 1024:
                return self._json(400, {"error": "image must be 1 byte to 12 MB"})
            ok, detail = set_splash_image(self.rfile.read(n))
            self._json(200 if ok else 400, {"ok": ok, "detail": detail})

        elif path == "/api/wifi/disconnect":
            dev, _ = wifi_device()
            ok, out, err = nmcli("device", "disconnect", dev or "")
            self._json(200 if ok else 502, {"ok": ok, "detail": err or out})

        elif path == "/api/wifi/forget":
            ssid = (self._body().get("ssid") or "").strip()
            names = wifi_saved(ssid) if ssid else []
            if not names:
                return self._json(404, {"error": "no saved network by that name"})
            ok, out, err = nmcli("connection", "delete", names[0])
            self._json(200 if ok else 502, {"ok": ok, "detail": err or out})

        elif path == "/api/wifi/radio":
            on = bool(self._body().get("on", True))
            ok, out, err = nmcli("radio", "wifi", "on" if on else "off")
            self._json(200 if ok else 502, {"ok": ok, "detail": err or out})

        elif path == "/api/ssh/key":
            ok, detail, info = add_auth_key(self._body().get("key", ""))
            self._json(200 if ok else 400,
                       {"ok": ok, "detail": detail, "key": info})

        elif path == "/api/ssh/key/delete":
            ok, detail = remove_auth_key(self._body().get("fingerprint", ""))
            self._json(200 if ok else 404, {"ok": ok, "detail": detail})

        elif path == "/api/ssh/password":
            ok, detail = set_login_password(self._body().get("password", ""))
            self._json(200 if ok else 400, {"ok": ok, "detail": detail})

        elif path == "/api/ssh/password-auth":
            on = bool(self._body().get("on", True))
            try:
                p = subprocess.run(
                    ["sudo", "-n", "/usr/local/sbin/kiosk-sshd-auth",
                     "on" if on else "off"],
                    capture_output=True, text=True, timeout=20)
                ok = p.returncode == 0
                self._json(200 if ok else 502,
                           {"ok": ok, "detail": (p.stderr or p.stdout).strip()})
            except Exception as exc:                          # noqa: BLE001
                self._json(502, {"ok": False, "detail": str(exc)})

        elif path == "/api/admin-password":
            pw = self._body().get("password", "")
            GLib.idle_add(k.set_option, "admin_password", pw)
            self._json(200, {"ok": True, "protected": bool(pw)})

        elif path == "/api/restart":
            self._json(200, {"ok": True})
            GLib.timeout_add(300, lambda: k.quit(0))

        elif path == "/api/reboot":
            self._json(200, {"ok": True})
            threading.Timer(0.5, lambda: subprocess.call(
                ["sudo", "-n", "/sbin/reboot"])).start()

        elif path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Timer(0.5, lambda: subprocess.call(
                ["sudo", "-n", "/sbin/poweroff"])).start()

        else:
            self._send(404, "not found\n")


def start_admin(cfg):
    """Listen before the browser exists.

    This used to be called *after* `DisplayClient(cfg)` returned, which had two
    consequences. The constructor ends by pointing the browser at
    http://127.0.0.1:8080/startup, so the very first load of the network page
    raced a server that was not listening yet. And anything that threw while
    building the client meant the admin page never came up at all -- the one
    moment you most need a way in, with kiosk-session restarting the process
    every two seconds behind you.

    Listening first makes the admin page the *first* thing that works, not the
    last.
    """
    AdminHandler.cfg = cfg
    bind = cfg["admin_bind"]
    port = int(cfg["admin_port"])

    # Port 80 as well as 8080, not instead of it.
    #
    # 80 is what you want to say out loud -- "open http://10.0.2.15" beats
    # reciting a port number to someone standing at a counter. 8080 stays
    # because every existing client, note and bookmark uses it, and a port
    # change is not worth breaking those over.
    #
    # Binding 80 as an unprivileged user needs
    # net.ipv4.ip_unprivileged_port_start=80, which the image sets. If it is
    # missing -- an older image, or a client that has not rebooted since the
    # sysctl landed -- 80 simply fails and 8080 carries on. Never fatal.
    served = []
    for p in [port] + ([int(cfg.get("admin_port_alt") or 0)]
                       if int(cfg.get("admin_port_alt") or 0) not in (0, port)
                       else []):
        try:
            httpd = ThreadingHTTPServer((bind, p), AdminHandler)
        except OSError as exc:
            print("ds: admin port %d unavailable (%s)" % (p, exc),
                  file=sys.stderr)
            continue
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="kiosk-admin-%d" % p).start()
        served.append((p, httpd))

    if not served:
        raise OSError("could not listen on any admin port")

    AdminHandler.ports = [p for p, _ in served]
    ip = primary_ip()
    for p, _ in served:
        print("ds: admin on http://%s%s/" % (ip, "" if p == 80 else ":%d" % p),
              flush=True)
    return served[0][1]


# --------------------------------------------------------------------------

IDENTITY = {}


def main():
    global IDENTITY
    IDENTITY = load_identity()
    print("ds: %s (%s)" % (IDENTITY.get("name"), IDENTITY.get("id")),
          flush=True)
    cfg = load_config()

    # Admin first, browser second. See start_admin().
    start_admin(cfg)

    try:
        kiosk = DisplayClient(cfg)
    except Exception as exc:                                  # noqa: BLE001
        # Do not die. kiosk-session would restart us in two seconds and we
        # would fail the same way, taking the admin page down and up with us
        # and leaving nobody a way to correct whatever setting caused it.
        # Stay up, keep serving, and say plainly what happened.
        import traceback
        print("ds: the display failed to start: %s" % exc, file=sys.stderr)
        traceback.print_exc()
        print("ds: staying up so the admin page on port %s is still reachable"
              % cfg.get("admin_port", 8080), file=sys.stderr)
        AdminHandler.start_error = str(exc)
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        while True:
            time.sleep(3600)

    AdminHandler.kiosk = kiosk
    signal.signal(signal.SIGTERM, lambda *_: kiosk.quit(0))
    signal.signal(signal.SIGINT, lambda *_: kiosk.quit(0))
    Gtk.main()


if __name__ == "__main__":
    main()
