#!/usr/bin/env python3
"""Display System client: a barebones browser with a built-in HTTP admin backend.

One process: a fullscreen WebKit view with no browser chrome at all, plus a
small HTTP server (default :8080) for setting the URL, reloading and
restarting it from another machine on the LAN.

Config lives in /etc/kiosk/config.json and is rewritten whenever a setting is
changed through the admin page, so changes survive a reboot.
"""

import base64
import hmac
import io
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Before gi, because WebKit reads this when its process starts and the web
# process inherits it from here.
#
# The DMA-BUF renderer hands buffers straight to the GPU, and on a driver that
# does not implement the export it needs, the page still composites -- so
# nothing errors, the admin page looks healthy, and the screen shows a black
# rectangle where the video should be. It is a property of the graphics
# driver, so it turns up when a machine is swapped rather than when anything
# here changes.
#
# Set in kiosk-session too. Here is what makes it arrive with an ORDINARY
# UPDATE: kiosk-session is a shell loop that is already running, so replacing
# that file changes nothing until X restarts, and a display showing a black
# video would have needed a reboot to be fixed.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, WebKit2  # noqa: E402

CONFIG_PATH = os.environ.get("KIOSK_CONFIG", "/etc/kiosk/config.json")
IDENTITY_PATH = os.environ.get("KIOSK_IDENTITY", "/etc/kiosk/identity.json")
VERSION = "1.14.44"
STARTED = time.time()

DEFAULTS = {
    "url": "about:blank",
    "admin_port": 80,
    "admin_port_alt": 8080,        # FALLBACK only, if 80 cannot be bound

    "admin_bind": "0.0.0.0",
    "admin_password": "",          # empty = no auth (LAN only); stored hashed
    "admin_idle_minutes": 15,      # sign out after this long idle; 0 = never
    "recovery_email": "",          # stored only; nothing sends mail yet
    "printing_enabled": True,      # False stops and masks cupsd entirely
                                   #
                                   # There is deliberately no audio equivalent.
                                   # This image runs no sound daemon at all --
                                   # just ALSA kernel modules, about 1.3 MB --
                                   # so a switch could only hide the controls,
                                   # and a switch that reclaims nothing is a
                                   # switch that lies about what it does.
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
    "osk_caps": "first",   # shift the first letter only. "lock" SHOUTS, which
                           # is right for a code and wrong for a wifi password
                           # -- and a wifi password is what people actually type
                           # on these, usually while they cannot see the field.
    # Nudge the keys away from a dead patch on the digitiser, in pixels.
    # x positive moves right, y positive lifts the keys off the bottom edge.
    # Needed on a real panel whose glass had stopped registering presses in
    # one band, whichever key happened to be sitting there.
    "osk_offset_x": 0,
    "osk_offset_y": 0,
    # Key size, as a multiplier on the standard 56px row: 1, 1.25, 1.5, 1.8.
    # Fixed steps, not a free value -- each one is meant to be checked on a
    # real panel, and a preset can be. The keyboard clamps itself to 55% of
    # screen height, so a large setting on a small display shrinks to fit
    # rather than burying the page.
    "osk_scale": 1.0,
    "retry_seconds": 15,             # retry interval after a failed load
    "rotation": "normal",            # normal | left | right | inverted
    "resolution": "auto",            # auto, or WxH (scaled if not native)
    "wait_for_network": True,        # show the network page until reachable
    "offline_after_seconds": 45,     # then give up waiting and use the cache

    # Show the last page this display had on screen when it comes up with no
    # network. OFF BY DEFAULT, and the default is the honest one: a menu board
    # showing yesterday's menu is better than one showing a diagnostic screen,
    # but an interactive kiosk showing a stale page is not -- and the failure
    # of a stale page is silent while the failure of a network screen is
    # obvious. So it is a per-client choice made by whoever knows which kind
    # of display this is.
    #
    # It fires ONLY at boot. A display that loses its network during the day
    # keeps showing what it is already showing: the page is rendered, and
    # replacing it with a photograph of itself gains nothing.
    "cached_page": False,
    "cached_page_wait": 60,          # seconds on the network screen first, and
                                     # any touch, key or mouse move starts it
                                     # again from the top
    "cached_page_max_age_hours": 24,  # older than this, show the network page
    "cached_page_refresh_seconds": 3600,   # re-take it hourly, while online
    "offline_border": True,          # 2px red edge while the site is unreachable
    "splash_managed": True,          # branding is server-controlled; see below
    "clear_cache_on_start": False,   # purge the disk cache at every start
    "allow_location": True,          # answer the page's geolocation request.
                                     # On by default: a display is at a fixed,
                                     # known address chosen by whoever installed
                                     # it, and a page that wants the location of
                                     # a screen bolted to a wall is asking
                                     # something the owner already knows.
    "print_enabled": True,           # window.print() prints, with no dialog
    "printer": "",                   # CUPS queue; empty = system default
    "print_margins_mm": 0,           # receipt paper has no margins to give
    "print_width_mm": 80,            # 80 mm roll; 58 for the narrow ones
    "printer_backup": "",            # used when the primary is not ready
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
# The same mark, inline, for pages that show it beside the name. Reusing the
# favicon rather than drawing a second one: two marks is how a brand ends up
# with two brands, which is the argument already made for the wordmark.
BRAND_MARK = '<img class=bmark src="' + FAVICON + '" alt="">' 


def brandify(page):
    """Put the mark, its colours and the favicon into a built-in page.

    One pass over every page rather than four hand-styled copies. The tokens
    are HTML comments so the pages stay valid and readable on their own, and so
    a page that forgets one degrades to plain text instead of showing markup.
    """
    return (page
            .replace("<!--FAVICON-->", FAVICON_TAG)
            .replace("<!--BRANDCSS-->", BRAND_CSS)
            .replace("<!--BRANDMARK-->", BRAND_MARK)
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
    ok, why = password_complaint(password)
    if not ok:
        return False, why
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
SCREENCONNECT_HELPER = "/usr/local/sbin/kiosk-screenconnect"
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


UPDATE_CONFIG = "/etc/kiosk/update.json"


def update_config():
    """The updater's settings, for showing in the admin page.

    Read directly rather than by running `kiosk-update check`, which needs the
    network: a client that cannot reach the release host must still be able to
    show whether automatic updates are on, and that is exactly when somebody
    wants to know.
    """
    try:
        with open(UPDATE_CONFIG) as f:
            c = json.load(f)
    except Exception:                                         # noqa: BLE001
        return {}
    ch = str(c.get("channel", "stable"))
    return {"auto": bool(c.get("auto", False)),
            "channel": ch if ch in ("stable", "testing") else "stable",
            "window": "%s-%s" % (c.get("window_start", "03:00"),
                                 c.get("window_end", "05:00"))}


def update_set(key, value):
    """Change one updater setting, through the helper that owns the file."""
    if key not in ("auto", "channel"):
        return False, "not a settable key"
    if key == "auto":
        # Coerce explicitly. `"true" if value else "false"` made the JSON
        # STRING "false" truthy, so a caller asking to switch automatic
        # updates OFF switched them ON -- the exact inversion this setting
        # exists to prevent, on a display sitting in a shop.
        if isinstance(value, str):
            val = "true" if value.strip().lower() in ("true", "1", "yes", "on") else "false"
        else:
            val = "true" if value else "false"
    else:
        val = str(value)
    try:
        p = subprocess.run(["sudo", "-n", UPDATE_HELPER, "config", key, val],
                           capture_output=True, text=True, timeout=20)
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc)
    ok = p.returncode == 0
    return ok, ((p.stdout or p.stderr or "").strip()[:200]
                or ("saved" if ok else "failed"))


SNAPSHOT_HELPER = "/usr/local/sbin/kiosk-snapshot"


def snapshot_command(action, arg=""):
    """Run kiosk-snapshot through sudo. `list` is read-only; the rest are not."""
    if action not in ("list", "create", "restore", "delete", "prune"):
        return False, "unknown action", []
    cmd = ["sudo", "-n", SNAPSHOT_HELPER, action]
    if action in ("create", "restore", "delete") and arg:
        cmd.append(arg)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "timed out", []
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc), []
    out = (p.stdout or "").strip()
    if action == "list":
        # Check the exit status BEFORE parsing. `json.loads(out or "[]")`
        # happily parsed the default when the helper was missing entirely, so
        # a failure came back as ok:false with an EMPTY detail -- an operator
        # told something went wrong and not one word about what. Seen on .245
        # while the helper had been skipped by an older updater.
        if p.returncode != 0:
            err = (p.stderr or "").strip()
            return False, ("\n".join(x for x in (err, out) if x)
                           or "the snapshot helper is not installed on this client")[:300], []
        try:
            return True, "", json.loads(out or "[]")
        except Exception:                                     # noqa: BLE001
            return False, (out or "unreadable output").strip()[:300], []
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        return False, ("\n".join(x for x in (err, out) if x) or "failed")[:300], []
    return True, out[:300], []


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
    if p.returncode == 0:
        return True, out[:400], {}
    # On failure, lead with stderr. `out or p.stderr` meant that any stdout at
    # all hid the reason entirely: a refused update printed the client id to
    # stdout and "manifest signature is NOT valid -- refusing" to stderr, so
    # the admin page reported the failure as the bare client id. Watched it
    # happen on .169. The reason is the whole point of the message.
    err = (p.stderr or "").strip()
    return False, ("\n".join(x for x in (err, out) if x) or "failed")[:400], {}


OSUPDATE_PROGRESS = "/run/kiosk/osupdate-progress"


def osupdate_progress():
    """The phase an OS update is in, written by the helper as it goes.

    Plain file read, no sudo, no helper call: this is polled every couple of
    seconds while an update runs, and it has to stay cheap enough that watching
    the update cannot itself slow the update down.

    Missing means "no update is running", which is also what it means straight
    after a reboot -- the file lives on a tmpfs, so it does not survive to be
    mistaken for the progress of a run that already finished.
    """
    try:
        with open(OSUPDATE_PROGRESS) as fh:
            return fh.read().strip()[:200]
    except Exception:                                         # noqa: BLE001
        return ""


def osupdate_command(action):
    """Run kiosk-osupdate through sudo. `check` is read-only; `apply` is not.

    Kept separate from update_command rather than folded into it, because the
    two are not the same kind of operation and should not be able to be
    confused for one another by a typo in an action name. A software update
    replaces files and restarts a browser. This one rewrites the other root
    filesystem and reboots the machine.

    `apply` always carries --reboot. Arming a slot and *not* rebooting leaves a
    client in a state nobody can see from the admin page: the next boot -- an
    hour later, a power cut, whenever -- would silently be the attempt, and
    whoever pressed the button would be somewhere else. The reboot is what
    makes the try observable by the person who asked for it.

    The timeout is long because the payload is most of a gigabyte and eMMC is
    slow. In practice the reboot ends this call before the timeout can: the
    connection drops mid-request, which is expected and is what the page is
    written to handle.
    """
    if action not in ("status", "check", "apply", "download", "install",
                      "cancel"):
        return False, "unknown action", {}
    cmd = ["sudo", "-n", OSUPDATE_HELPER, action]
    limit = 120
    if action in ("apply", "install"):
        cmd.append("--reboot")
        limit = 3600
    elif action == "download":
        # No --reboot: this half writes nothing to a slot. It is the only part
        # of an update that can still be called off, which is why it is a
        # separate request at all.
        limit = 3600
    elif action == "status":
        limit = 30
    elif action == "cancel":
        limit = 30
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return False, "timed out", {}
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc), {}
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if action == "status":
        # `status` needs no network, which is the whole reason it is offered
        # separately from `check`. After an OS update the question that matters
        # is "did this slot get committed" -- and a client that came up but
        # cannot reach the release host must still be able to answer it.
        #
        # Parsed from the helper's own "key : value" lines rather than asking
        # it for JSON, so the command a person runs in a terminal and the one
        # the page runs stay the same command with the same output.
        st = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            st[k.strip()] = v.strip()
        return p.returncode == 0, ("" if p.returncode == 0 else err[:400]), st
    if action == "check":
        try:
            return p.returncode == 0, "", json.loads(out)
        except Exception:                                     # noqa: BLE001
            # kiosk-osupdate `die`s to stderr: no manifest published, a
            # signature that does not verify, a root that predates A/B. Lead
            # with that, for the same reason update_command does -- the reason
            # is the whole message.
            return False, (err or out or "no output").strip()[:400], {}
    if p.returncode == 0:
        return True, (out or "applied")[:400], {}
    # 10 is the download saying it was cancelled, which is somebody getting the
    # answer they asked for. Reporting it as a failure would put a red message
    # on the screen of a person who had just pressed Cancel and had it work.
    if action == "download" and p.returncode == 10:
        return True, "cancelled", {"cancelled": True}
    return False, ("\n".join(x for x in (err, out) if x) or "failed")[:400], {}




def printer_status():
    """CUPS queues, and whether the TM-T88V driver is actually present.

    Checked rather than assumed: the card used to state flatly that the PPD
    was missing, which stayed on screen after the driver was installed.
    """
    st = {"cups": False, "queues": [], "default": "", "detail": "",
          "enabled": True, "widths": {}, "problems": {}, "models": {}}
    # Asked of systemd, not read back from our own config: the two can differ
    # -- someone masks cups by hand, or a config write does not take -- and
    # the card must show what is true on the machine.
    try:
        p = subprocess.run(["systemctl", "is-enabled", "cups.service"],
                           capture_output=True, text=True, timeout=10)
        st["enabled"] = "masked" not in p.stdout
    except Exception:                                         # noqa: BLE001
        pass
    if not st["enabled"]:
        st["detail"] = "printing is switched off on this display"
        return st
    try:
        p = subprocess.run(["lpstat", "-a"], capture_output=True, text=True,
                           timeout=10)
        st["cups"] = True
        st["queues"] = [l.split()[0] for l in p.stdout.splitlines() if l.strip()]
        d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True,
                           timeout=10).stdout
        st["default"] = d.split(":")[-1].strip() if ":" in d else ""
        # Width is per queue now, so the card cannot show one number. Read it
        # from the helper, which is the only thing that knows the fallback
        # rules.
        ok, out, _ = run_root(PRINTER_HELPER, "list", timeout=20)
        if ok:
            for line in out.splitlines():
                if line.startswith("width=") and ":" in line:
                    q, _, w = line[len("width="):].partition(":")
                    try:
                        st["widths"][q] = int(w)
                    except ValueError:
                        pass
        st["problems"] = {q: queue_problem(q) for q in st["queues"]}
        # Label each queue with the printer in it. Dropped when the device is
        # not there at all -- naming a printer that has been unplugged is
        # worse than saying nothing, and the queue's own name is the truth at
        # that moment. Out of paper is NOT absent: that printer is present and
        # worth naming.
        absent = ("unplugged", "not answering", "no such queue")
        # lpstat -v, not the helper's output: `list` reports drivers and
        # widths, and the device URI -- which is where the model name is --
        # only appears here.
        try:
            devs = subprocess.run(["lpstat", "-v"], capture_output=True,
                                  text=True, timeout=10).stdout
        except Exception:                                     # noqa: BLE001
            devs = ""
        for line in devs.splitlines():
            if not line.startswith("device for "):
                continue
            qn = line[len("device for "):].split(":", 1)[0]
            quri = line.split(":", 1)[-1].strip()
            if st["problems"].get(qn) in absent:
                continue
            model = queue_model(quri, queue_service(qn))
            if model and model != qn:
                st["models"][qn] = model
    except FileNotFoundError:
        st["detail"] = "cups-client not installed"
    except Exception as exc:                                  # noqa: BLE001
        st["detail"] = str(exc)
    return st


def escpos_paper(host, port, timeout=1.0):
    """Ask a thermal printer whether it has paper. (ok, detail, answered).

    An open socket says the printer is switched on and nothing more. It says
    nothing about paper, and out of paper is the way a receipt printer fails
    in a shop -- the owner's point, and a fair one.

    ESC/POS has a real-time status query for exactly this: DLE EOT 4 returns
    the paper roll sensor without disturbing anything queued.

    The reply carries fixed bits, and they are the guard against treating a
    stray byte from something that is not a receipt printer as a paper status:
    bits 1 and 4 SET, bits 0 and 7 clear -- so `v & 0x93 == 0x12`.

    Bit 4 is set, not clear. Getting that backwards made every real answer
    look invalid: the TP200 replies 0x12, which was rejected as "did not
    answer". The unit tests agreed with the bug because the fixtures were
    built from the same wrong assumption -- it took asking the printer.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sk:
            sk.settimeout(timeout)
            sk.sendall(b"\x10\x04\x04")
            reply = sk.recv(1)
    except OSError:
        # A refused connection is not an ANSWER. Reporting it as one meant the
        # caller stopped there and never went on to ask whether the printer had
        # simply moved -- so a queue whose printer changed address stayed
        # broken, with the code to fix it sitting one line further down.
        return False, "not answering", False
    if not reply:
        return True, "", False
    v = reply[0]
    if v & 0x93 != 0x12:                      # not a DLE EOT status byte
        return True, "", False
    if v & 0x60 == 0x60:
        return False, "out of paper", True
    if v & 0x0C == 0x0C:
        return True, "paper nearly out", True
    return True, "", True


def queue_model(uri, service=""):
    """The printer's own name for a queue, from what the queue already knows.

    No discovery. `lpinfo` takes seconds and cannot run on every page refresh,
    and it does not need to: a USB queue's URI already carries the model, and
    a queue built from an advert has that advert stored beside it. Both were
    written down when the printer was attached.

        usb://EPSON/TM-T88V?serial=...                    -> TM-T88V
        dnssd://TP200%2FTP200W_19ABB9._pdl-datastream...  -> TP200/TP200W_19ABB9

    An address is not a name, so a queue typed in by hand as
    socket://192.168.1.50:9100 has none, and keeps the name it was given.
    """
    from urllib.parse import unquote, urlparse

    if service.startswith("dnssd://"):
        body = unquote(service[len("dnssd://"):]).split("?", 1)[0].rstrip("/")
        m = _DNSSD_RE.match(body)
        if m:
            return m.group("inst")
    if uri.startswith("usb://"):
        model = urlparse(uri).path.strip("/").split("?", 1)[0]
        return unquote(model) if model else ""
    if uri.startswith("dnssd://"):
        body = unquote(uri[len("dnssd://"):]).split("?", 1)[0].rstrip("/")
        m = _DNSSD_RE.match(body)
        if m:
            return m.group("inst")
    return ""


def queue_service(name):
    """The mDNS advert a queue was built from, if it was found rather than typed."""
    try:
        with open("/etc/kiosk/printer.json") as fh:
            cfg = json.load(fh)
        return ((cfg.get("queues") or {}).get(name, {}) or {}).get("service", "")
    except Exception:                                         # noqa: BLE001
        return ""


def heal_queue_address(name):
    """A printer that has moved is looked up again rather than given up on.

    The queue has to hold an address, because this image cannot resolve
    .local. An address on DHCP is right until the lease changes -- and then
    the queue points at nothing, or at whatever took the lease, and somebody
    has to notice and re-add the printer.

    So when a socket queue stops answering, the advert it was found at is
    resolved again. If the printer has simply moved, the queue is pointed at
    where it is now and nothing else has to happen. If it has genuinely gone,
    this costs one lookup and changes nothing.

    Returns the new address if it moved, "" otherwise.
    """
    service = queue_service(name)
    if not service.startswith("dnssd://"):
        return ""
    address, port = _device_address(service)
    if not address:
        return ""
    action, new_uri, _proto = _add_plan(service, address, port)
    if not new_uri or not action:
        return ""
    try:
        cur = subprocess.run(["lpstat", "-v", name], capture_output=True,
                             text=True, timeout=8).stdout
    except Exception:                                         # noqa: BLE001
        return ""
    if new_uri in cur:
        return ""                       # it has not moved; it is just down
    ok, _detail = printer_command(action, name, new_uri, "", service)
    if not ok:
        return ""
    print("ds: %s moved, queue repointed at %s" % (name, new_uri),
          file=sys.stderr)
    return address


def queue_problem(name):
    """Why this queue would not print, in words, or "" if it would.

    Split out from the yes/no because the admin page should be able to say
    "out of paper" rather than a red dot the operator has to interpret.

    Kept quick, because it runs before every print. One lpstat answers four
    questions -- state, reasons, whether it is accepting, its device and its
    jobs -- and asking them separately costs four process spawns on a board
    that is not fast. It used to look the queue's DRIVER up as well, through
    the helper, and that single call was 2.6 seconds of a 3.1 second check and
    the reason a test print sat there for seven. It bought nothing: the fixed
    bits in an ESC/POS reply are what stop a non-thermal printer's answer being
    read as a paper status, not knowing the driver in advance.
    """
    if not name:
        return "no printer chosen"
    try:
        p = subprocess.run(["lpstat", "-l", "-p", name, "-a", name,
                            "-v", name, "-o", name],
                           capture_output=True, text=True, timeout=10)
    except Exception:                                         # noqa: BLE001
        return "cannot be read"
    if p.returncode != 0 and "is idle" not in p.stdout:
        return "no such queue"
    out = p.stdout
    low = out.lower()
    for reason, said in (("media-empty", "out of paper"),
                         ("media-needed", "out of paper"),
                         ("media-jam", "paper jam"),
                         ("cover-open", "cover open")):
        if reason in low:
            return said
    if "disabled" in low:
        return "stopped"
    if "not accepting" in low:
        return "not accepting jobs"

    uri = ""
    jobs = False
    for line in out.splitlines():
        if line.startswith("device for "):
            uri = line.split(":", 1)[-1].strip()
        elif line.startswith(name + "-"):
            jobs = True

    if uri.startswith("socket://"):
        from urllib.parse import urlparse

        u = urlparse(uri)
        if not u.hostname:
            return ""
        # Do not interrupt a printer that is already working. Many accept one
        # connection at a time, and refusing to print because the printer is
        # busy printing would be its own way of losing a receipt.
        if jobs:
            return ""
        ok, detail, answered = escpos_paper(u.hostname, u.port or 9100)
        if answered:
            return "" if ok else (detail or "not ready")
        try:
            with socket.create_connection((u.hostname, u.port or 9100),
                                          timeout=0.75):
                return ""
        except OSError:
            pass
        # It is not answering where we last saw it. Before calling it broken,
        # ask whether it simply moved.
        moved = heal_queue_address(name)
        if moved:
            try:
                with socket.create_connection((moved, u.port or 9100),
                                              timeout=0.75):
                    return ""
            except OSError:
                pass
        return "not answering"
    elif uri.startswith("usb://"):
        if not any(os.path.exists(d) for d in ("/dev/usb/lp0", "/dev/usb/lp1")):
            return "unplugged"
    return ""


def queue_reachable(name):
    """Is this queue in a state that will actually print, right now?

    Two questions, because CUPS only answers one of them. `lpstat` knows the
    queue is stopped -- but only AFTER a job has failed, because the default
    error policy stops a printer when its backend fails. Relying on that alone
    means the first receipt of the day is always the one that is lost.

    And an open socket is not an answer either. It says the printer is
    switched on and nothing more -- not whether it has paper, which is how a
    receipt printer actually fails in a shop. So a thermal printer is asked,
    in ESC/POS. See queue_problem, which does the work and says why.
    """
    return not queue_problem(name)


def cups_default():
    """CUPS' own system default queue, or empty if there is none.

    `lp`/`lpr` fall back to this automatically when no queue is named, which
    is why the admin page's Test Print worked on a display whose config had
    never had a printer set: it goes through `lp`, and `lp` asked CUPS. GTK's
    print machinery does not do this on its own -- an empty `GtkPrintSettings`
    is not "use the system default", it is simply empty, and this is the
    function that closes that gap.
    """
    try:
        d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True,
                           timeout=10).stdout
        return d.split(":")[-1].strip() if ":" in d else ""
    except Exception:                                         # noqa: BLE001
        return ""


def choose_printer(cfg):
    """(queue, why) -- which queue a job should go to now.

    A shop with a spare printer behind the counter should not lose receipts
    because the first one jammed. Falling back is only useful if it is
    automatic: nobody is going to open the admin page mid-service.

    If NEITHER is ready the primary is returned anyway, so the failure is
    reported against the printer the operator expects rather than being
    quietly blamed on the spare.
    """
    primary = (cfg.get("printer") or "").strip()
    backup = (cfg.get("printer_backup") or "").strip()
    if not backup or backup == primary:
        return primary, ""
    if not primary:
        return primary, ""
    # Asked once, and the answer is reused for the message. Saying "was not
    # ready" when we know it is out of paper wastes the one chance to tell
    # somebody something they can act on -- and "out of paper" is a sentence
    # that gets a roll changed.
    problem = queue_problem(primary)
    if not problem:
        return primary, ""
    if queue_reachable(backup):
        return backup, "%s is %s" % (primary, problem)
    return primary, ""


def printer_command(action, name="", uri="", extra="", service=""):
    if action not in ("list", "detect", "test", "default",
                      "add-thermal", "add-ipp", "add", "remove", "ruler",
                      "enable", "disable", "persist", "width"):
        return False, "unknown action"
    args = [PRINTER_HELPER, action]
    if name:
        args.append(name)
    if uri:
        args.append(uri)
    if extra:
        args.append(extra)
    if service:
        args.append(service)
    ok, out, err = run_root(*args, timeout=60)
    # On success say what happened; on failure say why. `err or out` put
    # stderr first unconditionally, so adding a printer reported CUPS'
    # harmless "Printer drivers are deprecated" warning instead of "added
    # BenchNet" -- an alarming message on a step that worked perfectly.
    text = (out or err) if ok else (err or out)
    return ok, (text or "done").strip()[-600:]


# What `lpinfo -v` calls a device, and what a person would call it.
_DEV_KINDS = (
    ("usb:", "USB"),
    ("dnssd:", "network"),
    ("ipps:", "network"),
    ("ipp:", "network"),
    ("socket:", "network"),
    ("lpd:", "network"),
    ("serial:", "serial"),
    ("parallel:", "parallel"),
)


# dnssd://<instance>._<svc>._tcp.<domain>/  -- the instance may itself contain
# dots and slashes (a real one on the bench is "TP200/TP200W_19ABB9"), so this
# anchors on the service type from the right rather than splitting on the
# first dot and hoping.
_DNSSD_RE = re.compile(r"^(?P<inst>.+)\.(?P<type>_[^.]+\._(?:tcp|udp))\."
                       r"(?P<domain>.+)$")


def _resolve_mdns(instance, stype, domain):
    """Ask avahi where a service is: (address, port).

    The port is taken from the advert rather than assumed. A
    _pdl-datastream._tcp printer is raw-socket printing and is nearly always
    on 9100, but "nearly always" is how you end up with a queue pointed at
    the wrong port on the one printer that is different.

    Not avahi-resolve: this image ships the daemon but not avahi-utils, so
    there is no such binary. Not getaddrinfo either -- a dnssd URI names the
    SERVICE, and "Canon G600 series.local" was never a hostname, which is why
    resolving it failed and the list showed no address at all.

    GLib is already here for the browser, so this needs nothing new installed.
    """
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.Avahi", "/",
        "org.freedesktop.Avahi.Server", None)
    # (interface, protocol, name, type, domain, aprotocol, flags).
    #
    # aprotocol 0 is IPv4, and asking for it explicitly matters: with -1
    # ("whatever you have") avahi is free to answer with an IPv6 link-local
    # address, and fe80::6327:8644:e8cf:8d36 is not something anybody can type
    # into another program or read out over the phone -- which was the whole
    # point of showing an address. Fall back to unspecified so an IPv6-only
    # printer still resolves to something rather than nothing.
    for aproto in (0, -1):
        try:
            res = proxy.call_sync(
                "ResolveService",
                GLib.Variant("(iisssiu)",
                             (-1, -1, instance, stype, domain, aproto, 0)),
                Gio.DBusCallFlags.NONE, 3000, None)
        except Exception:                                     # noqa: BLE001
            continue
        got = res.unpack()
        if got[7]:
            return got[7], int(got[8])       # address, advertised port
    return "", 0


def _device_address(uri):
    """(address, port) for a network device.

    A service name is not something you can ping, type into another program,
    or read out over the phone. An address is.
    """
    from urllib.parse import unquote, urlparse

    if uri.startswith(("usb:", "serial:", "parallel:", "file:")):
        return "", 0
    if uri.startswith("dnssd:"):
        body = unquote(uri[len("dnssd://"):]).split("?", 1)[0].rstrip("/")
        m = _DNSSD_RE.match(body)
        if not m:
            return ""
        try:
            return _resolve_mdns(m.group("inst"), m.group("type"),
                                 m.group("domain"))
        except Exception:                                     # noqa: BLE001
            return "", 0
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    port = parsed.port or 0
    if not host:
        return "", 0
    if re.fullmatch(r"[0-9.]+|\[?[0-9A-Fa-f:]+\]?", host):
        return host.strip("[]"), port
    try:
        info = socket.getaddrinfo(host, None, socket.AF_INET)
        return (info[0][4][0] if info else ""), port
    except Exception:                                         # noqa: BLE001
        return "", port


_MDNS_NSS = None


def mdns_hostnames_resolve():
    """Can this machine resolve a `.local` hostname at all?

    It decides how a discovered printer is attached, so it is worth being
    exact about. avahi resolving a SERVICE is not the same thing as the system
    resolving the HOST that service points at, and conflating the two is what
    made a queue that could never print:

      * avahi found TP200/TP200W_19ABB9 and said 192.168.202.67:9100. True.
      * CUPS' dnssd backend resolved the same service, got the SRV target
        `TP200\047TP200W19ABB9.local` -- the name the printer advertises for
        itself, slash and all -- and asked the C library for it.
      * nsswitch here is `hosts: files dns`. No mdns. So it failed with
        "Unable to locate printer", and the job sat in the queue.

    Read once: nsswitch does not change under a running client.
    """
    global _MDNS_NSS
    if _MDNS_NSS is None:
        _MDNS_NSS = False
        try:
            with open("/etc/nsswitch.conf") as fh:
                for line in fh:
                    if line.strip().startswith("hosts:") and "mdns" in line:
                        _MDNS_NSS = True
                        break
        except Exception:                                     # noqa: BLE001
            pass
    return _MDNS_NSS


def _add_plan(uri, address, port):
    """How to attach this device: (action, uri, what it is in words).

    Classified by the mDNS SERVICE TYPE, not by the URI scheme. Getting that
    wrong is what broke adding the TP200 on the bench: it advertises
    _pdl-datastream._tcp -- raw socket printing, port 9100 -- and every dnssd
    URI was being handed to `lpadmin -m everywhere`, which speaks IPP. CUPS
    then built a hostname out of the service name, mangled the "/" in
    "TP200/TP200W_19ABB9" into an escape, and failed to resolve a machine that
    was sitting right there at 192.168.202.67.

    So: use the resolved address rather than the advertised name -- CUPS does
    not have to guess a hostname if it is given a number -- and pick the
    protocol from what the printer says it speaks.
    """
    from urllib.parse import unquote

    low = uri.lower()
    if low.startswith(("ipp://", "ipps://")):
        return "add-ipp", uri, "IPP"
    if low.startswith("usb://"):
        return "add-thermal", uri, "USB"
    if low.startswith("socket://"):
        return "add-thermal", uri, "raw socket"
    if low.startswith("dnssd://"):
        body = unquote(uri[len("dnssd://"):]).split("?", 1)[0].rstrip("/")
        m = _DNSSD_RE.match(body)
        stype = m.group("type") if m else ""
        # A dnssd URI is the better thing to build a queue on -- it follows the
        # printer when DHCP moves it -- but ONLY where the machine can resolve
        # the .local host it leads to. Without that it is a queue that accepts
        # jobs and never prints, which is the worst of both.
        named = mdns_hostnames_resolve()
        if stype in ("_ipp._tcp", "_ipps._tcp"):
            if named:
                return "add-ipp", uri, "IPP"
            if address:
                return ("add-ipp",
                        "ipp://%s:%d/ipp/print" % (address, port or 631),
                        "IPP")
            return "", "", "IPP"
        if stype in ("_pdl-datastream._tcp", "_printer._tcp"):
            proto = ("raw socket" if stype == "_pdl-datastream._tcp"
                     else "LPD")
            if named:
                return "add-thermal", uri, proto
            if not address:
                return "", "", proto
            # No mDNS in nsswitch, so the address is the only thing CUPS can
            # actually reach. This DOES pin a DHCP address, and the queue will
            # need re-adding if the printer moves -- the honest trade against
            # a queue that cannot print at all today. Fixed properly by
            # putting libnss-mdns in the image; see build/build-rootfs.sh.
            if stype == "_pdl-datastream._tcp":
                return ("add-thermal",
                        "socket://%s:%d" % (address, port or 9100), proto)
            return "add-thermal", "lpd://%s/" % address, proto
    return "", "", ""


def printer_devices():
    """Printers this display can see, as something a page can render.

    The helper's own `detect` prints CUPS' raw two-column output. That is the
    right thing on a terminal and useless in a list somebody has to choose
    from: the interesting half is buried in a percent-encoded URI.
    """
    ok, out, err = run_root(PRINTER_HELPER, "detect", timeout=45)
    if not ok:
        return {"ok": False, "detail": (err or out)[-400:], "devices": []}
    from urllib.parse import unquote

    devices = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        uri = parts[1].strip()
        # "network ipps" with no address is CUPS naming a backend it has, not
        # a printer it found. Offering it as something to add is a trap.
        if "://" not in uri:
            continue
        kind = "other"
        for prefix, label in _DEV_KINDS:
            if uri.startswith(prefix):
                kind = label
                break
        pretty = unquote(uri)
        # The bit a human recognises: the model, or the host.
        name = pretty.split("://", 1)[1]
        name = name.split("?", 1)[0].rstrip("/")
        if "._" in name:                       # dnssd: "Canon G600._ipp._tcp.local"
            name = name.split("._", 1)[0]
        elif "/" in name:
            head, tail = name.split("/", 1)
            name = tail or head
        devices.append({"uri": uri, "kind": kind, "name": name,
                        "pretty": pretty, "address": "", "port": 0})
    # Resolve the addresses together rather than one after another. Each
    # lookup is quick but a printer that has gone away costs the full timeout,
    # and doing four in a row is the difference between the list appearing and
    # the operator deciding the button is broken.
    from concurrent.futures import ThreadPoolExecutor

    net = [d for d in devices if d["kind"] == "network"]
    if net:
        with ThreadPoolExecutor(max_workers=min(8, len(net))) as pool:
            for dev, (addr, port) in zip(net, pool.map(
                    lambda d: _device_address(d["uri"]), net)):
                dev["address"] = addr
                dev["port"] = port

    # CUPS reports one network printer several times -- dnssd, ipp and ipps
    # are three ways to reach the same machine, and the Canon on the bench
    # showed up twice. Offering the same printer twice invites picking the
    # wrong one and then wondering why there are two queues.
    #
    # dnssd wins: it resolves by name through mDNS, so it keeps working when
    # the printer's address changes, which on a shop's DHCP it will.
    rank = {"dnssd:": 0, "ipps:": 1, "ipp:": 2, "socket:": 3, "lpd:": 4}

    def score(dev):
        for prefix, n in rank.items():
            if dev["uri"].startswith(prefix):
                return n
        return 9

    # Collapse twice, because neither key alone is enough.
    #
    # By NAME first: the same printer offered as dnssd, ipp and ipps has one
    # name and three URIs. By ADDRESS second: a printer can also appear under
    # two different names -- the Canon on the bench is both
    # "Canon G600 series" and "192.168.202.58" -- and only the resolved
    # address shows they are one machine.
    #
    # An entry whose address would not resolve keeps its name as its identity
    # rather than collapsing into everything else that failed to resolve.
    def collapse(items, keyfn):
        out = {}
        for d in items:
            k = keyfn(d)
            if k is None:
                out[id(d)] = d          # nothing to match on; keep it
            elif k not in out or score(d) < score(out[k]):
                out[k] = d
        return list(out.values())

    devices = collapse(devices, lambda d: (d["kind"], d["name"].lower()))
    devices = collapse(devices,
                       lambda d: (d["kind"], d["address"]) if d["address"]
                       else None)
    # Which of these already have a queue.
    #
    # Without this the Epson sits in the found list looking like something to
    # add, when it is already set up as "Thermal" -- and the owner reported
    # wanting to press Add on it despite knowing better. Adding it again is
    # not harmful, it just makes a second queue pointing at the same printer,
    # which is a confusion to discover later rather than an error to see now.
    #
    # Matched on the device rather than the name: a queue built from a dnssd
    # advert is stored as socket://<ip>:<port>, so comparing the strings would
    # never line up. USB is matched on serial, which is the one part of a USB
    # URI that identifies the hardware.
    existing = {}
    try:
        out = subprocess.run(["lpstat", "-v"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if not line.startswith("device for "):
                continue
            qname = line[len("device for "):].split(":", 1)[0]
            quri = line.split(":", 1)[-1].strip()
            existing[qname] = quri
    except Exception:                                         # noqa: BLE001
        pass

    def already(dev):
        from urllib.parse import urlparse

        cand = {dev["uri"], dev.get("add_uri") or ""}
        serial = ""
        if dev["uri"].startswith("usb://") and "serial=" in dev["uri"]:
            serial = dev["uri"].split("serial=", 1)[1].split("&")[0]
        for qname, quri in existing.items():
            if quri in cand:
                return qname
            if serial and serial in quri:
                return qname
            if dev.get("address") and quri.startswith("socket://"):
                u = urlparse(quri)
                if u.hostname == dev["address"]:
                    return qname
        return ""

    models = {}
    offer = []
    for dev in devices:
        action, add_uri, proto = _add_plan(dev["uri"], dev.get("address", ""),
                                           dev.get("port", 0))
        dev["add_action"] = action
        dev["add_uri"] = add_uri
        dev["protocol"] = proto
        queue = already(dev)
        if queue:
            # Already set up, so it is not something to add. Its model name is
            # worth keeping, though: a queue called "Thermal" says nothing
            # about which printer is on it, and the list of queues is the one
            # place that can say.
            models[queue] = dev["name"]
        else:
            offer.append(dev)
    offer.sort(key=lambda d: (d["kind"] != "USB", d["name"].lower()))
    return {"ok": True, "devices": offer, "models": models}


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

# The saved picture of the last good page, for the same reason and in the same
# place: /home/kiosk is bind-mounted from /data, so it survives an OS update,
# and this process is not root so /var/lib/kiosk is not available to it.
CACHE_DIR = os.path.expanduser("~/.steadyscreen")
CACHED_PAGE_IMAGE = os.path.join(CACHE_DIR, "lastpage.jpg")
CACHED_PAGE_META = os.path.join(CACHE_DIR, "lastpage.json")

# Written by kiosk-netwatch, which runs as root on a timer. Read, never
# written, from here: two writers and one file is how a state file starts
# disagreeing with itself.
NETWATCH_STATE = "/var/lib/kiosk/netwatch.json"


def cached_page_state(cfg=None):
    """What is in the cache, and whether it is still worth showing."""
    out = {"exists": False, "usable": False, "age_seconds": None,
           "when": 0, "url": ""}
    try:
        size = os.path.getsize(CACHED_PAGE_IMAGE)
    except OSError:
        return out
    if size <= 0:
        return out
    out["exists"] = True
    try:
        with open(CACHED_PAGE_META) as fh:
            meta = json.load(fh)
        out["when"] = float(meta.get("when") or 0)
        out["url"] = meta.get("url") or ""
    except Exception:                                         # noqa: BLE001
        # No metadata means no age, and no age means it cannot be judged
        # against the maximum. Fall back to the file's own mtime rather than
        # showing something of unknown vintage.
        try:
            out["when"] = os.path.getmtime(CACHED_PAGE_IMAGE)
        except OSError:
            return out
    out["age_seconds"] = max(0, int(time.time() - out["when"]))
    hours = float((cfg or {}).get("cached_page_max_age_hours", 24) or 0)
    out["max_age_hours"] = hours
    out["usable"] = hours <= 0 or out["age_seconds"] <= hours * 3600
    return out


def secure_boot():
    """Is this machine running with Secure Boot on?

    Two independent answers, because either alone can mislead. The EFI
    variable is what the firmware says; /sys/kernel/security/lockdown is what
    the kernel did about it. A display that boots through shim on a machine
    with Secure Boot off looks identical from the ESP, so the ESP is not the
    place to ask.

    Returns None on a machine with no efivars at all -- a legacy BIOS -- which
    is "not applicable" rather than "off".
    """
    out = {"firmware": None, "lockdown": ""}
    try:
        d = "/sys/firmware/efi/efivars"
        var = next((f for f in os.listdir(d) if f.startswith("SecureBoot-")),
                   None)
        if var:
            with open(os.path.join(d, var), "rb") as fh:
                raw = fh.read(5)
            # The first four bytes are the variable's attributes; the fifth is
            # the value.
            if len(raw) == 5:
                out["firmware"] = bool(raw[4])
    except OSError:
        pass
    try:
        with open("/sys/kernel/security/lockdown") as fh:
            # "none [integrity] confidentiality" -- the one in brackets is in
            # force.
            for part in fh.read().split():
                if part.startswith("["):
                    out["lockdown"] = part.strip("[]")
    except OSError:
        pass
    return out


def netwatch_state():
    """What the network watchdog last saw. Absent is a normal answer: a client
    that has not taken the update yet has no watchdog and no file."""
    try:
        with open(NETWATCH_STATE) as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            return {}
        return {"fails": st.get("fails", 0),
                "last_ok": st.get("last_ok", 0),
                "last_reboot": st.get("last_reboot", 0),
                "actions": st.get("actions", 0),
                "ever_ok": st.get("ever_ok", False),
                "last_probe": st.get("last_probe") or {},
                "log": (st.get("log") or [])[-5:]}
    except Exception:                                         # noqa: BLE001
        return {}


def first_boot_update():
    """Bring a brand new install up to the current release, once, by itself.

    An image is out of date the day after it is built, and the person doing the
    install is standing in a shop, not reading release notes. Waiting for the
    nightly window means a display runs old software all day for no reason --
    and the very first thing anyone does with a fresh machine is look at it.

    Once only, and recorded on /data so it survives a slot switch: after that
    the normal timer owns updates. It waits for a network for as long as this
    process runs rather than for a fixed spell, because "the first time it
    connects" is a real moment and it is not always in the first half hour.
    """
    if os.path.exists(FIRST_UPDATE_MARK):
        return
    # No deadline. This gave up after 30 minutes, which quietly broke the
    # promise the docstring makes. A box gets switched on and left while
    # somebody goes to find the wifi password, or the installer moves on to
    # another job and comes back; join the network at minute 31 and the
    # display runs image-aged software until 03:00. "Tries again the next time
    # it starts" is no help either -- a display that is working is not
    # restarted, so there is no next time for days.
    #
    # Waiting is one nmcli call, so it waits: every 20 seconds while somebody
    # is plausibly still standing at the machine, then every five minutes for
    # as long as this process lives.
    waited = 0
    while True:
        step = 20 if waited < 1800 else 300
        time.sleep(step)
        waited += step
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
        # The saved page takes the screen only once, and only before the real
        # page has ever been on it this boot. _page_shown_ok is what makes
        # "at boot, never mid-session" a property of the code rather than a
        # promise in a comment: once the configured page has loaded, this is
        # true for the rest of the boot and the fallback can never fire.
        self.showing_cached = False
        self._page_shown_ok = False
        self._cached_cancelled = False
        self.cached_id = None
        self._cached_save_id = None
        self._cached_refresh_id = None
        self._cached_saving = False
        self.last_cached_save = ""
        self.retry_id = None
        self.refresh_id = None
        self.idle_id = None
        self.last_input = time.time()
        # Scripted popups (window.open with no URL), each with the offscreen
        # window holding it. Kept so they are not garbage collected out from
        # under the page that is still writing into them.
        self._popups = {}

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
        self._arm_cached_refresh()

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
        """A target=_blank, or a window.open(). They are not the same thing.

        A LINK to a real URL is loaded in THIS view. A kiosk has one window
        and one page; opening a second one nobody can close or move is how a
        display ends up showing something the customer cannot get out of.
        That is the original behaviour and it stays.

        A SCRIPTED POPUP WITH NO URL is not a navigation at all. It is the
        receipt-printing pattern, and it is what almost every point-of-sale
        page ever written does:

            var w = window.open("", "", "width=400,height=600");
            w.document.write(...the receipt...);
            w.focus(); w.print(); w.close();

        Treating that as a navigation is what broke printing at Flying Fish
        Memphis on 2026-09-03. The URL is the empty string, normalise_url("")
        is "about:blank", and the MAIN view was sent there -- so the kiosk
        page vanished, `window.open` returned null because we created no
        view, `w.print()` threw on null, and nothing was ever sent to the
        printer. The screen went blank and the only visible symptom was a
        display that had lost its page.

        So a popup with no URL gets a REAL view to write into. It is never
        shown: it lives in an offscreen window, which gives it a size to lay
        out against without putting anything over the customer's page. It
        prints through the same handler as the main view, and when the script
        closes it -- or if the script forgets to -- it is destroyed.
        """
        req = nav_action.get_request()
        uri = req.get_uri() if req is not None else ""

        if uri and uri not in ("about:blank", "about:"):
            GLib.idle_add(self.navigate, uri)
            return None

        try:
            popup = WebKit2.WebView.new_with_related_view(view)
        except Exception as exc:                              # noqa: BLE001
            # Better to lose the popup than the page: fall back to the old
            # behaviour rather than leaving the script with a null it will
            # dereference either way.
            print("ds: could not make a popup view for window.open(): %s" % exc,
                  file=sys.stderr)
            return None

        popup.connect("print", self._on_print)
        popup.connect("close", self._on_popup_close)

        # Offscreen, with a real size. A view with no allocation can lay out
        # to nothing, and a receipt that prints blank is worse than one that
        # does not print at all -- nobody would know.
        holder = Gtk.OffscreenWindow()
        holder.set_default_size(400, 600)
        holder.add(popup)
        holder.show_all()
        self._popups[popup] = holder

        # If the page never calls close() -- a script that throws halfway
        # through, which is exactly what used to happen here -- the view would
        # sit in memory for the life of the browser. Receipts are seconds;
        # give it a couple of minutes and then take it away.
        GLib.timeout_add_seconds(
            120, lambda: (self._on_popup_close(popup), False)[1])
        return popup

    def _on_popup_close(self, popup):
        """window.close() on a scripted popup, or our own safety timeout."""
        holder = self._popups.pop(popup, None)
        if holder is None:
            return False            # already gone; the timeout lost the race
        try:
            popup.destroy()
            holder.destroy()
        except Exception:                                     # noqa: BLE001
            pass
        return False

    def _on_load_changed(self, view, event):
        if event == WebKit2.LoadEvent.FINISHED and self.load_ok:
            self._cancel_retry()
            self._inject_osk()
            if self.showing_cached:
                # The picture is up. Frame it, so the screen says offline the
                # way every other offline screen in the fleet does -- and so
                # the border disappearing is the signal that the real page is
                # back, which on a menu board is the only visible difference
                # between the two.
                self.set_offline(True)
            if not (self.showing_startup or self.showing_welcome
                    or self.showing_cached):
                # The configured page is up. Two things follow: the saved-page
                # fallback is finished for this boot, and there is now
                # something worth saving.
                #
                # The honest limit, stated rather than discovered: load_ok is
                # false for a network-level failure (DNS, refused, timeout),
                # which is the case that matters -- but a site that answers
                # with its own 500 page renders, and WebKit reports that as a
                # finished load. A picture of somebody's error page can
                # therefore be saved. Nothing available here can tell those
                # apart; the age limit is what bounds the damage.
                self._page_shown_ok = True
                self._cancel_cached()
                self._arm_cached_save()

    def _inject_osk(self):
        """Put the on-screen keyboard into the page that just loaded.

        Only somebody else's page: our own built-in screens have their own
        keyboard and would end up with two.

        Injected per load rather than through a UserContentManager because the
        WebView is already built by the time we know the setting, and because a
        page that reloads itself -- which a scanner page does -- gets it again
        without any extra machinery. The script makes itself idempotent.
        """
        if self.showing_welcome or self.showing_cached:
            return
        if self.showing_startup:
            # The setup screen gets the keyboard whenever the glass can be
            # tapped, regardless of the `osk` setting -- which is about the
            # customer's page, not ours.
            #
            # It deliberately does NOT go through osk_wanted(). That treats a
            # detected keyboard as "no keyboard needed", and a wedge barcode
            # scanner is indistinguishable from a keyboard to udev. On a
            # scanner kiosk with no real keyboard, auto mode would leave the
            # wifi password untypeable on the one screen that has to work
            # before there is any network to reach the admin page from. That
            # is a lockout with no way out.
            if not input_devices().get("touch"):
                return
        elif not osk_wanted(self.cfg):
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
        try:
            sc = float(self.cfg.get("osk_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            sc = 1.0
        sc = min(2.2, max(0.8, sc))
        js = ("window.__dsOskCaps=%r;window.__dsOskOffset={x:%d,y:%d};"
              "window.__dsOskScale=%s;\n%s"
              % (mode, ox, oy, repr(sc), js))
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
            name, why = choose_printer(self.cfg)
            if why:
                print("ds: printing to %s instead -- %s" % (name, why),
                      file=sys.stderr)

            # An EXPLICIT printer name, always, never an empty PrintSettings.
            #
            # choose_printer() returning "" means "no queue configured, use
            # whatever CUPS considers default" -- which is exactly what `lp`
            # does, and exactly what the admin page's Test Print button uses,
            # which is why testing worked on a display whose config had never
            # had a printer set. But an empty GtkPrintSettings is not the same
            # request: this is WebKitGTK printing straight to the platform's
            # print backend with no dialog and no prior enumeration, and GTK
            # only knows about a printer once its name is IN this settings
            # object. Leaving it blank does not mean "pick the default" here;
            # it means the backend has nothing to resolve at all.
            #
            # cups_default() is the same lookup CUPS itself uses, so a display
            # with no printer configured explicitly still prints to the queue
            # its Test Print button already proved works -- instead of handing
            # WebKit's native print code an empty settings object and finding
            # out what that does on a machine nobody is standing at.
            if not name:
                name = cups_default()
                if name:
                    print("ds: no printer configured; using CUPS' default (%s)"
                          % name, file=sys.stderr)

            if not name:
                # Nothing to print to, and nothing CUPS can name either. Say
                # so and stop -- do not hand WebKit's print internals a
                # settings object with no printer in it just to find out what
                # happens.
                self.last_print = "print failed: no printer configured, and CUPS has no default"
                print("ds: %s" % self.last_print, file=sys.stderr)
                return True

            settings.set_printer(name)
            settings.set_use_color(False)
            print_operation.set_print_settings(settings)

            # print(), not run_dialog(): no window, no questions
            print_operation.print_()
            self.last_print = "sent to %s" % name
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

        if phase == "ready" and (self.showing_startup or self.showing_cached
                                 or changed):
            self._fell_back = False          # a new outage gets a new attempt
            if self.showing_startup or self.showing_cached:
                # The moment the network is back, the real page. Including
                # from the saved picture: that is the whole recovery story,
                # and it must not wait for a timer or for anyone to touch it.
                self.showing_startup = False
                self.showing_cached = False
                self._cancel_cached()
                self.navigate(self.cfg["url"])
        elif phase != "ready" and self.showing_startup:
            # A menu board that reboots during an outage must not sit on the
            # network page until someone fixes the internet. After a grace
            # period, load the page anyway: WebKit serves what it has cached,
            # so the board comes back showing yesterday's menu rather than a
            # diagnostic screen nobody in the building can act on.
            grace = int(self.cfg.get("offline_after_seconds", 45) or 0)
            # ...unless the saved page is going to take over, which is the
            # better answer and is usually due a few seconds later. Letting
            # both run means this one navigates first, WebKit serves whatever
            # half of the page it happens to still hold, that counts as the
            # page having loaded -- and the saved-page fallback is then
            # disabled for the rest of the boot by its own "never
            # mid-session" rule. The feature would be switched on and never
            # once fire, for the most confusing possible reason.
            if self.cached_remaining() is not None:
                return False
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
        # The saved page is NOT in that list, and used to be. "Nothing written
        # on the picture" was about text -- a "saved copy from ..." line that
        # would end up photographed and sent in as a fault -- and it took the
        # offline border with it by mistake. The border is not a message about
        # the cache; it is the same red edge every offline display in this
        # fleet shows, and this is the one screen that needs it most: a
        # photograph of a working menu board looks exactly like a working menu
        # board. Without it there is no way to tell, standing in front of the
        # machine, that anything is wrong -- or, when the network comes back,
        # that anything has changed. Reported from a real test, 2026-09-02.
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
        self.showing_cached = False
        self._cancel_cached()
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
        # Arm the saved-page countdown. It is a no-op unless every condition
        # in cached_remaining() holds, so calling it here unconditionally is
        # the cheapest way to be sure it is never armed anywhere else.
        self._arm_cached()
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
            # The saved page is deliberately NOT one of these. It is a
            # picture of the customer's page, shown to a room, and it wants
            # the same bare screen the real one gets.
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
            self.showing_cached = False
            self._cancel_cached()
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
        elif key in ("cached_page", "cached_page_refresh_seconds"):
            self._arm_cached_refresh()
            # Switching it on with a page already up takes the picture now.
            # Otherwise the setting does nothing at all until the next hour,
            # and the first thing anyone does after turning something on is
            # check whether it worked.
            if self.cfg.get("cached_page", False):
                self._save_cached_page("switched on")
        elif key in ("osk", "osk_caps", "osk_scale",
                     "osk_offset_x", "osk_offset_y"):
            # Same reasoning: the keyboard read its settings once, when it was
            # injected, so a new key size did nothing until the page reloaded
            # or the browser restarted.
            self._push_osk_config()
        return False

    def _push_osk_config(self):
        """Re-style the keyboard already on the page, without a reload.

        A no-op if the page has no keyboard on it -- __dsOskConfig only exists
        once osk.js has been injected, and the guard is in the JavaScript so a
        page that never got one is untouched.
        """
        try:
            sc = float(self.cfg.get("osk_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            sc = 1.0
        try:
            ox = int(self.cfg.get("osk_offset_x", 0) or 0)
            oy = int(self.cfg.get("osk_offset_y", 0) or 0)
        except (TypeError, ValueError):
            ox = oy = 0
        caps = str(self.cfg.get("osk_caps", "lock")).lower()
        if caps not in ("lock", "first", "off"):
            caps = "lock"
        # Must match the rule _inject_osk used, or saving an unrelated
        # keyboard setting would switch the keyboard off on the setup screen
        # -- the one page where losing it is a lockout with no way out.
        if self.showing_startup:
            enabled = bool(input_devices().get("touch"))
        else:
            enabled = bool(osk_wanted(self.cfg))
        cfg = {"scale": min(2.2, max(0.8, sc)),
               "offset": {"x": ox, "y": oy},
               "caps": caps,
               "enabled": enabled}
        js = ("if(window.__dsOskConfig){window.__dsOskConfig(%s);}"
              % json.dumps(cfg))
        try:
            self.view.run_javascript(js, None, None, None)
        except Exception as exc:                               # noqa: BLE001
            print("ds: could not update the on-screen keyboard: %s" % exc,
                  file=sys.stderr)

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
            # Only retry from here when we are not already parked on one of
            # our own screens -- there the network watcher owns the
            # navigation. Including the saved page: a retry firing under it
            # would navigate to a site there is still no network for, fail,
            # and drop the display back to the network screen it had just
            # left.
            if not (self.showing_startup or self.showing_cached):
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

    # -- the saved page ----------------------------------------------------
    #
    # One trigger, at one moment: this display came up, a minute has gone by,
    # and it still cannot reach anything. Everything below exists to keep that
    # sentence true.

    def _arm_cached_save(self):
        """Take the picture a few seconds after the page says it has loaded.

        Not immediately: "finished" is the document, not the paint, and a
        capture taken on that signal catches a page that is still white.
        """
        if self._cached_save_id:
            GLib.source_remove(self._cached_save_id)
            self._cached_save_id = None
        if not self.cfg.get("cached_page", False):
            return

        def go():
            self._cached_save_id = None
            self._save_cached_page("the page loaded")
            return False

        self._cached_save_id = GLib.timeout_add_seconds(4, go)

    def _arm_cached_refresh(self):
        """Re-take it on a slow timer, so the saved copy follows the menu."""
        if self._cached_refresh_id:
            GLib.source_remove(self._cached_refresh_id)
            self._cached_refresh_id = None
        secs = int(self.cfg.get("cached_page_refresh_seconds", 3600) or 0)
        if secs <= 0 or not self.cfg.get("cached_page", False):
            return

        def go():
            self._save_cached_page("hourly")
            return True

        self._cached_refresh_id = GLib.timeout_add_seconds(secs, go)

    def _save_cached_page(self, why=""):
        """Keep a picture of the page that is on screen, if one is.

        THE TRAP THIS IS BUILT AROUND: never save a failed load. A display
        showing "cannot reach the server" must not turn that into tomorrow's
        saved copy, and the previous good picture has to survive an outage
        rather than being overwritten by the screen the outage produced. So
        every reason not to save is checked first, and the old file is left
        exactly where it is.
        """
        if not self.cfg.get("cached_page", False):
            return
        if self.showing_startup or self.showing_welcome or self.showing_cached:
            return                       # one of our own screens, not a page
        if not self.load_ok or not self._page_shown_ok:
            return
        if is_unconfigured(self.cfg):
            return
        if self._cached_saving:
            return
        self._cached_saving = True
        # Read HERE, not in the worker: this is a GDK call and every caller of
        # this method is on the main loop. Asking the display for its geometry
        # from a thread is the kind of thing that works until it does not.
        #
        # Full width, too: the picture is going back on the same screen it
        # came from, and a scaled-down copy stretched over it is a blurred
        # menu that reads as a broken display rather than a saved one.
        width = max(640, screen_geometry(self.window).width)

        def work():
            try:
                img = self.screenshot(max_width=width, fmt="jpeg", quality=82)
                if not img:
                    self.last_cached_save = "could not capture the screen"
                    return
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = CACHED_PAGE_IMAGE + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(img)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, CACHED_PAGE_IMAGE)
                meta = {"when": time.time(), "url": self.view.get_uri() or "",
                        "why": why, "bytes": len(img)}
                tmp = CACHED_PAGE_META + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(meta, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, CACHED_PAGE_META)
                self.last_cached_save = "%s, %d KB (%s)" % (
                    time.strftime("%H:%M"), len(img) // 1024, why)
            except Exception as exc:                          # noqa: BLE001
                self.last_cached_save = "failed: %s" % exc
                print("ds: could not save the page picture: %s" % exc,
                      file=sys.stderr)
            finally:
                self._cached_saving = False

        # screenshot() waits on a capture it dispatches to the main loop, so
        # calling it FROM the main loop deadlocks until the timeout expires.
        threading.Thread(target=work, daemon=True, name="cached-page").start()

    def cached_remaining(self):
        """Seconds until the saved page takes over, or None if it will not.

        Every condition is here rather than scattered through the callers, so
        the countdown on the network screen and the swap itself can never
        disagree about whether it is going to happen.
        """
        if not self.cfg.get("cached_page", False):
            return None
        if self._cached_cancelled or self._page_shown_ok:
            return None
        if not self.showing_startup or is_unconfigured(self.cfg):
            return None
        if not cached_page_state(self.cfg).get("usable"):
            return None
        wait = int(self.cfg.get("cached_page_wait", 60) or 60)
        if wait <= 0:
            return None
        return max(0, int(round(self.last_input + wait - time.time())))

    def _arm_cached(self):
        if self.cached_id:
            return
        self.cached_id = GLib.timeout_add_seconds(1, self._cached_tick)

    def _cancel_cached(self):
        if self.cached_id:
            GLib.source_remove(self.cached_id)
            self.cached_id = None

    def _cached_tick(self):
        left = self.cached_remaining()
        if left is None:
            self.cached_id = None
            return False
        if left <= 0:
            self.cached_id = None
            self.show_cached()
            return False
        return True

    def defer_cached(self):
        """Somebody is here. Start the minute again.

        Called from the network page on any touch, key or pointer movement.
        It goes through the page rather than through GTK because the WebView
        consumes the events that land on it -- the window handler below sees
        the ones that miss the page, which on a full-screen browser is none
        of them.
        """
        self.last_input = time.time()
        return False

    def keep_startup(self):
        """Stop the countdown for the rest of this boot.

        Offered when Scan is pressed, because pressing Scan is somebody saying
        they are working on this machine. After that the screen stays put
        until they fix it or walk away and it reboots.
        """
        self._cached_cancelled = True
        self._cancel_cached()
        return False

    def show_cached(self):
        """Put the saved picture on screen. Nothing else goes with it."""
        st = cached_page_state(self.cfg)
        if not st.get("usable"):
            return False
        print("ds: no network %ds after starting; showing the page saved "
              "%d minutes ago" % (int(self.cfg.get("cached_page_wait", 60)),
                                  (st.get("age_seconds") or 0) // 60),
              file=sys.stderr)
        self._cancel_retry()
        self.showing_cached = True
        self.showing_startup = False
        self.showing_welcome = False
        self.load_ok = True
        # The border goes on when the picture has PAINTED, not now. The border
        # is the window showing through a 2px margin, so setting it against a
        # view that is mid-load risks the same full-red frame that made this
        # suppressed on the network page in the first place.
        self.apply_cursor()
        self.view.load_uri("http://127.0.0.1:%d/cached"
                           % int(self.cfg["admin_port"]))
        return False

    def leave_cached(self, reason="asked"):
        """Back to the network screen, from the saved page.

        A long press or a key, never a tap: two of the panels in this fleet
        have dead digitiser strips that report touches nobody made, and a
        menu board that flips to a diagnostic screen on a phantom tap is a
        fault report from the shop. A two-second hold is not something a
        broken digitiser produces.
        """
        if not self.showing_cached:
            return False
        print("ds: leaving the saved page (%s)" % reason, file=sys.stderr)
        self._cached_cancelled = True
        self.showing_cached = False
        self.show_startup("left the saved page")
        return False

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
        # Never from one of our own screens. The network page and the saved
        # page are not "somewhere the customer wandered off to"; navigating
        # away from them means loading a page there is no network for, which
        # fails, which puts the network page straight back -- and with the
        # saved page it is worse than pointless, because the failure resets
        # the countdown and the screen cycles between the two for ever.
        if self.showing_startup or self.showing_welcome or self.showing_cached:
            return True
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
            "showing_cached": self.showing_cached,
            "cached_page": self.cfg.get("cached_page", False),
            "cached_page_wait": self.cfg.get("cached_page_wait", 60),
            "cached_page_max_age_hours":
                self.cfg.get("cached_page_max_age_hours", 24),
            "cached_page_state": cached_page_state(self.cfg),
            "last_cached_save": self.last_cached_save,
            # The watchdog is a separate program on its own timer, so without
            # this it is invisible: a thing that reboots the display and never
            # says why is worse than no watchdog at all.
            "netwatch": netwatch_state(),
            "secure_boot": secure_boot(),
            "netwatch_enabled": self.cfg.get("netwatch", True),
            "netwatch_reboot": self.cfg.get("netwatch_reboot", True),
            "wait_for_network": self.cfg.get("wait_for_network", True),
            "clear_cache_on_start": self.cfg.get("clear_cache_on_start", False),
            "cache_detail": self.last_cache_detail,
            "allow_location": self.cfg.get("allow_location", False),
            "osk": self.cfg.get("osk", "auto"),
            "osk_caps": self.cfg.get("osk_caps", "lock"),
            "osk_offset_x": self.cfg.get("osk_offset_x", 0),
            "osk_offset_y": self.cfg.get("osk_offset_y", 0),
            "osk_scale": self.cfg.get("osk_scale", 1.0),
            "update_config": update_config(),
            "last_permission": self.last_permission,
            "print_enabled": self.cfg.get("print_enabled", True),
            "printer_backup": self.cfg.get("printer_backup", ""),
            "printer": self.cfg.get("printer", ""),
            "last_print": self.last_print,
            "admin_protected": bool(self.cfg.get("admin_password")),
            "admin_idle_minutes": self.cfg.get("admin_idle_minutes", 15),
            "recovery_email": self.cfg.get("recovery_email", ""),
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
 /* The mark to the LEFT of the name, which is the order it is read in. */
 .brandhead{display:flex;align-items:center;gap:.6rem}
 .brandhead .bmark{width:1.9rem;height:1.9rem;display:block}
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
 .cards .card{margin:0;min-width:0}
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
 /* Every text-ish input, not a list of two.
    This was input[type=text],input[type=number] -- so password and email
    fields fell through to the browser's own styling and sat in the middle of
    the page looking like they belonged to a different program. A list you
    have to remember to extend is a list that will be wrong again the next
    time a field is added, so this says what it means: everything except the
    controls that have a look of their own. */
 input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]):not([type=color]):not([type=submit]):not([type=button]),
 textarea{width:100%;box-sizing:border-box;padding:.7rem .8rem;
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
 /* Greyed rather than hidden. A button that vanishes when it cannot be used
    leaves nowhere to put the reason; a greyed one still carries its title=,
    which is where the explanation goes. So pointer-events stays ON -- turning
    it off would suppress the tooltip and take the explanation with it. */
 button:disabled{opacity:.45;cursor:not-allowed}
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
   width:auto;min-width:0;flex:0 0 auto}
 .card h3{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;
   color:var(--dim);font-weight:600;margin:1.2rem 0 .6rem;
   padding:0;border:0}
 /* A folded subsection reads as one of the headings it sits among, so the
    summary is styled as an h3 that happens to open. list-style:none kills the
    default triangle in every engine; the chevron is drawn instead so it can
    turn, and so it is big enough to hit with a thumb. */
 .card details > summary{font-size:.78rem;text-transform:uppercase;
   letter-spacing:.06em;color:var(--dim);font-weight:600;cursor:pointer;
   list-style:none;display:flex;align-items:center;gap:.5rem;
   padding:.45rem 0;user-select:none}
 .card details > summary::-webkit-details-marker{display:none}
 .card details > summary::before{content:"";width:.45rem;height:.45rem;
   border-right:2px solid var(--dim);border-bottom:2px solid var(--dim);
   transform:rotate(-45deg);transition:transform .15s;flex:0 0 auto;
   margin-left:.15rem}
 .card details[open] > summary::before{transform:rotate(45deg)}
 .card details > summary:hover{color:var(--fg)}
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
 .card > h2 + .shotwrap{margin-top:0;margin-bottom:1rem}
 .shothead{display:flex;align-items:center;justify-content:space-between;
   gap:.6rem;margin-bottom:.5rem}
 #shotstamp{font-size:.8rem;color:var(--dim);text-transform:uppercase;
   letter-spacing:.04em}
 button.small{flex:0 0 auto;min-width:0;min-height:2.2rem;padding:.35rem .8rem;
   font-size:.85rem}
 /* A reveal belongs beside the field it reveals, not stacked under it: below,
    it reads as another action in the form rather than part of the input. */
 .pwrow{display:flex;gap:.5rem;align-items:stretch;margin:.15rem 0 0}
 /* flex-basis 0, not auto, so a pair of fields comes out the SAME WIDTH. With
    auto each starts from its own content width and they only share what is
    left over, which is why the confirm field was always the narrower of the
    two.
    There was a .pwpair grid here that existed solely to work around that, on
    the one card whose second field had no reveal of its own. Now that every
    password field carries its own Show button the row is symmetrical, so the
    grid had nothing left to fix and is gone. */
 .pwrow input{flex:1 1 0;min-width:0}
 .pwrow button{flex:0 0 auto;min-height:0;padding:.35rem .9rem;font-size:.85rem}
 /* On a phone -- which is how most of these are actually set up -- four items
    on one line leave each field about 100px wide with its placeholder cut off
    mid-word: "at least 12 ch". One pair per line instead. A grid rather than
    flex-wrap because the pairing has to be exact: every input in column 1 and
    every button in column 2, which also does the right thing for the rows
    that have only one field, like the wifi password. */
 @media (max-width:560px){
   .pwrow{display:grid;grid-template-columns:1fr auto}
 }
 #shot{display:block;width:100%;max-height:46vh;object-fit:contain;
   border:1px solid var(--line);border-radius:8px;background:#0b0d0f;
   min-height:80px}
 #shot.stale{opacity:.45}

</style></head><body><div class=wrap>
<h1 class=brandhead><!--BRANDMARK--><span><!--BRAND--> Admin</span></h1>
<div class=sub id=host>&nbsp;</div>
<div class=strip id=strip></div>
<div class=banner id=updbanner style="display:none"></div>
<div id=pmodal style="display:none;position:fixed;inset:0;z-index:60;
     background:rgba(0,0,0,.66);padding:1rem;overflow:auto">
 <div style="max-width:44rem;margin:3vh auto;background:#161a1f;
      border:1px solid var(--line);border-radius:.8rem;padding:1.4rem">
  <div style="display:flex;align-items:center;gap:1rem">
   <h2 style="margin:0;flex:1">Printers</h2>
   <button type=button class=small onclick="closePrinters()">Close</button>
  </div>

  <h3 style="margin:1.2rem 0 .4rem;font-size:.95rem">Set up on this display</h3>
  <div id=pmcurrent></div>

  <h3 style="margin:1.4rem 0 .4rem;font-size:.95rem">Found on this display</h3>
  <div class=row style="margin:0 0 .6rem">
   <button type=button onclick="pmScan()" id=pmscanbtn>Search again</button>
  </div>
  <div id=pmfound><div class=msg>Searching&hellip;</div></div>

  <h3 style="margin:1.4rem 0 .4rem;font-size:.95rem">Add one by address</h3>
  <div class=grid2>
   <div><label for=pmhost>Address (IP or hostname)</label>
    <input type=text id=pmhost spellcheck=false autocapitalize=off
           placeholder="192.168.1.50"></div>
   <div><label for=pmname>Name it</label>
    <input type=text id=pmname spellcheck=false autocapitalize=off
           placeholder="Receipt"></div>
  </div>
  <label for=pmkind style="margin-top:.7rem">What kind</label>
  <select id=pmkind style="width:100%;padding:.7rem .8rem;background:#111316;
    border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
   <option value=thermal>Thermal receipt printer (ESC/POS, port 9100)</option>
   <option value=ipp>Network printer (IPP / AirPrint, no driver needed)</option>
  </select>
  <div class=row><button type=button onclick="pmAddManual()">Add it</button></div>
  <div class=msg id=pmmsg></div>
 </div>
</div>

<!-- The update modal.

     An update restarts the thing you are watching it through. The page you
     pressed the button on is served BY the machine that is updating, so the
     moment it works the page stops being able to say so -- and every failure
     mode looks identical from here: a slow unsquashfs, a wedged one, and a
     machine that will never come back all present as "no reply".

     So the modal never claims to know more than it does. It reports the phase
     the device itself last wrote down, then says plainly that the device is
     rebooting, then counts down a bounded wait while it keeps knocking. If the
     count runs out it does NOT say the update failed -- it says we have not
     been able to reconnect, and asks somebody to go and look. Those are
     different statements and only one of them is true from here. -->
<div id=osmodal style="display:none;position:fixed;inset:0;z-index:70;
     background:rgba(0,0,0,.78);padding:1rem;overflow:auto">
 <div style="max-width:34rem;margin:10vh auto;background:#161a1f;
      border:1px solid var(--line);border-radius:.8rem;padding:1.4rem">
  <h2 style="margin:0 0 1rem" id=osmtitle>Updating</h2>
  <div id=osmphase style="font-size:1.05rem;font-weight:600;margin-bottom:.45rem;
       color:var(--fg)">Starting&hellip;</div>
  <div id=osmnote style="color:var(--dim);font-size:.9rem;line-height:1.5"></div>

  <div id=osmwait style="display:none;margin-top:1.1rem">
   <div style="height:.5rem;background:#22262c;border-radius:.25rem;overflow:hidden">
    <div id=osmbar style="height:100%;width:0%;background:var(--acc);
         transition:width 1s linear"></div>
   </div>
   <div id=osmcount style="color:var(--dim);font-size:.85rem;margin-top:.45rem"></div>
  </div>

  <div class=msg id=osmmsg></div>

  <!-- The buttons appear only where there is a decision to make, and there
       are exactly two: during the download, and after it. There is
       deliberately no way to dismiss this while an update is running -- a
       modal you can close is one somebody closes at the wrong moment and then
       cannot find again, on a machine that is mid-write. Close comes back only
       when the operation has finished, failed, or given up, because trapping
       someone in a dialog about a machine that is never coming back would be
       its own kind of cruelty. -->
  <div class=row id=osmcancelrow style="display:none">
   <button type=button onclick="osCancelDownload()" id=osmcancelbtn
     >Cancel the download</button>
  </div>
  <div class=row id=osmgorow style="display:none">
   <button type=button class=danger onclick="osProceed()" id=osmgobtn
     >Proceed with the update</button>
   <button type=button onclick="osCancelDownload()">Cancel and discard</button>
   <button type=button onclick="osmClose()">Decide later</button>
  </div>
  <div class=row id=osmcloserow style="display:none">
   <button id=osmclose onclick="osmClose()">Close</button>
  </div>

  <div style="color:var(--dim);font-size:.82rem;margin-top:.9rem;
       border-top:1px solid var(--line);padding-top:.7rem">
   The update runs on the display itself, not in this browser. <b>Closing this
   window or losing the connection does not stop it</b> &mdash; it carries on,
   and this page will pick it up again when it can.
  </div>
 </div>
</div>
<div class=tabs id=tabs></div>

<div class=card>
 <h2>Page</h2>
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
         placeholder="at least 12 characters" oninput="sshPwMatch()">
  <button type=button class=small onclick="pwToggle('sshpw', this)"
          aria-pressed=false>Show</button>
  <input type=password id=sshpw2 autocomplete=new-password spellcheck=false
         placeholder="type it again" oninput="sshPwMatch()">
  <button type=button class=small onclick="pwToggle('sshpw2', this)"
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
  <input type=password id=adminpw autocomplete=new-password spellcheck=false
         placeholder="no password set">
  <button type=button class=small onclick="pwToggle('adminpw', this)"
          aria-pressed=false>Show</button>
  <input type=password id=adminpw2 autocomplete=new-password spellcheck=false
         placeholder="type it again">
  <button type=button class=small onclick="pwToggle('adminpw2', this)"
          aria-pressed=false>Show</button>
 </div>
 <div id=pwmatch style="font-size:.8rem;min-height:1.2em;margin-top:.35rem"></div>

 <label for=recoveryemail style="margin-top:.9rem">Recovery email</label>
 <input type=email id=recoveryemail autocomplete=off spellcheck=false
        placeholder="nobody@example.com">
 <div style="color:var(--dim);font-size:.8rem;margin-top:.3rem">
  Saved, but <b>not used yet</b>. Emailing a temporary password needs a server
  to send it, which does not exist. Until then <b>Forgot password</b> gives you
  the ssh way back in.
 </div>

 <label for=adminidle style="margin-top:.9rem">Sign out after (minutes idle,
  0 never)</label>
 <input type=number id=adminidle min=0 max=480 step=1 style="max-width:8rem">
 <div class=row><button onclick="setAdminPw()">Apply</button>
  <button type=button class=small id=signoutbtn onclick="signOut()"
          style="display:none">Sign out now</button></div>
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
 <h2>Updates</h2>

 <!-- What this display is running, before anyone presses anything.

      Both subsections could already report a version, but only after a check:
      the software one needs the button, and the OS one is behind a fold. So a
      card called Updates opened with nothing on it at all, and the OS version
      in particular was not readable anywhere in this admin without unfolding a
      section and running a network check to find out.

      This comes from /api/status, which is already polled, so it is filled on
      load and needs nothing to reach the release host. It is the one line here
      that is still true when the machine is offline. -->
 <dl id=updnow></dl>

 <h3>Software</h3>
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

 <!-- The operating system.

      Kept behind a gate rather than beside the software button, because the
      two are not the same size of action however similar they look. A software
      update replaces a few files and restarts a browser, and rolls itself back
      if the browser does not come up. An OS update rewrites the other root
      filesystem and reboots. The A/B design means a bad slot is abandoned at
      the next boot without anyone doing anything -- but "the next boot" is the
      recovery, and if the machine does not reach a boot at all, the recovery
      is somebody driving to it with a flash drive.

      So: the warning is visible before anything can be pressed, the
      acknowledgement is the same read-tick-hold sequence as Shut down, and the
      install itself is a second ten-second hold. Two holds is a lot of
      friction. That is the point. -->
 <!-- Folded shut. It was a subsection like any other, which put a live OS installer
      permanently on screen for anybody scrolling the System tab looking for the
      clock. Closed by default it has to be asked for, and closing it re-arms the
      acknowledgement -- so somebody who opened this to look, and walked away,
      does not leave a one-press OS install sitting on a counter.

      A details element, not a div and a click handler: it opens with a keyboard, it
      opens with a screen reader, and it opens on a touchscreen with no keyboard
      attached, which is what most of these displays are. -->
 <details id=osfold style="margin-top:1.6rem">
  <summary>Operating system</summary>
  <div style="margin-top:.9rem">
   <div id=osgate style="padding:.9rem 1rem;border:1px solid #7c5a2d;
     border-radius:10px;background:#1d1811">
    <div style="font-weight:600;color:#ffcf8b;margin-bottom:.5rem">
     An OS update can fail, and not every machine can be tested first.
    </div>
    <div style="color:var(--dim);font-size:.92rem;line-height:1.5">
     This replaces the whole operating system on this display's spare boot slot
     and restarts it. It is designed to fall back on its own &mdash; if the new
     system does not come up, the next boot returns to the one running now, and
     nothing is lost. But hardware varies, and no release can be tested on every
     configuration that exists.
     <b style="color:var(--fg)">Only go ahead if you have a way to recover this
     machine physically &mdash; a flash drive to reflash it, and access to the
     device itself.</b>
     Proceed at your own risk.
    </div>
    <label style="display:flex;align-items:center;gap:.6rem;margin:.9rem 0 .2rem;
      text-transform:none;letter-spacing:0;font-size:.95rem;color:var(--fg)">
     <input type=checkbox id=osack style="width:auto;min-height:0;margin:0">
     <span>I can physically reach this device and reflash it if it does not come
      back.</span>
    </label>
    <div class=row>
     <button id=osackhold disabled
       style="position:relative;overflow:hidden">Hold to continue &mdash; 10s</button>
    </div>
    <div class=msg id=osackmsg></div>
   </div>

   <div id=ospanel style="display:none">
    <dl id=osinfo></dl>
    <div class=row>
     <button onclick="osCheck()" id=oscheckbtn>Check again</button>
     <button onclick="osHide()">Hide</button>
    </div>
    <!-- Reinstalling is deliberately the quieter of the two doors. It is the
         only way to repair a spare slot that will not boot, and the only way
         to move back onto the slot you are not running -- but somebody who
         opened this section to read a version number should not find a live
         installer already pointed at their disk. -->
    <div id=osrerow class=row style="display:none">
     <button type=button class=mini onclick="osOfferReinstall()"
       >Reinstall the operating system&hellip;</button>
    </div>
    <div id=osgorow class=row style="display:none">
     <button id=osgo class=danger
       style="position:relative;overflow:hidden">Hold to install &mdash; 10s</button>
    </div>
    <div class=msg id=osmsg></div>
    <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem">
     The OS is signed and checksummed the same way software is. It installs to
     the slot this display is <b>not</b> running from, so the system running now
     is never written to. The display reboots into the new slot and keeps it only
     after it has come up and stayed up; otherwise the next boot returns here.
    </div>
   </div>
  </div>
 </details>

 <div style="border-top:1px solid var(--line);margin-top:1.6rem;padding-top:.9rem">
  <h3 style="margin-top:0">Update settings</h3>
  <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer">
   <input type=checkbox id=updauto style="width:1.1rem;height:1.1rem">
   <span>Install updates automatically</span></label>
  <div style="color:var(--dim);font-size:.85rem;margin:.35rem 0 .9rem 1.7rem">
   When this is off nothing is installed unless someone presses
   <b>Install now</b> above. Turn it off for a display in a shop, where an
   update arriving on its own is a surprise to somebody else's business.
   <span id=updwindow></span>
  </div>
  <div style="max-width:22rem">
   <label for=updchannel>Update channel</label>
   <select id=updchannel style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=stable>stable &mdash; tested releases</option>
    <option value=testing>latest &mdash; new builds first</option></select></div>
  <div style="color:var(--dim);font-size:.85rem;margin-top:.5rem">
   <b>latest</b> gets new builds before they are proven. Use it on a bench
   machine, not on one a shop depends on. The channel applies to both the
   software and the operating system.
  </div>
  <div class=row><button class=primary onclick="saveUpdatePolicy()">Apply update settings</button></div>
  <div class=msg id=updpolmsg></div>
 </div>
</div>

<div class=card>
 <h2>Snapshots</h2>
 <div style="color:var(--dim);font-size:.85rem;margin-bottom:.7rem">
  This display's settings, network profiles and page configuration &mdash; saved
  automatically before every update. Restoring reboots the display and does not
  touch the page content, only how this machine is set up. The five most recent
  are kept.
 </div>
 <div id=snaplist style="font-size:.9rem"></div>
 <div class=row>
  <button onclick="snapRefresh()">Refresh</button>
  <button onclick="snapCreate()">Save current state</button>
 </div>
 <div class=msg id=snapmsg></div>
</div>

<div class=card>
 <h2>Printing</h2>
 <dl id=printinfo></dl>
 <div class=grid2 style="margin-top:.9rem">
  <div><label for=printer>Printer</label>
   <select id=printer style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value="">system default</option></select></div>
  <div><label for=printerbackup>Backup printer</label>
   <select id=printerbackup style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value="">none</option></select></div>
  <div><label for=printwidth>Paper width (mm)</label>
   <input type=number id=printwidth min=40 max=210 step=1></div>
 </div>
 <div class=row>
  <button onclick="savePrint()">Apply printing settings</button>
  <button onclick="printerTest()">Test print</button>
  <button onclick="openPrinters()" id=managebtn>Manage printers</button>
 </div>
 <label style="display:flex;align-items:center;gap:.5rem;margin-top:.9rem">
  <input type=checkbox id=printingon style="width:auto;margin:0"
         onchange="togglePrinting(this)">
  <span>Enable print service</span>
 </label>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem" id=backupnote></div>
 <div class=msg id=printmsg></div>
 <div style="color:var(--dim);font-size:.8rem;margin-top:.5rem" id=printnote></div>
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
  <div><label for=oskscale>Key size</label>
   <select id=oskscale style="width:100%;padding:.7rem .8rem;background:#111316;
     border:1px solid var(--line);border-radius:7px;color:var(--fg);font:inherit">
    <option value=1>standard</option>
    <option value=1.25>large</option>
    <option value=1.5>extra large</option>
    <option value=1.8>huge</option></select></div>
  <div><label for=oskdx>Nudge across (px, -left / +right)</label>
   <input type=number id=oskdx step=10></div>
  <div><label for=oskdy>Nudge up (px)</label>
   <input type=number id=oskdy step=10></div>
 </div>
 <div style="color:var(--dim);font-size:.85rem;margin-top:.7rem">
  Key size scales the whole keyboard, keys and text together. It is capped
  against the screen height, so on a short display the larger settings still
  fit &mdash; they stay distinct from each other, but a bigger key on a small
  panel is necessarily less big than on a tall one.
 </div>
 <div style="color:var(--dim);font-size:.85rem;margin-top:.4rem">
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
 <h3>Offline</h3>
 <div class=grid2>
  <div><label for=offsecs>Give up waiting after (seconds) *</label>
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
  <b>*</b> While there is no network the device shows its own network page instead of a
  &ldquo;cannot reach the page&rdquo; error, and loads the real page as soon as
  the site answers. <b>After the timeout above it stops waiting and loads the
  page from cache instead</b> &mdash; a menu board that reboots during an
  outage comes back showing the last menu it saw, rather than a diagnostic
  screen nobody in the building can act on. Set 0 to wait indefinitely.
  While the site is unreachable a 2px red edge marks the screen as offline.
 </div>

 <h3>Show the last page when there is no network at boot</h3>
 <div style="color:var(--dim);font-size:.85rem;margin-bottom:.7rem">
  With this on, a display that comes up with no network waits a minute on the
  network screen and then shows a picture of the last page it had on screen,
  taken while it was working and refreshed hourly. Nothing is written over the
  picture &mdash; it is the page, as it was. The moment the network comes back
  the real page loads. It happens only at boot: a display that loses its
  network during the day carries on showing what it is already showing.
  <b>Leave this off for anything people touch</b>, where a stale page that
  looks live is worse than a screen that says what is wrong.
 </div>
 <div class=grid2>
  <div><label for=cachedwait>Wait this long first (seconds)</label>
   <input type=number id=cachedwait min=5 max=3600 step=5></div>
  <div><label for=cachedage>Never show one older than (hours)</label>
   <input type=number id=cachedage min=1 max=168 step=1></div>
 </div>
 <div class=row>
  <button onclick="toggleCached()" id=cachedbtn>...</button>
  <button onclick="saveCached()">Apply</button>
  <button onclick="saveNow()" id=cachedsavebtn>Save one now</button>
  <button onclick="window.open('/cached','_blank')">View the saved page</button>
 </div>
 <dl id=cachedstate style="margin-top:.8rem"></dl>
 <div class=msg id=cachedmsg></div>
</div>

<div class=card>
 <h2>Network watchdog</h2>
 <div style="color:var(--dim);font-size:.85rem;margin-bottom:.7rem">
  Checks once a minute that this display can still reach its default gateway.
  If it cannot, it renews the connection, then re-activates it, then restarts
  NetworkManager, and only after all of that has failed does it reboot &mdash;
  and it will not reboot a machine whose network has never worked since it
  started, because that would only repeat the boot it has already had.
  A site that is down is <b>not</b> a network fault and never triggers any of
  this.
 </div>
 <dl id=nwstate></dl>
 <div class=row>
  <button onclick="toggleNw()" id=nwbtn>...</button>
  <button onclick="toggleNwReboot()" id=nwrebootbtn>...</button>
 </div>
 <div class=msg id=nwmsg></div>
</div>

<div class=card>
 <h2>Remote support</h2>
 <div style="color:var(--dim);font-size:.9rem;margin-bottom:.9rem">
  Install a ScreenConnect access agent on this display, so it can be reached
  from your ScreenConnect console the way your other machines are.
 </div>
 <dl id=scinfo></dl>
 <label for=scurl style="margin-top:.9rem">Installer link from your ScreenConnect console</label>
 <input type=text id=scurl spellcheck=false autocapitalize=off autocomplete=off
        placeholder="https://your.screenconnect.com/Bin/ScreenConnect.ClientSetup.exe?e=Access&amp;y=Guest&amp;t=..."
        oninput="scDirty()">
 <div style="color:var(--dim);font-size:.8rem;margin-top:.3rem">
  Paste the link exactly as the console gives it, including everything after
  the <code>?</code> &mdash; that is what carries the name and the custom
  fields, and an agent installed without them arrives unnamed.
 </div>
 <div class=row>
  <button onclick="scCheck()" id=sccheckbtn>Check this link</button>
 </div>
 <div id=scdetail style="display:none"></div>
 <div class=row id=scgorow style="display:none">
  <button id=scgo class=primary style="position:relative;overflow:hidden"
    >Hold to install &mdash; 10s</button>
 </div>
 <div class=msg id=scmsg></div>
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
  ['network', 'Network', ['Network', 'Startup behaviour',
                          'Network watchdog']],
  ['display',  'Display', ['Display', 'Keyboard', 'Boot splash']],
  ['system',   'System',  ['Status', 'Updates', 'Printing', 'Audio', 'Clock',
                          'Remote access (SSH)', 'Remote support', 'Snapshots',
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
  //
  // It happened again on 2026-08-31, and worse, because this list is keyed on
  // a HUMAN-READABLE HEADING: renaming the card's heading from "Software update"
  // to "Updates" moved the card to the Page tab without touching a line of tab
  // code. This warning fired exactly as designed and nobody was looking at a
  // console. build/check-client.py now fails the build on it instead.
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

for (const id of ['adminpw', 'adminpw2'])
  for (const ev of ['input', 'change'])
    document.getElementById(id).addEventListener(ev, pwCheckMatch);

let dirty = false;
/* Every control that refresh() writes back into MUST be in this list.
 *
 * refresh() runs every five seconds and repopulates these from the client's
 * real state, which is right -- unless somebody is part-way through changing
 * one. `dirty` is what stops that, and it is only set for fields listed here.
 *
 * Five OSK controls were missing, so choosing a key size and then pausing for
 * five seconds put the old value back. It looked like the dropdown "reverting
 * on its own", and the only way to win was to hit Apply fast enough. Reported
 * from the bench 2026-08-30.
 *
 * Keep this in step with the `if(!dirty)` block in refresh(). If you add a
 * field there and not here, it will silently fight the person using it. */
const SETTING_FIELDS = [
  'url','zoom','refresh','idle','rotation','hwaccel','tz','ntpsel',
  'resolution','geo','printer','printwidth',
  'osk','oskcaps','oskdx','oskdy','oskscale','printerbackup',
  'adminidle','recoveryemail'
];
for (const id of SETTING_FIELDS)
  for (const ev of ['input','change']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(ev, () => dirty = true);
  }

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
        msg(r.ok?'Set':'Failed: '+r.status, r.ok?'':'warn'); dirty=false;
        if(r.ok){
          msg('Set \u2014 refreshing the screenshot in 5s');
          setTimeout(() => { try { shot(); msg('Set'); } catch(e){} }, 5000);
        } }
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
    osk_offset_y: parseInt(document.getElementById('oskdy').value||0),
    osk_scale: parseFloat(document.getElementById('oskscale').value||1)
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

/* A button that has to be held down, continuously, for ten seconds.
 *
 * Used for the actions on this page that cannot be undone from this page:
 * shutting a display down, and replacing its operating system. A confirm()
 * dialog sits one absent-minded OK away from somebody driving to a shop, so
 * those get a deliberate sequence instead — and the hold has to be
 * continuous, because a ten-second press is not something anybody does by
 * accident.
 *
 * Written once and shared rather than copied per button. This began as the
 * shutdown button's own code with sdTimer and sdLeft as module globals; a
 * second button would have had to duplicate every line of it, including a fill
 * animation that has to stay in step with a countdown. The timer and the count
 * now live in the closure, one set per button.
 *
 *   holdButton({id, msg, idle, run, done, fire, fill, track})
 *
 * Returns {reset, stop, el}. reset() restores the idle label and does NOT
 * touch `disabled`: callers gate that themselves, which is how the shutdown
 * checkbox and the OS acknowledgement both work.
 */
function holdButton(o){
  var b = document.getElementById(o.id);
  if(!b){ return null; }
  var timer = null, left = 10;
  var fill = o.fill || '#c0392b', track = o.track || '#3a1d1d';

  function say(t){
    var e = o.msg && document.getElementById(o.msg);
    if(e){ e.textContent = t; e.className = 'msg'; }
  }
  function reset(){
    left = 10; b.textContent = o.idle; b.style.background = '';
  }
  function stop(){
    if(timer){ clearInterval(timer); timer = null; }
    reset();
  }
  function start(e){
    if(e){ e.preventDefault(); }
    if(b.disabled || timer){ return; }
    left = 10;
    b.textContent = o.run + ' — ' + left + 's';
    timer = setInterval(function(){
      left -= 1;
      if(left > 0){
        b.textContent = o.run + ' — ' + left + 's';
        /* fills left to right as the count runs down */
        var pct = Math.round((10 - left) * 10);
        b.style.background =
          'linear-gradient(to right,' + fill + ' 0%,' + fill + ' ' + pct + '%,'
          + track + ' ' + pct + '%,' + track + ' 100%)';
        return;
      }
      clearInterval(timer); timer = null;
      b.textContent = o.done;
      b.disabled = true;
      o.fire();
    }, 1000);
  }
  function end(e){
    if(e){ e.preventDefault(); }
    if(!timer){ return; }
    stop();
    say('Released early — nothing was sent. Hold the full ten seconds.');
  }

  ['pointerdown','touchstart','mousedown'].forEach(function(ev){
    b.addEventListener(ev, function(e){
      if(ev === 'pointerdown' || !window.PointerEvent){ start(e); }
      else { e.preventDefault(); }
    }, {passive:false});
  });
  ['pointerup','pointercancel','pointerleave','touchend','touchcancel',
   'mouseup','mouseleave'].forEach(function(ev){
    b.addEventListener(ev, end);
  });
  reset();
  // `label` mutates the idle text and repaints. One button here has to say
  // install, reinstall, or install-an-older-version depending only on what is
  // published, and a separate button per verb would put three ten-second
  // holds on a page where two is already a lot.
  return {reset: reset, stop: stop, el: b,
          label: function(t){ o.idle = t; reset(); }};
}

/* Shutdown: read it, tick it, hold it. Nothing on the network can switch this
 * device back on, so the sequence is the safeguard. */
var sdHold = holdButton({
  id: 'sdhold', msg: 'sdmsg',
  idle: 'Hold to shut down — 10s',
  run:  'Keep holding',
  done: 'Shutting down...',
  fire: function(){
    document.getElementById('sdmsg').textContent =
      'Sent. The device is powering off.';
    post('/api/shutdown', 'Shutting down');
  }
});

function armShutdown(){
  document.getElementById('sdconfirm').style.display = '';
  document.getElementById('sdstart').disabled = true;
  var ack = document.getElementById('sdack');
  ack.checked = false;
  var hold = document.getElementById('sdhold');
  hold.disabled = true;
  ack.onchange = function(){ hold.disabled = !ack.checked; };
  if(sdHold){ sdHold.reset(); }
}

function cancelShutdown(){
  if(sdHold){ sdHold.stop(); }
  document.getElementById('sdconfirm').style.display = 'none';
  document.getElementById('sdstart').disabled = false;
  document.getElementById('sdmsg').textContent = '';
}



function dur(s){ s=Math.max(0,s|0);
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600),
        m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`: h?`${h}h ${m}m`: `${m}m ${s%60}s`; }

let seenVersion = null;

/* This page IS kiosk.py, so an update replaces it underneath whoever has it
 * open. The stale copy keeps polling happily and looks fine, but its buttons
 * post to a version that is gone -- which shows up as "I pressed it and
 * nothing happened", the least debuggable symptom there is.
 *
 * Never reload out from under somebody's typing: an update lands in the
 * maintenance window, but a person can also press Install now while halfway
 * through a URL. If anything is unsaved or focused, offer it instead. */
function versionChanged(v){
  if (seenVersion === null) { seenVersion = v; return; }
  if (!v || v === seenVersion) { return; }
  const a = document.activeElement;
  const busy = dirty || (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  const b = document.getElementById('updbanner');
  if (!busy) { location.reload(); return; }
  if (b) {
    b.innerHTML = 'This device updated to <b>' + esc(v) + '</b>. '
      + 'This page is the old version &mdash; '
      + '<a href="#" id="updreload">reload it</a> when you have finished.';
    b.style.display = '';
    const link = document.getElementById('updreload');
    if (link) link.addEventListener('click', e => { e.preventDefault(); location.reload(); });
  }
}

async function refresh(){
  let s; try { s = await (await fetch('/api/status')).json(); } catch(e){ return; }
  versionChanged(s.version);
  idleTimer.set(s.admin_idle_minutes, s.admin_protected);
  const so = document.getElementById('signoutbtn');
  if (so) so.style.display = s.admin_protected ? '' : 'none';
  const ai = document.getElementById('adminidle');
  if (ai && document.activeElement !== ai && !dirty)
    ai.value = s.admin_idle_minutes;
  const re = document.getElementById('recoveryemail');
  if (re && document.activeElement !== re && !dirty)
    re.value = s.recovery_email || '';

  const host = document.getElementById('host');
  // innerHTML, not textContent, so the wordmark can carry its colours -- and
  // every dynamic part is escaped, because client_name is set by whoever has
  // the admin page and would otherwise be a way to inject markup here.
  //
  // Both versions, both labelled. It read "SteadyScreen b1.13.27" -- one
  // number, and you had to already know it was the software one. There are
  // two versions on this machine that move independently, and the OS is the
  // one you cannot find out any other way: it is not on any other page of this
  // admin, and the OS card that would tell you is folded shut.
  //
  // Phrased "software X - os Y" to match the startup page and the status page,
  // which already say it that way. Three pages inventing three wordings for
  // the same two fields is worse than any one of the wordings.
  //
  // os 0 is shown as 0, not hidden or prettified. It is a real value meaning
  // "older than every numbered OS" -- true of any client installed from an
  // image and never updated -- and a client that reports it should look
  // different from one that reports 1.3, because it is.
  host.innerHTML =
    esc(s.client_name || s.kiosk_name || s.hostname) + ' \u00b7 ' + esc(s.ip)
    + ' \u00b7 <!--BRAND--> \u00b7 software '
    + esc(s.software_version || s.version || '?')
    + ' \u00b7 os ' + esc(s.os_version === undefined ? '?' : s.os_version);
  const cid = s.client_id || s.kiosk_id;
  host.title = cid ? 'id ' + cid : '';

  const un = document.getElementById('updnow');
  if(un){
    un.innerHTML =
      '<dt>Software</dt><dd>' + esc(s.software_version || s.version || '?')
      + '</dd><dt>Operating system</dt><dd>'
      + (s.os_version === undefined ? '?'
         : s.os_version === '0'
           // Said once, here, rather than left as a bare 0 that reads like an
           // error. The OS card repeats it because the card can be reached
           // without ever scrolling past this.
           ? '0 <span style="color:var(--dim)">&mdash; installed from an image, '
             + 'never updated</span>'
           : esc(s.os_version))
      + '</dd>'
      // When this display last SPOKE to the release host, which until
      // 2026-09-03 was not recorded anywhere: last_check was written only at
      // the end of an install and carried the same timestamp as last_applied,
      // so a display stale for six months looked exactly like one checked an
      // hour ago. The hourly timer now records a real check even outside the
      // install window, and even when automatic updates are off.
      + (function(){
          const u = s.update || {};
          const t = u.last_check;
          const ms = t ? Date.parse(t) : NaN;
          const old = !t || isNaN(ms) ||
                      (Date.now() - ms) / 1000 > UPD_STALE_S;
          let v = old ? '<span class=warn>' + agoIso(t) + '</span>'
                      : '<span class=ok>' + agoIso(t) + '</span>';
          // Why it is stale matters more than that it is. A display that
          // cannot reach the release host and one whose owner switched
          // automatic updates off are different problems with different
          // answers, and "not checked in 9 days" alone sends you looking at
          // the wrong one.
          if(u.last_check_result === 'unreachable')
            v += ' <span class=warn>&mdash; cannot reach the release host</span>';
          else if(old && s.update_config && s.update_config.auto === false)
            v += ' <span style="color:var(--dim)">&mdash; automatic updates are off</span>';
          return '<dt>Last checked</dt><dd>' + v + '</dd>';
        })()
      + (function(){
          const u = s.update || {};
          if(!u.pending || !u.available) return '';
          return '<dt>Waiting to install</dt><dd><span class=warn>'
               + esc(u.available) + '</span>'
               + ' <span style="color:var(--dim)">&mdash; press Install now, '
               + 'or leave it for the nightly window</span></dd>';
        })();
  }

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
    document.getElementById('oskscale').value = String(s.osk_scale || 1);
    // Fill the saved-states list once, on the first status that arrives.
    if(!window.__snapLoaded){ window.__snapLoaded = 1; snapRefresh(); }
    // Show what the updater is actually set to, not what was last typed --
    // this is the control someone checks before sending a machine to a shop.
    const uc = s.update_config || {};
    const ua = document.getElementById('updauto');
    if(ua && document.activeElement !== ua) ua.checked = !!uc.auto;
    const uch = document.getElementById('updchannel');
    if(uch && document.activeElement !== uch) uch.value = uc.channel || 'stable';
    const uw = document.getElementById('updwindow');
    if(uw) uw.textContent = uc.auto && uc.window
        ? 'Automatic installs happen between ' + uc.window + '.' : '';
  }
}

function scmsg(t, cls){ const m=document.getElementById('scmsg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

let scChecked = null;

/* Any edit invalidates a previous check. Otherwise somebody checks one link,
   edits it, and holds a button that installs what they checked rather than
   what is in the box. */
function scDirty(){
  scChecked = null;
  document.getElementById('scgorow').style.display = 'none';
  document.getElementById('scdetail').style.display = 'none';
  scmsg('');
}

async function scCheck(){
  const url = document.getElementById('scurl').value.trim();
  if(!url){ scmsg('Paste the installer link first', 'warn'); return; }
  const b = document.getElementById('sccheckbtn');
  b.disabled = true;
  scmsg('Checking...');
  try{
    const r = await fetch('/api/screenconnect/check', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify({url})});
    const d = await r.json();
    const box = document.getElementById('scdetail');
    if(!d.ok){
      box.style.display = 'none';
      document.getElementById('scgorow').style.display = 'none';
      scmsg(d.detail || 'that link was refused', 'warn');
      return;
    }
    scChecked = url;
    /* Everything here came off the pasted URL, so all of it is escaped. */
    box.style.display = '';
    box.innerHTML =
      '<dl>'
      + '<dt>Server</dt><dd>' + esc(d.host) + '</dd>'
      + '<dt>Certificate</dt><dd>' + (d.trusted
          ? esc(d.cert || '') + '<br><span class=ok>verified: this host is '
            + esc(d.cert_org || 'unknown') + "'s</span>"
          : '<span class=warn>' + esc(d.cert || 'could not be verified')
            + '</span>') + '</dd>'
      + (d.issuer ? '<dt>Issued by</dt><dd>' + esc(d.issuer)
          + (d.expires ? ', valid to ' + esc(d.expires) : '') + '</dd>' : '')
      + '<dt>Will install</dt><dd>' + esc(d.installer) + '</dd>'
      + (d.name ? '<dt>Name in console</dt><dd>' + esc(d.name) + '</dd>' : '')
      + '<dt>Custom fields</dt><dd>' + (d.fields > 0
          ? esc(String(d.fields)) + ' carried through'
          : '<span class=warn>none &mdash; it will arrive unnamed</span>') + '</dd>'
      + '</dl>';
    document.getElementById('scgorow').style.display = '';
    const go = document.getElementById('scgo');
    if(d.trusted){
      go.disabled = false;
      go.textContent = 'Hold to install — 10s';
      scmsg('');
    } else {
      go.disabled = true;
      go.textContent = 'Cannot verify this server — will not install';
      scmsg('The certificate for ' + d.host + ' does not show it belongs to '
        + 'ConnectWise. A certificate authority will not issue one naming them '
        + 'to anybody else, so a host that cannot show one is not ScreenConnect '
        + 'whatever its address looks like.'
        + (d.why ? '  \u2014  ' + d.why : ''), 'warn');
    }
  }catch(e){ scmsg('Failed: '+e, 'warn'); }
  finally{ b.disabled = false; }
}

async function scInstall(){
  const url = document.getElementById('scurl').value.trim();
  if(url !== scChecked){ scmsg('The link changed since it was checked — check it again', 'warn'); return; }
  scmsg('Installing... this takes a minute.');
  try{
    const r = await fetch('/api/screenconnect/install', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify({url})});
    const d = await r.json();
    scmsg(d.detail || (d.ok ? 'installed' : 'failed'), d.ok ? '' : 'warn');
    scRefresh();
  }catch(e){ scmsg('Failed: '+e, 'warn'); }
}

async function scRefresh(){
  const dl = document.getElementById('scinfo');
  if(!dl) return;
  try{
    const d = await (await fetch('/api/screenconnect')).json();
    dl.innerHTML =
      '<dt>Agent</dt><dd>' + (d.installed
        ? '<span class=ok>installed</span>'
          + (d.running ? '' : ' <span class=warn>but not running</span>')
        : 'not installed') + '</dd>'
      + (d.service ? '<dt>Service</dt><dd>' + esc(d.service)
          + (d.running ? ' <span class=ok>running</span>' : '') + '</dd>' : '')
      + (d.helper === false
          ? '<dt>Helper</dt><dd><span class=warn>not here yet &mdash; arrives '
            + 'on the next update check, within the hour</span></dd>' : '');
    // Greyed with the reason, not offered and then refused. Same rule as the
    // OS-gated install button: a button that is pressed and rejected teaches
    // somebody the page is unreliable.
    const cb = document.getElementById('sccheckbtn');
    if(cb){
      cb.disabled = (d.helper === false);
      cb.title = cb.disabled
        ? 'This display has the page but not yet the program behind it. It '
          + 'installs itself on the next update check, usually within the '
          + 'hour \u2014 nothing needs doing.'
        : '';
    }
  }catch(e){ dl.innerHTML = ''; }
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

function cmsg(s, cls){
  const e = document.getElementById('cachedmsg');
  e.textContent = s; e.className = 'msg ' + (cls || '');
}

async function setSettings(obj, say, where){
  try{
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify(obj)});
    where(r.ok ? say : 'failed', r.ok ? '' : 'warn');
    refresh();
    return r.ok;
  }catch(e){ where('failed: '+e, 'warn'); return false; }
}

function toggleCached(){
  const on = !(lastStatus && lastStatus.cached_page);
  setSettings({cached_page: on},
              'showing the last saved page is ' + (on ? 'on' : 'off'), cmsg);
}

function saveCached(){
  const w = parseInt(document.getElementById('cachedwait').value, 10);
  const a = parseInt(document.getElementById('cachedage').value, 10);
  if(!(w >= 5)){ cmsg('wait at least 5 seconds', 'warn'); return; }
  if(!(a >= 1)){ cmsg('give a maximum age in hours', 'warn'); return; }
  setSettings({cached_page_wait: w, cached_page_max_age_hours: a},
              'saved', cmsg);
}

async function saveNow(){
  try{
    const r = await fetch('/api/cached/save', {method:'POST'});
    cmsg(r.ok ? 'taking a picture of the page now...' : 'failed',
         r.ok ? '' : 'warn');
    setTimeout(refresh, 2500);
  }catch(e){ cmsg('failed: '+e, 'warn'); }
}

function nwmsg(s, cls){
  const e = document.getElementById('nwmsg');
  e.textContent = s; e.className = 'msg ' + (cls || '');
}
function toggleNw(){
  const on = !(lastStatus && lastStatus.netwatch_enabled);
  setSettings({netwatch: on}, 'watchdog ' + (on ? 'on' : 'off'), nwmsg);
}
function toggleNwReboot(){
  const on = !(lastStatus && lastStatus.netwatch_reboot);
  setSettings({netwatch_reboot: on},
              'reboot as a last resort ' + (on ? 'on' : 'off'), nwmsg);
}

function ago(ts){
  if(!ts) return 'never';
  const s = Math.max(0, Math.round(Date.now()/1000 - ts));
  if(s < 90) return s + 's ago';
  if(s < 5400) return Math.round(s/60) + ' min ago';
  if(s < 172800) return Math.round(s/3600) + ' hours ago';
  return Math.round(s/86400) + ' days ago';
}

/* last_check is an ISO 8601 string, because that is what `date -Is` writes and
   what a person reads in the state file over ssh. ago() takes unix seconds.
   Convert here rather than changing what kiosk-update records: the readable
   string in the file is worth more than four lines of JavaScript. */
function agoIso(t){
  if(!t) return 'never';
  const ms = Date.parse(t);
  if(isNaN(ms)) return esc(String(t));
  return ago(Math.round(ms/1000));
}

/* Older than this and the display is not being kept up to date by anything,
   whatever the config claims. Two days rather than one: the window is nightly,
   so a single missed night is a machine that was switched off, not a fault. */
const UPD_STALE_S = 172800;

function renderCached(s){
  const b = document.getElementById('cachedbtn');
  if(b) b.textContent = 'Saved page: ' + (s.cached_page ? 'ON' : 'OFF');
  const w = document.getElementById('cachedwait');
  if(w && document.activeElement !== w) w.value = s.cached_page_wait;
  const a = document.getElementById('cachedage');
  if(a && document.activeElement !== a) a.value = s.cached_page_max_age_hours;
  const st = s.cached_page_state || {};
  const dl = document.getElementById('cachedstate');
  if(dl){
    let rows = '';
    if(!st.exists){
      rows = '<dt>Saved page</dt><dd>nothing saved yet</dd>';
    } else {
      rows = '<dt>Saved</dt><dd>' + ago(st.when)
           + (st.usable ? '' : ' <span class=warn>&mdash; too old to show</span>')
           + '</dd>';
    }
    if(s.last_cached_save)
      rows += '<dt>Last attempt</dt><dd>' + s.last_cached_save + '</dd>';
    if(s.showing_cached)
      rows += '<dt>On screen now</dt><dd><span class=warn>the saved page</span></dd>';
    dl.innerHTML = rows;
  }
}

function renderNetwatch(s){
  const nw = s.netwatch || {};
  const dl = document.getElementById('nwstate');
  const b = document.getElementById('nwbtn');
  if(b) b.textContent = 'Watchdog: ' + (s.netwatch_enabled ? 'ON' : 'OFF');
  const rb = document.getElementById('nwrebootbtn');
  if(rb) rb.textContent = 'Reboot as a last resort: '
                        + (s.netwatch_reboot ? 'ON' : 'OFF');
  if(!dl) return;
  if(!Object.keys(nw).length){
    // A client that has not taken the update yet has no watchdog and no
    // file. Say that, rather than drawing an empty table that reads as a
    // watchdog reporting nothing wrong.
    dl.innerHTML = '<dt>Status</dt><dd>not running on this display yet</dd>';
    return;
  }
  const p = nw.last_probe || {};
  let rows = '<dt>Last check</dt><dd>'
    + (p.ok ? '<span class=ok>reached the gateway</span>'
            : '<span class=warn>' + (p.why || 'failed') + '</span>')
    + (p.at ? ' &middot; ' + ago(p.at) : '') + '</dd>'
    + '<dt>Failed checks</dt><dd>' + (nw.fails || 0) + ' in a row</dd>'
    + '<dt>Last good</dt><dd>' + ago(nw.last_ok) + '</dd>';
  if(nw.actions)
    rows += '<dt>Repairs attempted</dt><dd>' + nw.actions + '</dd>';
  if(nw.last_reboot)
    rows += '<dt>Last watchdog reboot</dt><dd>' + ago(nw.last_reboot) + '</dd>';
  if((nw.log || []).length)
    rows += '<dt>Recent</dt><dd>' + nw.log.slice().reverse()
              .map(e => esc(e.what)).join('<br>') + '</dd>';
  dl.innerHTML = rows;
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

function snapmsg(t, c){ const e=document.getElementById('snapmsg');
  e.textContent=t; e.className='msg '+(c||''); }

async function snapCall(action, id){
  const r = await fetch('/api/snapshot', {method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({action, id})});
  return [r.ok, await r.json()];
}

async function snapRefresh(){
  const box = document.getElementById('snaplist');
  try{
    const [ok, d] = await snapCall('list');
    if(!ok){ box.innerHTML=''; snapmsg(d.detail || 'could not read saved states','warn'); return; }
    const list = d.snapshots || [];
    if(!list.length){ box.innerHTML =
      '<div style="color:var(--dim)">none saved yet</div>'; return; }
    box.innerHTML = list.map(s => {
      const when = (s.created || s.id).replace('T',' ').slice(0,16);
      const what = s.label ? esc(s.label) : 'manual';
      const ver  = s.software ? ' &middot; ' + esc(s.software) : '';
      return '<div style="display:flex;align-items:center;gap:.6rem;'
           + 'padding:.35rem 0;border-bottom:1px solid var(--line)">'
           + '<div style="flex:1"><b>' + esc(when) + '</b> &middot; ' + what + ver
           + '</div>'
           + '<button onclick="snapRestore(\'' + esc(s.id) + '\')">Restore</button>'
           + '</div>';
    }).join('');
  }catch(e){ snapmsg('could not read saved states: '+e,'warn'); }
}

async function snapCreate(){
  snapmsg('saving...');
  try{
    const [ok, d] = await snapCall('create', 'manual');
    snapmsg(d.detail || (ok ? 'saved' : 'failed'), ok ? '' : 'warn');
    snapRefresh();
  }catch(e){ snapmsg('failed: '+e,'warn'); }
}

async function snapRestore(id){
  if(!confirm('Put this display back to the state saved at ' + id + '?\n\n'
            + 'Settings, network profiles and the configured page are all '
            + 'restored, and the display reboots.\n\nThe current state is '
            + 'saved first, so this can itself be undone.')) return;
  snapmsg('restoring...');
  try{
    const [ok, d] = await snapCall('restore', id);
    if(!ok){ snapmsg(d.detail || 'failed','warn'); return; }
    snapmsg('restored -- rebooting to finish');
    // /api/reboot -- there is no /api/power. A restore that quietly did not
    // reboot would leave the running client using the settings it loaded at
    // boot, so the operator would see "restored" and no change whatsoever.
    await fetch('/api/reboot', {method:'POST'}).catch(()=>{});
  }catch(e){ snapmsg('failed: '+e,'warn'); }
}

async function saveUpdatePolicy(){
  const msg = (t, c) => { const e = document.getElementById('updpolmsg');
    e.textContent = t; e.className = 'msg ' + (c || ''); };
  const auto = document.getElementById('updauto').checked;
  const channel = document.getElementById('updchannel').value;
  if(channel === 'testing' && !confirm(
      'The "latest" channel installs new builds before they are proven.\n\n'
    + 'Use it on a bench machine, not on one a shop depends on. Continue?')){
    return;
  }
  msg('saving...');
  try{
    const r = await fetch('/api/update/settings', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({auto, channel})});
    const d = await r.json();
    msg(d.detail || (r.ok ? 'saved' : 'failed'), r.ok ? '' : 'warn');
    if(r.ok) refresh();
  }catch(e){ msg('failed: '+e, 'warn'); }
}

// Wait for the client to come back after it restarts itself, and say what it
// came back as. Installing an update restarts the browser, which drops the
// request in flight and used to leave the operator reading "connection lost,
// re-check in a moment" -- telling somebody to go and do by hand the one
// thing the page is in a position to do for them, at the exact moment they
// most want to know whether it worked.
/* Poll /api/status until this display answers again.
 *
 * Shared by both updates, because both restart the thing this page is served
 * by and so both have to answer the same question: is it back, and is it back
 * as what we installed?
 *
 * Answering "yes" at the first successful request is wrong. The request can
 * land before the restart has even begun, so the page would congratulate
 * itself against the version it was already running. Waiting until we have
 * seen it go down, OR until the value we are watching actually changes, is
 * what makes the answer mean anything.
 *
 *   pollForReturn({deadline, field, was, tick})
 *     tick(alive, secondsLeft) is called on every attempt, so a caller can
 *     show a countdown without owning the loop.
 *
 *   -> {ok:true,  status, value, unchanged}   it came back
 *      {ok:false}                             it did not, within the deadline
 */
/* The port this page is on may not be the port the client comes back on.
 *
 * An update can move a client from 8080 to 80 -- that is the whole point of the
 * migration -- and the page waiting for it is served from 8080, polling 8080,
 * which is now silent. It waits out its full deadline and reports a client that
 * never returned, about a machine that came back immediately somewhere else.
 * Reported from the field on 2026-08-31: "appears to hang the interface due to
 * the port change. I went to port 80 and it's updated now."
 *
 * So the other port is tried too, and if that is where it went, the page says
 * so and follows it.
 */
function otherPortUrl(path){
  const p = location.port || '80';
  const other = (p === '8080') ? '80' : (p === '80' || p === '') ? '8080' : null;
  if(other === null) return null;
  return location.protocol + '//' + location.hostname
       + (other === '80' ? '' : ':' + other) + path;
}

async function pollForReturn(o){
  const end = Date.now() + o.deadline;
  let seenDown = false;
  while(Date.now() < end){
    await new Promise(r => setTimeout(r, 2000));
    let st = null;
    try{
      const r = await fetch('/api/status', {cache:'no-store'});
      if(r.ok) st = await r.json();
    }catch(e){ /* still down; that is expected, keep knocking */ }
    if(!st){
      // Not here. Is it on the other port?
      const u = otherPortUrl('/api/status');
      if(u){
        try{
          const r2 = await fetch(u, {cache:'no-store'});
          if(r2.ok){
            const s2 = await r2.json();
            return {ok:true, status:s2, value:s2[o.field], moved:u,
                    unchanged: !!(o.was && s2[o.field] === o.was)};
          }
        }catch(e){ /* not there either; carry on waiting */ }
      }
    }
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    if(!st){ seenDown = true; if(o.tick) o.tick(false, left); continue; }
    if(!seenDown && st[o.field] === o.was){
      if(o.tick) o.tick(true, left);
      continue;
    }
    return {ok:true, status:st, value:st[o.field],
            unchanged: !!(o.was && st[o.field] === o.was)};
  }
  return {ok:false};
}

async function waitForClient(was){
  const res = await pollForReturn({
    deadline: 180000,               // installs are slow on eMMC
    field: 'software_version', was: was,
    tick: function(alive){
      if(!alive) umsg('installing — the client is restarting...');
    }
  });
  if(!res.ok){
    umsg('the client has not answered for three minutes. It may still be '
       + 'installing; if the screen is dark it may need power cycling.', 'warn');
    return;
  }
  if(res.moved){
    const to = res.moved.replace('/api/status', '/');
    umsg('installed. this display has moved to ' + to.replace(/\/$/, '')
       + ' \u2014 opening it...');
    setTimeout(() => { location.href = to; }, 1500);
    return;
  }
  const now = res.value || 'an unknown version';
  if(res.unchanged){
    umsg('the client came back still running ' + now
       + ' — the update did not take, or it was rolled back.', 'warn');
  } else {
    umsg('installed. now running ' + now);
  }
  refresh(); updCheck();
}

async function updApply(){
  const avail = (lastUpd && lastUpd.available) || 'the published release';
  if(!confirm('Install ' + avail + ' now?\n\nThe browser restarts. If the new '
            + 'version fails to come up it is rolled back automatically.')) return;
  const b = document.getElementById('updapplybtn');
  const was = (lastStatus && lastStatus.software_version) || '';
  b.disabled = true; umsg('installing -- this can take a minute...');
  try{
    const r = await fetch('/api/update', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'apply'})});
    const d = await r.json();
    if(!r.ok){ umsg(d.detail || 'failed', 'warn'); return; }
    umsg(d.detail || 'installed');
    await waitForClient(was);
  }catch(e){
    // The browser restarting mid-request looks exactly like a network error.
    // It is the expected case, not a failure -- so wait it out and report
    // what came back rather than handing the job to the operator.
    await waitForClient(was);
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
  // Gated on the OS this release declares it needs. kiosk-update refuses such
  // an install anyway -- this is not the safeguard, it is the explanation. A
  // button that is offered, pressed, and then refused teaches somebody that
  // the page is unreliable; a greyed one carrying the reason teaches them
  // which thing to update first.
  //
  // requires_os is per RELEASE, not per component: the manifest carries one
  // value for the whole thing. So this greys the install as a whole rather
  // than picking components out of the list, which would imply a granularity
  // that does not exist.
  const needOs = String(u.os_required || '0');
  const haveOs = String(u.os_installed || '0');
  const gated = osCmp(needOs, haveOs) > 0;
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
  + (gated ? `<dt>Needs OS</dt><dd><span class=warn>${esc(needOs)} &mdash; `
           + `this display is on ${haveOs === '0' ? 'an unversioned OS'
                : esc(haveOs)}</span></dd>` : '')
  + `<dt>Automatic</dt><dd>${u.auto
        ? ('yes, between ' + (u.window || '03:00-05:00')
           + (u.in_window ? ' (window open now)' : ''))
        : 'no, manual only'}</dd>`;
  // A downgrade is not offered as a one-click action. The updater refuses it
  // too; this keeps the button from claiming something the backend will reject.
  btn.style.display = (u.update_available && !down) ? '' : 'none';
  btn.textContent = 'Install ' + (u.available || 'update');
  // Greyed, not hidden, and the reason travels on the button itself: hover or
  // long-press gives the whole sentence, which is the only place there is room
  // for it.
  btn.disabled = gated;
  btn.classList.toggle('primary', !gated);
  btn.title = gated
    ? 'This release needs operating system ' + needOs + '. This display is on '
      + (haveOs === '0' ? 'an unversioned OS' : haveOs) + '. Update the '
      + 'operating system first \u2014 see Operating system, below.'
    : '';
  if(u.update_available){
    umsg(down ? 'not offering this: it would move this client backwards.'
       : gated
         ? 'this release needs operating system ' + needOs + ' — update the '
           + 'operating system below first, and it brings this software with it.'
         : '', (down || gated) ? 'warn' : '');
    return;
  }
  // "Nothing to install" is the whole truth for a shop. It is not the whole
  // truth for whoever is testing, so the explanation is one tap away rather
  // than either shouted or hidden: an info symbol that toggles it, and on the
  // testing channel it starts open, because that is who wants it.
  const e = document.getElementById('updmsg');
  e.className = 'msg';
  if(!u.note_detail){ e.textContent = 'nothing to install'; return; }
  const open = (u.channel === 'testing');
  e.innerHTML = 'nothing to install'
    + (u.note ? ' &mdash; <span style="color:var(--dim)">' + esc(u.note) + '</span>' : '')
    // min-width and min-height, not just width and height.
    //
    // The page's own `button` rule sets min-width:8.5rem and min-height:2.9rem
    // so that the rows of real buttons line up. Setting width/height here does
    // not beat a minimum -- so this rendered as an 8.5rem oval with a single
    // letter lost in the middle of it. flex:0 0 auto for the same reason: the
    // rule says flex:1 1 0, which would stretch it to fill whatever row it
    // lands in.
    + ' <button type=button id=updwhy aria-expanded="' + open + '"'
    + ' style="border:1px solid var(--line);background:transparent;color:var(--dim);'
    + 'border-radius:50%;width:1.35rem;height:1.35rem;min-width:0;min-height:0;'
    + 'flex:0 0 auto;padding:0;line-height:1;font:inherit;font-size:.8rem;'
    + 'display:inline-flex;align-items:center;justify-content:center;'
    + 'vertical-align:middle;cursor:pointer" title="why">i</button>'
    + '<div id=updwhytext style="display:' + (open ? 'block' : 'none')
    + ';color:var(--dim);font-size:.85rem;margin-top:.5rem;max-width:46rem">'
    + esc(u.note_detail) + '</div>';
  const b = document.getElementById('updwhy'), t = document.getElementById('updwhytext');
  if(b && t) b.onclick = () => {
    const now = t.style.display === 'none';
    t.style.display = now ? 'block' : 'none';
    b.setAttribute('aria-expanded', String(now));
  };
}

/* ---- the operating system --------------------------------------------
 *
 * Version comparison, component-wise: 1 < 1.1 < 1.2 < 1.10 < 2.
 *
 * NOT numerically. Read as a decimal 1.10 is smaller than 1.9, which is the
 * oldest version bug there is. The same comparison exists in kiosk-update
 * (python) and kiosk-osupdate (shell); this is the third copy and it has to
 * agree with both, because this one decides whether a button is greyed out and
 * those two decide whether the install is refused. A disagreement shows up as
 * a button that is offered and then rejected, or worse, one that is greyed out
 * for no reason anybody can see.
 *
 *   osCmp(a, b) -> -1 a older, 0 same, 1 a newer
 */
function osCmp(a, b){
  const parts = v => String(v == null ? '0' : v).trim().split('.')
                       .map(x => /^[0-9]+$/.test(x) ? parseInt(x, 10) : 0);
  const x = parts(a), y = parts(b);
  const n = Math.max(x.length, y.length);
  for(let k = 0; k < n; k++){
    const p = x[k] || 0, q = y[k] || 0;
    if(p !== q) return p > q ? 1 : -1;
  }
  return 0;
}

/* ---- the update modal ------------------------------------------------- */

var osmCountTimer = null;

function osmOpen(title){
  if(osmCountTimer){ clearInterval(osmCountTimer); osmCountTimer = null; }
  document.getElementById('osmtitle').textContent = title;
  document.getElementById('osmwait').style.display = 'none';
  document.getElementById('osmbar').style.width = '0%';
  document.getElementById('osmcount').textContent = '';
  const m = document.getElementById('osmmsg');
  m.textContent = ''; m.className = 'msg';
  osmRow(null);
  document.getElementById('osmodal').style.display = '';
}

/* Exactly one row of buttons is ever visible, or none.
 *
 * Which one IS the state machine: cancel while downloading, decide once it has
 * landed, nothing at all while the slot is being written, close when it is
 * over. Driving them from one place is what stops a Cancel button surviving
 * into a phase where cancelling is no longer possible. */
function osmRow(which){
  const rows = {cancel:'osmcancelrow', go:'osmgorow', close:'osmcloserow'};
  for(const k in rows){
    document.getElementById(rows[k]).style.display = (k === which) ? '' : 'none';
  }
}

function osmPhase(headline, note){
  document.getElementById('osmphase').textContent = headline;
  document.getElementById('osmnote').innerHTML = note || '';
}

/* The countdown is honest about what it is: a bound on how long this page will
 * keep knocking, not an estimate of how long the device will take. It is
 * labelled that way on screen for the same reason. */
function osmCountdown(total){
  const wrap  = document.getElementById('osmwait');
  const bar   = document.getElementById('osmbar');
  const count = document.getElementById('osmcount');
  wrap.style.display = '';
  let left = total;
  const draw = () => {
    const pct = Math.max(0, Math.min(100, ((total - left) / total) * 100));
    bar.style.width = pct.toFixed(1) + '%';
    const m = Math.floor(left / 60), sec = left % 60;
    count.textContent = left > 0
      ? 'Still trying — giving up in ' + (m ? m + 'm ' : '')
        + (sec < 10 && m ? '0' : '') + sec + 's'
      : 'No answer yet.';
  };
  draw();
  if(osmCountTimer) clearInterval(osmCountTimer);
  osmCountTimer = setInterval(() => { left = Math.max(0, left - 1); draw(); }, 1000);
  return {
    set: v => { left = Math.max(0, v); },
    stop: () => { if(osmCountTimer){ clearInterval(osmCountTimer); osmCountTimer = null; } }
  };
}

/* The same bar as the countdown, driven directly instead of by a timer.
 *
 * One bar, two meanings -- a download counting up and a reconnect counting
 * down -- which is safe only because the two phases cannot overlap: the
 * heartbeat that drives this is stopped before the countdown starts. The
 * `watching` flag in osApply is what actually enforces that, because
 * clearInterval does not cancel a callback already awaiting a fetch.
 */
function osmProgress(frac, label){
  document.getElementById('osmwait').style.display = '';
  document.getElementById('osmbar').style.width =
    Math.max(0, Math.min(100, frac * 100)).toFixed(1) + '%';
  document.getElementById('osmcount').textContent = label;
}

function osmHideBar(){
  if(osmCountTimer) return;          // a countdown owns it; do not take it away
  document.getElementById('osmwait').style.display = 'none';
}

/* Sizes a person reads, not bytes. 690000000 tells you nothing at a glance;
 * "658 MB" tells you whether it is nearly done. */
function osmBytes(n){
  n = Number(n) || 0;
  if(n >= 1048576) return (n / 1048576).toFixed(0) + ' MB';
  if(n >= 1024)    return (n / 1024).toFixed(0) + ' kB';
  return n + ' B';
}

function osmDone(headline, note, cls){
  if(osmCountTimer){ clearInterval(osmCountTimer); osmCountTimer = null; }
  document.getElementById('osmwait').style.display = 'none';
  osmPhase(headline, note);
  const m = document.getElementById('osmmsg');
  m.className = 'msg ' + (cls || '');
  osmRow('close');
}

function osmClose(){
  if(osmCountTimer){ clearInterval(osmCountTimer); osmCountTimer = null; }
  document.getElementById('osmodal').style.display = 'none';
  refresh(); osCheck(); updCheck();
}

/* ---- the OS section --------------------------------------------------- */

let lastOsUpd = null;

/* Which of the three things the install button would do: 'update', 'reinstall',
 * 'older' (a rollback) or 'none'. Set by renderOs, read by the modal so it
 * reports the operation the operator actually chose. */
let osMode = 'none';

/* The version sitting downloaded and verified on the display, waiting for a
 * decision. Null whenever there is nothing staged. */
let osStagedFor = null;

function osmsg(t, cls){
  const e = document.getElementById('osmsg');
  e.textContent = t; e.className = 'msg ' + (cls || '');
}

/* The acknowledgement gate. Read it, tick it, hold it — the same sequence as
 * Shut down, because it protects against the same kind of mistake: an action
 * whose recovery may involve travelling to the device. */
var osAckHold = holdButton({
  id: 'osackhold', msg: 'osackmsg',
  idle: 'Hold to continue — 10s',
  run:  'Keep holding',
  done: 'Checking...',
  fill: '#b07d2b', track: '#3a2f1d',
  fire: function(){ osReveal(); }
});

(function(){
  const ack = document.getElementById('osack');
  const btn = document.getElementById('osackhold');
  if(!ack || !btn) return;
  ack.onchange = function(){ btn.disabled = !ack.checked; };

  // Closing the fold puts the acknowledgement back, for the same reason
  // Hide does: an opened OS section left on a screen is a one-press install
  // that whoever opened it is no longer standing in front of.
  const fold = document.getElementById('osfold');
  if(fold) fold.addEventListener('toggle', function(){
    if(!fold.open) osHide();
  });
})();

function osReveal(){
  document.getElementById('osgate').style.display = 'none';
  document.getElementById('ospanel').style.display = '';
  osCheck();
}

/* Hiding re-arms the gate rather than leaving it open. Somebody who opened
 * this to look, and walked away, should not leave a one-press OS install
 * sitting on a screen on a counter. */
function osHide(){
  document.getElementById('ospanel').style.display = 'none';
  document.getElementById('osgate').style.display = '';
  const ack = document.getElementById('osack');
  const btn = document.getElementById('osackhold');
  if(ack) ack.checked = false;
  if(btn) btn.disabled = true;
  if(osAckHold){ osAckHold.el.disabled = true; osAckHold.stop(); }
  osmsg('');
}

async function osCheck(){
  const b = document.getElementById('oscheckbtn');
  if(!document.getElementById('ospanel') ||
     document.getElementById('ospanel').style.display === 'none') return;
  if(b) b.disabled = true;
  osmsg('checking...');

  // Status first, and separately. It needs no network, so what is sitting in
  // each slot appears even when the release host cannot be reached -- which is
  // exactly the moment somebody is deciding whether to go back to the other
  // slot. Folding it into `check` would have tied the one answer that is
  // always available to the one that is not.
  let slots = null;
  try{
    const r = await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'status'})});
    if(r.ok){ const d = await r.json(); slots = d.result || null; }
  }catch(e){ /* it will simply not be shown */ }

  try{
    const r = await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'check'})});
    const d = await r.json();
    if(!r.ok){
      renderOs(slots ? {slots: slots, running_slot: osSlotOf(slots.running),
                        committed_slot: osSlotOf(slots.committed),
                        os_installed: slots.os || '0',
                        software_installed: slots.software || ''} : null);
      osmsg(d.detail || 'check failed', 'warn');
      return;
    }
    const res = d.result || {};
    res.slots = slots;
    renderOs(res);
  }catch(e){ renderOs(null); osmsg('check failed: ' + e, 'warn'); }
  finally{ if(b) b.disabled = false; }
}

/* "slot b  (/dev/mmcblk0p4)" and "slot b" both mean b.
 *
 * Shared by every reader of the helper's status output. It exists because the
 * two fields are formatted differently and a comparison once ran "a" against
 * "slot a" -- never equal, so a committed update reported itself as
 * uncommitted for five minutes and told the operator to go fix it. */
function osSlotOf(v){
  return String(v || '').replace(/^slot\s+/, '').split(/\s/)[0];
}

function renderOs(u){
  lastOsUpd = u;
  const dl    = document.getElementById('osinfo');
  const row   = document.getElementById('osgorow');
  const reRow = document.getElementById('osrerow');
  row.style.display = 'none';
  reRow.style.display = 'none';
  osMode = 'none';
  if(!u){ dl.innerHTML = ''; return; }

  const have = u.os_installed || '0';
  const avail = u.os_available || '';
  const run = u.running_slot || '';
  const com = u.committed_slot || '';
  const target = run === 'a' ? 'b' : (run === 'b' ? 'a' : '');

  // An OS update carries the software baked into its rootfs, wholesale, and
  // nothing on that path can refuse a downgrade. kiosk-update compares
  // versions and declines to move a client backwards; this cannot, because
  // the software is not being installed here, it is being arrived at. So an
  // OS payload built before the current software release silently moves the
  // client back -- and it looks like an upgrade the whole way, because the OS
  // version really is going up.
  //
  // Seen for real on .245, 2026-08-31: os 1.3 bundles b1.13.25 on a client
  // running b1.13.26, so taking the OS update would have removed the very
  // page this warning is written on.
  const swNow = u.software_installed || '';
  const swNew = u.bundles_software || '';
  const swBack = !!(swNow && swNew &&
                    osCmp(swNow.replace(/^b/, ''), swNew.replace(/^b/, '')) > 0);

  dl.innerHTML =
    `<dt>Installed</dt><dd>${have === '0'
        ? 'unversioned <span style="color:var(--dim)">(installed from an image, '
          + 'never updated)</span>' : esc(have)}</dd>`
  + `<dt>Published</dt><dd>${esc(avail || 'unknown')} (${esc(u.channel || '')})</dd>`
  + (u.bundles_software
      ? `<dt>Brings software</dt><dd>${esc(u.bundles_software)}${
          swBack ? ' <span class=warn>&mdash; older than the ' + esc(swNow)
                 + ' running now</span>' : ''}</dd>` : '')
  + (run ? `<dt>Running</dt><dd>slot ${esc(run)}${
        com && com !== run ? ' &mdash; <b>not yet kept</b>' : ''}</dd>` : '')
  // Said in full, as its own row, because the short version was read as a
  // fault and it usually is not one. "committed slot is a -- a reboot would
  // return there" is true and tells you almost nothing: not what is over
  // there, not that this state is normal for two minutes after an update, and
  // not that it clears itself. Somebody read exactly that line and had to ask
  // whether rebooting would downgrade their machine.
  //
  // Both halves matter. During the grace it is the safety mechanism working
  // and the answer is to wait. Long after, it means the commit never happened
  // and the next restart -- a power cut months from now -- silently undoes the
  // update. The page cannot tell which from one reading, so it says both and
  // says which is likely.
  + (run && com && com !== run
      ? `<dt>Careful</dt><dd><span class=warn>This display is running slot `
        + `${esc(run)} but has not committed to it yet.</span> Until it does, `
        + `restarting would take it back to slot ${esc(com)} &mdash; the `
        + `system it was running before.<br><br>`
        + `Just after an update this is normal and lasts about two minutes: `
        + `the display keeps the new system only once it has come up and `
        + `stayed up, and commits on its own. If it still says this several `
        + `minutes later, the commit did not happen and the update will be `
        + `undone at the next restart.</dd>`
      : '')
  + (target ? `<dt>Would install to</dt><dd>slot ${esc(target)}</dd>` : '')
  // What is actually in each slot.
  //
  // "Would install to slot a" says where the write lands and nothing about
  // what it lands on. Somebody deciding whether a rollback is safe, or whether
  // a spare slot needs repairing, is asking what is over there -- and until
  // now the only way to find out was to mount the partition by hand as root.
  + osSlotRows(u);

  // A machine whose root is not a labelled slot predates A/B entirely. There
  // is no second slot to write and no way back if the write went wrong, so
  // this is not offered at all -- reflashing is the only route and saying so
  // is more use than a button that refuses.
  if(!run){
    osmsg('This display was installed before A/B slots existed, so it cannot '
        + 'take an OS update. It has to be reflashed from a USB stick.', 'warn');
    return;
  }

  // Labelled slots are not the same as being able to CHOOSE one.
  //
  // 10.10.9.178 had ds-root-a/b partitions and no arbiter on its ESP: it boots
  // its root directly, by UUID. The update wrote the spare slot, carried the
  // software forward, and then could not select it -- ten minutes of work,
  // nothing achieved, and a spare slot left holding a system that will never
  // be booted. Offered up front now, because a button that spends ten minutes
  // discovering it cannot work is worse than no button.
  if(u.can_arm === false){
    osmsg('This display has the two slots but boots without the A/B arbiter, '
        + 'so a new system could be written and then never selected. Its boot '
        + 'path has to be migrated first \u2014 run '
        + 'sudo kiosk-migrate-boot on it \u2014 and then this will work.', 'warn');
    return;
  }
  // Something already downloaded outranks everything below. It is a question
  // the operator was asked and has not answered -- most likely because the
  // window went away -- and it is 690 MB sitting on the display either way.
  //
  // But it must not be a dead end, and it must not hide a newer OS. Both were
  // true: the only control offered was Continue, and the only way to discard a
  // staged payload was ssh. If 1.4 were published later this card would have
  // gone on saying "1.3 is already downloaded" and never mentioned it.
  if(osStagedFor){
    reRow.style.display = 'none';
    row.style.display = 'none';
    const stale = avail && osCmp(avail, osStagedFor) > 0;
    const e = document.getElementById('osmsg');
    e.className = 'msg' + (stale ? ' warn' : '');
    e.innerHTML = 'Operating system <b>' + esc(osStagedFor) + '</b> is already '
      + 'downloaded and verified on this display, waiting to be installed. '
      + 'Nothing has been written yet.'
      + (stale
          ? '<br><br><b>' + esc(avail) + ' has since been published.</b> '
            + 'Discard this one to fetch it instead \u2014 installing what is '
            + 'here would put ' + esc(osStagedFor) + ' on, not ' + esc(avail) + '.'
          : '')
      + '<div class=row>'
      + '<button type=button class=mini onclick="osResumeStaged()">'
      + (stale ? 'Install ' + esc(osStagedFor) + ' anyway&hellip;' : 'Continue&hellip;')
      + '</button>'
      + '<button type=button class=mini onclick="osDiscardStaged()">Discard it</button>'
      + '</div>';
    return;
  }

  // What pressing the button would ACTUALLY do, which is not always "update".
  //
  // The helper installs whatever the manifest names into the slot this machine
  // is not running from. Whether that is an update, a reinstall or a rollback
  // depends only on what is published -- the action is identical. Calling all
  // three "install an update" is how somebody presses it expecting a repair
  // and gets a downgrade.
  osMode = !avail ? 'none'
         : osCmp(avail, have) > 0 ? 'update'
         : osCmp(avail, have) < 0 ? 'older'
         : 'reinstall';

  if(osMode === 'update'){
    osShowGo('Hold to install ' + avail + ' \u2014 10s');
    osExplainSoftware(swBack, swNow, swNew);
    return;
  }

  // Up to date, or the published OS is older. Neither is an update and neither
  // is nothing: writing the spare slot is the only way to repair a slot that
  // will not boot, and the only way to get back onto the slot you are not
  // running. Offered, but behind its own button rather than armed by default.
  // Not when nothing is published: a reinstall button with no version behind
  // it arms a ten-second hold to install the empty string. There is no
  // recovery to offer here, only a release host that did not answer.
  if(osMode !== 'none') reRow.style.display = '';
  osmsg(osMode === 'older'
    ? 'the published operating system (' + avail + ') is older than the '
      + have + ' running here'
    : osMode === 'none'
      ? 'nothing is published for this channel'
      : 'the operating system is up to date');
}

/* The two slots and what each holds.
 *
 * Read out of `status`, which needs no network, so this is present even when a
 * check has failed. The running slot is marked, and so is the committed one --
 * they are usually the same and it is the moment they differ that matters.
 *
 * A slot that will not mount says so rather than being left blank. That is the
 * single most useful line this table can carry: it is why a machine came back
 * on the old system, and it is exactly what a reinstall repairs.
 */
function osSlotRows(u){
  const sl = u && u.slots;
  if(!sl) return '';
  const run = osSlotOf(sl.running);
  const com = osSlotOf(sl.committed);
  // "1.3 (downloaded, waiting to be installed)" -> "1.3"
  const staged = String(sl.staged || '').replace(/\s*\(.*\)\s*$/, '').trim();
  osStagedFor = (staged && staged !== 'none') ? staged : null;

  const one = name => {
    const raw = sl['slot ' + name];
    if(!raw) return '';
    const m = String(raw).match(/^os\s+(\S+)\s+software\s+(\S+)$/);
    const marks = [];
    if(name === run) marks.push('running');
    if(name === com) marks.push('kept');
    const tag = marks.length
      ? ' <span style="color:var(--dim)">&mdash; ' + marks.join(', ') + '</span>'
      : '';
    const body = m
      ? 'os ' + esc(m[1] === '0' ? '0 (unversioned)' : m[1])
        + ' <span style="color:var(--dim)">&middot;</span> ' + esc(m[2])
      // "empty or unreadable", "no such slot", "unknown (needs root to read)"
      : '<span class=warn>' + esc(raw) + '</span>';
    return `<dt>Slot ${esc(name)}</dt><dd>${body}${tag}</dd>`;
  };
  return one('a') + one('b');
}

/* Reveal the hold, named for what it will do. */
function osOfferReinstall(){
  const u = lastOsUpd || {};
  const avail = u.os_available || '';
  const have = u.os_installed || '0';
  document.getElementById('osrerow').style.display = 'none';
  const target = u.running_slot === 'a' ? 'b' : 'a';
  if(osMode === 'older'){
    osShowGo('Hold to install ' + avail + ' \u2014 10s');
    const e = document.getElementById('osmsg');
    e.className = 'msg warn';
    e.innerHTML = 'This installs <b>' + esc(avail) + '</b>, which is <b>older</b> '
      + 'than the ' + esc(have) + ' running now, into slot ' + esc(target)
      + ' and restarts into it. That is a rollback, not a repair.';
    return;
  }
  osShowGo('Hold to reinstall ' + (avail || 'this version') + ' \u2014 10s');
  const e = document.getElementById('osmsg');
  e.className = 'msg';
  e.innerHTML = 'This writes <b>' + esc(avail || 'the published version')
    + '</b> again into slot <b>' + esc(target) + '</b> '
    + '&mdash; the one this display is <b>not</b> running &mdash; and restarts '
    + 'into it. The system running now is not touched, and if the rewritten '
    + 'slot does not come up, the next start returns here. Use it to repair a '
    + 'spare slot that will not boot, or to move back onto the other slot.';
  osExplainSoftware(swBackNow(), (lastStatus && lastStatus.software_version) || '',
                    u.bundles_software || '', true);
}

/* Whether installing what is published would carry the software backwards.
 * Recomputed rather than captured, because the reinstall button is pressed
 * some time after renderOs ran and a check may have happened in between. */
function swBackNow(){
  const u = lastOsUpd || {};
  const now = u.software_installed || '';
  const nw  = u.bundles_software || '';
  return !!(now && nw &&
            osCmp(now.replace(/^b/, ''), nw.replace(/^b/, '')) > 0);
}

/* The software downgrade warning, shared by both doors.
 *
 * Not blocked: on a bench, installing an OS against the software it actually
 * ships with is a legitimate thing to want, and refusing it would remove the
 * only way to test that pairing. Said plainly instead, before the hold,
 * because an OS version going up makes it look like an upgrade in every other
 * respect.
 */
function osExplainSoftware(back, swNow, swNew, append){
  const e = document.getElementById('osmsg');
  if(!back){ if(!append){ e.className = 'msg'; e.textContent = ''; } return; }
  const line = 'This operating system carries software <b>' + esc(swNew)
    + '</b>, which is older than the <b>' + esc(swNow) + '</b> running here. '
    + 'Installing it will take this display back to ' + esc(swNew)
    + '. A software update afterwards brings it forward again.';
  e.className = 'msg warn';
  e.innerHTML = append && e.innerHTML ? e.innerHTML + '<br><br>' + line : line;
}

function osShowGo(label){
  document.getElementById('osgorow').style.display = '';
  const go = document.getElementById('osgo');
  if(go) go.disabled = false;
  if(osGoHold) osGoHold.label(label);
}

var osGoHold = holdButton({
  id: 'osgo', msg: 'osmsg',
  idle: 'Hold to install — 10s',
  run:  'Keep holding',
  done: 'Starting...',
  fire: function(){ osApply(); }
});

/* The install.
 *
 * Everything interesting here is about not lying during the gap. The helper
 * downloads most of a gigabyte, writes the other slot, arms it and reboots --
 * and the reboot kills the request this function is waiting on. A dropped
 * connection is therefore the SUCCESS path, not an error, and the code has to
 * treat it that way or it will report a failure every single time it works.
 */
/* An OS update, in three acts, because only one of them can be undone.
 *
 * Downloading writes nothing and can be called off. Installing writes a
 * filesystem and cannot -- stopping half way through mkfs is how you get a
 * slot that neither boots nor announces that it will not. Splitting them is
 * what lets the page ask, after 690 MB has landed and before anything is
 * written, whether the person still means it.
 *
 * The work runs on the display, not in this browser. Closing the window does
 * not stop it, and the modal says so, because the alternative is somebody
 * closing a tab to abort an update and finding out later that it went ahead.
 */
async function osApply(){
  const u = lastOsUpd || {};
  osStagedFor = null;

  osmOpen(osMode === 'reinstall' ? 'Reinstalling the operating system'
        : osMode === 'older'     ? 'Installing an older operating system'
        : 'Updating the operating system');
  osmPhase('Starting', 'Nothing on this display has changed yet.');
  osmRow('cancel');

  const beat = osWatchProgress();
  let d = null;
  try{
    const r = await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'download'})});
    d = await r.json();
    beat.stop();
    if(!r.ok){
      osmDone('The download did not finish',
        'Nothing was written and this display is unchanged.<br><br>'
        + '<span style="color:var(--dim)">' + esc(d.detail || 'no reason given')
        + '</span>', 'warn');
      return;
    }
  }catch(e){
    // The download runs on the display. A dropped connection here means this
    // page lost the display, not that the download stopped -- so it is not
    // reported as a failure, and the machine is left to get on with it.
    beat.stop();
    osmDone('Lost contact with the display',
      'The download may still be running on the display itself &mdash; it does '
      + 'not stop when this page goes away. Nothing has been written to any '
      + 'slot. Check again in a few minutes.', 'warn');
    return;
  }

  if(d.result && d.result.cancelled){
    osmDone('Cancelled',
      'The download was stopped and what had arrived was discarded. Nothing '
      + 'was written to any slot and this display is exactly as it was.');
    return;
  }

  // Downloaded, verified, and nothing touched yet. This is the last moment at
  // which stopping is free, so it is the one place the page asks.
  osStagedFor = u.os_available || '';
  osmHideBar();
  osmPhase('Ready to install',
    'Operating system <b>' + esc(osStagedFor || 'the published version')
    + '</b> has been downloaded to this display and its signature and checksum '
    + 'both check out. <b>Nothing has been written yet</b> and cancelling now '
    + 'costs nothing.<br><br>'
    + 'Installing writes slot <b>'
    + esc(u.running_slot === 'a' ? 'b' : 'a') + '</b> and restarts the display. '
    + 'From the moment you press it there is no way to stop it part way.');
  osmRow('go');
}

/* Poll the phase the display writes down, and drive the bar from it.
 *
 * Returned as an object with stop() rather than a bare interval id, because
 * clearInterval cancels the schedule and not a callback already awaiting a
 * fetch -- a reply landing a moment late would repaint the modal over
 * whatever came next. The flag is what actually stops it. */
function osWatchProgress(){
  let watching = true, seen = '';
  const id = setInterval(async () => {
    let d;
    try{
      const r = await fetch('/api/osupdate/progress', {cache:'no-store'});
      if(!r.ok) return;
      d = await r.json();
    }catch(e){ return; }
    if(!watching || !d.phase) return;
    const parts = d.phase.split(/\s+/);
    const key = parts[0];
    if(key === 'downloading' && parts.length >= 3){
      const got = Number(parts[1]), total = Number(parts[2]);
      if(isFinite(got) && isFinite(total) && total > 0){
        osmPhase(OS_PHASES.downloading, OS_PHASE_NOTE.downloading);
        osmProgress(got / total,
          osmBytes(got) + ' of ' + osmBytes(total)
          + '  \u00b7  ' + Math.floor((got / total) * 100) + '%');
        seen = d.phase;
        return;
      }
    }
    if(d.phase === seen) return;
    seen = d.phase;
    osmHideBar();
    osmPhase(OS_PHASES[key] || d.phase, OS_PHASE_NOTE[key] || '');
  }, 2000);
  return {stop: function(){ watching = false; clearInterval(id); }};
}

/* Reopen the decision that a closed window left behind.
 *
 * The gap this fills: the question "do you want to install it" lived only in a
 * browser tab, and tabs get closed. Without this the payload stayed on disk
 * with nothing able to find it -- 690 MB in /var/tmp, and an operator who
 * would have to download it all again to get back to the question. */
function osResumeStaged(){
  const u = lastOsUpd || {};
  osmOpen('Installing the operating system');
  osmHideBar();
  osmPhase('Ready to install',
    'Operating system <b>' + esc(osStagedFor || '') + '</b> was already '
    + 'downloaded to this display and its signature and checksum both check '
    + 'out. <b>Nothing has been written yet</b> and cancelling now costs '
    + 'nothing.<br><br>'
    + 'Installing writes slot <b>'
    + esc(u.running_slot === 'a' ? 'b' : 'a') + '</b> and restarts the display. '
    + 'From the moment you press it there is no way to stop it part way.');
  osmRow('go');
}

/* Discard a staged payload from the page.
 *
 * The only way to do this used to be ssh and `kiosk-osupdate cancel`. A display
 * on a counter with 690 MB it will never install, and a card that offers only
 * "Continue", is a state somebody is stuck in rather than a decision they are
 * being asked to make. */
async function osDiscardStaged(){
  osmOpen('Discarding the downloaded update');
  osmPhase('Discarding', 'Removing what was downloaded. Nothing was written to '
    + 'any slot, so this display is unaffected.');
  osmRow(null);
  let detail = '';
  try{
    const r = await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'cancel'})});
    const d = await r.json();
    if(!r.ok) detail = d.detail || 'it could not be discarded';
  }catch(e){ detail = String(e); }
  osStagedFor = null;
  if(detail){
    osmDone('It was not discarded', esc(detail), 'warn');
    return;
  }
  osmDone('Discarded',
    'The downloaded system was removed and the space reclaimed. Nothing was '
    + 'written to any slot.');
}

async function osCancelDownload(){
  osmRow(null);
  osmPhase('Cancelling', 'Stopping the download and discarding what arrived.');
  try{
    await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'cancel'})});
  }catch(e){ /* the display may already have stopped; the report is the same */ }
  osStagedFor = null;
  osmHideBar();
  osmDone('Cancelled',
    'Nothing was written to any slot and this display is exactly as it was.');
}

/* The point of no return, and the only thing on this page that deserves the
 * name. Everything before it can be undone by walking away. */
async function osProceed(){
  const u = lastOsUpd || {};
  const wasOs = (lastStatus && lastStatus.os_version) || '';
  osmRow(null);
  osmPhase('Installing', 'Writing the spare slot. This cannot be stopped now.');

  const beat = osWatchProgress();
  let failed = null;
  try{
    const r = await fetch('/api/osupdate', {method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'install'})});
    const d = await r.json();
    if(!r.ok) failed = d.detail || 'no reason given';
  }catch(e){
    // Expected: the machine rebooted out from under the request.
  }
  beat.stop();

  if(failed){
    osmDone('The update did not install',
      'The display is unchanged and still running its current system.<br><br>'
      + '<span style="color:var(--dim)">' + esc(failed) + '</span>', 'warn');
    return;
  }

  osmPhase('The display is rebooting',
    'It is starting into slot ' + ((u.running_slot === 'a') ? 'b' : 'a')
    + ', which has just been written. If that slot does not come up, the '
    + 'display returns to the one it was running before, by itself, at the '
    + 'next boot.');

  const WAIT = 600000;
  const cd = osmCountdown(WAIT / 1000);
  const res = await pollForReturn({
    deadline: WAIT, field: 'os_version', was: wasOs,
    tick: function(alive, left){
      cd.set(left);
      if(!alive){
        osmPhase('Waiting for the display to come back',
          'It has stopped answering, which is what a reboot looks like from '
          + 'here. This page keeps checking on its own.');
      }
    }
  });
  cd.stop();

  if(!res.ok){
    osmDone('Not able to reconnect',
      'This page has not been able to reach the display for ten minutes.<br><br>'
      + '<b>This does not mean the update failed.</b> Please check the display '
      + 'itself: if it has come back up, it may be on a different address, or '
      + 'this page may simply have lost it. If the screen is dark or stuck, the '
      + 'device is designed to return to its previous operating system on the '
      + 'next boot &mdash; power it off and on once, and check again.<br><br>'
      + 'If it does not come back after that, it needs reflashing from a USB '
      + 'stick.', 'warn');
    return;
  }
  if(res.unchanged){
    if(osMode === 'reinstall'){
      osmDone('Reinstalled',
        'The display is back on operating system ' + esc(res.value || '')
        + '. Confirming which slot it kept&hellip;');
      osConfirmCommit();
      return;
    }
    osmDone('It came back on the same operating system',
      'The display is answering again and is still on ' + esc(res.value || 'the '
      + 'version it had') + '. The new slot did not come up, so it returned to '
      + 'this one by itself &mdash; which is the design working, not a fault. '
      + 'Nothing was lost.', 'warn');
    return;
  }
  osmDone(osMode === 'older' ? 'Rolled back' : 'Updated',
    'The display is back and running operating system ' + esc(res.value || '')
    + '. Confirming it has kept the new slot&hellip;');
  osConfirmCommit();
}


/* The one silent failure this design has.
 *
 * `apply` arms ONE attempt. If nothing commits the slot, the next boot returns
 * to the old one -- which is the rollback, and is exactly right when the new
 * system is broken. But it means a SUCCESSFUL update quietly undoes itself
 * unless the commit actually happens. kiosk.py does it 120 seconds after the
 * browser is up, so between the display answering again and that commit
 * landing there is a window where everything looks finished and is not.
 *
 * Reporting "Updated" and stopping would be reporting the update at exactly
 * the moment it is least certain. So the modal keeps looking until running
 * and committed agree, and says which it saw. `status` is used rather than
 * `check` because it needs no network: a display that came back but cannot
 * reach the release host still has to be able to answer this.
 */
async function osConfirmCommit(){
  const note = document.getElementById('osmnote');
  const end = Date.now() + 300000;              // grace is 120s; allow for slow
  let last = null;
  while(Date.now() < end){
    await new Promise(r => setTimeout(r, 10000));
    try{
      const r = await fetch('/api/osupdate', {method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({action:'status'})});
      if(!r.ok) continue;
      const d = await r.json();
      const st = d.result || {};
      // Both sides through the SAME normaliser -- see osSlotOf.
      const run = osSlotOf(st.running);
      const com = osSlotOf(st.committed) === '<unset>' ? '' : osSlotOf(st.committed);
      last = {run: run, com: com, os: st.os || ''};
      if(run && com && run === com){
        note.innerHTML =
          'The display is back on operating system ' + esc(last.os)
          + ' and has kept slot ' + esc(run) + '. It is finished, and this '
          + 'slot is the one it will start from from now on.';
        return;
      }
    }catch(e){ /* it may still be settling; keep looking */ }
  }
  // Came back, did not commit. Said plainly, because the next reboot -- which
  // may be a power cut weeks from now -- silently returns to the old system,
  // and somebody should know that before it happens rather than after.
  note.innerHTML =
    'The display is back and running the new system, but it has <b>not</b> '
    + 'committed to it' + (last && last.run && last.com
        ? ' (running slot ' + esc(last.run) + ', committed slot '
          + esc(last.com) + ')' : '')
    + '. That means the next restart will return it to the slot it was running '
    + 'before. If the display is working, run '
    + '<code>sudo kiosk-osupdate commit</code> on it to keep this one.';
  const m = document.getElementById('osmmsg');
  if(m){ m.className = 'msg warn'; }
}

/* The helper's own words, said in the page's voice. Anything not in this map
 * falls through and is shown verbatim, so a phase added to the helper later
 * still appears rather than disappearing. */
const OS_PHASES = {
  'downloading':  'Downloading the operating system',
  'verifying':    'Checking the download',
  'formatting':   'Preparing the spare slot',
  'unpacking':    'Writing the new system',
  'arming':       'Setting it to be tried at the next start',
  'rebooting':    'Restarting the display'
};
const OS_PHASE_NOTE = {
  'downloading':  'Straight to this display. Nothing has been changed yet, and '
                + 'stopping here would leave it exactly as it is.',
  'verifying':    'Checking the signature and the checksum before anything is '
                + 'written.',
  'formatting':   'Erasing the spare slot. The slot running now is untouched.',
  'unpacking':    'Writing to the slot this display is <b>not</b> running from. '
                + 'The system running now is not being touched.',
  'arming':       'One attempt only. If it does not come up, the next start '
                + 'returns to the system running now.',
  'rebooting':    'The connection to this page is about to drop. That is '
                + 'expected.'
};

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

/* The ssh password's confirm box, same rules as the admin one.
 *
 * Added after the admin one because the ssh password has the sharper edge:
 * setting it wrong locks somebody out of the shell of a machine they may not
 * be able to reach, and there is no "type it again" anywhere else to catch a
 * typo before it becomes a site visit. */
function sshPwMatch(){
  const a = document.getElementById('sshpw');
  const b = document.getElementById('sshpw2');
  const el = document.getElementById('sshpwmatch');
  if(!a || !b || !el) return true;
  if(!b.value){ el.textContent = ''; return a.value === b.value; }
  if(a.value === b.value){
    el.textContent = 'Both match.';
    el.style.color = 'var(--ok, #7ddc9a)';
    return true;
  }
  if(a.value.startsWith(b.value)){
    el.textContent = 'Keep going\u2026';
    el.style.color = 'var(--dim)';
    return false;
  }
  el.textContent = 'These do not match yet.';
  el.style.color = 'var(--warn, #ffb454)';
  return false;
}

/* Say it while they are typing, not after they press Apply.
 *
 * Deliberately quiet until the second field has something in it: flagging a
 * mismatch against an empty box would mean the warning is on screen the whole
 * time somebody is typing the first character, which trains people to ignore
 * it. And a bare "don't match" while the second field is still shorter than
 * the first is noise, so that case says "keep going" instead. */
function pwCheckMatch(){
  const a = document.getElementById('adminpw').value;
  const b = document.getElementById('adminpw2').value;
  const el = document.getElementById('pwmatch');
  if(!el) return true;
  if(!b){ el.textContent = ''; el.className = ''; return a === b; }
  if(a === b){
    el.textContent = 'Both match.';
    el.style.color = 'var(--ok, #7ddc9a)';
    return true;
  }
  if(a.startsWith(b)){
    el.textContent = 'Keep going\u2026';
    el.style.color = 'var(--dim)';
    return false;
  }
  el.textContent = 'These do not match yet.';
  el.style.color = '#ff9a9a';
  return false;
}

async function setAdminPw(){
  const pw = document.getElementById('adminpw').value;
  const pw2 = document.getElementById('adminpw2').value;
  const m = document.getElementById('pwmsg');
  // Checked here as well as on the server. The server is what actually
  // enforces it; this is so the answer is instant and the field can be
  // pointed at.
  if (pw !== pw2){
    m.className='msg warn';
    m.textContent = 'The two passwords do not match. Nothing was changed.';
    document.getElementById('adminpw2').focus();
    return;
  }
  try {
    const idleEl = document.getElementById('adminidle');
    const idle = Math.max(0, parseInt(idleEl.value || '15', 10) || 0);
    const mail = document.getElementById('recoveryemail').value.trim();
    await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({admin_idle_minutes: idle, recovery_email: mail})});
    const r = await fetch('/api/admin-password',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({password: pw, confirm: pw2})});
    const d = await r.json();
    if (!d.ok){
      m.className='msg warn';
      m.textContent = d.detail || 'Could not change the password.';
      return;
    }
    document.getElementById('adminpw').value = '';
    document.getElementById('adminpw2').value = '';
    pwCheckMatch();
    m.className = 'msg';
    m.textContent = d.protected
      ? 'Password set. You are still signed in here; everywhere else has been '
        + 'signed out.'
      : 'Password cleared. The admin page is open to the LAN again.';
    idleTimer.reset();
    refresh();
  } catch(e){ m.className='msg warn'; m.textContent = 'Failed: '+e; }
}

async function signOut(){
  try { await fetch('/api/logout',{method:'POST'}); } catch(e){}
  location.replace('/login');
}

/* Signing out on idle.
 *
 * The server has the last word -- a session expires there whether or not any
 * page is open. This is what makes it visible: without it the page sits
 * looking signed in, and the first thing you press after walking away is the
 * thing that fails.
 *
 * Idle means no human, not no traffic. This page polls itself every five
 * seconds, so anything counting requests would never see an idle moment. */
const idleTimer = (function(){
  let minutes = 0, at = Date.now(), timer = null;
  function tick(){
    if (!minutes) return;
    if (Date.now() - at >= minutes * 60000) { signOut(); return; }
  }
  function reset(){ at = Date.now(); }
  ['mousedown','keydown','touchstart','wheel','pointerdown'].forEach(ev =>
    window.addEventListener(ev, reset, {passive:true, capture:true}));
  return {
    reset,
    set(mins, on){
      minutes = on ? (mins || 0) : 0;
      if (timer) { clearInterval(timer); timer = null; }
      if (minutes) { at = Date.now(); timer = setInterval(tick, 5000); }
    }
  };
})();

/* Any request that comes back 401 means the session went away -- it timed
 * out, or somebody changed the password elsewhere. Wrapping fetch once
 * catches every call site on this page, including the ones added later. */
(function(){
  const real = window.fetch.bind(window);
  window.fetch = async function(...a){
    const r = await real(...a);
    if (r.status === 401 && location.pathname !== '/login') {
      location.replace('/login');
    }
    return r;
  };
})();


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
  if(d.enabled === false){
    info.innerHTML =
      '<dt>Printing</dt><dd><span class=warn>switched off on this display</span>'
      + '</dd><dt>Queues</dt><dd>kept, but nothing is running to use them</dd>';
    const on0 = document.getElementById('printingon');
    if(on0 && !on0.disabled) on0.checked = false;
    for(const id of ['printer','printwidth','managebtn']){
      const el = document.getElementById(id); if(el) el.disabled = true;
    }
    return;
  }
  info.innerHTML =
    `<dt>CUPS</dt><dd>${d.cups ? '<span class=ok>installed</span>'
                              : '<span class=warn>not installed</span>'}</dd>` +

    `<dt>Queues</dt><dd>${d.queues.length
        ? d.queues.map(q => esc((d.models||{})[q] || q)).join(', ')
        : 'none configured'}</dd>` +
    `<dt>Default</dt><dd>${d.default
        ? esc((d.models||{})[d.default] || d.default) : '-'}</dd>` +
    (s.last_print ? `<dt>Last print</dt><dd>${s.last_print}</dd>` : '');
  const on = d.enabled !== false;
  const cb = document.getElementById('printingon');
  if(cb && !cb.disabled) cb.checked = on;
  // Everything below the switch is meaningless with the server stopped.
  for(const id of ['printer','printwidth','managebtn']){
    const el = document.getElementById(id);
    if(el) el.disabled = !on;
  }
  const mb = document.getElementById('managebtn');
  if(mb) mb.disabled = !on;
  // Say what is true NOW. This used to tell you to press "Add TM-T88V" even
  // after the queue existed, which reads as though nothing had happened.
  const note = document.getElementById('printnote');
  if(note){
    // One line of status, and the rest behind a disclosure. The card is read
    // when something needs doing, not when somebody wants to learn how
    // printing works, and four paragraphs of that in the way is four
    // paragraphs to read past every time.
    // Nothing to say when there are printers: the Default row above already
    // says which one. Only the empty case is worth a sentence, because
    // "none configured" does not tell anybody what to do about it.
    note.innerHTML = d.queues.length ? ''
      : '<b>Nothing is set up to print yet</b> &mdash; open '
        + '<b>Manage printers</b> to add one.';
  }
  const sel = document.getElementById('printer');
  const bak = document.getElementById('printerbackup');
  if(sel && !dirty){
    const want = s.printer || '';
    const wantB = s.printer_backup || '';
    sel.innerHTML = '<option value="">system default</option>';
    if(bak) bak.innerHTML = '<option value="">none</option>';
    // The option's VALUE stays the queue name -- that is what the config
    // stores and what the CLI takes. Only the label leads with the printer,
    // because "Thermal" does not tell anybody which machine they are picking.
    const label = q => (d.models||{})[q] || q;
    for(const q of d.queues){
      const o = document.createElement('option');
      o.value = q; o.textContent = label(q); sel.appendChild(o);
      if(bak){
        const o2 = document.createElement('option');
        o2.value = q; o2.textContent = label(q); bak.appendChild(o2);
      }
    }
    sel.value = want;
    if(bak) bak.value = wantB;
    const w = document.getElementById('printwidth');
    if(w && !w.value) w.value = 80;
  }
  // Say what will happen rather than making somebody reason about it. A
  // backup that is the same queue as the primary is not a backup.
  const bnote = document.getElementById('backupnote');
  if(bnote){
    const b = s.printer_backup || '';
    const pmain = s.printer || d.default || '';
    if(!b) bnote.textContent = '';
    else if(b === pmain)
      bnote.innerHTML = '<span class=warn>The backup is the same printer as '
        + 'the primary, so there is nothing to fall back to.</span>';
    else {
      const nm = q => esc((d.models||{})[q] || q);
      bnote.innerHTML = 'If <b>' + (pmain ? nm(pmain) : 'the default printer')
        + '</b> is not ready, receipts go to <b>' + nm(b) + '</b> instead.';
    }
  }
}

async function togglePrinting(cb){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent = cb.checked ? 'Starting the print server...'
                                                : 'Stopping the print server...';
  cb.disabled = true;
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action: cb.checked ? 'enable' : 'disable'})});
    const d = await r.json();
    m.className='msg'+(d.ok?'':' warn'); m.textContent=d.detail||'done';
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
  cb.disabled = false;
  refreshPrinter();
}

/* The printer modal.
 *
 * "Add printer" used to be one button that took the first USB device it found
 * and gave you no way back if it took the wrong one -- and no way at all to
 * attach anything on the network. Adding a printer, removing one added by
 * mistake, and choosing which is the default are the same job, so they live
 * in one place. */
function pmMsg(t, warn){
  const m = document.getElementById('pmmsg');
  m.className = 'msg' + (warn ? ' warn' : ''); m.textContent = t || '';
}

function openPrinters(){
  document.getElementById('pmodal').style.display = '';
  pmMsg('');
  pmCurrent();
  pmScan();
}
function closePrinters(){
  document.getElementById('pmodal').style.display = 'none';
  refreshPrinter();
  // ...and again, twice, a moment later.
  //
  // cupsd writes printers.conf lazily, so a queue added seconds ago is not
  // always in `lpstat` yet. This closed the modal, read once, found nothing,
  // and left a card that said there were no printers -- of a display that had
  // just been given one. Reported from the field: "it didn't show up as one of
  // the printers until I refreshed the page".
  //
  // Re-reading twice is the honest fix for something eventually consistent.
  // Polling until it appears would be worse: there is no promise a printer was
  // added at all, so a loop waiting for one would spin whenever somebody
  // opened the modal and closed it again.
  setTimeout(refreshPrinter, 1200);
  setTimeout(refreshPrinter, 3500);
}
/* Escape closes it. A modal with only one way out is a modal people get
 * stuck in on a touchscreen with no keyboard visible. */
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' &&
      document.getElementById('pmodal').style.display !== 'none') {
    closePrinters();
  }
});

async function pmCurrent(){
  const box = document.getElementById('pmcurrent');
  let d; try { d = await (await fetch('/api/printer')).json(); }
  catch(e){ box.innerHTML = '<div class="msg warn">Could not read the queues</div>'; return; }
  if(!d.queues || !d.queues.length){
    box.innerHTML = '<div class=msg>Nothing set up yet.</div>'; return;
  }
  const widths = d.widths || {};
  const problems = d.problems || {};
  // What is actually plugged into each queue, when the search has run. A
  // queue called "Thermal" is a slot, not a printer; saying which printer is
  // in it is the difference between reading the list and interpreting it.
  // When the printer is gone the queue falls back to its own name, which is
  // the truth at that moment.
  const models = d.models || {};
  box.innerHTML = d.queues.map(q => {
    const isDef = q === d.default;
    const w = widths[q] || 576;
    // Width belongs to the printer, not the display. Two printers on one
    // display can want different numbers, and before this the second one
    // printed short with nothing to say why.
    const opts = [384, 512, 576, 640].concat(
        [384,512,576,640].includes(w) ? [] : [w])
      .sort((a,b) => a-b)
      .map(v => '<option value="' + v + '"' + (v === w ? ' selected' : '') + '>'
                + v + ' dots &middot; ' + Math.round(v * 25.4 / 203) + ' mm'
                + '</option>').join('');
    return '<div style="padding:.55rem 0;border-bottom:1px solid var(--line)">'
      + '<div style="display:flex;align-items:center;gap:.6rem">'
      + '<div style="flex:1"><b>' + esc(models[q] || q) + '</b>'
      + (models[q] ? ' <span style="color:var(--dim)">as ' + esc(q) + '</span>'
                   : '')
      + (isDef ? ' <span class=ok>&middot; default</span>' : '') + '</div>'
      + (isDef ? '' : '<button type=button class=small data-pmdef="' + esc(q)
                      + '">Make default</button>')
      + '<button type=button class=small data-pmdel="' + esc(q)
      + '">Remove</button></div>'
      + '<div style="display:flex;align-items:center;gap:.6rem;margin-top:.4rem">'
      + '<label style="margin:0;flex:0 0 auto">Prints</label>'
      + '<select data-pmw="' + esc(q) + '" style="flex:1 1 auto;padding:.35rem .5rem;'
      + 'background:#111316;border:1px solid var(--line);border-radius:6px;'
      + 'color:var(--fg);font:inherit;font-size:.85rem">' + opts + '</select>'
      + '<button type=button class=small data-pmtest="' + esc(q)
      + '">Test</button>'
      + '<button type=button class=small data-pmruler="' + esc(q)
      + '">Ruler</button></div></div>';
  }).join('');
  box.querySelectorAll('[data-pmdef]').forEach(b =>
    b.addEventListener('click', () => pmAct('default', b.dataset.pmdef)));
  box.querySelectorAll('[data-pmdel]').forEach(b =>
    b.addEventListener('click', () => pmRemove(b.dataset.pmdel)));
  box.querySelectorAll('[data-pmruler]').forEach(b =>
    b.addEventListener('click', () => pmAct('ruler', b.dataset.pmruler)));
  box.querySelectorAll('[data-pmtest]').forEach(b =>
    b.addEventListener('click', () => pmAct('test', b.dataset.pmtest)));
  box.querySelectorAll('[data-pmw]').forEach(sel =>
    sel.addEventListener('change', () => pmWidth(sel.dataset.pmw, sel.value)));
}

/* Removing a queue is not destructive to anything but the queue, and it is
 * the fix for the mistake this modal exists to allow -- so it asks, once,
 * inline, rather than throwing a confirm() that a touchscreen user dismisses
 * without reading. */
function pmRemove(name){
  pmMsg('');
  const box = document.getElementById('pmcurrent');
  const bar = document.createElement('div');
  bar.className = 'msg warn';
  bar.innerHTML = 'Remove <b>' + esc(name) + '</b>? '
    + '<button type=button class=small id=pmyes>Remove it</button> '
    + '<button type=button class=small id=pmno>Keep it</button>';
  box.appendChild(bar);
  document.getElementById('pmno').addEventListener('click', () => pmCurrent());
  document.getElementById('pmyes').addEventListener('click',
    () => pmAct('remove', name));
}

async function pmWidth(queue, dots){
  pmMsg('Setting the width for ' + queue + '...');
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'width', name:queue, dots:String(dots)})});
    const d = await r.json();
    pmMsg((d.detail || 'done')
          + (d.ok ? '  Print a ruler to check the printer agrees.' : ''), !d.ok);
  }catch(e){ pmMsg('Failed: ' + e, true); }
  pmCurrent();
}

async function pmAct(action, name, uri, service){
  pmMsg(action === 'remove' ? 'Removing...' : 'Working...');
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action, name, uri, service})});
    const d = await r.json();
    pmMsg(d.detail || 'done', !d.ok);
  }catch(e){ pmMsg('Failed: ' + e, true); }
  pmCurrent();
}

async function pmScan(){
  const box = document.getElementById('pmfound');
  const btn = document.getElementById('pmscanbtn');
  btn.disabled = true;
  box.innerHTML = '<div class=msg>Searching&hellip;</div>';
  let d;
  try { d = await (await fetch('/api/printer/devices')).json(); }
  catch(e){ box.innerHTML = '<div class="msg warn">Search failed</div>';
            btn.disabled = false; return; }
  btn.disabled = false;
  if(!d.ok){ box.innerHTML = '<div class="msg warn">' + esc(d.detail || 'Search failed')
                             + '</div>'; return; }
  if(!d.devices.length){
    box.innerHTML = '<div class=msg>Nothing new found. Printers already set up '
      + 'are listed above, not here. A printer that is missing needs to be '
      + 'switched on and plugged in, or on this network &mdash; or you can add '
      + 'one by address below.</div>';
    return;
  }
  box.innerHTML = d.devices.map((dev, i) =>
    '<div style="display:flex;align-items:center;gap:.6rem;padding:.55rem 0;'
    + 'border-bottom:1px solid var(--line)">'
    + '<div style="flex:1;min-width:0">'
    + '<b>' + esc(dev.name) + '</b> <span class=ok>' + esc(dev.kind) + '</span>'
    // The address is the useful half for anything on the network -- a
    // service name like "Canon G600._ipp._tcp.local" is not something you
    // can ping, type elsewhere, or read out to somebody.
    + (dev.address ? ' <b style="color:var(--acc)">' + esc(dev.address) + '</b>'
                   : (dev.kind === 'network'
                      ? ' <span class=warn>address not resolving</span>' : ''))
    + (dev.protocol ? ' <span style="color:var(--dim)">' + esc(dev.protocol)
                      + '</span>' : '')
    // What will actually be used, not the advert. They differ for a
    // raw-socket printer, and the difference is the whole bug.
    + '<div style="color:var(--dim);font-size:.75rem;overflow:hidden;'
    + 'text-overflow:ellipsis">' + esc(dev.add_uri || dev.pretty) + '</div></div>'
    + '<button type=button class=small data-pmadd="' + i + '"'
    + (dev.add_action ? '' : ' disabled title="cannot be added automatically"')
    + '>Add</button></div>'
  ).join('');
  box.querySelectorAll('[data-pmadd]').forEach(b =>
    b.addEventListener('click', () => pmAddFound(d.devices[+b.dataset.pmadd])));
}

function pmSuggestName(dev){
  // A CUPS queue name cannot hold spaces, / or #, and the server rejects
  // anything outside [A-Za-z0-9_.-]. Make one rather than making the person
  // discover the rule by being refused.
  let n = (dev.name || 'Printer').replace(/[^A-Za-z0-9_.-]+/g, '_')
                                 .replace(/^_+|_+$/g, '');
  return n.slice(0, 40) || 'Printer';
}

async function pmAddFound(dev){
  // The server works out how to attach each device, because it is the side
  // that knows what the printer said it speaks and what address it resolved
  // to. Deciding here from the URI scheme is what sent a raw-socket printer
  // to `lpadmin -m everywhere` and failed.
  if(!dev.add_action || !dev.add_uri){
    pmMsg('This display can see ' + dev.name + ' but cannot work out how to '
          + 'talk to it. Add it by address below.', true);
    return;
  }
  // Pass the advert as well as the address. The queue points at the address,
  // because .local does not resolve here; the advert is how it is found again
  // if DHCP moves the printer.
  await pmAct(dev.add_action, pmSuggestName(dev), dev.add_uri,
              dev.uri.startsWith('dnssd://') ? dev.uri : '');
  pmScan();
}

async function pmAddManual(){
  const host = document.getElementById('pmhost').value.trim();
  const kind = document.getElementById('pmkind').value;
  let name = document.getElementById('pmname').value.trim();
  if(!host){ pmMsg('Type the printer\'s address first.', true); return; }
  if(!name) name = kind === 'thermal' ? 'Receipt' : 'Printer';
  if(!/^[A-Za-z0-9_.-]{1,63}$/.test(name)){
    pmMsg('That name can only use letters, digits, dot, dash and underscore.',
          true);
    return;
  }
  // A bare host is what people type. Turn it into the URI the printer wants:
  // 9100 for a thermal printer, the standard IPP path for anything else.
  let uri = host;
  if(!/:\/\//.test(host)){
    uri = kind === 'thermal'
        ? 'socket://' + host + (host.includes(':') ? '' : ':9100')
        : 'ipp://' + host + '/ipp/print';
  }
  await pmAct(kind === 'thermal' ? 'add-thermal' : 'add-ipp', name, uri);
  document.getElementById('pmhost').value = '';
  document.getElementById('pmname').value = '';
}


async function savePrint(){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent='Saving...';
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({
        printer: document.getElementById('printer').value,
        printer_backup: document.getElementById('printerbackup').value,
        print_width_mm: parseInt(document.getElementById('printwidth').value||80),
        print_margins_mm: 0, print_enabled: true})});
    await r.json(); m.textContent='Saved'; dirty=false;
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
  refreshPrinter();
}

/* "Test print" on the card asks whether a RECEIPT would come out, so it names
 * no queue and lets the client choose -- primary, or the backup if the primary
 * is not ready.
 *
 * The ruler used to sit beside it and does not any more. It measures ONE
 * printer's printable width, and a button that may quietly fall back to a
 * different printer is the wrong place to measure from -- the owner asked for
 * it to move before it caused a width to be set from the wrong machine. It
 * lives in Manage printers, per queue, where the printer is named. */
async function printerTest(){
  const m = document.getElementById('printmsg');
  m.className='msg'; m.textContent='Printing...';
  try{
    const r = await fetch('/api/printer',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'test'})});
    const d = await r.json();
    m.className='msg'+(d.ok?'':' warn'); m.textContent=d.detail||'done';
  }catch(e){ m.className='msg warn'; m.textContent='Failed: '+e; }
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
  renderCached(s);
  renderNetwatch(s);
  const np = document.getElementById('netphase');
  if(np && s.offline)
    np.innerHTML += '<dt>Offline</dt><dd><span class=warn>yes &mdash; red edge '
                  + 'is showing</span></dd>';
}

/* Ten seconds, like the other holds. holdButton's duration is fixed at ten
   and takes no option, and a button labelled 5s that counts to 10 is the exact
   defect this file keeps being audited for. */
var scHold = holdButton({
  id: 'scgo', msg: 'scmsg',
  idle: 'Hold to install \u2014 10s',
  run:  'Keep holding',
  done: 'Installing\u2026',
  fill: '#4c8dff', track: '#1d2a3a',
  fire: scInstall
});
scRefresh();
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
 /* A footer, not a card: one quiet line across the bottom saying which
    machine this is, where to reach it and what it runs. */
 .ident{margin:1.4rem 0 .2rem;color:var(--dim);text-align:center;
   font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
   font-size:.8rem;line-height:1.5;opacity:.75}

.pwrow{display:flex;gap:.5rem;align-items:stretch;margin:.15rem 0 .2rem}
.pwrow input{flex:1 1 auto;min-width:0}
.pwrow button{flex:0 0 auto;padding:.5rem 1rem;font-size:.9rem}
 .hide{display:none}
 /* belt and braces: each list scrolls on its own too, so neither can push
    the buttons below it off the bottom of the screen */
 #scanlist,#saved{max-height:46vh;overflow-y:auto}
 .scrollnote{color:var(--dim);font-size:.85rem;margin-top:.4rem}
 /* Above the status, not beside it: the status line changes constantly and
    the name does not, so they should not share a row and compete. The mark
    sits to the LEFT of the name, which is the order it is read in. */
 .brandbar{display:flex;align-items:center;gap:.65rem;
           font-size:2.1rem;font-weight:700;letter-spacing:.2px;
           margin:0 0 1.2rem 0;line-height:1}
 .bmark{width:2.4rem;height:2.4rem;display:block}
 /* Smaller than the name above it. The name is what this display IS; the
    status is what it happens to be doing this second. */
 .head h1{font-size:1.5rem}
</style></head><body><div class=wrap>

<!-- The name at the top, above the status (owner, 2026-09-02).
     This is the first screen a display ever shows and, when a network goes
     away, the only one anybody sees. It should say what it is before it says
     what is wrong with it. The wordmark was previously only a watermark
     behind the footer, which is decoration rather than identification. -->
<div class=brandbar><!--BRANDMARK--><span><!--BRAND--></span></div>

<div class=head>
  <span class=dot id=dot></span>
  <div><h1 id=title>Starting up</h1><div class=sub id=sub>&nbsp;</div></div>
</div>

<!-- No "change this device" card here. This screen exists to get a network
     up, nothing else; once there is one the machine shows the welcome page,
     which carries the setup address. Two places telling you the same thing
     means one of them is wrong the day it changes. -->

<!-- The countdown, and the only message this feature ever puts on a screen.
     It belongs HERE and not on the saved page: the saved page is a menu
     board and carries nothing, but the screen it is about to replace is a
     status screen already, and something that is about to take the display
     over should say so before it does. -->
<div class=card id=cachedcard style="display:none;border-color:var(--acc)">
  <div id=cachedmsg style="font-size:1.05rem"></div>
  <div class=row id=cachedstop style="display:none">
    <button onclick="keepScreen()" id=cachedstopbtn>Stay on this screen</button>
  </div>
</div>

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

<div class=card id=noinput style="display:none">
  <h2>Display only</h2>
  <div style="color:var(--dim)" id=noinputtext>
    No touchscreen, keyboard or mouse is connected, so the controls are
    hidden. Plug one in and they appear, or configure this device from the
    admin page on another machine on the network.
  </div>
</div>

<div class=ident id=ident>&nbsp;</div>

<div class=card id=pwcard style="display:none">
  <h2 id=pwtitle>Password</h2>
  <div class=pwrow>
    <input type=password id=pw autocomplete="off" autocapitalize="off"
           spellcheck="false" placeholder="touch here to type">
    <button type=button id=pwshow onmousedown="event.preventDefault()"
      onclick="pwToggle2()">Show</button>
  </div>
  <div class=row>
    <button class=primary onclick="joinNow()">Connect</button>
    <button onclick="cancelPw()">Cancel</button>
  </div>
</div>

</div>
<script>
let scanning = false, pwSsid = null;

/* The saved-page countdown.
   The client owns the clock -- it is the thing that will actually do the
   swap -- and this only draws it, ticking locally between polls so the
   number moves once a second instead of once every four. Every poll
   resyncs, so drift cannot accumulate into a countdown that says 12 while
   the screen is already changing. */
var cachedLeft = null, cachedTick = null, scanned = false, lastDefer = 0;

function drawCountdown(){
  var card = document.getElementById('cachedcard');
  var box = document.getElementById('cachedmsg');
  if(cachedLeft === null || cachedLeft === undefined){
    card.style.display = 'none';
    if(cachedTick){ clearInterval(cachedTick); cachedTick = null; }
    return;
  }
  card.style.display = '';
  var n = Math.max(0, Math.round(cachedLeft));
  box.innerHTML = n > 0
    ? 'Showing the last saved page in <b>' + n + '</b> second' + (n === 1 ? '' : 's')
      + '. <span style="color:var(--dim)">Touch the screen to keep this one.</span>'
    : 'Showing the last saved page...';
  document.getElementById('cachedstop').style.display = scanned ? '' : 'none';
}

function showCountdown(d){
  cachedLeft = (typeof d.cached_in === 'number') ? d.cached_in : null;
  if(cachedLeft !== null && !cachedTick){
    cachedTick = setInterval(function(){
      if(cachedLeft === null) return;
      cachedLeft = Math.max(0, cachedLeft - 1);
      drawCountdown();
    }, 1000);
  }
  drawCountdown();
}

/* Any interaction starts the minute again -- touch, key, mouse. Somebody
   halfway through typing a wifi password must not have the screen jump to a
   picture under them, and that is exactly the person who is standing at the
   machine working on it. Throttled, because mousemove fires continuously and
   this is a POST. */
async function sawSomebody(){
  if(cachedLeft === null) return;
  var now = Date.now();
  if(now - lastDefer < 1200) return;
  lastDefer = now;
  try{
    var r = await fetch('/api/cached/defer', {method:'POST'});
    var d = await r.json();
    if(typeof d['in'] === 'number'){ cachedLeft = d['in']; drawCountdown(); }
  }catch(e){}
}
['pointerdown','touchstart','keydown','mousemove'].forEach(function(ev){
  document.addEventListener(ev, sawSomebody, {passive:true});
});

/* Pressing Scan means somebody is working on this machine, so that is where
   the way out belongs -- visible, at the moment it becomes obvious a person
   is present, rather than a setting buried somewhere they cannot reach. */
async function keepScreen(){
  try{
    await fetch('/api/cached/keep', {method:'POST'});
    cachedLeft = null;
    drawCountdown();
    msg('This screen will stay up.');
  }catch(e){ msg('Failed: '+e, 'warn'); }
}

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

  // The field must never be readonly. It used to be made readonly when no
  // keyboard was detected, so that only the page's own key buttons could
  // write into it -- but the shared on-screen keyboard ignores a readonly
  // field entirely, and so does a real keyboard. Always writable.
  document.getElementById('pw').removeAttribute('readonly');
}

function esc(t){ return (t||'').replace(/[<&"]/g, c=>({'<':'&lt;','&':'&amp;','"':'&quot;'}[c])); }
function msg(t, cls){ const m=document.getElementById('msg');
  m.textContent=t||''; m.className='msg '+(cls||''); }

async function refresh(){
  let d; try { d = await (await fetch('/api/startup-state')).json(); } catch(e){ return; }
  applyInputMode(d.input || {});
  showCountdown(d);
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
    if(d.admin_ip){
      // Prefer port 80, exactly as the welcome page does: the address without
      // a port is the one a person can repeat out loud down a phone. The
      // client listens on both, so :8080 here was just the harder of the two
      // to read back.
      const ports = d.ports || [];
      const p = ports.includes(80) ? 80 : (ports[0] || d.admin_port || 8080);
      bits.push('http://' + d.admin_ip + (p === 80 ? '' : ':' + p));
    }
    bits.push('software ' + (d.software_version || '?')
              + '  \u00b7  os ' + (d.os_version === undefined ? '?' : d.os_version));
    ident.innerHTML = bits.join(' &nbsp;&middot;&nbsp; ');
  }

  const line = x => x ? `${x.state}${x.connection?' &middot; '+esc(x.connection):''}`
                       + `${x.ip?' &middot; '+x.ip:''}` : 'not present';
  document.getElementById('state').innerHTML = `
    <dt>Wired</dt><dd>${line(d.net.wired)}</dd>
    <dt>Wi-Fi</dt><dd>${line(d.net.wifi)}</dd>
    <dt>Internet</dt><dd>${d.connectivity}</dd>`;

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
  finally{ scanning=false; b.disabled=false; b.textContent='Scan for networks';
           scanned = true; drawCountdown(); }
}

// The password field uses the same on-screen keyboard as the customer's page
// -- injected by the client, not built here. Two keyboards meant two sets of
// bugs and two things to test, and the one on this page had no caps lock, no
// symbol layer and no key size setting.
function askPassword(ssid, why){
  pwSsid = ssid;
  document.getElementById('pwtitle').textContent = 'Password for ' + ssid;
  const p = document.getElementById('pw');
  p.value='';
  p.type = 'password';                      // never reopen already revealed
  const sb = document.getElementById('pwshow');
  if (sb) { sb.textContent = 'Show'; sb.setAttribute('aria-pressed','false'); }
  document.getElementById('pwcard').style.display='';
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

# The saved page. Deliberately the plainest thing in this file.
#
# It carries NO branding, no badge, no "saved copy from ..." line and no
# timestamp. It is a menu board, not a status page, and a line of text
# explaining itself is the kind of thing that gets photographed and sent in
# as a fault. The picture is the whole page.
#
# The only thing here that is not the picture is the way out: a two-second
# hold, or any key. Two panels in this fleet report touches nobody makes, so
# a tap must not do it -- but a display with no network and no way back to
# the network screen is a machine somebody has to drive to, and this project
# has enough of those.
CACHED_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SteadyScreen</title>
<style>
 html,body{margin:0;height:100%;background:#12141a;overflow:hidden;
   -webkit-user-select:none;user-select:none;cursor:none}
 #p{position:fixed;inset:0;background:#12141a url(/api/cached/image.jpg)
   center/contain no-repeat}
 /* Shown only while a finger is actually held down, so nothing is on the
    screen at rest. */
 #h{position:fixed;left:50%;bottom:6vh;transform:translateX(-50%);
   padding:.6rem 1.1rem;border-radius:10px;background:rgba(0,0,0,.72);
   color:#eef1f5;font:16px system-ui,-apple-system,sans-serif;opacity:0;
   transition:opacity .15s;pointer-events:none}
 #h.on{opacity:1}
</style></head><body>
<div id=p></div><div id=h>Keep holding to set this display up...</div>
<script>
var timer = null, hint = document.getElementById('h');
function leave(how){
  fetch('/api/cached/leave', {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({how: how})}).catch(function(){});
}
function down(){
  if(timer) return;
  hint.className = 'on';
  timer = setTimeout(function(){ timer = null; hint.className = '';
                                 leave('long press'); }, 2000);
}
function up(){
  if(timer){ clearTimeout(timer); timer = null; }
  hint.className = '';
}
addEventListener('pointerdown', down);
addEventListener('pointerup', up);
addEventListener('pointercancel', up);
addEventListener('touchstart', down, {passive:true});
addEventListener('touchend', up);
// A keyboard means somebody deliberate; no hold needed.
addEventListener('keydown', function(){ leave('key'); });
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



# ---------------------------------------------------------------------------
# Admin page authentication.
#
# The password used to be stored in the clear in config.json and compared with
# ==. It is now a PBKDF2 hash, and a value that is not one is treated as a
# legacy plaintext password and upgraded the first time it is used, so nobody
# has to be told to re-enter anything.
#
# 120k rounds: about a tenth of a second on the slowest board in the fleet,
# which is unnoticeable on a login form and a wall in front of guessing.
PBKDF2_ROUNDS = 120000
SESSION_COOKIE = "ds_admin"
SESSION_MAX = 16                 # concurrent logins; oldest is evicted
DEFAULT_IDLE_MINUTES = 15

_sessions = {}                   # token -> last activity, monotonic seconds
_sessions_lock = threading.Lock()


LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>SteadyScreen</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;min-height:100vh;display:grid;place-items:center;
      background:#11151a;color:#e8eef2;
      font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
 form{width:min(22rem,90vw);padding:2rem;background:#182029;
      border:1px solid #24303c;border-radius:.8rem;text-align:center}
 h1{margin:0 0 .3rem;font-size:1.35rem;font-weight:600}
 .b{color:#6fe3f2}
 p{margin:0 0 1.4rem;color:#8fa3b3;font-size:.85rem}
 input{width:100%;box-sizing:border-box;padding:.7rem .8rem;font-size:1.05rem;
       background:#0e1318;color:#e8eef2;border:1px solid #2b3947;
       border-radius:.45rem}
 input:focus{outline:none;border-color:#6fe3f2}
 button{width:100%;margin-top:.9rem;padding:.7rem;font-size:1rem;
        background:#6fe3f2;color:#06222a;border:0;border-radius:.45rem;
        font-weight:600;cursor:pointer}
 .err{margin:.9rem 0 0;color:#ff9a9a;font-size:.85rem;min-height:1.2em}
 .lnk{display:inline-block;margin-top:.9rem;color:#8fa3b3;font-size:.8rem;
      text-decoration:underline;cursor:pointer;background:none;border:0;
      font-family:inherit}
 #help{display:none;margin-top:1rem;padding:.9rem;text-align:left;
       background:#0e1318;border:1px solid #2b3947;border-radius:.5rem;
       color:#b9c8d4;font-size:.8rem;line-height:1.45}
 #help code{display:block;margin-top:.5rem;padding:.5rem;background:#070a0d;
            border-radius:.35rem;color:#6fe3f2;font-size:.72rem;
            overflow-x:auto;white-space:pre-wrap;word-break:break-all}
</style></head><body>
<form id=f>
 <h1>Steady<span class=b>Screen</span></h1>
 <p>This display is password protected</p>
 <input type=password id=pw autocomplete=current-password
        placeholder="Password" autofocus>
 <button type=submit>Sign in</button>
 <p class=err id=e></p>
 <button type=button class=lnk id=forgot>Forgot password?</button>
 <div id=help>
  <b>There is no reset button</b>, on purpose &mdash; one on a page you cannot
  sign into would not be a lock at all.
  <br><br>If a recovery email is set, a temporary password will one day be
  sent to it. That needs a server to send the mail and there is not one yet,
  so today the way back in is ssh. The password lives in a file the
  <code style="display:inline;padding:0;background:none">kiosk</code> account
  owns, so no sudo is needed:
  <code>ssh kiosk@THIS-DISPLAY
python3 -c 'import json;p="/etc/kiosk/config.json";c=json.load(open(p));c["admin_password"]="";json.dump(c,open(p,"w"),indent=2)'</code>
  The page is then open again and you can set a new password from it.
 </div>
</form>
<script>
document.getElementById('forgot').addEventListener('click', function () {
  var h = document.getElementById('help');
  h.style.display = h.style.display === 'block' ? 'none' : 'block';
});
var f = document.getElementById('f');
f.addEventListener('submit', function (ev) {
  ev.preventDefault();
  var e = document.getElementById('e');
  e.textContent = '';
  fetch('/api/login', {method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({password: document.getElementById('pw').value})})
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ok) { location.replace('/'); return; }
      e.textContent = d.detail || 'Wrong password';
      document.getElementById('pw').value = '';
      document.getElementById('pw').focus();
    })
    .catch(function (err) { e.textContent = 'Could not reach the display'; });
});
</script></body></html>"""


def hash_password(pw):
    """A password as it is stored. Never store what the user typed."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), dk.hex())


def check_password(stored, given):
    """(matches, needs_rehash). Constant time on the comparison itself."""
    if not stored:
        return False, False
    if not stored.startswith("pbkdf2_sha256$"):
        # A password written by an older build. Accept it once, then upgrade.
        return hmac.compare_digest(stored, given), True
    try:
        _, rounds, salt_hex, want_hex = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
    except Exception:                                         # noqa: BLE001
        return False, False
    return hmac.compare_digest(dk.hex(), want_hex), False


def session_new():
    tok = secrets.token_urlsafe(32)
    with _sessions_lock:
        # Bounded, so a script that logs in in a loop cannot grow this without
        # limit. Evicting the least recently used is the least surprising rule.
        while len(_sessions) >= SESSION_MAX:
            del _sessions[min(_sessions, key=_sessions.get)]
        _sessions[tok] = time.monotonic()
    return tok


def session_valid(tok, idle_seconds, touch):
    """Is this token live? `touch` says whether to count this as activity.

    Background polling must NOT count. The admin page asks for /api/status
    every five seconds, so if every request refreshed the session an idle
    timeout could never fire on a page left open -- which is the one case it
    exists for.
    """
    if not tok:
        return False
    now = time.monotonic()
    with _sessions_lock:
        seen = _sessions.get(tok)
        if seen is None:
            return False
        if idle_seconds and now - seen > idle_seconds:
            del _sessions[tok]
            return False
        if touch:
            _sessions[tok] = now
    return True


def session_drop(tok):
    with _sessions_lock:
        _sessions.pop(tok, None)


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
                "/selftest", "/api/splash/image.png", "/api/startup-state",
                "/cached", "/api/cached/image.jpg",
                # Signing in cannot wait for the browser to exist, or a
                # protected display that is still starting locks everybody out
                # of the page that would tell them why.
                "/login", "/api/login", "/api/logout")

    # The pages the display renders for itself, and the endpoints they need.
    OWN_SCREENS = ("/startup", "/welcome", "/cached",
                   "/api/startup-state", "/api/splash/image.png",
                   "/api/cached/image.jpg", "/api/cached/defer",
                   "/api/cached/keep", "/api/cached/leave")

    def _from_loopback(self):
        try:
            return (self.client_address[0] or "").split("%")[0] in (
                "127.0.0.1", "::1", "::ffff:127.0.0.1")
        except Exception:                                     # noqa: BLE001
            return False

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

    def _cfg(self):
        # cfg is set before the server starts, so the password is enforced from
        # the first request even though the client is not built yet.
        return self.kiosk.cfg if self.kiosk is not None else (self.cfg or {})

    def _cookie(self, name):
        # No http.cookies: one header, one name, and SimpleCookie raises on
        # input it dislikes -- which a browser can be talked into sending.
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _idle_seconds(self):
        try:
            mins = int(self._cfg().get("admin_idle_minutes",
                                       DEFAULT_IDLE_MINUTES))
        except (TypeError, ValueError):
            mins = DEFAULT_IDLE_MINUTES
        return max(0, mins) * 60          # 0 means never time out

    def _authed(self):
        from urllib.parse import urlparse

        want = self._cfg().get("admin_password") or ""
        if not want:
            return True

        path = urlparse(self.path).path
        # The login form and the endpoint it posts to cannot require a login.
        if path in ("/login", "/api/login", "/api/logout"):
            return True

        # Nor can the display's own screens. These are what THIS MACHINE
        # loads into its own browser: the network page it shows while it is
        # looking for a connection, the welcome page, and the saved page. A
        # password on those does not protect anything -- there is nothing to
        # protect, they are what is already on the glass in front of whoever
        # is standing there -- and it turns a protected display into one that
        # boots to a login form instead of the screen that would let somebody
        # join it to a network. Loopback only, so nothing on the LAN can pull
        # a picture of the screen without signing in.
        if path in self.OWN_SCREENS and self._from_loopback():
            return True

        # Polling must not count as activity, or a page left open on a desk
        # would hold its own session alive for ever and the idle timeout could
        # never fire. Real actions are POSTs and page loads.
        touch = self.command != "GET" or not path.startswith("/api/")
        if session_valid(self._cookie(SESSION_COOKIE), self._idle_seconds(),
                         touch):
            return True

        if path.startswith("/api/"):
            # JSON, not a login page: the caller is a script or this page's
            # own fetch, and an HTML body would be gibberish to both.
            self._json(401, {"error": "auth", "detail": "not signed in"})
        else:
            # 200 with the login form rather than 401 with a realm. A 401 is
            # what makes the browser throw up its own credentials dialog,
            # which is the thing being replaced.
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        return False

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              cookie=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
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
        if path == "/login":
            # No password set, or already signed in (_authed let this through):
            # there is nothing to ask for, so show the page they wanted.
            page = (LOGIN_PAGE if (self._cfg().get("admin_password") or "")
                    and not session_valid(self._cookie(SESSION_COOKIE),
                                          self._idle_seconds(), False)
                    else ADMIN_PAGE)
            self._send(200, page, "text/html; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200, ADMIN_PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, self.kiosk.status())

        elif path == "/api/osupdate/progress":
            self._json(200, {"phase": osupdate_progress()})

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
        elif path == "/cached":
            self._send(200, CACHED_PAGE, "text/html; charset=utf-8")
        elif path == "/api/cached/image.jpg":
            try:
                with open(CACHED_PAGE_IMAGE, "rb") as fh:
                    img = fh.read()
            except OSError:
                return self._json(404, {"error": "nothing saved yet"})
            self._send(200, img, "image/jpeg")
        elif path == "/api/cached":
            self._json(200, cached_page_state(self.kiosk.cfg))
        elif path == "/api/netwatch":
            self._json(200, netwatch_state())
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
                # None means it is not going to happen -- switched off, no
                # saved page, too old, already cancelled, or the real page
                # has been up this boot. The page shows the line only when
                # this is a number, so the two can never disagree.
                "cached_in": k.cached_remaining(),
                "cached_cancelled": bool(getattr(k, "_cached_cancelled",
                                                 False)),
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
        elif path == "/api/screenconnect":
            # Status only. Everything that can change the machine is a POST.
            hosts = []
            try:
                with open("/etc/kiosk/screenconnect-hosts") as fh:
                    hosts = [ln.strip() for ln in fh if ln.strip()]
            except OSError:
                pass

            # dpkg decides whether it is installed, because dpkg is the thing
            # that installed it. Earlier versions asked systemd, and systemd
            # keeps a not-found reference to a unit whose file has gone --
            # printing it with a leading bullet, which was then read as the
            # service name. An uninstalled agent reported itself installed,
            # with a service called "●".
            installed = False
            svc = ""
            try:
                pr = subprocess.run(["dpkg-query", "-W", "-f",
                                     "${Package} ${Status}\n",
                                     "connectwisecontrol-*", "screenconnect-*"],
                                    capture_output=True, text=True, timeout=15)
                for ln in (pr.stdout or "").splitlines():
                    # "name install ok installed" -- anything else (deinstall,
                    # config-files) is a package that is on its way out.
                    if ln.strip().endswith("install ok installed"):
                        installed = True
                        break
            except Exception:                                  # noqa: BLE001
                pass

            # The unit name, for display, skipping systemd's bullet and any
            # not-found leftover.
            try:
                pr = subprocess.run(["systemctl", "list-units", "--type=service",
                                     "--all", "--no-legend", "*connectwise*"],
                                    capture_output=True, text=True, timeout=10)
                for ln in (pr.stdout or "").splitlines():
                    if "not-found" in ln:
                        continue
                    for tok in ln.split():
                        if tok.endswith(".service"):
                            svc = tok
                            break
                    if svc:
                        break
            except Exception:                                  # noqa: BLE001
                pass

            # The vendor's unit is Type=oneshot and forks, so it is dead within
            # seconds of a good install while the agent runs on. The process is
            # the only honest answer -- matched on the jar rather than the
            # directory, so a shell that merely mentions the path in its own
            # command line is not counted as the agent.
            running = False
            try:
                pr = subprocess.run(
                    ["pgrep", "-f", "/opt/connectwisecontrol.*ScreenConnect.Client.jar"],
                    capture_output=True, text=True, timeout=10)
                running = pr.returncode == 0 and bool((pr.stdout or "").strip())
            except Exception:                                  # noqa: BLE001
                pass

            # Whether this display has the helper at all. It is a new
            # component, so a client whose kiosk.py has just been updated has
            # this card and not yet the program behind it -- kiosk-update
            # skips components its own table has no path for, and the table
            # arrives in the same pass as the page. The gap closes on the next
            # check, within the hour.
            return self._json(200, {"installed": installed, "service": svc,
                                    "running": running, "hosts": hosts,
                                    "helper": os.path.exists(SCREENCONNECT_HELPER)})

        elif path == "/api/printer":
            self._json(200, printer_status())
        elif path == "/api/printer/devices":
            self._json(200, printer_devices())
        elif path == "/api/printer/ready":
            # One queue, one answer. /api/printer checks every queue, which
            # costs a socket timeout each when one is unreachable -- fine for
            # a page somebody is looking at, too slow for a guard that has to
            # decide whether to send a job. A guard that times out is a guard
            # that waves the job through.
            from urllib.parse import parse_qs, urlparse

            q = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            self._json(200, {"name": q, "problem": queue_problem(q),
                             "ready": not queue_problem(q)})
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

        # Signing in and out come before the readiness gate below: they are
        # about the server, not the browser, and a display still starting up
        # would otherwise refuse the one request that gets somebody in.
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            stored = self._cfg().get("admin_password") or ""
            given = str(self._body().get("password", ""))
            if not stored:
                return self._json(200, {"ok": True, "detail": "no password set"})
            ok, needs_rehash = check_password(stored, given)
            if not ok:
                # A small, flat delay. Not a lockout: locking the owner out of
                # their own display over a few typos would be worse than the
                # guessing it prevents on a LAN.
                time.sleep(0.4)
                return self._json(401, {"ok": False, "detail": "Wrong password"})
            if needs_rehash and self.kiosk is not None:
                # It was stored in the clear by an older build. Now that we
                # know it is right, replace it with a hash. Nobody is asked
                # to re-enter anything.
                GLib.idle_add(self.kiosk.set_option, "admin_password",
                              hash_password(given))
            tok = session_new()
            # HttpOnly so page scripts cannot read it; SameSite=Lax so a form
            # on another site cannot ride the session. Not Secure: this is
            # plain http on a LAN, and Secure would stop the cookie working.
            self._send(200, json.dumps({"ok": True}).encode(),
                       "application/json",
                       cookie=("%s=%s; Path=/; HttpOnly; SameSite=Lax"
                               % (SESSION_COOKIE, tok)))
            return
        if path == "/api/logout":
            session_drop(self._cookie(SESSION_COOKIE))
            self._send(200, json.dumps({"ok": True}).encode(),
                       "application/json",
                       cookie="%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                              % SESSION_COOKIE)
            return

        if not self._ready():        # nothing here is meaningful without it
            return
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

        elif path == "/api/snapshot":
            b = self._body()
            action = (b.get("action") or "list").strip()
            arg = (b.get("id") or b.get("label") or "").strip()
            ok, detail, data = snapshot_command(action, arg)
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": detail, "snapshots": data})

        elif path == "/api/update/settings":
            b = self._body()
            done, detail = {}, []
            for key in ("auto", "channel"):
                if key not in b:
                    continue
                ok, msg = update_set(key, b[key])
                done[key] = ok
                detail.append(msg)
            if not done:
                return self._json(400, {"error": "nothing to set"})
            allok = all(done.values())
            self._json(200 if allok else 502,
                       {"ok": allok, "detail": "; ".join(detail),
                        "config": update_config()})

        elif path == "/api/update":
            action = (self._body().get("action") or "check").strip()
            ok, detail, data = update_command(action)
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": detail, "result": data})

        elif path == "/api/osupdate":
            action = (self._body().get("action") or "check").strip()
            ok, detail, data = osupdate_command(action)
            self._json(200 if ok else 502,
                       {"ok": ok, "detail": detail, "result": data})

        elif path in ("/api/screenconnect/check", "/api/screenconnect/install"):
            b = self._body()
            url = str(b.get("url") or "").strip()
            if not url:
                return self._json(400, {"ok": False, "detail": "no link given"})
            # Length-capped and character-checked before it is ever an argv
            # element. It is passed as a single argument to a helper -- never
            # through a shell -- but a control character or a newline in a URL
            # is a caller doing something other than pasting a link.
            if len(url) > 2000 or re.search(r"[\x00-\x1f\x7f\s]", url):
                return self._json(400, {"ok": False, "detail":
                                        "that does not look like an installer link"})

            # The card can arrive before the helper does, and for a while it
            # will. kiosk-screenconnect is a NEW component, and kiosk-update
            # skips components its own table has no path for -- so the update
            # that brings this page installs everything the old updater knew
            # about, and the helper lands on the NEXT pass, up to an hour
            # later. A fresh install from an older image is in the same state
            # for its first check.
            #
            # Without this, the button comes back with whatever sudo says
            # about a missing file, which reads like the feature is broken
            # rather than like it is on its way.
            if not os.path.exists(SCREENCONNECT_HELPER):
                return self._json(200, {"ok": False, "detail":
                    "the remote-support helper has not reached this display "
                    "yet. It arrives on the next update check, usually within "
                    "the hour. Nothing is wrong -- try again shortly."})

            if path.endswith("/check"):
                ok, out, err = run_root(SCREENCONNECT_HELPER, "check", url,
                                        timeout=30)
                text = (out or "") + (err or "")
                if not ok:
                    return self._json(200, {"ok": False,
                                            "detail": text.strip()[-400:]})
                # Parsed back out of the helper's own output rather than
                # reimplemented here, so the page cannot disagree with the
                # thing that will actually do the work.
                def field(label):
                    m = re.search(r"^\s*%s\s*:\s*(.+)$" % label, text, re.M)
                    return m.group(1).strip() if m else ""
                host = field("server").strip()
                cert = field("certificate")
                # The helper says "this host is X's" only when the certificate
                # actually proved it, so that line is the verdict rather than
                # a second opinion on it.
                trusted = "this link will be refused" not in text
                cert_org = ""
                mo = re.search(r"O=([^,]+(?:,\s*(?:LLC|Inc\.?|Ltd\.?))?)", cert)
                if mo:
                    cert_org = mo.group(1).strip()
                issuer = ""
                mi = re.search(r"issued by (.+?), valid to (.+)$", text, re.M)
                if mi:
                    issuer, expires = mi.group(1).strip(), mi.group(2).strip()
                else:
                    expires = ""
                fields = 0
                m = re.search(r"custom\s*:\s*(\d+)", text)
                if m:
                    fields = int(m.group(1))
                # Every line the helper printed about the certificate, so a
                # refusal says which of the checks failed rather than only
                # that one did.
                why = "; ".join(
                    ln.strip() for ln in text.splitlines()
                    if re.search(r"certificate|TLS|openssl|reach|expired|organisation",
                                 ln, re.I))[:400]
                return self._json(200, {
                    "ok": True, "host": host, "trusted": trusted,
                    "installer": field("installer"), "name": field("name"),
                    "fields": fields, "cert": cert, "cert_org": cert_org,
                    "issuer": issuer, "expires": expires, "why": why})

            # install
            ok, out, err = run_root(SCREENCONNECT_HELPER, "install", url,
                                    timeout=600)
            text = ((out or "") + (err or "")).strip()
            if ok:
                return self._json(200, {"ok": True,
                                        "detail": "installed; the agent should "
                                                  "appear in your console shortly"})
            return self._json(200, {"ok": False, "detail": text[-500:] or "failed"})

        elif path == "/api/printer":
            b = self._body()
            name = (b.get("name") or "").strip()
            uri = (b.get("uri") or "").strip()
            # The name becomes a CUPS queue and lands in a file path, so it is
            # checked here rather than trusted. CUPS itself forbids space, /
            # and #; anything outside this set is a caller doing something
            # other than naming a printer.
            if name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", name):
                return self._json(400, {"ok": False, "detail":
                                        "A printer name can only contain "
                                        "letters, digits, dot, dash and "
                                        "underscore."})
            # An allowlist of schemes, not "anything with ://". CUPS has a
            # file: backend -- off by default, but a default is not a control
            # -- and there is no reason the admin page should ever be able to
            # name one. These eight are every kind of printer this image can
            # actually drive.
            if uri and not re.fullmatch(
                    r"(usb|socket|ipp|ipps|dnssd|lpd|serial|parallel)://"
                    r"[^\s\x00-\x1f]{1,250}", uri):
                return self._json(400, {"ok": False, "detail":
                                        "That does not look like a printer "
                                        "address. Expected something like "
                                        "socket://192.168.1.50:9100 or "
                                        "ipp://printer.local/ipp/print."})
            dots = str(b.get("dots") or "").strip()
            if dots and not re.fullmatch(r"[0-9]{2,4}", dots):
                return self._json(400, {"ok": False,
                                        "detail": "Width must be a number."})
            action = (b.get("action") or "").strip()
            note = ""
            if action in ("test", "ruler") and not name:
                # No queue named means "the printer receipts go to right
                # now", so it makes the same choice a receipt makes --
                # including falling back. The buttons on the card are about
                # the printing, not about a particular printer; naming one is
                # what Manage printers is for.
                #
                # The ruler falls back too, which is only safe because the
                # ruler now prints the name of the printer it came out of. A
                # ruler is used to SET a width, and one that does not say
                # where it came from is a measurement waiting to be applied
                # to the wrong printer.
                name, why = choose_printer(k.cfg)
                if why:
                    note = "%s, so this went to %s. " % (why, name)
            service = (b.get("service") or "").strip()
            if service and not re.fullmatch(
                    r"dnssd://[^\s\x00-\x1f]{1,250}", service):
                service = ""
            ok, detail = printer_command(action, name, uri, dots, service)
            detail = note + detail
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
                        "hide_cursor", "admin_idle_minutes", "recovery_email",
                        # admin_password is deliberately NOT settable here.
                        # It goes through /api/admin-password, which is the
                        # only place that hashes it and checks the
                        # confirmation field. Two ways in meant one of them
                        # could store a plaintext password.
                        "clear_cache_on_start", "wait_for_network",
                        "allow_location", "print_enabled", "printer",
                        "printer_backup",
                        "print_margins_mm", "print_width_mm",
                        "screenshot_cache_secs", "osk", "osk_caps",
                        "osk_offset_x", "osk_offset_y", "osk_scale",
                        "offline_after_seconds", "offline_border",
                        "cached_page", "cached_page_wait",
                        "cached_page_max_age_hours",
                        "cached_page_refresh_seconds",
                        # The watchdog reads these itself, from the same file.
                        "netwatch", "netwatch_reboot"):
                # NOTE: splash_managed is deliberately NOT in this list. A
                # client that could unlock its own branding would make the
                # lock decorative.
                if key in body:
                    GLib.idle_add(k.set_option, key, body[key])
                    applied[key] = body[key]
            self._json(200, {"ok": True, "applied": applied})

        elif path == "/api/cached/defer":
            # Somebody touched the network screen. Start the minute again.
            #
            # Done here rather than through GLib.idle_add: this only assigns a
            # float, and going through the main loop would mean answering with
            # the countdown as it was BEFORE the reset -- the page would draw
            # the old number for a second and look like the touch did nothing.
            k.defer_cached()
            self._json(200, {"ok": True, "in": k.cached_remaining()})

        elif path == "/api/cached/keep":
            # ...and this one means "I am working on it, stop counting".
            GLib.idle_add(k.keep_startup)
            self._json(200, {"ok": True})

        elif path == "/api/cached/leave":
            how = (self._body().get("how") or "asked")[:40]
            GLib.idle_add(k.leave_cached, how)
            self._json(200, {"ok": True})

        elif path == "/api/cached/save":
            # Take one now. The admin page's "Save one now" button, so nobody
            # has to wait an hour to find out whether this works.
            GLib.idle_add(k._save_cached_page, "asked from the admin page")
            self._json(200, {"ok": True})

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
            body = self._body()
            pw = str(body.get("password", ""))
            confirm = str(body.get("confirm", pw))
            # A typo here locks the owner out of their own display, and the
            # only way back is ssh and a text editor. The second field is
            # cheap; being wrong about this is not.
            if pw != confirm:
                return self._json(400, {"ok": False,
                                        "detail": "The two passwords do not match"})
            stored = hash_password(pw) if pw else ""
            GLib.idle_add(k.set_option, "admin_password", stored)
            # Changing the password signs everyone out -- that is usually the
            # point of changing it -- but not the person who just changed it.
            with _sessions_lock:
                _sessions.clear()
            cookie = None
            if stored:
                cookie = ("%s=%s; Path=/; HttpOnly; SameSite=Lax"
                          % (SESSION_COOKIE, session_new()))
            self._send(200,
                       json.dumps({"ok": True, "protected": bool(stored)}).encode(),
                       "application/json", cookie=cookie)

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


def unprivileged_port_start():
    """The lowest port this process may bind without privilege.

    Read straight from /proc rather than by running `sysctl`: this is on the
    startup path, and `sysctl` lives in /usr/sbin, which is not on the kiosk
    user's PATH -- a detail that made an earlier check of exactly this value
    come back empty and get read as "no sysctl" when the sysctl was fine.

    Fails SAFE. Anything unreadable returns 1024, which means "do not move
    anything", so an image genuinely without the sysctl is left alone.
    """
    if os.geteuid() == 0:
        return 0                       # root may bind anything
    try:
        with open("/proc/sys/net/ipv4/ip_unprivileged_port_start") as fh:
            return int(fh.read().strip())
    except Exception:                                         # noqa: BLE001
        return 1024


def advertise_port(port):
    """Make the mDNS advert name the port this client is actually serving.

    The service file ships with <port>80</port> hardcoded, and the reasoning was
    that a client which fell back to 8080 then advertises an address that does
    not answer, which is "the correct complaint". It is a complaint nobody
    hears: `avahi-browse -rt _steadyscreen._tcp` is the documented way to find
    a display, so the machines that most need attention are exactly the ones it
    gives a broken address for.

    Rewritten only when it disagrees, so this is a no-op on every client that is
    already on 80 -- and failure is silent on purpose, because a display that
    cannot rewrite an advert must still come up and serve its page.
    """
    path = "/etc/avahi/services/steadyscreen.service"
    try:
        with open(path) as fh:
            xml = fh.read()
    except Exception:                                         # noqa: BLE001
        return
    want = "<port>%d</port>" % port
    new = re.sub(r"<port>\s*\d+\s*</port>", want, xml)
    if new == xml:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(new)
        os.replace(tmp, path)
        print("ds: mDNS advert now says port %d" % port, flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print("ds: could not update the mDNS advert (%s)" % exc, file=sys.stderr)


def password_complaint(password):
    """Is this password good enough, and if not, say exactly why.

    The rule was eight characters and nothing else, which accepts "12345678"
    and the machine's own name. These displays sit on shop networks that guests
    join, so the password is not protecting a laptop in a locked office.

    Deliberately modest: ten characters and two kinds of character. Long enough
    to matter, simple enough that somebody standing at a counter can satisfy it
    without a manager, and stated as a single sentence rather than a checklist
    that has to be decoded. A rule people route around is worse than a weaker
    rule they follow.

    Returns (ok, reason). The reason is shown to whoever typed it, so it says
    what is missing, not merely that something is.
    """
    p = password or ""
    if len(p) < 12:
        return False, ("password must be at least 12 characters "
                       "(that one is %d)" % len(p))
    kinds = sum([
        any(c.islower() for c in p),
        any(c.isupper() for c in p),
        any(c.isdigit() for c in p),
        any(not c.isalnum() for c in p),
    ])
    if kinds < 2:
        return False, ("password must use at least two of: lower case, "
                       "upper case, digits, symbols")
    # The obvious ones, and the words that are written on the machine itself.
    # A password that is the display's own name is one anybody reading the
    # screen already knows.
    low = p.lower()
    banned = ("password", "steadyscreen", "kiosk", "12345678", "changeme",
              "letmein", "qwerty", "admin")
    for b in banned:
        if b in low:
            return False, "password must not contain \"%s\"" % b
    try:
        name = (load_identity() or {}).get("name") or ""
    except Exception:                                         # noqa: BLE001
        name = ""
    if name and len(name) > 3 and name.lower() in low:
        return False, ("password must not contain this display's name "
                       "(%s) -- it is written on the screen" % name)
    return True, ""


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

    # Port 80, and 8080 only if 80 cannot be had.
    #
    # 80 is what you want to say out loud -- "open http://10.0.2.15" beats
    # reciting a port number to someone standing at a counter -- and it means
    # a display is found the same way as anything else on the network.
    #
    # 8080 is no longer served alongside it. It was there because every note
    # and bookmark used it, but two ports mean two answers to "where is this
    # display", and the sweep that finds the fleet had to guess.
    #
    # It stays as a FALLBACK, and that is not caution for its own sake.
    # Binding 80 unprivileged needs net.ipv4.ip_unprivileged_port_start=80,
    # which the image sets -- but the client updates independently of the OS,
    # so a machine can be running this code on an image that predates the
    # sysctl. Both landed in the same commit, so any older image lacks it.
    # kiosk-plano is exactly that shape: a live store, remote-only, no
    # ScreenConnect. Serving nothing there would mean a site visit.
    # Every client installed before this change has the old pair written into
    # its config.json: admin_port 8080, admin_port_alt 80. Changing the
    # DEFAULTS does nothing for those -- the stored values win -- and with the
    # first-one-wins rule below they would serve 8080 and stop serving 80,
    # which is backwards. So the stored value has to be moved.
    #
    # This used to match that ONE exact pair, and it missed the shape a fresh
    # install produces. The config.json baked into the image pinned
    # admin_port 8080 and carried no admin_port_alt at all, so the default
    # filled in and the stored pair read 8080/8080 -- which the old condition
    # did not match. Every machine installed from a current image therefore
    # served 8080 for ever, on an OS that could bind 80 perfectly well. Found
    # on .245 on 2026-08-31, and it would have shipped in the next image too:
    # a new image was the cause, not the cure.
    #
    # Deciding from the KERNEL instead of from a config pattern is the fix.
    # Whether 80 can be bound is a fact this process can read; matching stored
    # pairs was only ever a guess at that fact, and it guessed wrong the first
    # time a third pair existed.
    #
    # There is no risk of overriding a deliberate choice: nothing in the admin
    # page can set a port -- it is not a setting -- so a stored 8080 is only
    # ever an old default or the image seed.
    if port == 8080 and unprivileged_port_start() <= 80:
        print("ds: moving the admin page to port 80 (8080 becomes the fallback)",
              flush=True)
        cfg["admin_port"] = port = 80
        cfg["admin_port_alt"] = 8080
        try:
            save_config(cfg)
        except Exception as exc:                              # noqa: BLE001
            # Not fatal: the ports below are already right for this run, and
            # the same flip will simply happen again next boot.
            print("ds: could not save the port change (%s)" % exc,
                  file=sys.stderr)

    served = []
    fallback = int(cfg.get("admin_port_alt") or 0)
    for p in [port] + ([fallback] if fallback and fallback != port else []):
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
        # One port is the point. Only reach for the fallback if the real one
        # could not be bound.
        break

    if not served:
        raise OSError("could not listen on any admin port")
    if served[0][0] != port:
        print("ds: WARNING: port %d could not be bound, serving on %d instead."
              % (port, served[0][0]), file=sys.stderr)
        print("ds: this image is missing net.ipv4.ip_unprivileged_port_start=80",
              file=sys.stderr)

    AdminHandler.ports = [p for p, _ in served]
    advertise_port(served[0][0])
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
