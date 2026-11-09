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
    from PIL import ImageTk
except ImportError:
    tk = None


if tk is not None:
    class SettingsDialog(tk.Toplevel):
        def __init__(self, app):
            super().__init__(app.root)
            self.app = app
            self.picker = None
            cfg = app.cfg
            self.title(f"{APP} settings")
            self.configure(padx=20, pady=16)
            self.resizable(False, False)
            self.transient(app.root)
            self.attributes("-topmost", True)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.v_folder = tk.StringVar(value=cfg["folder"])
            self.v_recursive = tk.BooleanVar(value=cfg["recursive"])
            self.v_shuffle = tk.BooleanVar(value=cfg["shuffle"])
            self.v_interval = tk.StringVar(value=str(cfg["interval"]))
            self.v_fit = tk.StringVar(value=cfg["fit"])
            self.v_bg = tk.StringVar(value=cfg["background"])
            self.v_fade = tk.BooleanVar(value=cfg["fade"])
            self.v_clock = tk.BooleanVar(value=cfg["show_clock"])
            self.v_name = tk.BooleanVar(value=cfg["show_name"])
            self.v_awake = tk.BooleanVar(value=cfg["keep_awake"])
            self.v_autostart = tk.BooleanVar(value=os.path.exists(AUTOSTART_PATH))
            self.v_error = tk.StringVar()

            r = 0
            ttk.Label(self, text="Photo folder").grid(row=r, column=0, sticky="w")
            ttk.Entry(self, textvariable=self.v_folder, width=42).grid(row=r, column=1, padx=6)
            self.browse_btn = ttk.Button(self, text="Browse…", command=self.browse)
            self.browse_btn.grid(row=r, column=2)
            r += 1
            ttk.Checkbutton(self, text="Include subfolders", variable=self.v_recursive)\
                .grid(row=r, column=1, sticky="w", pady=(4, 0))
            r += 1
            ttk.Checkbutton(self, text="Shuffle photos", variable=self.v_shuffle)\
                .grid(row=r, column=1, sticky="w")
            r += 1
            ttk.Separator(self).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10)
            r += 1
            ttk.Label(self, text="Seconds per photo").grid(row=r, column=0, sticky="w")
            ttk.Spinbox(self, from_=3, to=3600, textvariable=self.v_interval, width=8)\
                .grid(row=r, column=1, sticky="w", padx=6)
            r += 1
            ttk.Label(self, text="Display").grid(row=r, column=0, sticky="w", pady=(6, 0))
            f = ttk.Frame(self)
            f.grid(row=r, column=1, sticky="w", padx=6, pady=(6, 0))
            ttk.Radiobutton(f, text="Show whole photo", value="fit",
                            variable=self.v_fit).pack(side="left")
            ttk.Radiobutton(f, text="Fill screen (crop)", value="fill",
                            variable=self.v_fit).pack(side="left", padx=10)
            r += 1
            ttk.Label(self, text="Background").grid(row=r, column=0, sticky="w")
            f = ttk.Frame(self)
            f.grid(row=r, column=1, sticky="w", padx=6)
            ttk.Radiobutton(f, text="Blurred photo", value="blur",
                            variable=self.v_bg).pack(side="left")
            ttk.Radiobutton(f, text="Black", value="black",
                            variable=self.v_bg).pack(side="left", padx=10)
            r += 1
            ttk.Checkbutton(self, text="Fade between photos", variable=self.v_fade)\
                .grid(row=r, column=1, sticky="w", pady=(6, 0))
            r += 1
            ttk.Checkbutton(self, text="Show clock and date", variable=self.v_clock)\
                .grid(row=r, column=1, sticky="w")
            r += 1
            ttk.Checkbutton(self, text="Show file name", variable=self.v_name)\
                .grid(row=r, column=1, sticky="w")
            r += 1
            ttk.Separator(self).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10)
            r += 1
            ttk.Checkbutton(self, text="Keep screen on while running "
                                       f"(sleep allowed below {BATTERY_LOW_PERCENT}% battery)",
                            variable=self.v_awake).grid(row=r, column=1, sticky="w")
            r += 1
            ttk.Checkbutton(self, text="Start automatically when I log in",
                            variable=self.v_autostart).grid(row=r, column=1, sticky="w")
            r += 1
            ttk.Label(self, textvariable=self.v_error, foreground="#c0392b")\
                .grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
            r += 1
            ttk.Label(self, foreground="#666", justify="left", text=(
                "While running:  S = settings   ←/→ = previous/next   "
                "Space = pause   Esc = quit")).grid(row=r, column=0, columnspan=3,
                                                    sticky="w", pady=(6, 0))
            r += 1
            b = ttk.Frame(self)
            b.grid(row=r, column=0, columnspan=3, sticky="e", pady=(14, 0))
            ttk.Button(b, text="Quit", command=app.quit).pack(side="right")
            ttk.Button(b, text="Start slideshow", command=self.on_start)\
                .pack(side="right", padx=8)
            self.bind("<Return>", lambda e: self.on_start())
            self.bind("<Escape>", lambda e: self.on_close())

            self.update_idletasks()
            x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
            y = (self.winfo_screenheight() - self.winfo_reqheight()) // 3
            self.geometry(f"+{x}+{y}")
            self.lift()
            self.focus_force()
            # No grab_set(): a grab on a window hidden behind the fullscreen
            # slideshow would make the whole app look frozen with no way out.
            self._keep_visible()

        def _keep_visible(self):
            if not self.winfo_exists():
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
            self.browse_btn.state(["disabled"])
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
            self.browse_btn.state(["!disabled"])
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
                self.browse_btn.state(["!disabled"])
                self.attributes("-topmost", True)
            except tk.TclError:
                pass

        def on_close(self):
            """Esc / window close: start if possible, otherwise never trap the user:
            go back to the running slideshow (discarding changes), or quit."""
            if self.on_start():
                return
            if self.app.photos:
                self.destroy()
                self.app.settings_closed()
            else:
                self.app.quit()

        def on_start(self):
            folder = os.path.expanduser(self.v_folder.get().strip())
            problem = folder_problem(folder)
            if not problem and not scan_folder(folder, self.v_recursive.get()):
                problem = "No photos found in that folder. Pick one with .jpg / .png / .webp images."
            if problem:
                self.v_error.set(problem)
                return False
            self.app.cfg = validate_config(dict(
                folder=folder, recursive=self.v_recursive.get(), shuffle=self.v_shuffle.get(),
                interval=self.v_interval.get(), fit=self.v_fit.get(),
                background=self.v_bg.get(), fade=self.v_fade.get(),
                show_clock=self.v_clock.get(), show_name=self.v_name.get(),
                keep_awake=self.v_awake.get(),
                rescan_minutes=self.app.cfg.get("rescan_minutes")))
            save_config(self.app.cfg)
            try:
                set_autostart(self.v_autostart.get())
            except OSError as e:
                log.warning("autostart change failed: %s", e)
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
            self.clock_sh = self._text(self.W - 38, self.H - 38, "se", 40, shadow=True)
            self.clock = self._text(self.W - 40, self.H - 40, "se", 40)
            self.date_sh = self._text(self.W - 38, self.H - 98, "se", 16, shadow=True)
            self.date = self._text(self.W - 40, self.H - 100, "se", 16)
            self.name_sh = self._text(42, self.H - 38, "sw", 13, shadow=True)
            self.name = self._text(40, self.H - 40, "sw", 13)
            self.status = self._text(self.W // 2, 40, "n", 15)

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

        def _text(self, x, y, anchor, size, shadow=False):
            return self.canvas.create_text(x, y, anchor=anchor, text="",
                                           fill="black" if shadow else "white",
                                           font=("Sans", size, "bold" if size > 20 else "normal"))

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
            self.settings = None
            self.root.configure(cursor="none")
            self.root.focus_force()
            self.frame = None  # re-render with new display options
            self.history, self.hist_pos = [], -1
            self.bad.clear()
            self.start()

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

        def step(self, direction):
            if self.settings is not None or self.closing:
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
            name = os.path.relpath(path, self.cfg["folder"]) if self.cfg["show_name"] else ""
            self.canvas.itemconfigure(self.name, text=name)
            self.canvas.itemconfigure(self.name_sh, text=name)
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

        def _clock_tick(self):
            try:
                on = self.cfg["show_clock"]
                t = time.strftime("%H:%M") if on else ""
                d = time.strftime("%A, %d %B") if on else ""
                for item, val in ((self.clock, t), (self.clock_sh, t),
                                  (self.date, d), (self.date_sh, d)):
                    if self.canvas.itemcget(item, "text") != val:
                        self.canvas.itemconfigure(item, text=val)
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
