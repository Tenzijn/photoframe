#!/usr/bin/env python3
"""
PhotoFrame - turn an old Ubuntu laptop into a full-screen photo gallery.

Keys while the slideshow runs:
    S / Enter / right-click   open settings
    Right / Left              next / previous photo
    Space                     pause / resume
    Esc / Q / Ctrl+Q          quit (always works, even with settings open)

Safety guard rails (so a bad photo or a crash can never take the laptop down):
  * Memory cap for this process (RLIMIT_DATA) + oversized images are skipped,
    so a huge photo gives a MemoryError here instead of freezing the system.
  * Runs at low CPU priority and volunteers itself to the OOM killer first.
  * Keep-awake uses GNOME's idle/suspend inhibitor, which the system drops
    automatically when this process ends - even on a crash or kill -9.
    No system settings are permanently changed.
  * Sleep is allowed again when the battery runs low, so the laptop is never
    drained to a hard power-off.
  * Only one copy can run at a time.
  * Restarts itself cleanly if its memory use creeps up over days.
  * Never grabs keyboard/mouse; Esc on the slideshow always quits.
  * Logs to ~/.cache/photoframe/ so problems can be diagnosed.
"""
import faulthandler
import fcntl
import glob
import json
import logging
import logging.handlers
import os
import random
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import warnings

APP = "PhotoFrame"
SCRIPT = os.path.abspath(__file__)
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "photoframe")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_STATE_PATH = os.path.join(CONFIG_DIR, "saved-power-settings.json")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"),
                         "photoframe")
LOG_PATH = os.path.join(CACHE_DIR, "photoframe.log")
CRASH_PATH = os.path.join(CACHE_DIR, "crash.log")
LOCK_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or CONFIG_DIR, "photoframe.lock")
AUTOSTART_PATH = os.path.join(HOME, ".config", "autostart", "photoframe.desktop")
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

# ---- guard-rail limits
MAX_FILE_BYTES = 200 * 1024 * 1024     # skip files bigger than this
MAX_DECODE_PIXELS = 40_000_000         # skip images that would decode bigger than this
MAX_SCAN_FILES = 100_000               # stop scanning after this many photos
MAX_SCAN_SECONDS = 15                  # ... or after this long
FORBIDDEN_ROOTS = ("/", "/proc", "/sys", "/dev", "/run", "/boot", "/usr", "/etc", "/var")
BATTERY_LOW_PERCENT = 20               # below this (on battery) sleep is allowed again
HEARTBEAT_MS = 500

DEFAULTS = {
    "folder": os.path.join(HOME, "Pictures"),
    "recursive": True,
    "shuffle": True,
    "interval": 10,          # seconds per photo
    "fit": "fit",            # "fit" = whole photo, "fill" = crop to fill screen
    "background": "blur",    # "blur" or "black" (used in fit mode)
    "fade": True,
    "show_clock": True,
    "show_name": False,
    "keep_awake": True,
    "rescan_minutes": 10,    # pick up new photos added to the folder
}

log = logging.getLogger("photoframe")


# --------------------------------------------------------------------------- safety setup
def setup_logging():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        # Keep the hard-crash log small, then record segfaults/aborts into it.
        if os.path.exists(CRASH_PATH) and os.path.getsize(CRASH_PATH) > 256 * 1024:
            os.replace(CRASH_PATH, CRASH_PATH + ".old")
        crash = open(CRASH_PATH, "a")
        crash.write(f"\n--- start {time.ctime()} pid {os.getpid()}\n")
        crash.flush()
        faulthandler.enable(file=crash)
        setup_logging.crash_file = crash  # keep it open
    except OSError:
        pass
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.INFO)


def total_ram():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return 2 * 1024 ** 3


def rss_bytes():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


GiB = 1024 ** 3
# Hard cap on memory this process may allocate: half the RAM, between 768 MB and 3 GB.
MEMORY_CAP = int(min(max(total_ram() * 0.5, 0.75 * GiB), 3 * GiB))
# Soft cap: above this resident size, restart cleanly (catches slow leaks over days).
RSS_RESTART = int(min(max(total_ram() * 0.3, 0.5 * GiB), 1.5 * GiB))


def protect_system():
    """Make sure this app is the one that suffers if memory runs out, not the OS."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        cap = MEMORY_CAP if hard == resource.RLIM_INFINITY else min(MEMORY_CAP, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (cap, hard))
    except (ValueError, OSError) as e:
        log.warning("could not set memory cap: %s", e)
    try:
        os.nice(10)
    except OSError:
        pass
    try:  # if the system ever runs out of memory, kill us first
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def single_instance_lock():
    """Return a held lock file, or None if another PhotoFrame is already running."""
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        fd = open(LOCK_PATH, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except BlockingIOError:
        return None
    except OSError:
        return True  # can't lock (odd filesystem) - don't block startup


# --------------------------------------------------------------------------- utils
def run(cmd, capture=False):
    """Run a command quietly; never raise, never hang."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, text=True)
        return r.stdout.strip() if capture and r.returncode == 0 else (r.returncode == 0)
    except (OSError, subprocess.SubprocessError):
        return None if capture else False


def atomic_write(path, text):
    """Write a file so a crash or power cut leaves either the old or the new version."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _clamp_int(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def validate_config(raw):
    """Turn whatever is in config.json into safe values."""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    for key in ("recursive", "shuffle", "fade", "show_clock", "show_name", "keep_awake"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    if isinstance(raw.get("folder"), str) and raw["folder"].strip():
        cfg["folder"] = os.path.expanduser(raw["folder"].strip())
    if raw.get("fit") in ("fit", "fill"):
        cfg["fit"] = raw["fit"]
    if raw.get("background") in ("blur", "black"):
        cfg["background"] = raw["background"]
    cfg["interval"] = _clamp_int(raw.get("interval"), 3, 86400, DEFAULTS["interval"])
    cfg["rescan_minutes"] = _clamp_int(raw.get("rescan_minutes"), 1, 1440,
                                       DEFAULTS["rescan_minutes"])
    return cfg


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return validate_config(json.load(f))
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError) as e:
        log.warning("config unreadable (%s), using defaults", e)
        return dict(DEFAULTS)


def save_config(cfg):
    try:
        atomic_write(CONFIG_PATH, json.dumps(validate_config(cfg), indent=2))
    except OSError as e:
        log.error("could not save config: %s", e)


def folder_problem(folder):
    """Return a reason this folder must not be scanned, or None if it is fine."""
    if not folder:
        return "No folder chosen."
    real = os.path.realpath(folder)
    if not os.path.isdir(real):
        return "That folder doesn't exist (is the USB drive plugged in?)."
    for bad in FORBIDDEN_ROOTS:
        if real == bad or (bad != "/" and real.startswith(bad + "/")):
            return "Please pick a photo folder, not a system folder."
    return None


def scan_folder(folder, recursive):
    """List photos in a folder. Bounded in time and count, never raises."""
    found = []
    if folder_problem(folder):
        return found
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    try:
        if recursive:
            # followlinks=False (default) avoids symlink loops; errors are skipped.
            for root, dirs, files in os.walk(folder, onerror=lambda e: None):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                found += [os.path.join(root, f) for f in files
                          if not f.startswith(".") and os.path.splitext(f)[1].lower() in EXTS]
                if len(found) >= MAX_SCAN_FILES or time.monotonic() > deadline:
                    log.warning("scan stopped early at %d photos (limit reached)", len(found))
                    break
        else:
            with os.scandir(folder) as it:
                for e in it:
                    if (not e.name.startswith(".")
                            and os.path.splitext(e.name)[1].lower() in EXTS):
                        found.append(e.path)
                        if len(found) >= MAX_SCAN_FILES:
                            break
    except OSError as e:
        log.warning("scan failed: %s", e)
    found = found[:MAX_SCAN_FILES]
    found.sort()
    return found


def set_autostart(enabled):
    if enabled:
        atomic_write(AUTOSTART_PATH,
                     "[Desktop Entry]\nType=Application\nName=PhotoFrame\n"
                     f"Exec=python3 \"{SCRIPT}\"\n"
                     "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=10\n")
    elif os.path.exists(AUTOSTART_PATH):
        os.remove(AUTOSTART_PATH)


def battery_low(threshold=BATTERY_LOW_PERCENT):
    """True when running on battery and the charge is at or below threshold."""
    caps, discharging = [], False
    for d in glob.glob("/sys/class/power_supply/*"):
        def rd(name):
            try:
                with open(os.path.join(d, name)) as f:
                    return f.read().strip()
            except OSError:
                return ""
        if rd("type") != "Battery" or rd("scope") == "Device":  # skip mouse/keyboard batteries
            continue
        if rd("status") == "Discharging":
            discharging = True
        try:
            caps.append(int(rd("capacity")))
        except ValueError:
            pass
    return bool(discharging and caps and sum(caps) / len(caps) <= threshold)


def restore_legacy_gsettings():
    """Older versions of PhotoFrame changed GNOME power settings and only put them
    back on a clean exit. If such a saved copy is still lying around, restore it."""
    try:
        with open(LEGACY_STATE_PATH) as f:
            originals = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        originals = None
    if not isinstance(originals, dict) or not originals:
        # Saved copy is unreadable: fall back to Ubuntu's defaults.
        originals = {f"{s} {k}": None for s, k in (
            ("org.gnome.desktop.session", "idle-delay"),
            ("org.gnome.desktop.screensaver", "idle-activation-enabled"),
            ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type"),
            ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-battery-type"),
            ("org.gnome.settings-daemon.plugins.power", "idle-dim"))}
    if shutil.which("gsettings"):
        for name, val in originals.items():
            schema, key = name.split(" ", 1)
            if val is None:
                run(["gsettings", "reset", schema, key])
            else:
                run(["gsettings", "set", schema, key, val])
        log.info("restored power settings left behind by an older version")
    try:
        os.remove(LEGACY_STATE_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- images
try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except ImportError:
    Image = None
else:
    # We do our own (stricter) size check after reduced-size decoding, so let
    # Pillow open big JPEGs; it still refuses truly absurd ones.
    Image.MAX_IMAGE_PIXELS = 250_000_000
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    _T = getattr(Image, "Transpose", Image)
    _ORIENT = {2: _T.FLIP_LEFT_RIGHT, 3: _T.ROTATE_180, 4: _T.FLIP_TOP_BOTTOM,
               5: _T.TRANSPOSE, 6: _T.ROTATE_270, 7: _T.TRANSVERSE, 8: _T.ROTATE_90}


class PhotoTooBig(Exception):
    pass


def _resize(img, size, method):
    try:
        return img.resize(size, method, reducing_gap=2.0)
    except TypeError:  # very old Pillow
        return img.resize(size, method)


def render_photo(path, W, H, fit="fit", background="blur"):
    """Decode a photo into a W x H RGB frame, with memory-use guard rails."""
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise PhotoTooBig(f"file larger than {MAX_FILE_BYTES // 2**20} MB")
    side = max(W, H)  # photo may be rotated by EXIF, so allow for either orientation
    with Image.open(path) as img:
        img.draft("RGB", (side, side))  # JPEG: decode at reduced size (huge saving)
        iw, ih = img.size
        if iw * ih > MAX_DECODE_PIXELS:
            raise PhotoTooBig(f"{iw}x{ih} is too large to decode safely")
        try:
            orientation = int(img.getexif().get(0x0112, 1))
        except Exception:
            orientation = 1
        img.load()
        # Shrink cheaply right away if still far bigger than the screen.
        factor = min(iw, ih) // side
        if factor >= 2 and hasattr(img, "reduce"):
            img = img.reduce(factor)
        img = img.convert("RGB")
    op = _ORIENT.get(orientation)
    if op is not None:
        img = img.transpose(op)
    if fit == "fill":
        return ImageOps.fit(img, (W, H), Image.BICUBIC)
    iw, ih = img.size
    scale = min(W / iw, H / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    fg = _resize(img, (nw, nh), Image.BICUBIC)
    if background == "blur" and (nw < W - 4 or nh < H - 4):
        small = ImageOps.fit(img, (max(1, W // 20), max(1, H // 20)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(2))
        base = ImageEnhance.Brightness(small.resize((W, H), Image.BILINEAR)).enhance(0.45)
    else:
        base = Image.new("RGB", (W, H), "black")
    base.paste(fg, ((W - nw) // 2, (H - nh) // 2))
    return base


# --------------------------------------------------------------------------- keep awake
class KeepAwake:
    """Stops screen blanking and suspend while running.

    Uses inhibitors that the system releases by itself when this process dies,
    so nothing is left behind after a crash. Nothing permanent is changed."""

    def __init__(self):
        self.wanted = False        # user setting
        self.battery_hold = False  # released because battery is low
        self.bus = None
        self.cookie = None
        self.proc = None
        self.xset_done = False
        self.last_poke = 0

    @property
    def active(self):
        return self.cookie is not None or self.proc is not None

    def set_wanted(self, wanted):
        self.wanted = wanted
        self._sync()

    def set_battery_low(self, low):
        if low != self.battery_hold:
            self.battery_hold = low
            log.info("battery low - allowing sleep" if low else "on power - keeping awake")
            self._sync()

    def _sync(self):
        if self.wanted and not self.battery_hold:
            if not self.active:
                self._acquire()
        elif self.active:
            self.release()

    def _acquire(self):
        if self._gnome_inhibit():
            log.info("keep-awake: GNOME inhibitor")
            return
        # Fallback for non-GNOME desktops. `tail --pid` exits as soon as we die,
        # so the inhibitor can never outlive PhotoFrame.
        if shutil.which("systemd-inhibit") and shutil.which("tail"):
            try:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", f"--who={APP}",
                     "--why=Showing photos", "--mode=block",
                     "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
                log.info("keep-awake: systemd-inhibit")
            except OSError:
                self.proc = None
        if os.environ.get("XDG_SESSION_TYPE") == "x11" and shutil.which("xset"):
            run(["xset", "s", "off"])
            run(["xset", "-dpms"])
            self.xset_done = True

    def _gnome_inhibit(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
            self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            r = self.bus.call_sync(
                "org.gnome.SessionManager", "/org/gnome/SessionManager",
                "org.gnome.SessionManager", "Inhibit",
                GLib.Variant("(susu)", (APP, 0, "Showing photos", 4 | 8)),  # suspend | idle
                GLib.VariantType("(u)"), 0, 3000, None)
            self.cookie = r.unpack()[0]
            return True
        except Exception as e:  # not GNOME, no python3-gi, D-Bus down...
            log.info("GNOME inhibitor unavailable: %s", e)
            self.cookie = None
            return False

    def poke(self):
        """Fallback mode only: nudge X screensavers now and then."""
        if self.proc is None or time.monotonic() - self.last_poke < 50:
            return
        self.last_poke = time.monotonic()
        if os.environ.get("XDG_SESSION_TYPE") == "x11" and shutil.which("xset"):
            run(["xset", "s", "reset"])

    def release(self):
        if self.cookie is not None:
            try:
                from gi.repository import GLib
                self.bus.call_sync(
                    "org.gnome.SessionManager", "/org/gnome/SessionManager",
                    "org.gnome.SessionManager", "Uninhibit",
                    GLib.Variant("(u)", (self.cookie,)), None, 0, 3000, None)
            except Exception as e:
                log.info("uninhibit failed (released on exit anyway): %s", e)
            self.cookie = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self.xset_done:
            run(["xset", "s", "on"])
            run(["xset", "+dpms"])
            self.xset_done = False


# --------------------------------------------------------------------------- GUI
try:
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter import font as tkfont
    from PIL import ImageTk
except ImportError:
    tk = None


if tk is not None:
    # ---- look & feel of the settings window (dark, to match the slideshow)
    T = dict(bg="#15171c", card="#1e2128", line="#2c303a", text="#e8eaed",
             muted="#9aa0a6", accent="#4f8cff", accent_hi="#6d9fff", input="#111318",
             off="#3a3f4b", off_hi="#4a505d", ok="#5bd08a", error="#ff7b72")
    INTERVALS = [3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 300, 600, 1800, 3600]

    def fmt_interval(sec):
        if sec < 60:
            return f"{sec} sec"
        m, s = divmod(sec, 60)
        return f"{m} min" if not s else f"{m} min {s} s"

    def ui_font_family():
        have = set(tkfont.families())
        for fam in ("Ubuntu Sans", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans"):
            if fam in have:
                return fam
        return "Sans"

    def ui_scale(family):
        """Pixel scale factor, so padding and switches grow with HiDPI fonts."""
        ls = tkfont.Font(family=family, size=10).metrics("linespace")
        return max(1.0, min(4.0, ls / 17))

    def flat_button(parent, text, command, s, font, primary=False):
        bg, hi = (T["accent"], T["accent_hi"]) if primary else (T["off"], T["off_hi"])
        b = tk.Button(parent, text=text, command=command, font=font, fg="white", bg=bg,
                      activebackground=hi, activeforeground="white", disabledforeground=T["muted"],
                      relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                      padx=int(16 * s), pady=int(6 * s))
        b.bind("<Enter>", lambda e: b.cget("state") != "disabled" and b.config(bg=hi))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    class Toggle(tk.Canvas):
        """An on/off switch."""

        def __init__(self, parent, var, s, command=None):
            self.w, self.h = int(42 * s), int(24 * s)
            super().__init__(parent, width=self.w, height=self.h, bg=T["card"], bd=0,
                             highlightthickness=0, cursor="hand2", takefocus=1)
            self.var, self.s, self.command = var, s, command
            for ev in ("<Button-1>", "<space>", "<Return>"):
                self.bind(ev, self.toggle)
            self.bind("<FocusIn>", lambda e: self.draw())
            self.bind("<FocusOut>", lambda e: self.draw())
            var.trace_add("write", lambda *a: self.draw())
            self.draw()

        def toggle(self, _e=None):
            self.var.set(not self.var.get())
            if self.command:
                self.command()
            return "break"

        def draw(self):
            self.delete("all")
            on, w, h = bool(self.var.get()), self.w, self.h
            col = T["accent"] if on else T["off"]
            self.create_oval(0, 0, h, h, fill=col, outline=col)
            self.create_oval(w - h, 0, w, h, fill=col, outline=col)
            self.create_rectangle(h / 2, 0, w - h / 2, h, fill=col, outline=col)
            pad = max(2, int(3 * self.s))
            x = w - h if on else 0
            focused = self.focus_get() is self
            self.create_oval(x + pad, pad, x + h - pad, h - pad, fill="white",
                             outline=T["accent_hi"] if focused and not on else "",
                             width=max(1, int(self.s)))

    class Segmented(tk.Frame):
        """A row of mutually exclusive choices (replaces radio buttons)."""

        def __init__(self, parent, var, options, s, font, command=None):
            super().__init__(parent, bg=T["line"], padx=1, pady=1, takefocus=1,
                             highlightthickness=0)
            self.var, self.values, self.command = var, [v for v, _ in options], command
            self.enabled, self.items = True, {}
            for i, (value, label) in enumerate(options):
                lb = tk.Label(self, text=label, font=font, padx=int(12 * s), pady=int(4 * s),
                              cursor="hand2")
                lb.grid(row=0, column=i, padx=(1 if i else 0, 0))
                lb.bind("<Button-1>", lambda e, v=value: self.pick(v))
                self.items[value] = lb
            self.bind("<Left>", lambda e: self.move(-1))
            self.bind("<Right>", lambda e: self.move(+1))
            self.bind("<FocusIn>", lambda e: self.config(bg=T["accent_hi"]))
            self.bind("<FocusOut>", lambda e: self.config(bg=T["line"]))
            var.trace_add("write", lambda *a: self.draw())
            self.draw()

        def pick(self, value):
            if self.enabled:
                self.var.set(value)
                if self.command:
                    self.command()

        def move(self, d):
            i = self.values.index(self.var.get()) if self.var.get() in self.values else 0
            self.pick(self.values[max(0, min(len(self.values) - 1, i + d))])

        def set_enabled(self, on):
            self.enabled = on
            self.draw()

        def draw(self):
            for value, lb in self.items.items():
                sel = value == self.var.get()
                if not self.enabled:
                    lb.config(bg=T["line"] if sel else T["card"], fg=T["off_hi"], cursor="")
                else:
                    lb.config(bg=T["accent"] if sel else T["input"],
                              fg="white" if sel else T["muted"], cursor="hand2")

    class Stepper(tk.Frame):
        """[ − ]  10 sec  [ + ]  for the time per photo."""

        def __init__(self, parent, var, s, font):
            super().__init__(parent, bg=T["card"])
            self.var = var
            self.values = sorted(set(INTERVALS) | {int(var.get())})
            self.minus = flat_button(self, "−", lambda: self.move(-1), s, font)
            self.label = tk.Label(self, font=font, fg=T["text"], bg=T["card"],
                                  width=7, anchor="center")
            self.plus = flat_button(self, "+", lambda: self.move(+1), s, font)
            for b in (self.minus, self.plus):
                b.config(padx=int(10 * s), pady=int(2 * s))
            self.minus.pack(side="left")
            self.label.pack(side="left")
            self.plus.pack(side="left")
            self.draw()

        def move(self, d):
            i = self.values.index(int(self.var.get())) + d
            self.var.set(self.values[max(0, min(len(self.values) - 1, i))])
            self.draw()

        def draw(self):
            v = int(self.var.get())
            self.label.config(text=fmt_interval(v))
            self.minus.config(state="disabled" if v <= self.values[0] else "normal")
            self.plus.config(state="disabled" if v >= self.values[-1] else "normal")

    class SettingsDialog(tk.Toplevel):
        def __init__(self, app):
            super().__init__(app.root)
            self.app = app
            self.picker = None
            self._scan_job = None
            self._scan_token = 0
            self._scan_result = None
            cfg = app.cfg
            fam = ui_font_family()
            s = self.s = ui_scale(fam)
            self.f_title = tkfont.Font(family=fam, size=15, weight="bold")
            self.f_body = tkfont.Font(family=fam, size=11)
            self.f_small = tkfont.Font(family=fam, size=9)
            self.f_section = tkfont.Font(family=fam, size=9, weight="bold")
            px = self.px = lambda v: int(v * s)

            self.title(f"{APP} settings")
            self.configure(bg=T["bg"], padx=px(24), pady=px(20))
            self.resizable(False, False)
            self.transient(app.root)
            self.attributes("-topmost", True)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.v_folder = tk.StringVar(value=cfg["folder"])
            self.v_recursive = tk.BooleanVar(value=cfg["recursive"])
            self.v_shuffle = tk.BooleanVar(value=cfg["shuffle"])
            self.v_interval = tk.IntVar(value=cfg["interval"])
            self.v_fit = tk.StringVar(value=cfg["fit"])
            self.v_bg = tk.StringVar(value=cfg["background"])
            self.v_fade = tk.BooleanVar(value=cfg["fade"])
            self.v_clock = tk.BooleanVar(value=cfg["show_clock"])
            self.v_name = tk.BooleanVar(value=cfg["show_name"])
            self.v_awake = tk.BooleanVar(value=cfg["keep_awake"])
            self.v_autostart = tk.BooleanVar(value=os.path.exists(AUTOSTART_PATH))
            self.v_status = tk.StringVar()
            # Last folder (+subfolder flag) confirmed to contain photos; only this
            # is applied to the slideshow, so a half-typed path never blanks it.
            self._good = (cfg["folder"], cfg["recursive"]) if app.photos else None
            self._apply_job = None

            # Header
            tk.Label(self, text="Settings", font=self.f_title, fg=T["text"], bg=T["bg"])\
                .grid(row=0, column=0, columnspan=2, sticky="w")
            tk.Label(self, text="Changes apply right away and are saved automatically.",
                     font=self.f_small, fg=T["muted"], bg=T["bg"])\
                .grid(row=1, column=0, columnspan=2, sticky="w")

            left = tk.Frame(self, bg=T["bg"])
            right = tk.Frame(self, bg=T["bg"])
            left.grid(row=2, column=0, sticky="nsew", padx=(0, px(10)))
            right.grid(row=2, column=1, sticky="nsew", padx=(px(10), 0))

            # Photos
            c = self._card(left, "Photos")
            box = tk.Frame(c, bg=T["card"])
            box.grid(row=0, column=0, columnspan=2, sticky="ew",
                     padx=px(16), pady=(px(12), px(10)))
            box.columnconfigure(0, weight=1)
            tk.Label(box, text="Folder", font=self.f_body, fg=T["text"], bg=T["card"])\
                .grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(6)))
            self.entry = tk.Entry(box, textvariable=self.v_folder, font=self.f_body, width=30,
                                  fg=T["text"], bg=T["input"], insertbackground=T["text"],
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=T["line"], highlightcolor=T["accent"])
            self.entry.grid(row=1, column=0, sticky="ew", ipady=px(4))
            self.browse_btn = flat_button(box, "Browse…", self.browse, s, self.f_body)
            self.browse_btn.grid(row=1, column=1, padx=(px(8), 0), sticky="ns")
            self.status = tk.Label(box, textvariable=self.v_status, font=self.f_small,
                                   fg=T["muted"], bg=T["card"], anchor="w", justify="left",
                                   wraplength=px(330))
            self.status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
            c.rows = 1
            self._toggle_row(c, "Include subfolders", self.v_recursive,
                             command=self._folder_changed)
            self._toggle_row(c, "Shuffle", self.v_shuffle, hint="Show photos in random order")

            # On screen
            c = self._card(left, "On screen")
            self._toggle_row(c, "Clock and date", self.v_clock)
            self._toggle_row(c, "File name", self.v_name)

            # Slideshow
            c = self._card(right, "Slideshow")
            self._row(c, "Time per photo", Stepper(c, self.v_interval, s, self.f_body))
            self._row(c, "Photo size", Segmented(
                c, self.v_fit, [("fit", "Whole photo"), ("fill", "Fill screen")], s,
                self.f_body, command=self._fit_changed))
            self.bg_seg = Segmented(c, self.v_bg, [("blur", "Blurred"), ("black", "Black")],
                                    s, self.f_body)
            self._row(c, "Background", self.bg_seg, hint="Around photos that don't fill the screen")
            self._toggle_row(c, "Fade between photos", self.v_fade)

            # Power
            c = self._card(right, "Power")
            self._toggle_row(c, "Keep screen on", self.v_awake,
                             hint=f"Sleep is allowed again below {BATTERY_LOW_PERCENT}% battery")
            self._toggle_row(c, "Start when I log in", self.v_autostart)

            # Footer
            foot = tk.Frame(self, bg=T["bg"])
            foot.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(px(20), 0))
            tk.Label(foot, font=self.f_small, fg=T["muted"], bg=T["bg"], justify="left",
                     text="While running:  S settings  ·  ←/→ browse  ·  Space pause  ·  Esc quit")\
                .pack(side="left")
            self.start_btn = flat_button(foot, "Done", self.on_start, s,
                                         self.f_body, primary=True)
            self.start_btn.pack(side="right")
            flat_button(foot, "Quit", app.quit, s, self.f_body).pack(side="right", padx=px(8))

            self.bind("<Return>", lambda e: self.on_start())
            self.bind("<KP_Enter>", lambda e: self.on_start())
            self.bind("<Escape>", lambda e: self.on_close())
            self.v_folder.trace_add("write", lambda *a: self._folder_changed())
            for v in (self.v_shuffle, self.v_interval, self.v_fit, self.v_bg, self.v_fade,
                      self.v_clock, self.v_name, self.v_awake, self.v_autostart):
                v.trace_add("write", lambda *a: self._schedule_apply())
            self._fit_changed()
            self._folder_changed(delay=0)

            self.update_idletasks()
            x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
            y = max(0, (self.winfo_screenheight() - self.winfo_reqheight()) // 3)
            self.geometry(f"+{x}+{y}")
            self.lift()
            self.focus_force()
            self.start_btn.focus_set()
            # No grab_set(): a grab on a window hidden behind the fullscreen
            # slideshow would make the whole app look frozen with no way out.
            self._keep_visible()

        # ---- layout helpers
        def _card(self, parent, title):
            px = self.px
            first = not parent.winfo_children()
            tk.Label(parent, text=title.upper(), font=self.f_section, fg=T["muted"], bg=T["bg"])\
                .pack(anchor="w", pady=(px(14) if first else px(18), px(6)))
            c = tk.Frame(parent, bg=T["card"], highlightthickness=1,
                         highlightbackground=T["line"], highlightcolor=T["line"])
            c.pack(fill="x")
            c.columnconfigure(0, weight=1)
            c.rows = 0
            return c

        def _row(self, card, label, control, hint=None):
            px = self.px
            if card.rows:
                tk.Frame(card, height=1, bg=T["line"]).grid(
                    row=card.rows * 2 - 1, column=0, columnspan=2, sticky="ew", padx=px(16))
            r = card.rows * 2
            box = tk.Frame(card, bg=T["card"])
            box.grid(row=r, column=0, sticky="w", padx=(px(16), px(12)), pady=px(9))
            lab = tk.Label(box, text=label, font=self.f_body, fg=T["text"], bg=T["card"])
            lab.pack(anchor="w")
            parts = [box, lab]
            if hint:
                h = tk.Label(box, text=hint, font=self.f_small, fg=T["muted"], bg=T["card"])
                h.pack(anchor="w")
                parts.append(h)
            control.grid(row=r, column=1, sticky="e", padx=(0, px(16)))
            card.rows += 1
            return parts

        def _toggle_row(self, card, label, var, hint=None, command=None):
            t = Toggle(card, var, self.s, command=command)
            for w in self._row(card, label, t, hint):  # clicking the text toggles too
                w.config(cursor="hand2")
                w.bind("<Button-1>", t.toggle)
            return t

        def _fit_changed(self):
            self.bg_seg.set_enabled(self.v_fit.get() == "fit")

        # ---- live folder check (runs in a thread so big folders never freeze the UI)
        def _set_status(self, text, kind="muted"):
            self.v_status.set(text)
            self.status.config(fg=T[kind])

        def _folder_changed(self, delay=400):
            if self._scan_job:
                self.after_cancel(self._scan_job)
            self._set_status("Checking folder…")
            self._scan_job = self.after(delay, self._start_scan)

        def _start_scan(self):
            self._scan_job = None
            folder = os.path.expanduser(self.v_folder.get().strip())
            recursive = self.v_recursive.get()
            self._scan_token += 1
            token = self._scan_token

            def work():
                problem = folder_problem(folder)
                n = 0 if problem else len(scan_folder(folder, recursive))
                self._scan_result = (token, problem, n, folder, recursive)

            threading.Thread(target=work, daemon=True).start()
            self.after(100, self._poll_scan)

        def _poll_scan(self):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            r = self._scan_result
            if r is None or r[0] != self._scan_token:
                self.after(100, self._poll_scan)
                return
            _, problem, n, folder, recursive = r
            keep = "  ·  still showing the previous folder" if self._good else ""
            if problem:
                self._set_status(problem + keep, "error")
            elif n == 0:
                self._set_status("No photos in this folder (.jpg, .png, .webp…)" + keep, "error")
            else:
                more = "+" if n >= MAX_SCAN_FILES else ""
                self._set_status(f"✓  {n:,}{more} photo{'s' if n != 1 else ''} found", "ok")
                if self._good != (folder, recursive):
                    self._good = (folder, recursive)
                    self._schedule_apply()

        # ---- live apply: every change shows on the slideshow behind right away
        def _schedule_apply(self):
            if self._apply_job:
                self.after_cancel(self._apply_job)
            self._apply_job = self.after(150, self._apply_live)

        def _collect(self):
            folder, recursive = self._good or (self.app.cfg["folder"], self.app.cfg["recursive"])
            return validate_config(dict(
                folder=folder, recursive=recursive, shuffle=self.v_shuffle.get(),
                interval=self.v_interval.get(), fit=self.v_fit.get(),
                background=self.v_bg.get(), fade=self.v_fade.get(),
                show_clock=self.v_clock.get(), show_name=self.v_name.get(),
                keep_awake=self.v_awake.get(),
                rescan_minutes=self.app.cfg.get("rescan_minutes")))

        def _apply_live(self):
            if self._apply_job:
                try:
                    self.after_cancel(self._apply_job)
                except tk.TclError:
                    pass
                self._apply_job = None
            new = self._collect()
            if new != self.app.cfg:
                self.app.apply_settings(new)
                save_config(new)
            if self.v_autostart.get() != os.path.exists(AUTOSTART_PATH):
                try:
                    set_autostart(self.v_autostart.get())
                except OSError as e:
                    log.warning("autostart change failed: %s", e)

        def _keep_visible(self):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if self.picker is None:
                self.lift()
            self.after(1500, self._keep_visible)

        # Folder picker runs as a separate process and is polled, so the app
        # never freezes waiting for it (and it can't hide behind us).
        def browse(self):
            if self.picker is not None:
                return
            start = self.v_folder.get() or HOME
            if shutil.which("zenity"):
                try:
                    self.picker = subprocess.Popen(
                        ["zenity", "--file-selection", "--directory",
                         "--title=Choose photo folder", f"--filename={start}/"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                except OSError:
                    self.picker = None
            if self.picker is None:
                self.attributes("-topmost", False)
                path = filedialog.askdirectory(parent=self, initialdir=start,
                                               title="Choose photo folder")
                self.attributes("-topmost", True)
                if path:
                    self.v_folder.set(path)
                return
            self.attributes("-topmost", False)
            self.browse_btn.config(state="disabled")
            self.picker_started = time.monotonic()
            self.after(200, self._poll_picker)

        def _poll_picker(self):
            if self.picker is None or not self.winfo_exists():
                return
            if self.picker.poll() is None:
                if time.monotonic() - self.picker_started > 600:  # abandoned
                    self._kill_picker()
                else:
                    self.after(200, self._poll_picker)
                return
            out = self.picker.stdout.read().strip() if self.picker.returncode == 0 else ""
            self.picker = None
            self.browse_btn.config(state="normal")
            self.attributes("-topmost", True)
            if out:
                self.v_folder.set(out)

        def _kill_picker(self):
            if self.picker is not None:
                try:
                    self.picker.kill()
                    self.picker.wait(timeout=2)
                except Exception:
                    pass
                self.picker = None
            try:
                self.browse_btn.config(state="normal")
                self.attributes("-topmost", True)
            except tk.TclError:
                pass

        def on_close(self):
            """Esc / window close: never trap the user. Close if the slideshow has
            photos (keeping the last folder that worked), otherwise quit."""
            if self.on_start():
                return
            if self.app.photos:
                self._apply_live()
                self.destroy()
                self.app.settings_closed()
            else:
                self.app.quit()

        def on_start(self):
            """Done: the folder in the box must be valid, then close."""
            folder = os.path.expanduser(self.v_folder.get().strip())
            recursive = self.v_recursive.get()
            if self._good != (folder, recursive):
                problem = folder_problem(folder)
                if not problem and not scan_folder(folder, recursive):
                    problem = "No photos in this folder (.jpg, .png, .webp…)"
                if problem:
                    self._set_status(problem, "error")
                    self.entry.focus_set()
                    return False
                self._good = (folder, recursive)
            self._apply_live()
            self._kill_picker()
            self.destroy()
            self.app.settings_closed()
            return True

        def destroy(self):
            self._kill_picker()
            super().destroy()

    class PhotoFrame:
        def __init__(self):
            self.cfg = load_config()
            self.root = tk.Tk(className="photoframe")
            self.root.report_callback_exception = self._on_error
            self.root.title(APP)
            self.root.configure(bg="black", cursor="none")
            self.root.attributes("-fullscreen", True)
            self.W = max(320, self.root.winfo_screenwidth())
            self.H = max(240, self.root.winfo_screenheight())

            self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self.img_item = self.canvas.create_image(self.W // 2, self.H // 2, anchor="center")
            # Fonts are measured so the date sits just above the clock at any
            # screen resolution / display scaling (fixed offsets overlapped on HiDPI).
            clock_font = tkfont.Font(family="Sans", size=40, weight="bold")
            small_font = tkfont.Font(family="Sans", size=16)
            name_font = tkfont.Font(family="Sans", size=13)
            m = 40                                               # margin from screen edge
            clock_y = self.H - m
            date_y = clock_y - clock_font.metrics("linespace")  # bottom of date = top of clock
            self.clock_sh = self._text(self.W - m + 2, clock_y + 2, "se", clock_font, shadow=True)
            self.clock = self._text(self.W - m, clock_y, "se", clock_font)
            self.date_sh = self._text(self.W - m + 2, date_y + 2, "se", small_font, shadow=True)
            self.date = self._text(self.W - m, date_y, "se", small_font)
            self.name_sh = self._text(m + 2, self.H - m + 2, "sw", name_font, shadow=True)
            self.name = self._text(m, self.H - m, "sw", name_font)
            self.status = self._text(self.W // 2, m, "n", tkfont.Font(family="Sans", size=15))
            self.fonts = (clock_font, small_font, name_font)  # keep references alive

            self.keep_awake = KeepAwake()
            self.photos, self.order, self.pos = [], [], -1
            self.bad = set()             # files that failed to load
            self.history, self.hist_pos = [], -1
            self.frame = None
            self.tkimg = None
            self.paused = False
            self.settings = None
            self.next_job = self.fade_job = self.status_job = None
            self.last_scan = 0
            self.closing = False
            self.stop_signal = None
            self.errors = []             # timestamps of recent callback errors
            self.last_battery_check = self.last_mem_check = 0

            r = self.root
            for k in ("<s>", "<S>", "<Return>", "<Button-3>"):
                r.bind(k, lambda e: self.open_settings())
            r.bind("<Right>", lambda e: self.step(+1))
            r.bind("<Left>", lambda e: self.step(-1))
            r.bind("<space>", lambda e: self.toggle_pause())
            for k in ("<Escape>", "<q>", "<Q>"):
                r.bind(k, lambda e: self.quit())
            r.bind_all("<Control-q>", lambda e: self.quit())  # works in every window
            r.protocol("WM_DELETE_WINDOW", self.quit)

            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(sig, self._on_signal)

            self._clock_tick()
            self._heartbeat()
            self.root.after(200, self.boot)

        def _text(self, x, y, anchor, font, shadow=False):
            return self.canvas.create_text(x, y, anchor=anchor, text="",
                                           fill="black" if shadow else "white", font=font)

        # ---- safety
        def _on_error(self, exc, val, tb):
            log.error("unexpected error", exc_info=(exc, val, tb))
            now = time.monotonic()
            self.errors = [t for t in self.errors if now - t < 60] + [now]
            if len(self.errors) > 30:  # something is badly wrong: stop rather than spin
                log.error("too many errors in a minute - quitting")
                self.quit()

        def _on_signal(self, signum, _frame):
            # Only record it; the heartbeat does the actual shutdown on the Tk thread.
            self.stop_signal = signum

        def _heartbeat(self):
            """Runs twice a second: lets signals through and checks battery/memory."""
            try:
                if self.stop_signal is not None:
                    log.info("got signal %s - quitting", self.stop_signal)
                    self.quit()
                    return
                now = time.monotonic()
                if now - self.last_battery_check > 60:
                    self.last_battery_check = now
                    low = battery_low()
                    if low and not self.keep_awake.battery_hold and self.keep_awake.wanted:
                        self.flash(f"Battery below {BATTERY_LOW_PERCENT}% - "
                                   "the laptop may now go to sleep", 15000)
                    self.keep_awake.set_battery_low(low)
                self.keep_awake.poke()
                if now - self.last_mem_check > 300:
                    self.last_mem_check = now
                    rss = rss_bytes()
                    log.info("memory in use: %d MB", rss // 2**20)
                    if rss > RSS_RESTART:
                        self.restart(f"memory use {rss // 2**20} MB")
                        return
            finally:
                if not self.closing:
                    self.root.after(HEARTBEAT_MS, self._heartbeat)

        def restart(self, reason):
            last = float(os.environ.get("PHOTOFRAME_LAST_RESTART", "0") or 0)
            if time.time() - last < 3600:
                log.error("%s again within an hour - quitting instead of restarting", reason)
                self.quit()
                return
            log.warning("%s - restarting cleanly", reason)
            self.quit()
            os.environ["PHOTOFRAME_LAST_RESTART"] = str(time.time())
            os.execv(sys.executable, [sys.executable, SCRIPT])

        # ---- lifecycle
        def boot(self):
            if scan_folder(self.cfg["folder"], self.cfg["recursive"]):
                self.start()
                self.flash("S = settings   ←/→ = navigate   Space = pause   Esc = quit", 6000)
            else:
                self.open_settings()

        def start(self):
            self.rescan(reset=True)
            self.keep_awake.set_wanted(self.cfg["keep_awake"])
            self.paused = False
            self.step(+1)

        def quit(self):
            if self.closing:
                return
            self.closing = True
            try:
                self.keep_awake.release()
            except Exception:
                log.exception("releasing keep-awake failed")
            try:
                self.root.destroy()
            except tk.TclError:
                pass

        def open_settings(self):
            if self.settings is not None:
                try:
                    if self.settings.winfo_exists():
                        self.settings.lift()
                        return
                except tk.TclError:
                    pass
            self._cancel_next()
            self.root.configure(cursor="")
            self.settings = SettingsDialog(self)

        def settings_closed(self):
            """Settings were already applied live; just resume the slideshow."""
            self.settings = None
            self.root.configure(cursor="none")
            self.root.focus_force()
            if self._current_path() is None:
                self.start()
            else:
                self.keep_awake.set_wanted(self.cfg["keep_awake"])
                self.paused = False
                self._schedule_next()

        def apply_settings(self, new):
            """Apply changed settings immediately, redrawing only what's needed."""
            old, self.cfg = self.cfg, new
            self.keep_awake.set_wanted(new["keep_awake"])
            if ((old["folder"], old["recursive"]) != (new["folder"], new["recursive"])
                    or self._current_path() is None):
                self.history, self.hist_pos = [], -1
                self.bad.clear()
                self.frame = None
                self.rescan(reset=True)
                self.step(+1, preview=True)  # show the new folder's first photo now
                return
            if old["shuffle"] != new["shuffle"]:
                self._new_order()
            if (old["fit"], old["background"]) != (new["fit"], new["background"]):
                self._rerender_current()
            if old["show_name"] != new["show_name"]:
                self._update_name(self._current_path())
            self._update_clock()

        def _current_path(self):
            if 0 <= self.hist_pos < len(self.history):
                return self.history[self.hist_pos]
            return None

        def _rerender_current(self):
            path = self._current_path()
            if path is None:
                return
            try:
                frame = render_photo(path, self.W, self.H, self.cfg["fit"], self.cfg["background"])
            except Exception as e:
                log.warning("re-render of %s failed: %s", path, e)
                return
            if self.fade_job:
                self.root.after_cancel(self.fade_job)
                self.fade_job = None
            self.frame = frame
            self._put(frame)

        # ---- playlist
        def rescan(self, reset=False):
            self.last_scan = time.monotonic()
            photos = scan_folder(self.cfg["folder"], self.cfg["recursive"])
            if not reset and set(photos) == set(self.photos):
                return
            self.photos = photos
            self._new_order()

        def _new_order(self, avoid=None):
            self.order = [p for p in self.photos if p not in self.bad]
            if self.cfg["shuffle"]:
                random.shuffle(self.order)
                if avoid and len(self.order) > 1 and self.order[0] == avoid:
                    self.order.append(self.order.pop(0))  # don't repeat back-to-back
            self.pos = -1

        def _next_path(self):
            if time.monotonic() - self.last_scan > self.cfg["rescan_minutes"] * 60:
                self.rescan()
            for _ in range(2):
                while self.pos + 1 < len(self.order):
                    self.pos += 1
                    if self.order[self.pos] not in self.bad:
                        return self.order[self.pos]
                last = self.order[-1] if self.order else None
                self._new_order(avoid=last)
            return None

        def step(self, direction, preview=False):
            # While settings are open the slideshow is paused; preview=True still
            # draws a photo (used when the folder is changed from settings).
            if self.closing or (self.settings is not None and not preview):
                return
            self._cancel_next()
            for _ in range(20):  # skip unreadable files
                if direction < 0:
                    if self.hist_pos <= 0:
                        break
                    self.hist_pos -= 1
                    path = self.history[self.hist_pos]
                elif self.hist_pos < len(self.history) - 1:
                    self.hist_pos += 1
                    path = self.history[self.hist_pos]
                else:
                    path = self._next_path()
                    if not path:
                        # Folder vanished (USB unplugged?) - keep retrying, never stall.
                        self.flash("No photos found - press S to choose a folder", 0)
                        self.last_scan = 0
                        self.bad.clear()  # give failed files another chance next time
                        self.next_job = self.root.after(60_000, lambda: self.step(+1))
                        return
                    self.history.append(path)
                    self.history = self.history[-200:]
                    self.hist_pos = len(self.history) - 1
                try:
                    frame = render_photo(path, self.W, self.H,
                                         self.cfg["fit"], self.cfg["background"])
                except MemoryError:
                    log.warning("out of memory on %s - skipping", path)
                    self.frame = None
                    self._skip(path)
                    continue
                except Exception as e:
                    log.warning("skipping %s: %s", path, e)
                    self._skip(path)
                    continue
                self.show(frame, path)
                break
            self._schedule_next()

        def _skip(self, path):
            self.bad.add(path)
            if path in self.history:
                self.history.remove(path)
                self.hist_pos = min(self.hist_pos, len(self.history) - 1)

        def _schedule_next(self):
            self._cancel_next()
            if not self.paused and self.settings is None and not self.closing:
                self.next_job = self.root.after(self.cfg["interval"] * 1000,
                                                lambda: self.step(+1))

        def _cancel_next(self):
            if self.next_job:
                try:
                    self.root.after_cancel(self.next_job)
                except tk.TclError:
                    pass
                self.next_job = None

        def toggle_pause(self):
            if self.settings is not None:
                return
            self.paused = not self.paused
            if self.paused:
                self._cancel_next()
                self.flash("Paused", 0)
            else:
                self.flash("Playing", 1500)
                self._schedule_next()

        # ---- drawing
        def show(self, frame, path):
            if self.fade_job:
                self.root.after_cancel(self.fade_job)
                self.fade_job = None
            prev, self.frame = self.frame, frame
            self._update_name(path)
            if self.cfg["fade"] and prev is not None and prev.size == frame.size:
                self._fade(prev, frame, 1)
            else:
                self._put(frame)

        def _fade(self, a, b, i, steps=8):
            if i >= steps:
                self._put(b)
                self.fade_job = None
                return
            try:
                self._put(Image.blend(a, b, i / steps))
            except MemoryError:  # skip the fade, just show the photo
                self._put(b)
                self.fade_job = None
                return
            self.fade_job = self.root.after(35, lambda: self._fade(a, b, i + 1, steps))

        def _put(self, img):
            self.tkimg = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.img_item, image=self.tkimg)

        def _update_name(self, path):
            name = ""
            if path and self.cfg["show_name"]:
                name = os.path.relpath(path, self.cfg["folder"])
            self.canvas.itemconfigure(self.name, text=name)
            self.canvas.itemconfigure(self.name_sh, text=name)

        def _update_clock(self):
            on = self.cfg["show_clock"]
            t = time.strftime("%H:%M") if on else ""
            d = time.strftime("%A, %d %B") if on else ""
            for item, val in ((self.clock, t), (self.clock_sh, t),
                              (self.date, d), (self.date_sh, d)):
                if self.canvas.itemcget(item, "text") != val:
                    self.canvas.itemconfigure(item, text=val)

        def _clock_tick(self):
            try:
                self._update_clock()
            finally:
                if not self.closing:
                    self.root.after(1000, self._clock_tick)

        def flash(self, msg, ms):
            if self.status_job:
                self.root.after_cancel(self.status_job)
                self.status_job = None
            self.canvas.itemconfigure(self.status, text=msg)
            if ms:
                self.status_job = self.root.after(
                    ms, lambda: self.canvas.itemconfigure(self.status, text=""))


def main():
    setup_logging()
    if tk is None or Image is None:
        sys.stderr.write("Missing dependencies. Install them with:\n"
                         "  sudo apt install python3-tk python3-pil python3-pil.imagetk\n")
        sys.exit(1)
    lock = single_instance_lock()
    if lock is None:
        log.info("PhotoFrame is already running - not starting a second copy")
        sys.exit(0)
    protect_system()
    restore_legacy_gsettings()
    log.info("starting (RAM %d MB, memory cap %d MB, restart above %d MB)",
             total_ram() // 2**20, MEMORY_CAP // 2**20, RSS_RESTART // 2**20)
    app = PhotoFrame()
    try:
        app.root.mainloop()
    except BaseException:
        log.exception("main loop crashed")
        raise
    finally:
        try:
            app.keep_awake.release()
        except Exception:
            pass
        log.info("stopped")


if __name__ == "__main__":
    main()
