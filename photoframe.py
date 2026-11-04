#!/usr/bin/env python3
"""
PhotoFrame - turn an old Ubuntu laptop into a full-screen photo gallery.

Keys while the slideshow runs:
    S / Enter / right-click   open settings
    Right / Left              next / previous photo
    Space                     pause / resume
    Esc / Q                   quit (screen & sleep settings are restored)
"""
import json
import os
import random
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk
except ImportError:
    sys.stderr.write(
        "Pillow is missing. Install it with:\n"
        "  sudo apt install python3-pil python3-pil.imagetk\n")
    sys.exit(1)

APP = "PhotoFrame"
SCRIPT = os.path.abspath(__file__)
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "photoframe")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "saved-power-settings.json")
AUTOSTART_PATH = os.path.join(os.path.expanduser("~"), ".config", "autostart",
                              "photoframe.desktop")
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

DEFAULTS = {
    "folder": os.path.join(os.path.expanduser("~"), "Pictures"),
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


# --------------------------------------------------------------------------- utils
def run(cmd, capture=False):
    """Run a command quietly; never raise."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, text=True)
        return r.stdout.strip() if capture and r.returncode == 0 else (r.returncode == 0)
    except (OSError, subprocess.SubprocessError):
        return None if capture else False


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def scan_folder(folder, recursive):
    found = []
    if not folder or not os.path.isdir(folder):
        return found
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            found += [os.path.join(root, f) for f in files
                      if os.path.splitext(f)[1].lower() in EXTS]
    else:
        found = [os.path.join(folder, f) for f in os.listdir(folder)
                 if os.path.splitext(f)[1].lower() in EXTS]
    found.sort()
    return found


def set_autostart(enabled):
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=PhotoFrame\n"
                    f"Exec=python3 \"{SCRIPT}\"\n"
                    "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\n")
    elif os.path.exists(AUTOSTART_PATH):
        os.remove(AUTOSTART_PATH)


def resize(img, size):
    try:
        return img.resize(size, Image.BICUBIC, reducing_gap=2.0)
    except TypeError:  # very old Pillow
        return img.resize(size, Image.BICUBIC)


# --------------------------------------------------------------------------- keep awake
class KeepAwake:
    """Stops screen blanking, screen lock timeout and suspend while running,
    and puts everything back on exit (even after a crash, on next start)."""

    GSETTINGS = [
        ("org.gnome.desktop.session", "idle-delay", "uint32 0"),
        ("org.gnome.desktop.screensaver", "idle-activation-enabled", "false"),
        ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'nothing'"),
        ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-battery-type", "'nothing'"),
        ("org.gnome.settings-daemon.plugins.power", "idle-dim", "false"),
    ]

    def __init__(self, root):
        self.root = root
        self.proc = None
        self.job = None
        self.active = False

    def start(self):
        if self.active:
            return
        self.active = True
        if shutil.which("systemd-inhibit"):
            try:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", f"--who={APP}",
                     "--why=Showing photos", "--mode=block", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.proc = None
        if shutil.which("gsettings"):
            self._apply_gsettings()
        if shutil.which("xset"):
            run(["xset", "s", "off"])
            run(["xset", "s", "noblank"])
            run(["xset", "-dpms"])
        self._tick()

    def _tick(self):
        # Belt and braces: poke any screensaver every 50 s.
        if shutil.which("xdg-screensaver"):
            run(["xdg-screensaver", "reset"])
        if shutil.which("xset"):
            run(["xset", "s", "reset"])
        self.job = self.root.after(50_000, self._tick)

    def _apply_gsettings(self):
        # If a previous run crashed, the saved originals are still on disk: keep them.
        if not os.path.exists(STATE_PATH):
            originals = {}
            for schema, key, _ in self.GSETTINGS:
                val = run(["gsettings", "get", schema, key], capture=True)
                if val:
                    originals[f"{schema} {key}"] = val
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(STATE_PATH, "w") as f:
                json.dump(originals, f)
        for schema, key, value in self.GSETTINGS:
            run(["gsettings", "set", schema, key, value])

    def stop(self):
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        if self.proc:
            self.proc.terminate()
            self.proc = None
        if self.active and shutil.which("xset"):
            run(["xset", "s", "on"])
            run(["xset", "+dpms"])
        self.active = False
        self.restore_gsettings()

    @staticmethod
    def restore_gsettings():
        try:
            with open(STATE_PATH) as f:
                originals = json.load(f)
        except (OSError, ValueError):
            return
        for name, val in originals.items():
            schema, key = name.split(" ", 1)
            run(["gsettings", "set", schema, key, val])
        os.remove(STATE_PATH)


# --------------------------------------------------------------------------- settings
class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        cfg = app.cfg
        self.title(f"{APP} settings")
        self.configure(padx=20, pady=16)
        self.resizable(False, False)
        self.transient(app.root)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.on_start)

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

        r = 0
        ttk.Label(self, text="Photo folder").grid(row=r, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.v_folder, width=42).grid(row=r, column=1, padx=6)
        ttk.Button(self, text="Browse…", command=self.browse).grid(row=r, column=2)
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
        ttk.Spinbox(self, from_=2, to=3600, textvariable=self.v_interval, width=8)\
            .grid(row=r, column=1, sticky="w", padx=6)
        r += 1
        ttk.Label(self, text="Display").grid(row=r, column=0, sticky="w", pady=(6, 0))
        f = ttk.Frame(self)
        f.grid(row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Radiobutton(f, text="Show whole photo", value="fit", variable=self.v_fit).pack(side="left")
        ttk.Radiobutton(f, text="Fill screen (crop)", value="fill", variable=self.v_fit).pack(side="left", padx=10)
        r += 1
        ttk.Label(self, text="Background").grid(row=r, column=0, sticky="w")
        f = ttk.Frame(self)
        f.grid(row=r, column=1, sticky="w", padx=6)
        ttk.Radiobutton(f, text="Blurred photo", value="blur", variable=self.v_bg).pack(side="left")
        ttk.Radiobutton(f, text="Black", value="black", variable=self.v_bg).pack(side="left", padx=10)
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
        ttk.Checkbutton(self, text="Keep screen on and never sleep while running",
                        variable=self.v_awake).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(self, text="Start automatically when I log in",
                        variable=self.v_autostart).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(self, foreground="#666", justify="left", text=(
            "While running:  S = settings   ←/→ = previous/next   "
            "Space = pause   Esc = quit")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))
        r += 1
        b = ttk.Frame(self)
        b.grid(row=r, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Quit", command=app.quit).pack(side="right")
        ttk.Button(b, text="Start slideshow", command=self.on_start).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self.on_start())
        self.bind("<Escape>", lambda e: self.on_start())

        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
        y = (self.winfo_screenheight() - self.winfo_reqheight()) // 3
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.focus_force()
        self.after(100, self._grab)

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def browse(self):
        start = self.v_folder.get() or os.path.expanduser("~")
        path = None
        if shutil.which("zenity"):  # native GNOME folder picker
            try:
                r = subprocess.run(["zenity", "--file-selection", "--directory",
                                    "--title=Choose photo folder", f"--filename={start}/"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                path = r.stdout.strip() if r.returncode == 0 else ""
            except OSError:
                path = None
        if path is None:
            path = filedialog.askdirectory(parent=self, initialdir=start,
                                           title="Choose photo folder")
        if path:
            self.v_folder.set(path)

    def on_start(self):
        try:
            interval = max(2, int(self.v_interval.get()))
        except (tk.TclError, ValueError):
            interval = DEFAULTS["interval"]
        folder = os.path.expanduser(self.v_folder.get().strip())
        if not scan_folder(folder, self.v_recursive.get()):
            messagebox.showwarning(APP, "No photos found in that folder.\n"
                                        "Pick a folder with .jpg / .png / .webp images.",
                                   parent=self)
            return
        self.app.cfg.update(
            folder=folder, recursive=self.v_recursive.get(), shuffle=self.v_shuffle.get(),
            interval=interval, fit=self.v_fit.get(), background=self.v_bg.get(),
            fade=self.v_fade.get(), show_clock=self.v_clock.get(),
            show_name=self.v_name.get(), keep_awake=self.v_awake.get())
        save_config(self.app.cfg)
        try:
            set_autostart(self.v_autostart.get())
        except OSError:
            pass
        self.grab_release()
        self.destroy()
        self.app.settings_closed()


# --------------------------------------------------------------------------- app
class PhotoFrame:
    def __init__(self):
        KeepAwake.restore_gsettings()  # clean up after a crashed run, if any
        self.cfg = load_config()
        self.root = tk.Tk(className="photoframe")
        self.root.title(APP)
        self.root.configure(bg="black", cursor="none")
        self.root.attributes("-fullscreen", True)
        self.W = self.root.winfo_screenwidth()
        self.H = self.root.winfo_screenheight()

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

        self.keep_awake = KeepAwake(self.root)
        self.photos, self.order, self.pos = [], [], -1
        self.history, self.hist_pos = [], -1
        self.frame = None          # current PIL frame (full screen size)
        self.tkimg = None
        self.paused = False
        self.settings = None
        self.next_job = self.fade_job = self.status_job = None
        self.last_scan = 0

        r = self.root
        for k in ("<s>", "<S>", "<Return>", "<Button-3>"):
            r.bind(k, lambda e: self.open_settings())
        r.bind("<Right>", lambda e: self.step(+1))
        r.bind("<Left>", lambda e: self.step(-1))
        r.bind("<space>", lambda e: self.toggle_pause())
        for k in ("<Escape>", "<q>", "<Q>"):
            r.bind(k, lambda e: self.quit())
        r.protocol("WM_DELETE_WINDOW", self.quit)

        self._clock_tick()
        self.root.after(200, self.boot)

    def _text(self, x, y, anchor, size, shadow=False):
        return self.canvas.create_text(x, y, anchor=anchor, text="",
                                       fill="black" if shadow else "white",
                                       font=("Sans", size, "bold" if size > 20 else "normal"))

    # ---- lifecycle
    def boot(self):
        if scan_folder(self.cfg["folder"], self.cfg["recursive"]):
            self.start()
            self.flash("S = settings   ←/→ = navigate   Space = pause   Esc = quit", 6000)
        else:
            self.open_settings()

    def start(self):
        self.rescan(reset=True)
        if self.cfg["keep_awake"]:
            self.keep_awake.start()
        else:
            self.keep_awake.stop()
        self.paused = False
        self.step(+1)

    def quit(self):
        self.keep_awake.stop()
        self.root.destroy()

    def open_settings(self):
        if self.settings and self.settings.winfo_exists():
            return
        self._cancel_next()
        self.root.configure(cursor="")
        self.settings = SettingsDialog(self)

    def settings_closed(self):
        self.settings = None
        self.root.configure(cursor="none")
        self.root.focus_force()
        self.frame = None  # re-render with new display options
        self.history, self.hist_pos = [], -1
        self.start()

    # ---- playlist
    def rescan(self, reset=False):
        self.last_scan = time.time()
        photos = scan_folder(self.cfg["folder"], self.cfg["recursive"])
        if not reset and set(photos) == set(self.photos):
            return
        self.photos = photos
        self._new_order()

    def _new_order(self, avoid=None):
        self.order = list(self.photos)
        if self.cfg["shuffle"]:
            random.shuffle(self.order)
            if avoid and len(self.order) > 1 and self.order[0] == avoid:
                self.order.append(self.order.pop(0))  # don't repeat back-to-back
        self.pos = -1

    def _next_path(self):
        if time.time() - self.last_scan > self.cfg["rescan_minutes"] * 60:
            self.rescan()
        if not self.order:
            return None
        self.pos += 1
        if self.pos >= len(self.order):
            last = self.order[-1]
            self._new_order(avoid=last)
            self.pos = 0
        return self.order[self.pos]

    def step(self, direction):
        if self.settings:
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
                    self.flash("No photos found - press S to choose a folder", 0)
                    return
                self.history.append(path)
                self.history = self.history[-200:]
                self.hist_pos = len(self.history) - 1
            try:
                frame = self.render(path)
            except Exception:
                if path in self.history:
                    self.history.remove(path)
                    self.hist_pos = min(self.hist_pos, len(self.history) - 1)
                if path in self.photos:
                    self.photos.remove(path)
                continue
            self.show(frame, path)
            break
        self._schedule_next()

    def _schedule_next(self):
        self._cancel_next()
        if not self.paused and not self.settings:
            self.next_job = self.root.after(self.cfg["interval"] * 1000, lambda: self.step(+1))

    def _cancel_next(self):
        if self.next_job:
            self.root.after_cancel(self.next_job)
            self.next_job = None

    def toggle_pause(self):
        if self.settings:
            return
        self.paused = not self.paused
        if self.paused:
            self._cancel_next()
            self.flash("Paused", 0)
        else:
            self.flash("Playing", 1500)
            self._schedule_next()

    # ---- drawing
    def render(self, path):
        W, H = self.W, self.H
        img = Image.open(path)
        img.draft("RGB", (W, H))  # fast JPEG decode at reduced size
        if hasattr(ImageOps, "exif_transpose"):
            img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if self.cfg["fit"] == "fill":
            return ImageOps.fit(img, (W, H), Image.BICUBIC)
        iw, ih = img.size
        scale = min(W / iw, H / ih)
        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
        fg = resize(img, (nw, nh))
        if self.cfg["background"] == "blur" and (nw < W - 4 or nh < H - 4):
            small = ImageOps.fit(img, (max(1, W // 20), max(1, H // 20)), Image.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(2))
            base = ImageEnhance.Brightness(small.resize((W, H), Image.BILINEAR)).enhance(0.45)
        else:
            base = Image.new("RGB", (W, H), "black")
        base.paste(fg, ((W - nw) // 2, (H - nh) // 2))
        return base

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
        self._put(Image.blend(a, b, i / steps))
        self.fade_job = self.root.after(35, lambda: self._fade(a, b, i + 1, steps))

    def _put(self, img):
        self.tkimg = ImageTk.PhotoImage(img)
        self.canvas.itemconfigure(self.img_item, image=self.tkimg)

    def _clock_tick(self):
        on = self.cfg["show_clock"]
        t = time.strftime("%H:%M") if on else ""
        d = time.strftime("%A, %d %B") if on else ""
        for item, val in ((self.clock, t), (self.clock_sh, t), (self.date, d), (self.date_sh, d)):
            if self.canvas.itemcget(item, "text") != val:
                self.canvas.itemconfigure(item, text=val)
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
    app = PhotoFrame()
    try:
        app.root.mainloop()
    finally:
        app.keep_awake.stop()


if __name__ == "__main__":
    main()
